import json
from pathlib import Path
import subprocess

from sashmo.projects import ProjectRegistry


def _git(path: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)


def _config(tmp_path: Path, roots: list[Path], projects: list[dict]) -> Path:
    path = tmp_path / "projects.local.json"
    path.write_text(json.dumps({"allowed_roots": [str(root) for root in roots], "projects": projects}), encoding="utf-8")
    return path


def test_registered_git_project_has_path_free_summary(tmp_path: Path) -> None:
    repo = tmp_path / "sample"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("hello", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")
    registry = ProjectRegistry(_config(tmp_path, [tmp_path], [{"id": "sample", "name": "Sample", "path": str(repo)}]))

    project = registry.get_project("sample")
    assert project is not None
    assert project["state"] == "ok"
    assert project["dirty"] is False
    assert project["branch"]
    assert project["last_commit"]
    assert str(repo) not in json.dumps(project)
    assert registry.get_project("unknown") is None

    (repo / "untracked.txt").write_text("change", encoding="utf-8")
    assert registry.get_project("sample")["dirty"] is True


def test_disallowed_path_fails_closed(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    other = tmp_path / "other"
    allowed.mkdir()
    other.mkdir()
    registry = ProjectRegistry(_config(tmp_path, [allowed], [{"id": "other", "name": "Other", "path": str(other)}]))
    assert registry.list_projects() == {"projects": [], "error": "path_not_allowed"}


def test_missing_and_invalid_registry(tmp_path: Path) -> None:
    assert ProjectRegistry(tmp_path / "missing.json").list_projects()["error"] == "not_configured"
    path = tmp_path / "invalid.json"
    path.write_text("not JSON", encoding="utf-8")
    assert ProjectRegistry(path).list_projects()["error"] == "invalid_config"


def test_missing_project_and_nested_directory(tmp_path: Path) -> None:
    repo = tmp_path / "sample"
    repo.mkdir()
    _git(repo, "init", "-q")
    nested = repo / "nested"
    nested.mkdir()
    missing = tmp_path / "missing"
    registry = ProjectRegistry(_config(tmp_path, [tmp_path], [
        {"id": "nested", "name": "Nested", "path": str(nested)},
        {"id": "missing", "name": "Missing", "path": str(missing)},
    ]))
    summaries = {item["id"]: item for item in registry.list_projects()["projects"]}
    assert summaries["nested"]["state"] == "not_git_root"
    assert summaries["missing"]["state"] == "missing_path"


def test_empty_repository_is_valid(tmp_path: Path) -> None:
    repo = tmp_path / "empty"
    repo.mkdir()
    _git(repo, "init", "-q")
    registry = ProjectRegistry(_config(tmp_path, [tmp_path], [{"id": "empty", "name": "Empty", "path": str(repo)}]))
    summary = registry.get_project("empty")
    assert summary["state"] == "ok"
    assert summary["last_commit"] is None


def test_discussion_context_uses_only_explicit_notes_and_git_status(tmp_path: Path) -> None:
    repo = tmp_path / "sample"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("Source content must stay local", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "initial")
    config = _config(tmp_path, [tmp_path], [{"id": "sample", "name": "Sample", "path": str(repo)}])
    registry = ProjectRegistry(config)
    assert registry.discussion_context("How is Sample going?") is None

    data = json.loads(config.read_text(encoding="utf-8"))
    data["projects"][0]["briefing"] = "A small scheduling tool. Next: improve reminders."
    config.write_text(json.dumps(data), encoding="utf-8")
    context = registry.discussion_context("How is Sample going?")
    assert context is not None
    assert context["briefing"] == "A small scheduling tool. Next: improve reminders."
    assert "Source content must stay local" not in json.dumps(context)
    assert str(repo) not in json.dumps(context)
    assert registry.discussion_context("What about another project?") is None


def test_file_discussion_reads_bounded_tracked_text_only_when_enabled(tmp_path: Path) -> None:
    repo = tmp_path / "sample"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / "README.md").write_text("This project schedules garden watering.", encoding="utf-8")
    (repo / "api_key.txt").write_text("must never appear", encoding="utf-8")
    (repo / "notes.txt").write_text("password = garden-secret", encoding="utf-8")
    (repo / "untracked.md").write_text("untracked private note", encoding="utf-8")
    _git(repo, "add", "README.md", "api_key.txt", "notes.txt")
    config = _config(tmp_path, [tmp_path], [{"id": "sample", "name": "Sample", "path": str(repo),
                                             "file_context": True}])
    context = ProjectRegistry(config).discussion_context("What should Sample do next?")
    assert context is not None
    assert "garden watering" in context["files"]
    assert "must never appear" not in context["files"]
    assert "garden-secret" not in context["files"]
    assert "untracked private note" not in context["files"]
    assert str(repo) not in json.dumps(context)
