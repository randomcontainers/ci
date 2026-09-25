"""Commits that `rc reconcile` plans and `rc apply-commits` makes.

The pass reads upstream releases, PyPI, the registry and every repository,
so it holds no token that can write to a repository. It writes the commits
it plans to a file. Only when the file is not empty does the workflow mint
a token with Contents and Workflows write, limited to the repositories the
file names, for `rc apply-commits`, which reads nothing but this file and
checks every value again before it is used.
"""

import base64
import binascii
import json
from pathlib import Path

from rc import names
from rc.errors import RcError
from rc.github import GitHub, safe_path

MAX_COMMITS = 50
MAX_FILES = 50
MAX_BYTES = 5_000_000
MAX_MESSAGE = 120
KEYS = {"name", "parent", "message", "files"}


def planned(name: str, parent: str, message: str, files: dict[str, bytes]) -> dict:
    encoded = {path: base64.b64encode(content).decode("ascii") for path, content in sorted(files.items())}
    return {"name": name, "parent": parent, "message": message, "files": encoded}


def check(commit, packages: tuple[str, ...]) -> dict:
    """Return the commit when every value is what the reconciler writes, or raise RcError.

    A package repository only ever gets package.yml and lock files, so its
    commits may not touch anything below the top level, such as .github/.
    """
    if not isinstance(commit, dict) or set(commit) != KEYS:
        raise RcError(f"unexpected commit {str(commit)[:80]!r}")
    name = commit["name"]
    problem = names.check_name(name)
    if problem:
        raise RcError(problem)
    if not isinstance(commit["parent"], str) or not names.GIT_SHA.match(commit["parent"]):
        raise RcError(f"{name}: the parent must be a commit id")
    if not names.is_single_line(commit["message"], MAX_MESSAGE):
        raise RcError(f"{name}: the message must be one line of at most {MAX_MESSAGE} characters")
    files = commit["files"]
    if not isinstance(files, dict) or not files or len(files) > MAX_FILES:
        raise RcError(f"{name}: expected 1 to {MAX_FILES} files")
    size = 0
    for path, content in files.items():
        if not isinstance(path, str) or not safe_path(path):
            raise RcError(f"{name}: {str(path)[:80]!r} is not a valid path")
        if name in packages and not names.FILE_NAME.match(path):
            raise RcError(f"{name}: {path!r} is not a file the reconciler writes to a package repository")
        if not isinstance(content, str):
            raise RcError(f"{name}: {path} is not base64")
        try:
            size += len(base64.b64decode(content, validate=True))
        except (binascii.Error, ValueError):
            raise RcError(f"{name}: {path} is not base64") from None
    if size > MAX_BYTES:
        raise RcError(f"{name}: the files add up to more than {MAX_BYTES} bytes")
    return commit


def check_all(commits, packages: tuple[str, ...]) -> list[dict]:
    if not isinstance(commits, list) or len(commits) > MAX_COMMITS:
        raise RcError(f"expected a list of at most {MAX_COMMITS} commits")
    checked = [check(c, packages) for c in commits]
    seen = [c["name"] for c in checked]
    if len(set(seen)) != len(seen):
        raise RcError("more than one commit to the same repository")
    return checked


def repositories(commits: list[dict]) -> str:
    """The repository names for the token, comma-separated."""
    return ",".join(sorted({c["name"] for c in commits}))


def load(path: Path, packages: tuple[str, ...]) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RcError(f"cannot read {path}: {exc}") from None
    try:
        return check_all(data, packages)
    except RcError as exc:
        raise RcError(f"{path}: {exc}") from None


def dump(commits: list[dict], path: Path, packages: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(check_all(commits, packages), indent=2) + "\n", encoding="utf-8")


def apply(commits: list[dict], writer: GitHub, org: str) -> list[str]:
    """Make each commit; return the problems. One failed commit does not stop the others."""
    problems = []
    for commit in commits:
        repo = f"{org}/{commit['name']}"
        files = {path: base64.b64decode(content) for path, content in commit["files"].items()}
        try:
            writer.commit(repo, commit["parent"], files, commit["message"])
        except RcError as exc:
            problems.append(f"commit \"{commit['message']}\" to {repo}: {exc}")
    return problems
