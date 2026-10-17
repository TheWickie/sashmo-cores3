"""Bounded, cancellable Cedar reply segments for the CoreS3 speaker."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import secrets
import time
from typing import Awaitable, Callable


AudioProducer = Callable[[str], Awaitable[bytes]]


@dataclass
class _Session:
    reply_id: str
    count: int
    queue: asyncio.Queue[tuple[int, bytes | None]]
    task: asyncio.Task[None]
    last_access: float


class VoiceStreamManager:
    """Keep at most two pending WAV segments; one device reply is active at a time."""

    def __init__(self, synthesize: AudioProducer) -> None:
        self.synthesize = synthesize
        self.active: _Session | None = None

    async def start(self, parts: list[str]) -> tuple[bytes, str | None, bool]:
        await self.cancel()
        if not parts:
            raise ValueError("No reply text")
        first = await self.synthesize(parts[0])
        if len(parts) == 1:
            return first, None, False
        reply_id = secrets.token_urlsafe(18)
        queue: asyncio.Queue[tuple[int, bytes | None]] = asyncio.Queue(maxsize=2)
        task = asyncio.create_task(self._produce(parts[1:], queue))
        self.active = _Session(reply_id, len(parts), queue, task, time.monotonic())
        return first, reply_id, True

    async def _produce(self, parts: list[str], queue: asyncio.Queue[tuple[int, bytes | None]]) -> None:
        try:
            for index, text in enumerate(parts, start=1):
                audio = await self.synthesize(text)
                await asyncio.wait_for(queue.put((index, audio)), timeout=120)
        except asyncio.CancelledError:
            raise
        except Exception:
            try:
                await asyncio.wait_for(queue.put((-1, None)), timeout=5)
            except asyncio.TimeoutError:
                pass

    async def next(self, reply_id: str) -> tuple[bytes, bool, int, int]:
        session = self.active
        if session is None or not secrets.compare_digest(session.reply_id, reply_id):
            raise LookupError("Reply has expired")
        if time.monotonic() - session.last_access > 180:
            await self.cancel(reply_id)
            raise LookupError("Reply has expired")
        session.last_access = time.monotonic()
        try:
            index, audio = await asyncio.wait_for(session.queue.get(), timeout=55)
        except asyncio.TimeoutError as exc:
            await self.cancel(reply_id)
            raise TimeoutError("Next reply segment was not ready") from exc
        if index < 0 or audio is None:
            await self.cancel(reply_id)
            raise RuntimeError("Cedar speech unavailable")
        more = index + 1 < session.count
        if not more:
            await self.cancel(reply_id)
        return audio, more, index + 1, session.count

    async def cancel(self, reply_id: str | None = None) -> bool:
        session = self.active
        if session is None or (reply_id is not None and not secrets.compare_digest(
                session.reply_id, reply_id)):
            return False
        self.active = None
        session.task.cancel()
        await asyncio.gather(session.task, return_exceptions=True)
        return True
