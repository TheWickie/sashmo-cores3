"""Exercise the supervisor with fake processes and an isolated work folder."""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "desktop"))
from service_host import ServiceHost, send_command  # noqa: E402


class FakeProcess:
    def __init__(self, pid: int):
        self.pid = pid
        self.exit_code = None
        self.terminated = False

    def poll(self):
        return self.exit_code

    def terminate(self):
        self.terminated = True
        self.exit_code = 0

    def wait(self, timeout=None):
        return self.exit_code

    def kill(self):
        self.terminate()


def _until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for service host")


def test_control_requires_capability_and_survives_client_disconnect(tmp_path):
    processes = []

    def spawn(_root):
        process = FakeProcess(1000 + len(processes))
        processes.append(process)
        return process

    host = ServiceHost(tmp_path, spawn_backend=spawn,
                       backend_health=lambda _: False, port_occupied=lambda _: False)
    thread = threading.Thread(target=host.run)
    thread.start()
    try:
        _until(lambda: len(processes) == 1)
        first = send_command("status", root=tmp_path)
        assert first["managed"] and first["running"] and first["pid"] == 1000
        state = json.loads((tmp_path / "work" / "service-host.json").read_text())
        with socket.create_connection(("127.0.0.1", state["port"])) as connection:
            connection.sendall(b'{"token":"wrong","command":"stop"}\n')
            assert json.loads(connection.recv(1024))["ok"] is False
        assert send_command("status", root=tmp_path)["running"]
        assert send_command("restart", root=tmp_path)["ok"]
        _until(lambda: len(processes) == 2)
        assert processes[0].terminated
        assert not processes[1].terminated
        assert send_command("stop", root=tmp_path)["ok"]
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert processes[1].terminated
        assert not (tmp_path / "work" / "service-host.json").exists()
    finally:
        if thread.is_alive():
            send_command("stop", root=tmp_path)
            thread.join(timeout=3)


def test_does_not_manage_existing_authenticated_backend(tmp_path):
    spawned = []
    host = ServiceHost(tmp_path, spawn_backend=lambda _: spawned.append(True),
                       backend_health=lambda _: True, port_occupied=lambda _: True)
    thread = threading.Thread(target=host.run)
    thread.start()
    try:
        _until(lambda: (tmp_path / "work" / "service-host.json").exists())
        _until(lambda: send_command("status", root=tmp_path).get("external"))
        assert spawned == []
        assert send_command("restart", root=tmp_path)["ok"] is False
        assert send_command("stop", root=tmp_path)["ok"] is False
        # Simulate the old dashboard/service closing. The host can then take over.
        host.backend_health = lambda _: False
        host.port_occupied = lambda _: False
        host.spawn_backend = lambda _: FakeProcess(2000)
        _until(lambda: send_command("status", root=tmp_path).get("managed"), timeout=4.0)
        assert send_command("stop", root=tmp_path)["ok"]
        thread.join(timeout=3)
        assert not thread.is_alive()
    finally:
        if thread.is_alive():
            host.stop_requested = True
            thread.join(timeout=3)


def test_rejects_stale_state_without_killing_any_pid(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "service-host.json").write_text(
        json.dumps({"pid": 1, "port": 1, "token": "a" * 64}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="not available"):
        send_command("stop", root=tmp_path, timeout=0.1)


def test_crashed_backend_restarts_and_second_host_does_not_spawn(tmp_path):
    processes = []

    def spawn(_root):
        process = FakeProcess(3000 + len(processes))
        processes.append(process)
        return process

    host = ServiceHost(tmp_path, spawn_backend=spawn,
                       backend_health=lambda _: False, port_occupied=lambda _: False)
    thread = threading.Thread(target=host.run)
    thread.start()
    try:
        _until(lambda: len(processes) == 1)
        rival = ServiceHost(tmp_path, spawn_backend=spawn,
                            backend_health=lambda _: False, port_occupied=lambda _: False)
        assert rival.run() is False
        assert len(processes) == 1
        processes[0].exit_code = 2
        _until(lambda: len(processes) == 2, timeout=4.0)
        assert send_command("status", root=tmp_path)["pid"] == 3001
        assert send_command("stop", root=tmp_path)["ok"]
        thread.join(timeout=3)
        assert not thread.is_alive()
    finally:
        if thread.is_alive():
            host.stop_requested = True
            thread.join(timeout=3)
