import asyncio

import pytest

from sashmo.codex_sessions import recent_sessions, recent_projects


class FakeRpc:
    def __init__(self, rows, turns=None):
        self.rows = rows
        self.turns = turns or {}
        self.calls = []

    async def request(self, method, params=None, timeout=30):
        self.calls.append((method, params))
        if method == "thread/list":
            return {"data": self.rows}
        value = self.turns[params["threadId"]]
        if isinstance(value, Exception):
            raise value
        return {"data": [{"status": value}]}


def test_recent_sessions_uses_read_methods_and_turn_evidence():
    rpc = FakeRpc([
        {"id": "running", "status": {"type": "active"}, "name": "secret", "cwd": "C:\\private"},
        {"id": "done", "status": {"type": "notLoaded"}, "updatedAt": 123, "preview": "private"},
        {"id": "failed", "status": {"type": "idle"}},
    ], {"done": "completed", "failed": "failed"})
    summaries = asyncio.run(recent_sessions(rpc))
    assert [s.state for s in summaries] == ["in_progress", "completed", "error"]
    assert summaries[1].updated_at == 123
    assert all("private" not in str(s.as_dict()) and "secret" not in str(s.as_dict()) for s in summaries)
    assert [name for name, _ in rpc.calls] == ["thread/list", "thread/turns/list", "thread/turns/list"]
    assert rpc.calls[0][1]["sourceKinds"] == ["cli", "vscode", "appServer"]
    assert rpc.calls[1][1]["itemsView"] == "notLoaded"


def test_missing_or_unsupported_turn_history_is_unknown():
    rpc = FakeRpc([
        {"id": "no-turns", "status": {"type": "idle"}},
        {"id": "unsupported", "status": {"type": "notLoaded"}},
        {"id": "system", "status": {"type": "systemError"}},
    ], {"no-turns": None, "unsupported": RuntimeError("private server path")})
    rpc.turns["no-turns"] = None
    summaries = asyncio.run(recent_sessions(rpc))
    assert [s.state for s in summaries] == ["unknown", "unknown", "error"]
    assert "private" not in str(summaries)


def test_bad_rows_are_skipped_and_limit_is_bounded():
    rpc = FakeRpc([None, {"name": "missing id"}, {"id": "ok", "status": {"type": "active"}, "updatedAt": True}])
    assert asyncio.run(recent_sessions(rpc, limit=3))[0].updated_at is None
    with pytest.raises(ValueError):
        asyncio.run(recent_sessions(rpc, limit=11))


def test_recent_projects_deduplicates_workspaces_and_skips_chat_scratch():
    rpc = FakeRpc([
        {"cwd": r"G:\Work\Sashmo", "recencyAt": 30, "preview": "private"},
        {"cwd": r"g:\work\sashmo", "recencyAt": 20},
        {"cwd": r"C:\Users\Example\Documents\Codex\2026-10-17\code"},
        {"cwd": r"G:\Work\Another", "updatedAt": 10},
    ])
    items = asyncio.run(recent_projects(rpc))
    assert [(item.name, item.updated_at) for item in items] == [("Sashmo", 30), ("Another", 10)]
    assert "private" not in str(items)
    assert "G:" not in str(items)
    assert all(method == "thread/list" for method, _ in rpc.calls)
