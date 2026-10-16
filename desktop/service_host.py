"""Independent, per-user Windows host for the Sashmo backend.

Run ``pythonw desktop/service_host.py run`` at Windows sign-in. The dashboard
may exit without affecting this process. Dashboard controls use a random
capability kept in the ignored ``work`` directory and a loopback socket;
neither a PID file nor an unauthenticated HTTP request can stop the backend.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parents[1]
RUN_VALUE = "Sashmo Backend"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STATE_NAME = "service-host.json"
LOCK_NAME = "service-host.lock"
BACKEND_LOG_NAME = "desktop-service.log"
HOST_LOG_NAME = "service-host.log"


def _paths(root: Path) -> tuple[Path, Path, Path]:
    work = root / "work"
    return work / STATE_NAME, work / LOCK_NAME, work / HOST_LOG_NAME


def _python_console() -> Path:
    executable = Path(sys.executable)
    if executable.name.casefold() == "pythonw.exe":
        console = executable.with_name("python.exe")
        if console.is_file():
            return console
    return executable


def _python_hidden() -> Path:
    executable = Path(sys.executable)
    hidden = executable.with_name("pythonw.exe")
    return hidden if os.name == "nt" and hidden.is_file() else executable


def _run_command() -> str:
    return f'"{_python_hidden()}" "{Path(__file__).resolve()}" run'


def autostart_enabled() -> bool:
    """Whether this user starts the service host at Windows sign-in."""
    if os.name != "nt":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
        return str(Path(__file__).resolve()).casefold() in str(value).casefold()
    except OSError:
        return False


def set_autostart(enabled: bool) -> None:
    """Register/unregister only Sashmo's user-level backend startup entry."""
    if os.name != "nt":
        raise RuntimeError("Automatic service startup is available on Windows only")
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, _run_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


def send_command(command: str, *, root: Path = ROOT, timeout: float = 2.0) -> dict:
    """Send a control command to this user's independent host.

    The ignored state file supplies a random capability. Only the loopback
    listener that holds it can act on stop/restart. A stale state file or PID
    cannot terminate an unrelated process.
    """
    if command not in {"status", "stop", "restart"}:
        raise ValueError("Unknown service command")
    state_path, _, _ = _paths(root)
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
        port = int(state["port"])
        token = str(state["token"])
        if not (1 <= port <= 65535 and len(token) == 64):
            raise ValueError("Invalid control state")
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as connection:
            connection.settimeout(timeout)
            connection.sendall((json.dumps({"token": token, "command": command}) + "\n").encode("utf-8"))
            response = _read_line(connection)
        result = json.loads(response)
        if not isinstance(result, dict) or not isinstance(result.get("ok"), bool):
            raise ValueError("Invalid control response")
        return result
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Sashmo service host is not available") from exc


def _read_line(connection: socket.socket) -> str:
    buffer = bytearray()
    while len(buffer) < 4096:
        part = connection.recv(min(1024, 4096 - len(buffer)))
        if not part:
            break
        buffer.extend(part)
        if b"\n" in part:
            break
    if b"\n" not in buffer:
        raise ValueError("Incomplete control message")
    return bytes(buffer).split(b"\n", 1)[0].decode("utf-8")


def _config(root: Path) -> dict:
    return json.loads((root / "config.local.json").read_text(encoding="utf-8"))


def _backend_health(root: Path) -> bool:
    config = _config(root)
    host = str(config["bind_host"])
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    request = urllib.request.Request(
        f"http://{host}:{int(config.get('port', 8765))}/v1/status",
        headers={"X-Sashmo-Token": str(config["device_token"])},
    )
    try:
        with urllib.request.urlopen(request, timeout=0.8) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _backend_port_occupied(root: Path) -> bool:
    config = _config(root)
    host = str(config["bind_host"])
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    try:
        with socket.create_connection((host, int(config.get("port", 8765))), timeout=0.4):
            return True
    except OSError:
        return False


def _spawn_backend(root: Path) -> subprocess.Popen:
    backend = root / "backend"
    log_path = root / "work" / BACKEND_LOG_NAME
    stream = log_path.open("ab", buffering=0)
    try:
        process = subprocess.Popen(
            [str(_python_console()), str(backend / "run.py")], cwd=backend,
            stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    finally:
        stream.close()
    return process


def _lock_file(path: Path):
    """Hold an exclusive one-byte lock for the lifetime of the host."""
    stream = path.open("a+b")
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt

            if not stream.read(1):
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        return None
    return stream


class ServiceHost:
    """Owns one backend process and restarts it independently of the GUI."""

    def __init__(self, root: Path = ROOT, *,
                 spawn_backend: Callable[[Path], subprocess.Popen] = _spawn_backend,
                 backend_health: Callable[[Path], bool] = _backend_health,
                 port_occupied: Callable[[Path], bool] = _backend_port_occupied) -> None:
        self.root = root
        self.spawn_backend = spawn_backend
        self.backend_health = backend_health
        self.port_occupied = port_occupied
        self.process: subprocess.Popen | None = None
        self.external = False
        self.stop_requested = False
        self.error: str | None = None
        self.next_attempt = 0.0
        self.retry_delay = 1.0
        self.last_spawn_at = 0.0
        self.token = secrets.token_hex(32)

    def _log(self, message: str) -> None:
        _, _, path = _paths(self.root)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")

    def _stop_backend(self) -> None:
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)

    def _tick(self) -> None:
        if self.process is not None:
            code = self.process.poll()
            if code is None:
                return
            self._log(f"Backend exited with code {code}; restarting")
            self.process = None
            if time.monotonic() - self.last_spawn_at < 2.0:
                self.next_attempt = time.monotonic() + self.retry_delay
                self.retry_delay = min(self.retry_delay * 2, 60.0)
            else:
                self.next_attempt = 0.0
                self.retry_delay = 1.0
        if time.monotonic() < self.next_attempt:
            return
        try:
            if self.backend_health(self.root):
                self.external = True
                self.error = None
                self.next_attempt = time.monotonic() + 2.0
                return
            self.external = False
            if self.port_occupied(self.root):
                self.error = "Backend port is occupied by another process"
                self.next_attempt = time.monotonic() + 5.0
                return
            self.process = self.spawn_backend(self.root)
            self.last_spawn_at = time.monotonic()
            self.error = None
            if self.retry_delay == 1.0:
                self.next_attempt = 0.0
            self._log("Backend started")
        except Exception as exc:
            # Keep private configuration and exception text out of diagnostics.
            self.error = f"Could not start backend ({type(exc).__name__})"
            self.next_attempt = time.monotonic() + self.retry_delay
            self.retry_delay = min(self.retry_delay * 2, 60.0)
            self._log(self.error)

    def _dispatch(self, request: dict) -> dict:
        if not secrets.compare_digest(str(request.get("token", "")), self.token):
            return {"ok": False, "message": "Unauthorized"}
        command = request.get("command")
        if command == "status":
            managed = self.process is not None and self.process.poll() is None
            return {"ok": True, "running": managed or self.external,
                    "managed": managed, "external": self.external,
                    "pid": self.process.pid if managed else None,
                    "error": self.error}
        if command == "stop":
            if self.external and self.process is None:
                return {"ok": False, "message": "Existing backend is not managed by Sashmo service host"}
            self.stop_requested = True
            return {"ok": True, "message": "Stopping Sashmo backend"}
        if command == "restart":
            if self.external and self.process is None:
                return {"ok": False, "message": "Existing backend is not managed by Sashmo service host"}
            self._stop_backend()
            self.next_attempt = 0.0
            return {"ok": True, "message": "Restarting Sashmo backend"}
        return {"ok": False, "message": "Unknown command"}

    def run(self) -> bool:
        """Serve until authenticated stop. False means another host owns lock."""
        state_path, lock_path, _ = _paths(self.root)
        state_path.parent.mkdir(parents=True, exist_ok=True)
        lock = _lock_file(lock_path)
        if lock is None:
            return False
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
                listener.bind(("127.0.0.1", 0))
                listener.listen(8)
                listener.settimeout(0.25)
                state = {"pid": os.getpid(), "port": listener.getsockname()[1], "token": self.token}
                staged = state_path.with_suffix(".tmp")
                staged.write_text(json.dumps(state), encoding="utf-8")
                os.replace(staged, state_path)
                self._log("Service host started")
                while not self.stop_requested:
                    self._tick()
                    try:
                        connection, _ = listener.accept()
                    except socket.timeout:
                        continue
                    with connection:
                        connection.settimeout(1.0)
                        try:
                            request = json.loads(_read_line(connection))
                            if not isinstance(request, dict):
                                raise ValueError("Invalid request")
                            result = self._dispatch(request)
                        except (OSError, ValueError, json.JSONDecodeError):
                            result = {"ok": False, "message": "Invalid control request"}
                        connection.sendall((json.dumps(result) + "\n").encode("utf-8"))
        finally:
            self._stop_backend()
            try:
                current = json.loads(state_path.read_text(encoding="utf-8"))
                if current.get("token") == self.token:
                    state_path.unlink()
            except (OSError, ValueError, json.JSONDecodeError):
                pass
            lock.close()
            self._log("Service host stopped")
        return True


def start_supervisor(*, root: Path = ROOT, wait_seconds: float = 5.0) -> dict:
    """Ensure the independent host is running; never create a duplicate."""
    try:
        return send_command("status", root=root)
    except RuntimeError:
        pass
    (root / "work").mkdir(parents=True, exist_ok=True)
    log_path = root / "work" / HOST_LOG_NAME
    with log_path.open("ab", buffering=0) as stream:
        subprocess.Popen(
            [str(_python_hidden()), str(Path(__file__).resolve()), "run"], cwd=root,
            stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        try:
            return send_command("status", root=root)
        except RuntimeError:
            time.sleep(0.1)
    raise RuntimeError("Sashmo service host did not start; check work/service-host.log")


def main() -> None:
    if len(sys.argv) == 2 and sys.argv[1] == "run":
        ServiceHost().run()
    elif len(sys.argv) == 2 and sys.argv[1] in {"status", "stop", "restart"}:
        print(json.dumps(send_command(sys.argv[1])))
    elif len(sys.argv) == 2 and sys.argv[1] == "start":
        print(json.dumps(start_supervisor()))
    else:
        raise SystemExit("Usage: service_host.py {run|start|status|stop|restart}")


if __name__ == "__main__":
    main()
