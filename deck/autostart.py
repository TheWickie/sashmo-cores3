"""Per-user Windows startup entry for the optional Deck app."""

from __future__ import annotations

import os
from pathlib import Path


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "Sashmo Deck"


def command(root: Path) -> str:
    root = root.resolve()
    pythonw = root / ".venv" / "Scripts" / "pythonw.exe"
    launcher = root / "deck" / "start.py"
    return f'"{pythonw}" "{launcher}" --root "{root}" --hardware'


def enabled(root: Path) -> bool:
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_NAME)
        return value == command(root)
    except FileNotFoundError:
        return False


def set_enabled(root: Path, value: bool) -> None:
    if os.name != "nt":
        raise OSError("Windows startup is available only on Windows")
    import winreg
    if value:
        if not (root / ".venv" / "Scripts" / "pythonw.exe").is_file():
            raise OSError("Start Deck once to install its optional dependencies")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, command(root))
    else:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            try:
                current, _ = winreg.QueryValueEx(key, RUN_NAME)
            except FileNotFoundError:
                return
            if current == command(root):
                winreg.DeleteValue(key, RUN_NAME)
