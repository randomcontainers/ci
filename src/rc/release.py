"""rc source-release: fetch and verify the upstream source for the v<version> release.

Every file must match the sha256 pinned in package.yml, the same value the
Dockerfile verifies. A package built from a git tag gets an archive of the
pinned commit, made with `git archive` after checking that the tag still
points to it. Detached signatures, when the package lists them, are
attached as published upstream so users can check them themselves.
"""

import hashlib
import os
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from rc import http, names
from rc.errors import RcError

GIT_TIMEOUT = 20 * 60
# Fetches go over https only; tests allow file.
GIT_PROTOCOL = "https"
# Makes git archive include every file of the commit and expand nothing,
# whatever the repository's .gitattributes say.
ARCHIVE_ATTRIBUTES = "* -export-ignore -export-subst\n"


@dataclass(frozen=True)
class Asset:
    path: Path
    description: str


def asset_name(url: str) -> str:
    name = names.url_file_name(url)
    if name is None:
        raise RcError(f"cannot derive a file name from {url}")
    return name


def _git(args: list[str], cwd: Path) -> str:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(cwd),
        "LC_ALL": "C",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
    }
    command = ["git", "-c", "protocol.allow=never", "-c", f"protocol.{GIT_PROTOCOL}.allow=always", "-c", "init.defaultBranch=main", *args]
    try:
        proc = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=GIT_TIMEOUT, check=False)  # noqa: S603 - fixed argv, values checked by the caller
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RcError(f"git {args[0]}: {exc}") from None
    if proc.returncode != 0:
        raise RcError(f"git {args[0]} failed: {proc.stderr.strip()[-500:]}")
    return proc.stdout


def git_archive(url: str, tag: str, commit: str, stem: str, dest: Path, work: Path) -> str:
    """Fetch a tag, check that it is the pinned commit and write <stem>.tar.gz of it; returns its sha256."""
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    _git(["init", "--quiet"], work)
    _git(["fetch", "--quiet", "--depth", "1", "--no-tags", "--", url, f"+refs/tags/{tag}:refs/tags/{tag}"], work)
    found = _git(["rev-parse", "--verify", "--end-of-options", f"refs/tags/{tag}^{{commit}}"], work).strip()
    if found != commit:
        raise RcError(f"tag {tag} at {url} is commit {found}, but package.yml pins {commit}")
    (work / ".git" / "info").mkdir(exist_ok=True)
    (work / ".git" / "info" / "attributes").write_text(ARCHIVE_ATTRIBUTES, encoding="utf-8")
    _git(["archive", "--format=tar.gz", f"--prefix={stem}/", "-o", str(dest.resolve()), commit], work)
    digest = hashlib.sha256()
    with dest.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _check(release: dict) -> None:
    """Re-check the values that reach git and file names; the plan passed through a job output."""
    git = release.get("git")
    if git:
        stem = git.get("archive", "")
        if (
            not names.GIT_URL.match(git.get("url", ""))
            or not names.is_git_tag(git.get("tag", ""))
            or not names.GIT_SHA.match(git.get("commit", ""))
            or not names.FILE_NAME.match(f"{stem}.tar.gz")
        ):
            raise RcError("the plan has an invalid git source")
    pinned = [e.get("sha256") or "" for e in release.get("extras", [])]
    if release.get("url"):
        pinned.append(release.get("sha256") or "")
    if not all(names.SHA256_HEX.match(digest) for digest in pinned):
        raise RcError("the plan has an invalid sha256")


Download = Callable[..., str]
Archive = Callable[[str, str, str, str, Path, Path], str]


def fetch(release: dict, out_dir: Path, download: Download = http.download, archive: Archive = git_archive) -> list[Asset]:
    """Download and verify every file of the release into out_dir/assets."""
    _check(release)
    assets = out_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    planned: list[tuple[str, str | None, str]] = []  # url, pinned sha256, description
    if release.get("url"):
        planned.append((release["url"], release["sha256"], f"from {release['url']}"))
        if release.get("signature"):
            planned.append((release["signature"], None, f"upstream signature, from {release['signature']}"))
    for extra in release.get("extras", []):
        where = f", only in the {' and '.join(d.capitalize() for d in extra['distros'])} images" if extra["distros"] else ""
        planned.append((extra["url"], extra["sha256"], f"{extra['name']} {extra['version']}{where}, from {extra['url']}"))
        if extra.get("signature"):
            planned.append((extra["signature"], None, f"upstream signature of {extra['name']}, from {extra['signature']}"))

    git = release.get("git")
    file_names = [asset_name(url) for url, _, _ in planned] + ([f"{git['archive']}.tar.gz"] if git else [])
    duplicates = sorted({n for n in file_names if file_names.count(n) > 1})
    if duplicates:
        raise RcError(f"two files of the release would both be named {', '.join(duplicates)}")

    out: list[Asset] = []
    if git:
        path = assets / f"{git['archive']}.tar.gz"
        digest = archive(git["url"], git["tag"], git["commit"], git["archive"], path, out_dir / "git")
        out.append(Asset(path, f"sha256 `{digest}`, `git archive` of commit `{git['commit']}`, tag `{git['tag']}` of {git['url']}"))
    for url, pinned, description in planned:
        path = assets / asset_name(url)
        if pinned is None:
            download(url, path, max_bytes=1024 * 1024)
            out.append(Asset(path, description))
            continue
        digest = download(url, path)
        if digest != pinned:
            path.unlink(missing_ok=True)
            raise RcError(f"{url} has sha256 {digest}, but package.yml pins {pinned}")
        out.append(Asset(path, f"sha256 `{pinned}`, {description}"))
    return out


def notes(plan: dict, release: dict, files: list[Asset]) -> str:
    lines = [
        f"Upstream source of {release['title'].removesuffix(' source')}, as built into the images from this repository.",
        "",
        *(f"- `{f.path.name}`: {f.description}" for f in files),
    ]
    if any(e.get("pinned_by_hand") for e in release.get("extras", [])):
        lines += ["", "The list is from the newest build of this version. Files attached for earlier builds stay attached."]
    lines += ["", f"Images: `{plan['image']}`"]
    return "\n".join(lines) + "\n"
