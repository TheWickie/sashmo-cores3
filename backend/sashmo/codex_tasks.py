"""Voice-drafted Codex work, approved only from the local desktop dashboard."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import re
import time
from typing import Any

from .codex_rpc import CodexAppServer
from .projects import ProjectRegistry


class CodexTaskManager:
    def __init__(self, registry: ProjectRegistry, rpc: CodexAppServer | None = None) -> None:
        self.registry = registry
        self.rpc = rpc or CodexAppServer()
        self.pending: dict[str, Any] | None = None
        self.last_task: dict[str, Any] | None = None
        self._lock = asyncio.Lock()

    def prepare(self, transcript: str) -> str | None:
        """Recognize an explicit project command; never infer coding intent from chat."""
        match = re.match(r"^\s*(?:please\s+)?in\s+(?:the\s+)?project\s+(.+)$", transcript, re.I)
        if not match:
            return None
        rest = match.group(1).strip()
        projects = self.registry.list_projects().get("projects") or []
        for project in sorted(projects, key=lambda item: len(item["name"]), reverse=True):
            for alias in (project["name"], project["id"]):
                if rest.casefold().startswith(alias.casefold() + " "):
                    instruction = rest[len(alias):].strip(" ,:")
                    if not 12 <= len(instruction) <= 180:
                        return "Please give a specific project change in 12 to 180 characters."
                    if project["state"] != "ok":
                        return f"{project['name']} is not available as an approved Git project."
                    self.pending = {"project_id": project["id"], "project_name": project["name"],
                                    "instruction": instruction, "state": "voice_confirmation",
                                    "created_at": time.monotonic()}
                    return (f"I heard: in {project['name']}, {instruction}. "
                            "Say 'confirm Codex request' to put it on your desktop for review, or say 'cancel request'.")
        return "I don't know that approved project yet. Add its Git folder in the Sashmo dashboard first."

    def confirm_voice(self, transcript: str) -> str | None:
        words = transcript.casefold().strip(" .!?")
        if words in {"cancel request", "cancel codex request"}:
            self.pending = None
            return "Codex request cancelled."
        if words not in {"confirm codex request", "confirm the codex request"}:
            return None
        pending = self.pending
        if not pending or pending["state"] != "voice_confirmation" or time.monotonic() - pending["created_at"] > 120:
            self.pending = None
            return "There is no recent Codex request to confirm. Please say the project request again."
        pending["state"] = "desktop_review"
        return (f"Your request for {pending['project_name']} is ready in the desktop dashboard. "
                "Review it there and select Send to Codex.")

    def review_status(self) -> dict[str, Any]:
        pending = self.pending
        return {
            "pending": ({key: pending[key] for key in ("project_name", "instruction", "state")}
                        if pending else None),
            "last_task": self.last_task,
        }

    async def reject(self) -> None:
        async with self._lock:
            self.pending = None

    async def approve(self) -> dict[str, Any]:
        async with self._lock:
            pending = self.pending
            if not pending or pending["state"] != "desktop_review":
                raise ValueError("No confirmed request is waiting for desktop review")
            root = self.registry.approved_path(pending["project_id"])
            if root is None:
                raise ValueError("Project approval or Git folder is no longer valid")
            await self.rpc.start()
            started = await self.rpc.request("thread/start", {
                "cwd": str(root), "approvalPolicy": "never", "sandbox": "workspaceWrite",
                "serviceName": "sashmo",
            })
            thread_id = ((started.get("thread") or {}).get("id") if isinstance(started, dict) else None)
            if not isinstance(thread_id, str) or not thread_id:
                raise RuntimeError("Codex did not return a task ID")
            prompt = (pending["instruction"] + "\n\nThis request was dictated to Sashmo. "
                      "Work only in this project. Follow its AGENTS.md instructions. "
                      "Do not publish, deploy, purchase, or contact anyone. "
                      "Report changes and tests in this Codex task.")
            await self.rpc.request("turn/start", {
                "threadId": thread_id, "input": [{"type": "text", "text": prompt}],
                "cwd": str(root), "approvalPolicy": "never",
                "sandboxPolicy": {"type": "workspaceWrite", "writableRoots": [str(root)],
                                  "networkAccess": False},
            })
            self.last_task = {"thread_id": thread_id, "project_name": pending["project_name"],
                              "submitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            self.pending = None
            return self.last_task

    async def close(self) -> None:
        await self.rpc.close()
