"""Private LAN API for the CoreS3 device."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from decimal import Decimal, InvalidOperation

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .calendar_worker import CalendarWorker
from .billing import BillingWorker
from .conversation import ConversationProvider
from .codex_tasks import CodexTaskManager
from .gmail import GmailWorker
from .projects import ProjectRegistry
from .speech import process_audio_text, recognize_wake, split_spoken_text, synthesize_reply, validate_wav
from .state import QuotaWorker, StateStore
from .voice_stream import VoiceStreamManager


LOG = logging.getLogger("sashmo")


@dataclass(frozen=True)
class Config:
    bind_host: str
    port: int
    device_token: str
    codex_poll_seconds: int = 300
    wake_word_enabled: bool = False
    openai_billing_api_key_id: str = ""
    api_monthly_budget_usd: Decimal | None = None

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
        return cls(str(host), port, token, poll, data.get("wake_word_enabled") is True,
                   key_id, budget)


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

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        worker = QuotaWorker(store, config.codex_poll_seconds) if start_worker else None
        gmail_worker = GmailWorker(store) if start_worker else None
        calendar_worker = CalendarWorker(store) if start_worker else None
        billing_worker = BillingWorker(store, config.openai_billing_api_key_id,
                                       config.api_monthly_budget_usd) if start_worker else None
        if worker:
            await worker.start()
        if gmail_worker:
            await gmail_worker.start()
        if calendar_worker:
            await calendar_worker.start()
        if billing_worker:
            await billing_worker.start()
        try:
            yield
        finally:
            if worker:
                await worker.stop()
            if gmail_worker:
                await gmail_worker.stop()
            if calendar_worker:
                await calendar_worker.stop()
            if billing_worker:
                await billing_worker.stop()
            await task_manager.close()
            await voice_stream.cancel()

    app = FastAPI(title="Sashmo", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store
    app.state.task_manager = task_manager
    app.state.voice_stream = voice_stream

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
                    wifi_rssi: int | None = None,
                    x_sashmo_token: str | None = Header(default=None)) -> JSONResponse:
        authenticate(x_sashmo_token)
        store.record_device(battery, charging, usb, wifi_rssi, screen_sleeping)
        return JSONResponse({**store.pulse(since), "wake_word_enabled": config.wake_word_enabled,
                             "api_billing": store.billing_pulse()},
                            headers={"Cache-Control": "no-store"})

    @app.get("/v1/status")
    async def status(request: Request, x_sashmo_token: str | None = Header(default=None)) -> JSONResponse:
        authenticate(x_sashmo_token)
        local_dashboard_only(request)
        return JSONResponse({**store.status(), "codex_task_requests": task_manager.review_status()},
                            headers={"Cache-Control": "no-store"})

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
