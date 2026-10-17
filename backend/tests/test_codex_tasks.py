import asyncio
import json
import subprocess

import pytest

from sashmo.codex_tasks import CodexTaskManager
from sashmo.projects import ProjectRegistry


class FakeRpc:
    def __init__(self):
        self.calls = []

    async def start(self):
        self.calls.append(("start", {}))

    async def request(self, method, params):
        self.calls.append((method, params))
        if method == "thread/start":
            return {"thread": {"id": "test-thread"}}
        return {"turn": {"status": "inProgress"}}

    async def close(self):
        pass


def test_voice_codex_request_needs_spoken_and_desktop_confirmation(tmp_path):
    repo = tmp_path / "sample"
    repo.mkdir()
    subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
    config = tmp_path / "projects.local.json"
    config.write_text(json.dumps({"allowed_roots": [str(tmp_path)], "projects": [
        {"id": "sample", "name": "Sample", "path": str(repo)}]}), encoding="utf-8")
    rpc = FakeRpc()
    manager = CodexTaskManager(ProjectRegistry(config), rpc=rpc)
    assert manager.prepare("In project Unknown add a dashboard for tasks") == (
        "I don't know that approved project yet. Add its Git folder in the Sashmo dashboard first.")
    heard = manager.prepare("In project Sample add a dashboard for tasks")
    assert "Say 'confirm Codex request'" in heard
    assert manager.review_status()["pending"]["state"] == "voice_confirmation"
    with pytest.raises(ValueError):
        asyncio.run(manager.approve())
    assert rpc.calls == []
    assert "ready in the desktop dashboard" in manager.confirm_voice("confirm Codex request")
    task = asyncio.run(manager.approve())
    assert task["thread_id"] == "test-thread"
    assert [method for method, _ in rpc.calls] == ["start", "thread/start", "turn/start"]
    assert rpc.calls[1][1]["cwd"] == str(repo)
    assert rpc.calls[2][1]["sandboxPolicy"]["writableRoots"] == [str(repo)]
    assert rpc.calls[2][1]["sandboxPolicy"]["networkAccess"] is False
    assert manager.review_status()["pending"] is None
