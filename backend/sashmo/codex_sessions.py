"""Read-only, privacy-conscious summaries of recent Codex App Server threads.

The App Server's ``thread/list`` and ``thread/turns/list`` methods are documented
at https://developers.openai.com/codex/app-server/ . No user prompts, turn
items, working directories, or project paths are copied into these summaries.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
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
