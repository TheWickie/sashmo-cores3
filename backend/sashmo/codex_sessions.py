"""Read-only, privacy-conscious summaries of recent Codex App Server threads.

The App Server's ``thread/list`` and ``thread/turns/list`` methods are documented
at https://developers.openai.com/codex/app-server/ . No user prompts, turn
items, full working directories, or full project paths are copied into these summaries.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import PureWindowsPath
import re
from typing import Any, Protocol


class RpcClient(Protocol):
    async def request(
        self, method: str, params: dict[str, Any] | None = None, timeout: float = 30
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SessionSummary:
    """A small allowlisted projection suitable for the CoreS3 or dashboard."""

    label: str
    state: str
    updated_at: int | None

    def as_dict(self) -> dict[str, str | int | None]:
        return asdict(self)


@dataclass(frozen=True)
class ProjectSummary:
    """A recent workspace name, with no working path or task content."""

    name: str
    updated_at: int | None

    def as_dict(self) -> dict[str, str | int | None]:
        return asdict(self)


@dataclass(frozen=True)
class WorkSummary:
    """A saved Codex task title; the id stays on the desktop."""

    thread_id: str
    title: str
    source: str
    updated_at: int | None

    def as_dict(self) -> dict[str, str | int | None]:
        return asdict(self)


_SENSITIVE_TEXT = re.compile(
    r"sk-[A-Za-z0-9_-]{16,}|AIza[A-Za-z0-9_-]{20,}|"
    r"(?i:password|api[_ -]?key|secret|token)\s*[:=]\s*\S+"
)


def _safe_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    clean = _SENSITIVE_TEXT.sub("[redacted]", value)
    clean = " ".join(clean.split())
    return clean[:limit]


async def recent_work(client: RpcClient, limit: int = 10) -> list[WorkSummary]:
    """Read recent CLI, VS Code and app-server task titles, including folderless work."""
    if not 1 <= limit <= 10:
        raise ValueError("limit must be between 1 and 10")
    result = await client.request("thread/list", {
        "limit": limit, "sortKey": "recency_at", "sortDirection": "desc",
        "sourceKinds": ["cli", "vscode", "appServer"], "archived": False,
        "useStateDbOnly": True,
    })
    rows = result.get("data") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return []
    summaries: list[WorkSummary] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        thread_id = row.get("id")
        if not isinstance(thread_id, str) or not 1 <= len(thread_id) <= 128:
            continue
        title = _safe_text(row.get("name") or row.get("preview"), 100)
        if not title:
            title = "Untitled Codex task"
        source = row.get("source")
        summaries.append(WorkSummary(thread_id, title,
                                     source if source in {"cli", "vscode", "appServer"} else "unknown",
                                     _timestamp(row.get("recencyAt") or row.get("updatedAt"))))
    return summaries


def _message_text(item: dict[str, Any]) -> str:
    if item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
        return _safe_text(item.get("text"), 800)
    if item.get("type") == "userMessage":
        parts = []
        for block in (item.get("content") or [])[:5]:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(_safe_text(block.get("text"), 400))
        return _safe_text(" ".join(parts), 400)
    return ""


async def work_excerpt(client: RpcClient, thread_id: str) -> list[dict[str, str]]:
    """Return bounded user requests and final Codex answers; omit tools and reasoning."""
    if not isinstance(thread_id, str) or not 1 <= len(thread_id) <= 128:
        raise ValueError("Invalid thread id")
    result = await client.request("thread/turns/list", {
        "threadId": thread_id, "limit": 12, "sortDirection": "desc", "itemsView": "summary",
    })
    rows = result.get("data") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return []
    messages: list[dict[str, str]] = []
    for turn in reversed(rows[:12]):
        if not isinstance(turn, dict):
            continue
        for item in (turn.get("items") or [])[:20]:
            if not isinstance(item, dict):
                continue
            role = {"userMessage": "user", "agentMessage": "assistant"}.get(item.get("type"))
            text = _message_text(item)
            if role and text:
                messages.append({"role": role, "text": text})
    return messages[-12:]


def _workspace_name(cwd: Any) -> tuple[str, str] | None:
    if not isinstance(cwd, str) or not cwd or len(cwd) > 1024:
        return None
    normal = cwd.replace("/", "\\").rstrip("\\")
    # Codex's dated chat scratch folders are not saved projects.
    if re.search(r"\\Documents\\Codex\\\d{4}-\d{2}-\d{2}\\", normal, re.I):
        return None
    name = PureWindowsPath(normal).name
    if re.fullmatch(r"g-p-[0-9a-f]{16,}", name, re.I):
        return None
    if not name or name in (".", "..") or any(ord(c) < 32 for c in name):
        return None
    return normal.casefold(), name[:80]


async def recent_projects(client: RpcClient, limit: int = 10) -> list[ProjectSummary]:
    """Group recent Codex tasks by workspace, newest first, without reading files.

    A bounded scan walks up to twenty pages of 20 tasks. Project names are local
    folder names; task prompts, source files, and full paths are not returned.
    """
    if not 1 <= limit <= 10:
        raise ValueError("limit must be between 1 and 10")
    found: list[ProjectSummary] = []
    seen: set[str] = set()
    cursor: str | None = None
    for _ in range(20):
        params: dict[str, Any] = {
            "limit": 20, "sortKey": "recency_at", "sortDirection": "desc",
            "sourceKinds": ["cli", "vscode", "appServer"], "archived": False,
            "useStateDbOnly": True,
        }
        if cursor:
            params["cursor"] = cursor
        result = await client.request("thread/list", params)
        rows = result.get("data") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            break
        for row in rows:
            if not isinstance(row, dict):
                continue
            workspace = _workspace_name(row.get("cwd"))
            if workspace is None or workspace[0] in seen:
                continue
            seen.add(workspace[0])
            found.append(ProjectSummary(workspace[1], _timestamp(row.get("recencyAt") or row.get("updatedAt"))))
            if len(found) >= limit:
                return found
        next_cursor = result.get("nextCursor")
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
            break
        cursor = next_cursor
    return found


def _timestamp(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _thread_status(thread: dict[str, Any]) -> str | None:
    status = thread.get("status")
    kind = status.get("type") if isinstance(status, dict) else None
    if kind == "systemError":
        return "error"
    if kind == "active":
        return "in_progress"
    return None


def _turn_status(result: dict[str, Any]) -> str:
    turns = result.get("data")
    if not isinstance(turns, list) or not turns or not isinstance(turns[0], dict):
        return "unknown"
    status = turns[0].get("status")
    return {
        "inProgress": "in_progress",
        "completed": "completed",
        "failed": "error",
        "interrupted": "interrupted",
    }.get(status, "unknown")


async def recent_sessions(client: RpcClient, limit: int = 5) -> list[SessionSummary]:
    """Return up to ``limit`` recent task states, without task content.

    Uses only read methods. A currently active thread takes precedence over an
    older completed turn. A thread that is idle/not loaded without a recorded
    turn is ``unknown``, rather than being reported as completed. Individual
    turn-read failures degrade to ``unknown`` and do not include exception text.
    """

    if not 1 <= limit <= 10:
        raise ValueError("limit must be between 1 and 10")
    result = await client.request("thread/list", {
        "limit": limit,
        "sortKey": "recency_at",
        "sortDirection": "desc",
        "sourceKinds": ["cli", "vscode", "appServer"],
        "archived": False,
    })
    rows = result.get("data") if isinstance(result, dict) else None
    if not isinstance(rows, list):
        return []

    summaries: list[SessionSummary] = []
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        thread_id = row.get("id")
        if not isinstance(thread_id, str) or not thread_id:
            continue
        state = _thread_status(row)
        if state is None:
            try:
                turns = await client.request("thread/turns/list", {
                    "threadId": thread_id,
                    "limit": 1,
                    "sortDirection": "desc",
                    "itemsView": "notLoaded",
                })
            except Exception:
                state = "unknown"
            else:
                state = _turn_status(turns) if isinstance(turns, dict) else "unknown"
        summaries.append(SessionSummary(
            label=f"Codex task {len(summaries) + 1}",
            state=state,
            updated_at=_timestamp(row.get("updatedAt")),
        ))
    return summaries
