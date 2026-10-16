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

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from .conversation import ConversationProvider
from .gmail import GmailWorker
from .projects import ProjectRegistry
from .speech import process_audio, validate_wav
from .state import QuotaWorker, StateStore


LOG = logging.getLogger("sashmo")


@dataclass(frozen=True)
class Config:
    bind_host: str
    port: int
    device_token: str
    codex_poll_seconds: int = 300

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
        return cls(str(host), port, token, poll)


def create_app(config: Config, *, start_worker: bool = True, state: StateStore | None = None,
               conversation: ConversationProvider | None = None) -> FastAPI:
    telemetry_path = Path(__file__).resolve().parents[2] / "work" / "battery-history.csv"
    store = state or StateStore(telemetry_path=telemetry_path if start_worker else None)
    voice_provider = conversation or (ConversationProvider(Path(__file__).resolve().parents[2] /
                                                          "projects.local.json", state=store) if start_worker else None)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        worker = QuotaWorker(store, config.codex_poll_seconds) if start_worker else None
        gmail_worker = GmailWorker(store) if start_worker else None
        if worker:
            await worker.start()
        if gmail_worker:
            await gmail_worker.start()
        try:
            yield
        finally:
            if worker:
                await worker.stop()
            if gmail_worker:
                await gmail_worker.stop()

    app = FastAPI(title="Sashmo", docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.store = store

    def authenticate(value: str | None) -> None:
        if not value or not secrets.compare_digest(value, config.device_token):
            raise HTTPException(status_code=401, detail="Unauthorized")

    @app.get("/v1/pulse")
    async def pulse(since: int = -1, battery: int | None = None, charging: bool | None = None,
                    usb: bool | None = None, wifi_rssi: int | None = None,
                    x_sashmo_token: str | None = Header(default=None)) -> JSONResponse:
        authenticate(x_sashmo_token)
        store.record_device(battery, charging, usb, wifi_rssi)
        return JSONResponse(store.pulse(since))

    @app.get("/v1/status")
    async def status(x_sashmo_token: str | None = Header(default=None)) -> JSONResponse:
        authenticate(x_sashmo_token)
        return JSONResponse(store.status(), headers={"Cache-Control": "no-store"})

    @app.get("/v1/projects")
    async def projects(x_sashmo_token: str | None = Header(default=None)) -> JSONResponse:
        authenticate(x_sashmo_token)
        registry = ProjectRegistry(Path(__file__).resolve().parents[2] / "projects.local.json")
        result = await run_in_threadpool(registry.list_projects)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.post("/v1/voice")
    async def voice(request: Request, x_sashmo_token: str | None = Header(default=None)) -> Response:
        authenticate(x_sashmo_token)
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > 200_000:
            raise HTTPException(status_code=413, detail="Audio too large")
        audio = await request.body()
        if len(audio) > 200_000:
            raise HTTPException(status_code=413, detail="Audio too large")
        try:
            validate_wav(audio)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        store.voice_state = "processing"
        try:
            transcript, answer, output = await process_audio(audio, store.quota, store.codex_connected,
                                                             provider=voice_provider)
            store.voice_provider = voice_provider.last_source if voice_provider else "local"
        except Exception:
            LOG.exception("Voice processing failed")
            raise HTTPException(status_code=503, detail="Speech processing unavailable") from None
        finally:
            store.voice_state = "idle"
            store.last_voice_local = datetime.now().astimezone().isoformat(timespec="seconds")
        LOG.info("Voice request completed (recognized=%s, response_bytes=%d)", bool(transcript), len(output))
        return Response(
            output,
            media_type="audio/wav",
            headers={"X-Sashmo-Recognized": "1" if transcript else "0", "Cache-Control": "no-store"},
        )

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
