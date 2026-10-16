"""Read-only voice response routing with optional ChatGPT plan conversation."""

from __future__ import annotations

import asyncio
from collections import deque
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
    def __init__(self, project_file: Path, client: SIWCClient | None = None) -> None:
        self.registry = ProjectRegistry(project_file)
        self.client = client
        self.turns: deque[dict[str, str]] = deque(maxlen=12)
        self.last_turn_at = 0.0
        self.last_source = "local"
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

    async def respond(self, transcript: str, quota: dict[str, Any] | None, connected: bool) -> str:
        async with self._lock:
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
