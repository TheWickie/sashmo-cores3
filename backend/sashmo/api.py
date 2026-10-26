"""Private LAN API for the CoreS3 device."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import re
import secrets
import time
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from decimal import Decimal, InvalidOperation

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .calendar_worker import CalendarWorker
from .chatgpt_reminders import ChatGPTReminderWorker, validate_chat_url
from .billing import BillingWorker
from .conversation import ConversationProvider
from .deck_commands import DeckCommands
from .codex_tasks import CodexTaskManager
from .gmail import GmailWorker
from .ipad import IpadNotificationStore, IpadPairing, changes, project_sections
from .projects import ProjectRegistry
from .speech import process_audio_text, recognize_wake, split_spoken_text, synthesize_reply, validate_wav
from .state import QuotaWorker, StateStore
from .voice_stream import VoiceStreamManager


LOG = logging.getLogger("sashmo")


def _ipad_chatgpt_status() -> dict[str, object]:
    """Expose only the active local plan model state, never account identity or tokens."""
    try:
        from .siwc import SIWCClient, WindowsTokenStore
        active = next((account for account in SIWCClient(WindowsTokenStore()).account_summaries()
                       if account.get("active")), None)
        model = active.get("model") if active else None
        return {"signed_in": active is not None, "ready": bool(model),
                "model": model if isinstance(model, str) else None, "error": None}
    except Exception:
        return {"signed_in": False, "ready": False, "model": None,
                "error": "Account status unavailable"}


@dataclass(frozen=True)
class Config:
    bind_host: str
    port: int
    device_token: str
    codex_poll_seconds: int = 300
    wake_word_enabled: bool = False
    openai_billing_api_key_id: str = ""
    api_monthly_budget_usd: Decimal | None = None
    screen_standby_after_seconds: int = 900
    chatgpt_reminder_chat_url: str = ""
    ipad_featured_projects: tuple[str, ...] = ()

    @classmethod
    def load(cls, path: Path) -> "Config":
        data = json.loads(path.read_text(encoding="utf-8"))
        host = ipaddress.ip_address(data["bind_host"])
        if host.version != 4 or not host.is_private or host.is_unspecified:
            raise ValueError("bind_host must be a private IPv4 address")
        token = data["device_token"]
        if not isinstance(token, str) or len(token) < 32:
            raise ValueError("device_token must be at least 32 characters")
        port = int(data.get("port", 8765))
        poll = int(data.get("codex_poll_seconds", 300))
        if not 1024 <= port <= 65535 or not 30 <= poll <= 3600:
            raise ValueError("Invalid port or Codex poll interval")
        key_id = str(data.get("openai_billing_api_key_id") or "").strip()
        if key_id and (not key_id.startswith("key_") or len(key_id) > 100 or not key_id.replace("_", "").isalnum()):
            raise ValueError("Invalid OpenAI billing API key ID")
        try:
            budget = (Decimal(str(data["api_monthly_budget_usd"]))
                      if data.get("api_monthly_budget_usd") not in (None, "") else None)
        except (InvalidOperation, TypeError) as exc:
            raise ValueError("Invalid API monthly budget") from exc
        if budget is not None and (not budget.is_finite() or budget <= 0 or budget > 1000000):
            raise ValueError("Invalid API monthly budget")
        standby = int(data.get("screen_standby_after_seconds", 900))
        if not 60 <= standby <= 3600:
            raise ValueError("Display standby delay must be 60 through 3600 seconds")
        chat_url = str(data.get("chatgpt_reminder_chat_url") or "").strip()
        if chat_url:
            validate_chat_url(chat_url)
        featured = data.get("ipad_featured_projects", [])
        if (not isinstance(featured, list) or len(featured) > 6
                or any(not isinstance(name, str) or not 1 <= len(name.strip()) <= 80
                       or any(ord(char) < 32 for char in name) for name in featured)):
            raise ValueError("Invalid iPad featured projects")
        return cls(str(host), port, token, poll, data.get("wake_word_enabled") is True,
                   key_id, budget, standby, chat_url, tuple(name.strip() for name in featured))


def create_app(config: Config, *, start_worker: bool = True, state: StateStore | None = None,
               conversation: ConversationProvider | None = None) -> FastAPI:
    telemetry_path = Path(__file__).resolve().parents[2] / "work" / "battery-history.csv"
    store = state or StateStore(telemetry_path=telemetry_path if start_worker else None)
    task_manager = CodexTaskManager(ProjectRegistry(Path(__file__).resolve().parents[2] /
                                                   "projects.local.json"))
    voice_provider = conversation or (ConversationProvider(Path(__file__).resolve().parents[2] /
                                                          "projects.local.json", state=store,
                                                          task_manager=task_manager) if start_worker else None)
    voice_stream = VoiceStreamManager(synthesize_reply)
    deck_commands = DeckCommands()
    root = Path(__file__).resolve().parents[2]
    ipad_static = root / "ipad"
    ipad_pairing = IpadPairing(config.device_token, config.bind_host)
    ipad_notifications = IpadNotificationStore(root / "work" / "ipad-notifications.json"
                                               if start_worker else None)
    ipad_registry = ProjectRegistry(root / "projects.local.json")
    ipad_project_cache: dict[str, object] = {"at": 0.0, "data": None}
    ipad_chatgpt_cache: dict[str, object] = {"at": 0.0, "data": None}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        worker = QuotaWorker(store, config.codex_poll_seconds) if start_worker else None
        gmail_worker = GmailWorker(store) if start_worker else None
        calendar_worker = CalendarWorker(store) if start_worker else None
        billing_worker = BillingWorker(store, config.openai_billing_api_key_id,
                                       config.api_monthly_budget_usd) if start_worker else None
        reminder_worker = (ChatGPTReminderWorker(store, config.chatgpt_reminder_chat_url)
                           if start_worker and config.chatgpt_reminder_chat_url else None)
        if worker:
            await worker.start()
        if gmail_worker:
            await gmail_worker.start()
        if calendar_worker:
            await calendar_worker.start()
        if billing_worker:
            await billing_worker.start()
        if reminder_worker:
            await reminder_worker.start()
        async def ingest_ipad_notifications() -> None:
            while True:
                ipad_notifications.ingest(store.notification_feed())
                await asyncio.sleep(15)
        ipad_ingest_task = asyncio.create_task(ingest_ipad_notifications()) if start_worker else None
        try:
            yield
        finally:
            if ipad_ingest_task:
                ipad_ingest_task.cancel()
                await asyncio.gather(ipad_ingest_task, return_exceptions=True)
            if worker:
                await worker.stop()
            if gmail_worker:
                await gmail_worker.stop()
            if calendar_worker:
                await calendar_worker.stop()
            if billing_worker:
                await billing_worker.stop()
            if reminder_worker:
                await reminder_worker.stop()
            await task_manager.close()
            await voice_stream.cancel()

    app = FastAPI(title="Sashmo", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.task_manager = task_manager
    app.state.voice_stream = voice_stream
    app.state.deck_commands = deck_commands
    app.state.ipad_pairing = ipad_pairing
    app.state.ipad_notifications = ipad_notifications

    app.mount("/ipad/assets", StaticFiles(directory=ipad_static / "assets"), name="ipad-assets")

    def ipad_peer(request: Request) -> str:
        peer = request.client.host if request.client else ""
        if not ipad_pairing.on_home_network(peer):
            raise HTTPException(status_code=403, detail="Home network only")
        return peer

    def ipad_authenticated(request: Request) -> str:
        peer = ipad_peer(request)
        if not ipad_pairing.valid(request.cookies.get("sashmo_ipad"), peer):
            raise HTTPException(status_code=401, detail="Pair this iPad in Sashmo")
        return peer

    def ipad_write_origin(request: Request) -> None:
        if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
            raise HTTPException(status_code=415, detail="JSON required")
        origin = request.headers.get("origin")
        if origin and origin not in {f"http://{config.bind_host}:{config.port}",
                                    f"https://{config.bind_host}:{config.port}"}:
            raise HTTPException(status_code=403, detail="Invalid origin")

    @app.get("/")
    @app.get("/ipad")
    @app.get("/ipad/")
    async def ipad_home() -> FileResponse:
        return FileResponse(ipad_static / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/ipad/manifest.webmanifest")
    async def ipad_manifest() -> FileResponse:
        return FileResponse(ipad_static / "manifest.webmanifest", media_type="application/manifest+json")

    @app.get("/ipad/sw.js")
    async def ipad_service_worker() -> FileResponse:
        return FileResponse(ipad_static / "sw.js", media_type="text/javascript")

    @app.get("/ipad/api/bootstrap")
    async def ipad_bootstrap(request: Request) -> JSONResponse:
        peer = ipad_peer(request)
        return JSONResponse({"paired": ipad_pairing.valid(request.cookies.get("sashmo_ipad"), peer)},
                            headers={"Cache-Control": "no-store"})

    @app.post("/ipad/api/pair")
    async def ipad_pair(request: Request) -> JSONResponse:
        peer = ipad_peer(request)
        ipad_write_origin(request)
        body = await request.body()
        if len(body) > 128:
            raise HTTPException(status_code=413, detail="Request too large")
        try:
            code = json.loads(body).get("code")
        except (ValueError, AttributeError):
            code = None
        if not isinstance(code, str) or not re.fullmatch(r"\d{8}", code):
            raise HTTPException(status_code=400, detail="Enter the eight-digit code")
        cookie = ipad_pairing.pair(code, peer)
        if not cookie:
            raise HTTPException(status_code=429, detail="Code incorrect or expired")
        response = JSONResponse({"paired": True}, headers={"Cache-Control": "no-store"})
        response.set_cookie("sashmo_ipad", cookie, max_age=30 * 86400, httponly=True,
                            secure=request.url.scheme == "https", samesite="strict", path="/ipad")
        return response

    @app.get("/ipad/api/updates")
    async def ipad_updates(request: Request) -> Response:
        ipad_authenticated(request)
        now = time.monotonic()
        if ipad_project_cache["data"] is None or now - float(ipad_project_cache["at"]) > 60:
            ipad_project_cache["data"] = await run_in_threadpool(ipad_registry.list_projects)
            ipad_project_cache["at"] = now
        if ipad_chatgpt_cache["data"] is None or now - float(ipad_chatgpt_cache["at"]) > 60:
            ipad_chatgpt_cache["data"] = await run_in_threadpool(_ipad_chatgpt_status)
            ipad_chatgpt_cache["at"] = now
        ipad_notifications.ingest(store.notification_feed())
        sections = project_sections(store, ipad_notifications, ipad_project_cache["data"],
                                    config.ipad_featured_projects, ipad_chatgpt_cache["data"])
        previous = {name: request.query_params.get("v_" + name, "") for name in sections}
        payload = changes(sections, previous)
        if not payload["sections"]:
            return Response(status_code=204, headers={"Cache-Control": "no-store"})
        return JSONResponse(payload, headers={"Cache-Control": "no-store"})

    @app.post("/ipad/api/notifications/{identifier}/read")
    async def ipad_mark_read(identifier: str, request: Request) -> JSONResponse:
        ipad_authenticated(request)
        ipad_write_origin(request)
        if not re.fullmatch(r"[0-9a-f]{24}", identifier):
            raise HTTPException(status_code=404, detail="Notification not found")
        body = await request.body()
        if len(body) > 128:
            raise HTTPException(status_code=413, detail="Request too large")
        try:
            read = json.loads(body).get("read")
        except (ValueError, AttributeError):
            read = None
        if not isinstance(read, bool):
            raise HTTPException(status_code=400, detail="Read state required")
        if not ipad_notifications.mark_read(identifier, read):
            raise HTTPException(status_code=404, detail="Notification not found")
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    @app.post("/ipad/api/preferences")
    async def ipad_preferences(request: Request) -> JSONResponse:
        ipad_authenticated(request)
        ipad_write_origin(request)
        body = await request.body()
        if len(body) > 1024:
            raise HTTPException(status_code=413, detail="Request too large")
        try:
            updates = json.loads(body)
            if not isinstance(updates, dict):
                raise ValueError
            preferences = ipad_notifications.set_preferences(updates)
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid alert preferences") from None
        return JSONResponse({"preferences": preferences}, headers={"Cache-Control": "no-store"})

    def authenticate(value: str | None) -> None:
        if not value or not secrets.compare_digest(value, config.device_token):
            raise HTTPException(status_code=401, detail="Unauthorized")

    def local_dashboard_only(request: Request) -> None:
        """Keep personal dashboard snapshots off the device-facing LAN API."""
        peer = request.client.host if request.client else ""
        try:
            address = ipaddress.ip_address(peer)
            if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
                address = address.ipv4_mapped
        except ValueError:
            raise HTTPException(status_code=403, detail="Desktop only") from None
        if str(address) != config.bind_host:
            raise HTTPException(status_code=403, detail="Desktop only")

    @app.get("/v1/pulse")
    async def pulse(since: int = -1, battery: int | None = None, charging: bool | None = None,
                    usb: bool | None = None, screen_sleeping: bool | None = None,
                    wifi_rssi: int | None = None, command_ack: int | None = None,
                    command_result: str | None = None,
                    x_sashmo_token: str | None = Header(default=None)) -> JSONResponse:
        authenticate(x_sashmo_token)
        store.record_device(battery, charging, usb, wifi_rssi, screen_sleeping)
        return JSONResponse({**store.pulse(since), "wake_word_enabled": config.wake_word_enabled,
                             "api_billing": store.billing_pulse(),
                             "screen_standby_after_seconds": config.screen_standby_after_seconds,
                             "deck_remote_enabled": deck_commands.fast_poll_enabled(),
                             "deck_command": deck_commands.for_pulse(command_ack, command_result)},
                            headers={"Cache-Control": "no-store"})

    @app.get("/v1/status")
    async def status(request: Request, x_sashmo_token: str | None = Header(default=None)) -> JSONResponse:
        authenticate(x_sashmo_token)
        local_dashboard_only(request)
        return JSONResponse({**store.status(), "codex_task_requests": task_manager.review_status(),
                             "ipad_pairing": ipad_pairing.current_code()},
                            headers={"Cache-Control": "no-store"})

    @app.get("/v1/deck/status")
    async def deck_status(request: Request,
                          x_sashmo_token: str | None = Header(default=None)) -> JSONResponse:
        authenticate(x_sashmo_token)
        local_dashboard_only(request)
        deck_commands.note_client_poll()
        ipad_notifications.ingest(store.notification_feed())
        return JSONResponse({**store.status(),
                             "vscode": {"items": store.deck_vscode_work,
                                        "error": store.deck_vscode_error},
                             "deck_notifications": ipad_notifications.list_items()[:25],
                             "deck_unread_count": ipad_notifications.unread_count(),
                             "deck_talk": deck_commands.status()},
                            headers={"Cache-Control": "no-store"})

    @app.post("/v1/deck/notifications/{identifier}/read")
    async def deck_mark_read(identifier: str, request: Request,
                             x_sashmo_token: str | None = Header(default=None)) -> JSONResponse:
        authenticate(x_sashmo_token)
        local_dashboard_only(request)
        if not re.fullmatch(r"[0-9a-f]{24}", identifier):
            raise HTTPException(status_code=404, detail="Notification not found")
        body = await request.body()
        if len(body) > 64:
            raise HTTPException(status_code=413, detail="Request too large")
        try:
            read = json.loads(body).get("read")
        except (ValueError, AttributeError):
            read = None
        if not isinstance(read, bool):
            raise HTTPException(status_code=400, detail="Read state required")
        if not ipad_notifications.mark_read(identifier, read):
            raise HTTPException(status_code=404, detail="Notification not found")
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    @app.post("/v1/deck/talk")
    async def deck_talk(request: Request,
                        x_sashmo_token: str | None = Header(default=None)) -> JSONResponse:
        authenticate(x_sashmo_token)
        local_dashboard_only(request)
        device = store.status()["device"]
        age = device["last_seen_seconds_ago"]
        low_battery = (device["usb_powered"] is False and
                       device["battery_percent"] is not None and device["battery_percent"] <= 15)
        ready = age is not None and age <= 90 and store.voice_state == "idle" and not low_battery
        try:
            issued = deck_commands.issue_talk(ready)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return JSONResponse(issued, headers={"Cache-Control": "no-store"})

    @app.post("/v1/codex-task/approve")
    async def approve_codex_task(request: Request,
                                 x_sashmo_token: str | None = Header(default=None)) -> JSONResponse:
        authenticate(x_sashmo_token)
        local_dashboard_only(request)
        try:
            task = await task_manager.approve()
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception:
            LOG.exception("Codex task dispatch failed")
            raise HTTPException(status_code=503, detail="Could not start Codex task") from None
        return JSONResponse(task, headers={"Cache-Control": "no-store"})

    @app.post("/v1/codex-task/reject")
    async def reject_codex_task(request: Request,
                                x_sashmo_token: str | None = Header(default=None)) -> JSONResponse:
        authenticate(x_sashmo_token)
        local_dashboard_only(request)
        await task_manager.reject()
        return JSONResponse({"ok": True}, headers={"Cache-Control": "no-store"})

    @app.get("/v1/projects")
    async def projects(request: Request, x_sashmo_token: str | None = Header(default=None)) -> JSONResponse:
        authenticate(x_sashmo_token)
        local_dashboard_only(request)
        registry = ProjectRegistry(Path(__file__).resolve().parents[2] / "projects.local.json")
        result = await run_in_threadpool(registry.list_projects)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.post("/v1/voice")
    async def voice(request: Request, x_sashmo_token: str | None = Header(default=None)) -> Response:
        authenticate(x_sashmo_token)
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > 650_000:
            raise HTTPException(status_code=413, detail="Audio too large")
        audio = await request.body()
        if len(audio) > 650_000:
            raise HTTPException(status_code=413, detail="Audio too large")
        try:
            validate_wav(audio)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.voice_state = "processing"
        follow_up = request.headers.get("X-Sashmo-Follow-Up") == "1"
        if voice_provider is not None and hasattr(voice_provider, "restore_device_memory"):
            voice_provider.restore_device_memory(request.headers.get("X-Sashmo-Memory"))
        try:
            transcript, answer = await process_audio_text(audio, store.quota, store.codex_connected,
                                                          provider=voice_provider,
                                                          silent_on_no_speech=follow_up)
            if follow_up and not transcript:
                return Response(status_code=204, headers={"Cache-Control": "no-store"})
            parts = split_spoken_text(answer)
            output, reply_id, more = await voice_stream.start(parts)
            store.voice_provider = voice_provider.last_source if voice_provider else "local"
        except Exception as exc:
            if isinstance(exc, RuntimeError) and str(exc).startswith("Cedar speech"):
                LOG.warning("Cedar speech unavailable")
                raise HTTPException(status_code=503, detail="Cedar speech unavailable",
                                    headers={"X-Sashmo-Error": "Cedar unavailable"}) from None
            LOG.exception("Voice processing failed")
            raise HTTPException(status_code=503, detail="Speech processing unavailable") from None
        finally:
            store.voice_state = "idle"
            store.last_voice_local = datetime.now().astimezone().isoformat(timespec="seconds")
        LOG.info("Voice request completed (recognized=%s, response_bytes=%d)", bool(transcript), len(output))
        headers = {"X-Sashmo-Recognized": "1" if transcript else "0",
                   "Cache-Control": "no-store"}
        if reply_id:
            headers["X-Sashmo-Reply-Id"] = reply_id
            headers["X-Sashmo-More"] = "1" if more else "0"
            headers["X-Sashmo-Part"] = "1"
            headers["X-Sashmo-Parts"] = str(len(parts))
        volume = getattr(voice_provider, "volume_percent", None)
        if volume is not None:
            headers["X-Sashmo-Volume-Percent"] = str(volume)
        if voice_provider is not None and hasattr(voice_provider, "compact_device_memory"):
            memory = voice_provider.compact_device_memory()
            if memory:
                headers["X-Sashmo-Memory"] = memory
        return Response(
            output,
            media_type="audio/wav",
            headers=headers,
        )

    @app.get("/v1/voice/reply/{reply_id}")
    async def next_voice_reply(reply_id: str,
                               x_sashmo_token: str | None = Header(default=None)) -> Response:
        authenticate(x_sashmo_token)
        try:
            output, more, index, count = await voice_stream.next(reply_id)
        except LookupError:
            raise HTTPException(status_code=404, detail="Reply expired") from None
        except TimeoutError:
            raise HTTPException(status_code=504, detail="Reply segment timed out",
                                headers={"X-Sashmo-Error": "Reply timeout"}) from None
        except RuntimeError:
            raise HTTPException(status_code=503, detail="Cedar speech unavailable",
                                headers={"X-Sashmo-Error": "Cedar unavailable"}) from None
        return Response(output, media_type="audio/wav", headers={
            "X-Sashmo-More": "1" if more else "0", "X-Sashmo-Part": str(index),
            "X-Sashmo-Parts": str(count), "Cache-Control": "no-store"})

    @app.delete("/v1/voice/reply/{reply_id}")
    async def stop_voice_reply(reply_id: str,
                               x_sashmo_token: str | None = Header(default=None)) -> Response:
        authenticate(x_sashmo_token)
        await voice_stream.cancel(reply_id)
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    @app.post("/v1/wake")
    async def wake(request: Request, x_sashmo_token: str | None = Header(default=None)) -> JSONResponse:
        authenticate(x_sashmo_token)
        if not config.wake_word_enabled:
            return JSONResponse({"wake": False}, headers={"Cache-Control": "no-store"})
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > 100_000:
            raise HTTPException(status_code=413, detail="Audio too large")
        audio = await request.body()
        if len(audio) > 100_000:
            raise HTTPException(status_code=413, detail="Audio too large")
        try:
            validate_wav(audio, max_seconds=3)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            detected = await run_in_threadpool(recognize_wake, audio)
        except Exception:
            LOG.exception("Local wake recognition failed")
            store.record_wake_check(False)
            return JSONResponse({"wake": False}, headers={"Cache-Control": "no-store"})
        store.record_wake_check(detected)
        return JSONResponse({"wake": detected}, headers={"Cache-Control": "no-store"})

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    path = Path(os.environ.get("SASHMO_CONFIG", Path(__file__).resolve().parents[2] / "config.local.json"))
    config = Config.load(path)
    uvicorn.run(create_app(config), host=config.bind_host, port=config.port,
                log_level="info", access_log=False)


if __name__ == "__main__":
    main()
