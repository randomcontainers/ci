"""Tag sets for package and combo images.

Floating tags can point at a different version after the next release
(latest, slim, ubuntu, 9.0, ...). The merge step moves them only forward.
Version tags (2026.08.19, 9.0.2-slim-alpine3.24, ...) always follow the
newest build of that version.
"""

from dataclasses import dataclass, field

from rc import names, versions
from rc.config import Distro
from rc.errors import RcError


@dataclass
class TagSet:
    tags: list[str] = field(default_factory=list)
    floating: list[str] = field(default_factory=list)

    def add(self, tag: str, *, floating: bool) -> None:
        if not names.TAG.match(tag):
            raise RcError(f"{tag!r} is not a valid OCI tag")
        if tag not in self.tags:
            self.tags.append(tag)
        if floating and tag not in self.floating:
            self.floating.append(tag)

    def extend(self, other: "TagSet") -> None:
        for tag in other.tags:
            self.add(tag, floating=tag in other.floating)

    @property
    def fixed(self) -> list[str]:
        return [t for t in self.tags if t not in self.floating]


def primary_tag(flavour: str, version: str, distro: Distro) -> str:
    """The most specific tag of an image, for example 9.0.2-slim-alpine3.24."""
    middle = "-slim-" if flavour == "slim" else "-"
    return f"{version}{middle}{distro.qualified}"


def package_tags(flavour: str, version: str, versioning: str, distro: Distro, default_distro: str) -> TagSet:
    if flavour not in ("slim", "default"):
        raise ValueError(flavour)
    short = versions.shortcuts(versioning, version)
    is_default = distro.id == default_distro
    d = distro.id
    ts = TagSet()
    if flavour == "default":
        if is_default:
            ts.add("latest", floating=True)
            ts.add(version, floating=False)
            for s in short:
                ts.add(s, floating=True)
        ts.add(d, floating=True)
        ts.add(f"{version}-{d}", floating=False)
        for s in short:
            ts.add(f"{s}-{d}", floating=True)
    else:
        if is_default:
            ts.add("slim", floating=True)
            ts.add(f"{version}-slim", floating=False)
            for s in short:
                ts.add(f"{s}-slim", floating=True)
        ts.add(f"slim-{d}", floating=True)
        ts.add(f"{version}-slim-{d}", floating=False)
        for s in short:
            ts.add(f"{s}-slim-{d}", floating=True)
    ts.add(primary_tag(flavour, version, distro), floating=False)
    return ts


def combo_tags(version: str, distro: Distro, default_distro: str) -> TagSet:
    ts = TagSet()
    if distro.id == default_distro:
        ts.add("latest", floating=True)
        ts.add(version, floating=False)
    ts.add(distro.id, floating=True)
    ts.add(f"{version}-{distro.id}", floating=False)
    ts.add(primary_tag("default", version, distro), floating=False)
    return ts
