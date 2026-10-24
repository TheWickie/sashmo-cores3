"""Read active ChatGPT Scheduled tasks through a dedicated signed-in browser chat.

This uses the visible ChatGPT UI, not undocumented account endpoints. The
browser profile is private to the Windows user and never sent to the CoreS3.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

from playwright.async_api import async_playwright


QUERY = (
    "For Sashmo, read my actual ChatGPT Scheduled list now. Return ALL active "
    "tasks, including monitoring tasks, as one JSON object only: "
    '{"tasks":[{"title":"exact title","schedule":"stored schedule"}]}. '
    "Do not use conversation memory or infer next-run times. If you cannot "
    'access Scheduled, return {"error":"unavailable"}. Do not change tasks.'
)


def validate_chat_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or parsed.hostname != "chatgpt.com" or not re.fullmatch(
        r"/c/[a-zA-Z0-9-]{20,100}", parsed.path
    ) or parsed.query or parsed.fragment:
        raise ValueError("ChatGPT reminder URL must be a chatgpt.com conversation")
    return value


def browser_profile() -> Path:
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        raise RuntimeError("Windows local app data is unavailable")
    return Path(base) / "Sashmo" / "chatgpt-browser"


def parse_task_reply(text: str) -> list[dict[str, str]]:
    """Accept only a complete, explicitly structured assistant result."""
    match = re.search(r"\{\s*\"(?:tasks|error)\"\s*:", text)
    if not match:
        raise ValueError("ChatGPT did not provide a task list")
    decoder = json.JSONDecoder()
    payload, _ = decoder.raw_decode(text[match.start():])
    if not isinstance(payload, dict) or payload.get("error"):
        raise ValueError("ChatGPT could not read Scheduled")
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or len(tasks) > 100:
        raise ValueError("Invalid ChatGPT task list")
    result: list[dict[str, str]] = []
    for item in tasks:
        if not isinstance(item, dict) or not isinstance(item.get("title"), str):
            raise ValueError("Invalid ChatGPT task")
        title = item["title"].strip()
        schedule = item.get("schedule", "")
        if not title or not isinstance(schedule, str):
            raise ValueError("Invalid ChatGPT task")
        result.append({"title": title[:80], "schedule": schedule.strip()[:120]})
    return result


async def query_chat(chat_url: str, *, profile: Path | None = None) -> list[dict[str, str]]:
    """Ask one read-only question in the dedicated chat and parse its answer."""
    validate_chat_url(chat_url)
    profile = profile or browser_profile()
    profile.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        context = await playwright.chromium.launch_persistent_context(
            str(profile), channel="chrome", headless=True,
            args=["--disable-background-networking", "--no-first-run"],
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(chat_url, wait_until="domcontentloaded", timeout=30000)
            editor = page.locator('[contenteditable="true"][role="textbox"]').last
            try:
                await editor.wait_for(state="visible", timeout=20000)
            except Exception as exc:
                if (await page.title()).strip().casefold().startswith("just a moment"):
                    raise RuntimeError("background_browser_blocked") from exc
                raise RuntimeError("ChatGPT sign-in or chat is unavailable") from exc
            responses = page.locator('[data-content-search-unit-key$=":assistant"]')
            before = await responses.count()
            await editor.fill(QUERY)
            await editor.press("Enter")
            await page.wait_for_function(
                "count => document.querySelectorAll('[data-content-search-unit-key$=\":assistant\"]').length > count",
                arg=before, timeout=90000,
            )
            latest = responses.last
            deadline = asyncio.get_running_loop().time() + 90
            while asyncio.get_running_loop().time() < deadline:
                answer = await latest.inner_text()
                try:
                    result = parse_task_reply(answer)
                except ValueError:
                    result = None
                if result is not None and not await page.get_by_role("button", name="Stop", exact=True).is_visible():
                    return result
                await asyncio.sleep(1)
            raise RuntimeError("ChatGPT did not return a complete task list")
        finally:
            await context.close()


class ChatGPTReminderWorker:
    def __init__(self, state: Any, chat_url: str, poll_seconds: int = 900) -> None:
        self.state = state
        self.chat_url = validate_chat_url(chat_url)
        self.poll_seconds = max(300, poll_seconds)
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        while True:
            try:
                self.state.update_chatgpt_reminders(await query_chat(self.chat_url), None)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = ("ChatGPT blocked the background reminder check"
                         if isinstance(exc, RuntimeError) and str(exc) == "background_browser_blocked"
                         else "ChatGPT reminder sync unavailable")
                self.state.update_chatgpt_reminders(None, error)
            await asyncio.sleep(self.poll_seconds)
