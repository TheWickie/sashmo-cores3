"""Background Calendar refresh, separate from device heartbeat traffic."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .calendar import CalendarClient, CalendarError, CalendarTokenStore


class CalendarWorker:
    def __init__(self, state: Any, poll_seconds: int = 300,
                 client: CalendarClient | None = None) -> None:
        self.state = state
        self.poll_seconds = max(120, poll_seconds)
        self.client = client
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        next_poll_at = 0.0
        while True:
            try:
                if self.client is None:
                    self.client = CalendarClient(CalendarTokenStore())
                configured = await asyncio.to_thread(self.client.configured)
                if configured:
                    if time.monotonic() >= next_poll_at:
                        snapshot = await asyncio.to_thread(self.client.upcoming, hours=48)
                        self.state.update_calendar(snapshot, None)
                        next_poll_at = time.monotonic() + self.poll_seconds
                else:
                    self.state.update_calendar(None, "Not connected")
                    self.client._cache = None
                    next_poll_at = 0.0
                await asyncio.sleep(15)
            except asyncio.CancelledError:
                raise
            except (CalendarError, OSError, ValueError):
                self.state.update_calendar(None, "Calendar unavailable; reconnect in the dashboard")
                next_poll_at = time.monotonic() + self.poll_seconds
                await asyncio.sleep(15)
