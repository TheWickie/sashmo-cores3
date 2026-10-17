from pathlib import Path

from sashmo import codex_rpc


def test_codex_executable_finds_desktop_install_without_path(monkeypatch, tmp_path: Path):
    cli = tmp_path / "OpenAI" / "Codex" / "bin" / "version-a" / "codex.exe"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"stub")
    monkeypatch.setattr(codex_rpc.shutil, "which", lambda _name: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert codex_rpc.CodexAppServer().executable == str(cli)


def test_codex_executable_prefers_path(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(codex_rpc.shutil, "which", lambda _name: "C:/Codex/codex.exe")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert codex_rpc.CodexAppServer().executable == "C:/Codex/codex.exe"
