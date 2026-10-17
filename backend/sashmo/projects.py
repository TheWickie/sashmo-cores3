"""Read-only summaries of explicitly registered local Git projects.

The registry file is private local configuration, never a source of commands.
Example::

    {"allowed_roots": ["D:/Development"],
     "projects": [{"id": "example", "name": "Example", "path": "D:/Development/example"}]}

Callers may display the returned summaries; paths and Git error output are never
included in them. Optional user-written briefings are kept in the private
registry and are returned only by ``discussion_context``. This module never
reads project file contents.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any


_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_MAX_PROJECTS = 32
_MAX_BRIEFING = 1500
_FILE_SUFFIXES = {".md", ".txt", ".py", ".js", ".ts", ".tsx", ".jsx", ".ino", ".cpp", ".h", ".cs", ".ps1"}
_PRIVATE_NAME = re.compile(r"secret|credential|password|token|private|api.?key|(^|[/\\])\.env", re.I)
_SECRET_TEXT = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY|\bsk-[A-Za-z0-9_-]{16,}|\bAIza[A-Za-z0-9_-]{20,}|\b(?:client_secret|api_key|password)\s*[:=]", re.I)


class RegistryError(ValueError):
    """Invalid or unavailable private registry configuration."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _absolute_path(value: Any) -> Path:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        raise RegistryError("invalid_config")
    return Path(value).resolve()


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _git(path: Path, *args: str) -> str:
    # Fixed arguments, no shell, no optional Git locks, and no fsmonitor hooks.
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(
            ["git", "-c", "core.fsmonitor=false", "-C", str(path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            env=env,
        )
    except FileNotFoundError as exc:
        raise RegistryError("git_unavailable") from exc
    except subprocess.TimeoutExpired as exc:
        raise RegistryError("git_timeout") from exc
    if result.returncode != 0:
        raise RegistryError("git_error")
    return result.stdout.strip()


class ProjectRegistry:
    """Loads an allowlist and returns bounded, path-free Git summaries."""

    def __init__(self, config_path: Path) -> None:
        self.config_path = Path(config_path)

    def _load(self) -> list[dict[str, Any]]:
        try:
            if self.config_path.stat().st_size > 64 * 1024:
                raise RegistryError("invalid_config")
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RegistryError("not_configured") from exc
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RegistryError("invalid_config") from exc
        if not isinstance(data, dict) or not isinstance(data.get("allowed_roots"), list) or not isinstance(data.get("projects"), list):
            raise RegistryError("invalid_config")
        roots = [_absolute_path(root) for root in data["allowed_roots"]]
        projects = data["projects"]
        if not roots or len(projects) > _MAX_PROJECTS:
            raise RegistryError("invalid_config")
        seen: set[str] = set()
        validated: list[dict[str, Any]] = []
        for project in projects:
            if not isinstance(project, dict):
                raise RegistryError("invalid_config")
            project_id, name = project.get("id"), project.get("name")
            if not isinstance(project_id, str) or not _ID.fullmatch(project_id) or project_id in seen:
                raise RegistryError("invalid_config")
            if (not isinstance(name, str) or not (1 <= len(name.strip()) <= 80)
                    or any(ord(char) < 32 for char in name)):
                raise RegistryError("invalid_config")
            briefing = project.get("briefing", "")
            if (not isinstance(briefing, str) or len(briefing) > _MAX_BRIEFING
                    or any(ord(char) < 32 and char not in "\n\t" for char in briefing)):
                raise RegistryError("invalid_config")
            file_context = project.get("file_context", False)
            if not isinstance(file_context, bool):
                raise RegistryError("invalid_config")
            seen.add(project_id)
            path = _absolute_path(project.get("path"))
            if not any(_under(path, root) for root in roots):
                raise RegistryError("path_not_allowed")
            validated.append({"id": project_id, "name": name.strip(), "path": path,
                              "briefing": briefing.strip(), "file_context": file_context})
        return validated

    def list_projects(self) -> dict[str, Any]:
        """Return summaries, or a single safe registry error if configuration fails."""
        try:
            projects = self._load()
        except RegistryError as exc:
            return {"projects": [], "error": exc.code}
        return {"projects": [self._summary(project) for project in projects], "error": None}

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        """Return one registered project; unknown IDs never access the filesystem."""
        try:
            projects = self._load()
        except RegistryError:
            return None
        for project in projects:
            if project["id"] == project_id:
                return self._summary(project)
        return None

    def approved_path(self, project_id: str) -> Path | None:
        """Resolve an explicitly approved Git root for local task dispatch."""
        try:
            projects = self._load()
        except RegistryError:
            return None
        for project in projects:
            if project["id"] == project_id and self._summary(project)["state"] == "ok":
                return project["path"]
        return None

    def discussion_context(self, utterance: str) -> dict[str, str] | None:
        """Find an explicitly named project with a user-authored briefing.

        The returned data is bounded and contains no path or source content.
        A project with no briefing remains status-only.
        """
        try:
            projects = self._load()
        except RegistryError:
            return None
        words = utterance.casefold()
        for project in projects:
            name = project["name"]
            project_id = project["id"]
            mentioned = (re.search(r"(?<!\w)" + re.escape(name.casefold()) + r"(?!\w)", words)
                         or re.search(r"(?<!\w)" + re.escape(project_id) + r"(?!\w)", words))
            if mentioned and (project["briefing"] or project["file_context"]):
                summary = self._summary(project)
                if summary["state"] != "ok":
                    return None
                return {"name": name, "briefing": project["briefing"],
                        "branch": summary["branch"] or "unknown",
                        "working_tree": "uncommitted changes" if summary["dirty"] else "clean",
                        "last_commit": summary["last_commit"] or "none",
                        "files": self._file_context(project["path"], utterance) if project["file_context"] else ""}
        return None

    @staticmethod
    def _file_context(root: Path, utterance: str) -> str:
        """Read a small, relevant slice of tracked text files in an approved repo.

        Tracked files only; no links outside the root, private-looking filenames,
        binary files, large files, or text with obvious credential patterns.
        """
        try:
            tracked = _git(root, "ls-files", "--cached", "-z").split("\0")
        except RegistryError:
            return ""
        terms = {word for word in re.findall(r"[a-z0-9]{4,}", utterance.casefold())
                 if word not in {"what", "with", "this", "that", "should", "could", "project", "about", "next", "please"}}
        candidates: list[tuple[int, str, Path]] = []
        for relative in tracked[:2000]:
            if not relative or len(relative) > 240 or _PRIVATE_NAME.search(relative):
                continue
            path = (root / relative).resolve()
            if not _under(path, root) or path.suffix.casefold() not in _FILE_SUFFIXES:
                continue
            lower = relative.casefold()
            if path.name.casefold() == "agents.md" or any(part.startswith(".") for part in Path(relative).parts):
                continue
            score = (20 if path.name.casefold().startswith("readme") else 0)
            score += 10 if lower.startswith("docs/") or lower.startswith("docs\\") else 0
            score += sum(8 for term in terms if term in lower)
            candidates.append((score, relative, path))
        candidates.sort(key=lambda item: (-item[0], item[1].casefold()))
        excerpts: list[str] = []
        budget = 12000
        for _, relative, path in candidates:
            if len(excerpts) >= 5 or budget < 500:
                break
            try:
                if not path.is_file() or path.stat().st_size > 30000:
                    continue
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            if "\0" in content or _SECRET_TEXT.search(content):
                continue
            excerpt = content[:min(3000, budget)].strip()
            if excerpt:
                excerpts.append(f"File {relative}:\n{excerpt}")
                budget -= len(excerpt)
        return "\n\n".join(excerpts)

    @staticmethod
    def _summary(project: dict[str, Any]) -> dict[str, Any]:
        summary: dict[str, Any] = {
            "id": project["id"], "name": project["name"],
            "state": "ok", "branch": None, "dirty": None, "last_commit": None,
            "file_context": project["file_context"],
        }
        path: Path = project["path"]
        if not path.is_dir():
            summary["state"] = "missing_path"
            return summary
        try:
            top = Path(_git(path, "rev-parse", "--show-toplevel")).resolve()
            if top != path:
                summary["state"] = "not_git_root"
                return summary
            summary["branch"] = _git(path, "branch", "--show-current")[:80] or "(detached)"
            summary["dirty"] = bool(_git(path, "status", "--porcelain", "--untracked-files=normal"))
            try:
                commit = _git(path, "log", "-1", "--format=%h %cI")
            except RegistryError:
                # A freshly initialised repository has no commits yet.
                commit = ""
            summary["last_commit"] = commit[:64] if commit else None
        except RegistryError as exc:
            summary["state"] = exc.code
        return summary
