"""What is published on ghcr.io, read anonymously the way a user would pull.

The merge job writes every image label as an index annotation, so the
published state of a tag is the annotations of its index: version,
revision, base digest, members and creation time.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime

from rc import names, versions
from rc.config import Distro
from rc.constants import REGISTRY
from rc.errors import RcError
from rc.github import parse_time
from rc.registry import Denied, NotFound, Registry, RegistryError, parse_reference

ANN = "org.opencontainers.image."
RC = "com.randomcontainers."


@dataclass(frozen=True)
class Published:
    tag: str
    digest: str
    annotations: dict

    def get(self, key: str) -> str:
        value = self.annotations.get(key, "")
        return value if isinstance(value, str) else ""

    @property
    def version(self) -> str:
        return self.get(ANN + "version")

    @property
    def revision(self) -> str:
        return self.get(ANN + "revision")

    @property
    def base_digest(self) -> str:
        return self.get(ANN + "base.digest")

    @property
    def distro_version(self) -> str:
        return self.get(RC + "distro-version")

    @property
    def created(self) -> datetime | None:
        return parse_time(self.get(ANN + "created"))

    @property
    def members(self) -> dict:
        try:
            value = json.loads(self.get(RC + "members") or "{}")
        except ValueError:
            return {}
        return value if isinstance(value, dict) else {}


@dataclass
class Image:
    name: str
    readable: bool
    tags: set[str] = field(default_factory=set)
    published: dict[str, Published] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class Member:
    name: str
    ready: bool
    reason: str = ""
    digest: str = ""
    version: str = ""


class RegistryState:
    """Anonymous reads of ghcr.io and Docker Hub, each made at most once per pass."""

    def __init__(self, registry: Registry, org: str):
        self.registry = registry
        self.org = org
        self._images: dict[str, Image] = {}
        self._bases: dict[str, str] = {}
        self._sizes: dict[tuple[str, str], dict[str, int]] = {}

    def ref(self, name: str, tag: str | None = None, digest: str | None = None):
        suffix = f":{tag}" if tag else ""
        suffix += f"@{digest}" if digest else ""
        return parse_reference(f"{REGISTRY}/{self.org}/{name}{suffix}")

    def base_digest(self, distro: Distro) -> str:
        if distro.id not in self._bases:
            self._bases[distro.id] = self.registry.index_digest(parse_reference(distro.image))
        return self._bases[distro.id]

    def image(self, name: str) -> Image:
        """Readable is False when the package is private or does not exist (ghcr.io answers both alike)."""
        if name not in self._images:
            try:
                tags = self.registry.tags(self.ref(name))
                image = Image(name, True, set(tags))
            except Denied:
                image = Image(name, False)
            except NotFound:
                image = Image(name, True)
            except RegistryError as exc:
                image = Image(name, False, errors=[str(exc)])
            self._images[name] = image
        return self._images[name]

    def tag(self, name: str, tag: str) -> Published | None:
        image = self.image(name)
        if not image.readable or tag not in image.tags:
            return None
        if tag not in image.published:
            try:
                manifest = self.registry.manifest(self.ref(name, tag))
            except NotFound:
                return None
            except RegistryError as exc:
                image.errors.append(f"{name}:{tag}: {exc}")
                return None
            if not manifest.is_index:
                image.errors.append(f"{name}:{tag} is not a multi-platform index")
                return None
            annotations = manifest.data.get("annotations") or {}
            image.published[tag] = Published(tag, manifest.digest, annotations if isinstance(annotations, dict) else {})
        return image.published[tag]

    def failed(self) -> dict[str, list[str]]:
        """Read errors by image name; the catalog keeps the previous entry of these images."""
        return {name: list(image.errors) for name, image in self._images.items() if image.errors}

    def sizes(self, name: str, digest: str) -> dict[str, int]:
        key = (name, digest)
        if key not in self._sizes:
            try:
                self._sizes[key] = self.registry.platform_sizes(self.ref(name, digest=digest))
            except RegistryError as exc:
                self.image(name).errors.append(f"{name}@{digest}: {exc}")
                self._sizes[key] = {}
        return self._sizes[key]

    def member(self, name: str, distro: Distro, versioning: str) -> Member:
        """A member's slim image on one distro, checked the same way `rc plan` checks it."""
        image = self.image(name)
        tag = f"slim-{distro.id}"
        if not image.readable:
            return Member(name, False, "unreadable" if image.errors else "private")
        published = self.tag(name, tag)
        if published is None:
            return Member(name, False, "missing")
        version = published.version
        try:
            versions.validate(versioning, version)
        except versions.VersionError:
            return Member(name, False, f"has no valid version annotation on {tag}")
        if published.distro_version != distro.version:
            return Member(name, False, f"{tag} is built on {published.distro_version or 'an unknown release'}")
        if not names.DIGEST.match(published.digest):
            raise RcError(f"{name}:{tag}: bad digest")
        return Member(name, True, digest=published.digest, version=version)
