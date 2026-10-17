"""A persistent, minimal JSON-lines client for the installed Codex App Server."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
from typing import Any


class CodexRpcError(RuntimeError):
    pass


def _codex_executable() -> str:
    found = shutil.which("codex")
    if found:
        return found
    # The Windows desktop app installs its CLI in a versioned directory that
    # may be absent from the PATH inherited by an autostarted service.
    local = os.environ.get("LOCALAPPDATA")
    if local:
        folder = Path(local) / "OpenAI" / "Codex" / "bin"
        candidates = (path for path in folder.glob("*/codex.exe") if path.is_file())
        latest = max(candidates, key=lambda path: path.stat().st_mtime, default=None)
        if latest:
            return str(latest)
    return "codex"


class CodexAppServer:
    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or _codex_executable()
        self.process: asyncio.subprocess.Process | None = None
        self.updated = asyncio.Event()
        self._reader_task: asyncio.Task[None] | None = None
        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def start(self) -> None:
        if self.alive:
            return
        self.process = await asyncio.create_subprocess_exec(
            self.executable, "app-server", "--stdio",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            # A single thread history reply can exceed asyncio's 64 KiB
            # default line limit, even when requesting one summary turn.
            limit=8 * 1024 * 1024,
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        await self.request("initialize", {
            "clientInfo": {"name": "sashmo", "title": "Sashmo", "version": "0.1.0"}
        })
        await self._send({"method": "initialized"})

    async def _send(self, message: dict[str, Any]) -> None:
        if not self.alive or self.process is None or self.process.stdin is None:
            raise CodexRpcError("Codex App Server is not running")
        async with self._write_lock:
            self.process.stdin.write((json.dumps(message, separators=(",", ":")) + "\n").encode())
            await self.process.stdin.drain()

    async def request(self, method: str, params: dict[str, Any] | None = None, timeout: float = 30) -> dict[str, Any]:
        if not self.alive:
            raise CodexRpcError("Codex App Server is not running")
        self._next_id += 1
        request_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"id": request_id, "method": method, "params": params or {}})
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)

    async def _read_loop(self) -> None:
        assert self.process and self.process.stdout
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                request_id = message.get("id")
                if request_id is not None:
                    future = self._pending.get(request_id)
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(CodexRpcError(str(message["error"])))
                        else:
                            future.set_result(message.get("result") or {})
                elif message.get("method") in ("account/rateLimits/updated", "account/updated"):
                    # Updates may be sparse. The service refreshes the full snapshot.
                    self.updated.set()
        finally:
            self.updated.set()
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(CodexRpcError("Codex App Server closed its connection"))

    async def close(self) -> None:
        process = self.process
        self.process = None
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if self._reader_task:
            self._reader_task.cancel()
            await asyncio.gather(self._reader_task, return_exceptions=True)
            self._reader_task = None
