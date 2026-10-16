"""Read-only voice response routing with optional ChatGPT plan conversation."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
import re
import time
from typing import Any

from .projects import ProjectRegistry
from .siwc import SIWCClient, SIWCError, WindowsTokenStore
from .speech import answer_command


_GUIDANCE = ("You are Sashmo, a concise spoken assistant. Answer in at most 45 words. "
             "You have no tools in this conversation. Never claim to have read an email, "
             "changed a file, performed an action, or checked live information unless it "
             "is explicitly provided in the conversation. Say when you do not know.")


class ConversationProvider:
    def __init__(self, project_file: Path, client: SIWCClient | None = None,
                 state: Any | None = None) -> None:
        self.registry = ProjectRegistry(project_file)
        self.client = client
        self.state = state
        self.turns: deque[dict[str, str]] = deque(maxlen=12)
        self.last_turn_at = 0.0
        self.last_source = "local"
        self.sensitive_output = False
        self._lock = asyncio.Lock()

    def _siwc(self) -> SIWCClient | None:
        if self.client is not None:
            return self.client
        try:
            self.client = SIWCClient(WindowsTokenStore())
        except SIWCError:
            return None
        return self.client

    @staticmethod
    def _local_command(transcript: str, quota: dict[str, Any] | None, connected: bool) -> str | None:
        words = transcript.casefold()
        if re.search(r"\b(quota|allowance|reset|time|date|today|connection|connected)\b", words):
            return answer_command(transcript, quota, connected)
        return None

    async def _project_answer(self, transcript: str) -> str | None:
        utterance = transcript.casefold()
        if "project" not in utterance and not utterance.startswith(("how's ", "how is ")):
            return None
        result = await asyncio.to_thread(self.registry.list_projects)
        for project in result["projects"]:
            if project["name"].casefold() in utterance or project["id"] in utterance:
                if project["state"] != "ok":
                    return f"I can't read {project['name']}'s Git status right now."
                change = "has uncommitted changes" if project["dirty"] else "has a clean working tree"
                return f"{project['name']} is on branch {project['branch']} and {change}."
        if "project" in utterance:
            if not result["projects"]:
                return "No projects are approved yet. Add one in the Sashmo desktop dashboard."
            return "I can check an approved project's branch and whether it has uncommitted changes. Which one?"
        return None

    def _mail_answer(self, transcript: str) -> str | None:
        utterance = transcript.casefold()
        if not re.search(r"\b(mail|email|emails|inbox)\b", utterance):
            return None
        self.sensitive_output = True
        snapshot = getattr(self.state, "gmail_snapshot", None)
        if snapshot is None:
            return "Gmail is not connected. You can connect it in Sashmo on Windows."
        count = snapshot.get("unread_count")
        if re.search(r"\b(latest|recent|newest)\b", utterance):
            messages = snapshot.get("messages") or []
            if messages:
                latest = messages[0]
                sender = str(latest.get("from") or "an unknown sender")[:80]
                subject = str(latest.get("subject") or "no subject")[:100]
                return f"The latest inbox message is from {sender}, about {subject}. I can only read headers right now."
        return f"You have {count} unread inbox messages." if count is not None else "Gmail status is unavailable."

    def _calendar_answer(self, transcript: str) -> str | None:
        utterance = transcript.casefold()
        if not re.search(r"\b(calendar|schedule|tomorrow|meeting|appointments?)\b", utterance):
            return None
        self.sensitive_output = True
        snapshot = getattr(self.state, "calendar_snapshot", None)
        if snapshot is None:
            return "Calendar is not connected. You can connect it in Sashmo on Windows."
        if getattr(self.state, "calendar_error", None):
            return "Calendar is temporarily unavailable. Check Sashmo on Windows."
        target = (datetime.now().astimezone() + timedelta(days=1)).date() if "tomorrow" in utterance else None
        events = []
        for item in snapshot.get("events") or []:
            raw = str(item.get("start") or "")
            try:
                start = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                local = start.astimezone() if start.tzinfo else start
                day = local.date()
                when = "all day" if item.get("all_day") else local.strftime("%I:%M %p").lstrip("0")
            except ValueError:
                continue
            if target is None or day == target:
                events.append(f"{str(item.get('title') or 'Event')[:80]} at {when}")
        if not events:
            return "You have no events tomorrow." if target else "No upcoming events are in the cached calendar window."
        day_label = "Tomorrow" if target else "Coming up"
        return day_label + ": " + "; ".join(events[:2]) + ("; and more." if len(events) > 2 else ".")

    async def respond(self, transcript: str, quota: dict[str, Any] | None, connected: bool) -> str:
        async with self._lock:
            self.sensitive_output = False
            local = self._mail_answer(transcript)
            if local is None:
                local = self._calendar_answer(transcript)
            if local is None:
                local = self._local_command(transcript, quota, connected)
            if local is None:
                local = await self._project_answer(transcript)
            if local is not None:
                self.last_source = "local"
                self._remember(transcript, local)
                return local

            client = self._siwc()
            if client is None:
                self.last_source = "local"
                return answer_command(transcript, quota, connected)
            try:
                selection = await asyncio.to_thread(client.active_selection)
            except SIWCError:
                self.last_source = "chatgpt_error"
                return "The saved ChatGPT account could not be read. Please reconnect it in Sashmo on Windows."
            if selection is None:
                self.last_source = "local"
                return answer_command(transcript, quota, connected)
            key, model = selection
            if time.monotonic() - self.last_turn_at > 1800:
                self.turns.clear()
            messages = [{"role": "user", "content": _GUIDANCE}, *self.turns,
                        {"role": "user", "content": transcript}]
            try:
                answer = await asyncio.to_thread(client.stream_text, key, model, messages)
            except SIWCError:
                self.last_source = "chatgpt_error"
                return "ChatGPT plan replies are unavailable. Please check the account and model in Sashmo on Windows."
            answer = answer.strip()
            if not answer:
                self.last_source = "chatgpt_error"
                return "ChatGPT returned no reply. Please try again."
            if len(answer) > 300:
                answer = answer[:297].rsplit(" ", 1)[0] + "..."
            self.last_source = "chatgpt_plan"
            self._remember(transcript, answer)
            return answer

    def _remember(self, transcript: str, answer: str) -> None:
        self.turns.append({"role": "user", "content": transcript})
        self.turns.append({"role": "assistant", "content": answer})
        self.last_turn_at = time.monotonic()
