"""Optional, read-only OpenAI billed-cost monitor for one API key."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx


ADMIN_KEY_FILE = Path(__file__).resolve().parents[2] / "openAIAdminKey.txt"
COSTS_URL = "https://api.openai.com/v1/organization/costs"


def month_start_utc(now: datetime) -> int:
    return int(datetime(now.year, now.month, 1, tzinfo=timezone.utc).timestamp())


def sum_costs(page: dict) -> Decimal:
    total = Decimal("0")
    for bucket in page.get("data", []):
        for result in bucket.get("results", []):
            amount = result.get("amount") or {}
            if amount.get("currency") != "usd":
                raise ValueError("OpenAI costs were not reported in USD")
            try:
                value = Decimal(str(amount["value"]))
            except (InvalidOperation, KeyError, TypeError) as exc:
                raise ValueError("OpenAI returned an invalid cost") from exc
            if not value.is_finite() or value < 0:
                raise ValueError("OpenAI returned an invalid cost")
            total += value
    return total


async def fetch_month_cost(api_key_id: str, admin_key: str,
                           now: datetime | None = None) -> tuple[Decimal, datetime]:
    now = now or datetime.now(timezone.utc)
    params = {"start_time": month_start_utc(now), "end_time": int(now.timestamp()),
              "limit": 31, "api_key_ids[]": api_key_id}
    total = Decimal("0")
    async with httpx.AsyncClient(timeout=15.0) as client:
        for _ in range(5):
            response = await client.get(COSTS_URL, params=params,
                                        headers={"Authorization": f"Bearer {admin_key}"})
            response.raise_for_status()
            page = response.json()
            if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                raise ValueError("OpenAI returned invalid cost data")
            total += sum_costs(page)
            if not page.get("has_more"):
                return total, now
            cursor = page.get("next_page")
            if not isinstance(cursor, str) or not cursor:
                raise ValueError("OpenAI costs pagination was incomplete")
            params["page"] = cursor
    raise ValueError("OpenAI costs pagination exceeded the safe limit")


class BillingWorker:
    def __init__(self, state, api_key_id: str, budget_usd: Decimal | None,
                 admin_key_file: Path = ADMIN_KEY_FILE, poll_seconds: int = 900) -> None:
        self.state = state
        self.api_key_id = api_key_id
        self.budget_usd = budget_usd
        self.admin_key_file = admin_key_file
        self.poll_seconds = poll_seconds
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await self._task

    async def _run(self) -> None:
        while not self._stop.is_set():
            if not self.api_key_id or not self.admin_key_file.is_file():
                self.state.update_billing(None, self.budget_usd, None,
                                          "Add an OpenAI admin key and this device's API key ID in Settings")
            else:
                try:
                    admin_key = self.admin_key_file.read_text(encoding="utf-8-sig").strip()
                    if not admin_key:
                        raise ValueError("Admin key file is empty")
                    spent, as_of = await fetch_month_cost(self.api_key_id, admin_key)
                    self.state.update_billing(spent, self.budget_usd, as_of, None)
                except (OSError, ValueError, httpx.HTTPError):
                    self.state.update_billing(None, self.budget_usd, None,
                                              "Could not refresh OpenAI billed costs; check the admin key and key ID")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
            except asyncio.TimeoutError:
                pass
