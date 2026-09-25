"""Upstream version schemes: validation, ordering and tag shortcuts.

calver  2026.08.19, optionally with a build suffix (2026.09.16.232951).
semver  9.0.2 or 9.0; no pre-release or build metadata.
loose   dotted or dashed numbers with optional letters: 7.1.2-31, 10.08.0.
"""

import re
from functools import total_ordering

SCHEMES = ("calver", "semver", "loose")

_CALVER = re.compile(r"^(\d{4})\.(\d{2})\.(\d{2})(?:\.(\d{1,12}))?$")
_SEMVER = re.compile(r"^(0|[1-9]\d{0,8})\.(0|[1-9]\d{0,8})(?:\.(0|[1-9]\d{0,8}))?$")
_LOOSE = re.compile(r"^[0-9]{1,12}(?:[.-][0-9A-Za-z]{1,32}){0,8}$")
_LOOSE_SPLIT = re.compile(r"([.-])")


class VersionError(ValueError):
    pass


def validate(scheme: str, version: str) -> None:
    if scheme not in SCHEMES:
        raise VersionError(f"unknown versioning {scheme!r}")
    if not isinstance(version, str):
        raise VersionError(f"version must be a string, got {type(version).__name__}")
    if scheme == "calver":
        m = _CALVER.match(version)
        if not m or not (1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(3)) <= 31):
            raise VersionError(f"{version!r} is not a calendar version like 2026.08.19")
    elif scheme == "semver":
        if not _SEMVER.match(version):
            raise VersionError(f"{version!r} is not a version like 9.0.2")
    elif not _LOOSE.match(version):
        raise VersionError(f"{version!r} is not a version like 7.1.2-31 or 10.08.0")


@total_ordering
class _Part:
    """One component of a loose version. Numbers sort above words."""

    __slots__ = ("num", "text")

    def __init__(self, text: str):
        self.num = int(text) if text.isdigit() else None
        self.text = text

    def _key(self):
        return (1, self.num, "") if self.num is not None else (0, 0, self.text)

    def __eq__(self, other):
        return self._key() == other._key()

    def __lt__(self, other):
        return self._key() < other._key()

    def __hash__(self):
        return hash(self._key())


def sort_key(scheme: str, version: str) -> tuple:
    validate(scheme, version)
    if scheme == "calver":
        m = _CALVER.match(version)
        return tuple(int(g) if g is not None else -1 for g in m.groups())
    if scheme == "semver":
        m = _SEMVER.match(version)
        return tuple(int(g) if g is not None else 0 for g in m.groups())
    return tuple(_Part(p) for p in re.split(r"[.-]", version))


def compare(scheme: str, a: str, b: str) -> int:
    ka, kb = sort_key(scheme, a), sort_key(scheme, b)
    return (ka > kb) - (ka < kb)


def shortcuts(scheme: str, version: str) -> list[str]:
    """Floating prefixes of a version, longest first.

    semver 9.0.2 gives 9.0 and 9; loose 7.1.2-31 gives 7.1.2, 7.1 and 7.
    Calendar versions have none. A prefix ends at a separator and contains
    only numeric components.
    """
    validate(scheme, version)
    if scheme == "calver":
        return []
    pieces = _LOOSE_SPLIT.split(version)
    parts = pieces[0::2]
    seps = pieces[1::2]
    out = []
    for n in range(len(parts) - 1, 0, -1):
        if not all(p.isdigit() for p in parts[:n]):
            continue
        prefix = parts[0]
        for sep, part in zip(seps[: n - 1], parts[1:n]):
            prefix += sep + part
        out.append(prefix)
    return out


def nodots(version: str) -> str:
    return version.replace(".", "")
