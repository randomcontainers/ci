"""Edit the values the reconciler pins in package.yml, as text.

These are upstream.version, upstream.artifact.sha256, upstream.git.commit
and the sha256 of each extra artifact that follows the package version.
Comments and layout stay as they are. After the edit the file is parsed
again, and the result must equal the old definition with only those values
changed; anything else is refused.
"""

import dataclasses
import re

import yaml

from rc import names, versions, yamlio
from rc.config import Distros, Package, parse_package
from rc.errors import RcError

_KEY = re.compile(r"^(?P<indent> *)(?P<key>[A-Za-z0-9_-]+):(?P<rest>.*)$")
# A value the reconciler may replace: a plain or quoted scalar without `#`,
# optionally followed by a comment, which is kept.
_VALUE = re.compile(r"""^(?P<value>"[^"#\\]*"|'[^'#]*'|[^\s#'"][^\s#]*)(?P<gap>\s*)(?P<comment>#.*)?$""")


def _scalar(value: str, quoted: bool) -> str:
    if quoted or yaml.safe_load(value) != value:
        return f'"{value}"'
    return value


def _block(lines: list[str], start: int, end: int, key: str) -> tuple[int, int, int] | None:
    """(line, block start, block end) of a mapping key directly inside lines[start:end]."""
    child_indent = None
    for i in range(start, end):
        m = _KEY.match(lines[i])
        if not m or not lines[i].strip() or lines[i].lstrip().startswith("#"):
            continue
        indent = len(m.group("indent"))
        if child_indent is None:
            child_indent = indent
        if indent != child_indent or m.group("key") != key:
            continue
        stop = end
        for j in range(i + 1, end):
            text = lines[j]
            if text.strip() and not text.lstrip().startswith("#") and len(text) - len(text.lstrip(" ")) <= indent:
                stop = j
                break
        return i, i + 1, stop
    return None


def _replace(lines: list[str], index: int, value: str) -> None:
    """Write a new value, keeping a trailing comment at its column where there is room."""
    m = _KEY.match(lines[index])
    rest = m.group("rest")
    old = _VALUE.match(rest.strip())
    if old is None or (old.group("comment") and not old.group("gap")):
        raise RcError(f"package.yml: cannot edit {m.group('key')!r} on line {index + 1}")
    quoted = old.group("value")[:1] in ("'", '"')
    line = f"{m.group('indent')}{m.group('key')}: {_scalar(value, quoted)}"
    if old.group("comment"):
        column = m.start("rest") + len(rest) - len(rest.lstrip()) + old.start("comment")
        line += " " * max(1, column - len(line)) + old.group("comment")
    lines[index] = line


def _edit(lines: list[str], block: tuple[int, int, int] | None, keys: list[str], value: str, what: str) -> None:
    """Replace the value at a path of mapping keys below a block."""
    for key in keys:
        block = _block(lines, block[1], block[2], key) if block else None
    if block is None:
        raise RcError(f"package.yml has no {what}")
    _replace(lines, block[0], value)


def update_upstream(
    text: str,
    distros: Distros,
    *,
    version: str | None = None,
    sha256: str | None = None,
    commit: str | None = None,
    extras: dict[str, str] | None = None,
) -> str:
    before = parse_package(yamlio.load_text(text, "package.yml"), "package.yml", distros)
    extras = extras or {}
    if version is not None:
        try:
            versions.validate(before.versioning, version)
        except versions.VersionError as exc:
            raise RcError(f"refusing to write version: {exc}") from None
    for digest in (sha256, *extras.values()):
        if digest is not None and not names.SHA256_HEX.match(digest):
            raise RcError(f"refusing to write sha256 {digest!r}")
    if commit is not None and not names.GIT_SHA.match(commit):
        raise RcError(f"refusing to write commit {commit!r}")
    for name in extras:
        extra = before.upstream.extra(name)
        if extra is None or extra.pinned_by_hand:
            raise RcError(f"refusing to pin {name!r}: it is not an extra artifact that follows the package version")
    lines = text.split("\n")
    upstream = _block(lines, 0, len(lines), "upstream")
    if upstream is None:
        raise RcError("package.yml has no upstream section")
    if version is not None:
        _edit(lines, upstream, ["version"], version, "upstream.version")
    if sha256 is not None:
        _edit(lines, upstream, ["artifact", "sha256"], sha256, "upstream.artifact.sha256")
    if commit is not None:
        _edit(lines, upstream, ["git", "commit"], commit, "upstream.git.commit")
    for name, digest in extras.items():
        _edit(lines, upstream, ["extra-artifacts", name, "sha256"], digest, f"upstream.extra-artifacts.{name}.sha256")
    result = "\n".join(lines)
    after = parse_package(yamlio.load_text(result, "package.yml"), "package.yml", distros)
    if after != _expected(before, version, sha256, commit, extras):
        raise RcError("editing package.yml changed more than the version and the pinned digests")
    return result


def _expected(package: Package, version: str | None, sha256: str | None, commit: str | None, extras: dict[str, str]) -> Package:
    upstream = package.upstream
    if version is not None:
        upstream = dataclasses.replace(upstream, version=version)
    if sha256 is not None:
        upstream = dataclasses.replace(upstream, artifact=dataclasses.replace(upstream.artifact, sha256=sha256))
    if commit is not None:
        upstream = dataclasses.replace(upstream, git=dataclasses.replace(upstream.git, commit=commit))
    if extras:
        upstream = dataclasses.replace(
            upstream,
            extra_artifacts=tuple(
                dataclasses.replace(e, artifact=dataclasses.replace(e.artifact, sha256=extras[e.name])) if e.name in extras else e
                for e in upstream.extra_artifacts
            ),
        )
    return dataclasses.replace(package, upstream=upstream)
