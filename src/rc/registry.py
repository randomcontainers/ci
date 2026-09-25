"""Read-only OCI registry client for ghcr.io and Docker Hub.

It resolves tags to index digests, reads index annotations and image
labels, and probes whether a package can be pulled anonymously. It never
writes to a registry.
"""

import base64
import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass

from rc import http, names
from rc.errors import RcError

OCI_INDEX = "application/vnd.oci.image.index.v1+json"
DOCKER_LIST = "application/vnd.docker.distribution.manifest.list.v2+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"
DOCKER_MANIFEST = "application/vnd.docker.distribution.manifest.v2+json"
INDEX_TYPES = (OCI_INDEX, DOCKER_LIST)
MANIFEST_ACCEPT = ", ".join((OCI_INDEX, DOCKER_LIST, OCI_MANIFEST, DOCKER_MANIFEST))

API_HOSTS = {"docker.io": "registry-1.docker.io", "ghcr.io": "ghcr.io"}
TOKEN_HOSTS = {"docker.io": "auth.docker.io", "ghcr.io": "ghcr.io"}

_REPO = re.compile(r"^[a-z0-9]+(?:[._/-][a-z0-9]+)*$")
_CHALLENGE = re.compile(r'(\w+)="([^"]*)"')
_NEXT = re.compile(r'<([^>]+)>\s*;\s*rel="?next"?')
TAG_PAGES = 50


class RegistryError(RcError):
    pass


class NotFound(RegistryError):
    """The repository is readable but the tag or digest does not exist."""


class Denied(RegistryError):
    """No pull access: the package is private or does not exist yet."""


@dataclass(frozen=True)
class Reference:
    registry: str
    repository: str
    tag: str | None = None
    digest: str | None = None

    @property
    def reference(self) -> str:
        return self.digest or self.tag or "latest"

    def __str__(self) -> str:
        host = "" if self.registry == "docker.io" else f"{self.registry}/"
        repo = self.repository
        if self.registry == "docker.io" and repo.startswith("library/"):
            repo = repo[len("library/") :]
        tag = f":{self.tag}" if self.tag else ""
        digest = f"@{self.digest}" if self.digest else ""
        return f"{host}{repo}{tag}{digest}"

    def with_digest(self, digest: str) -> "Reference":
        return Reference(self.registry, self.repository, self.tag, digest)


def parse_reference(ref: str) -> Reference:
    rest = ref
    digest = None
    if "@" in rest:
        rest, digest = rest.split("@", 1)
        if not names.DIGEST.match(digest):
            raise RcError(f"{ref!r}: bad digest")
    tag = None
    head, _, tail = rest.rpartition(":")
    if head and "/" not in tail:
        rest, tag = head, tail
        if not names.TAG.match(tag):
            raise RcError(f"{ref!r}: bad tag")
    first, _, remainder = rest.partition("/")
    if remainder and ("." in first or ":" in first or first == "localhost"):
        registry, repository = first, remainder
    else:
        registry, repository = "docker.io", rest
    if registry == "docker.io" and "/" not in repository:
        repository = f"library/{repository}"
    if registry not in API_HOSTS:
        raise RcError(f"{ref!r}: only {', '.join(API_HOSTS)} are supported")
    if not _REPO.match(repository):
        raise RcError(f"{ref!r}: bad repository name")
    return Reference(registry, repository, tag, digest)


@dataclass(frozen=True)
class Manifest:
    digest: str
    media_type: str
    data: dict

    @property
    def is_index(self) -> bool:
        return self.media_type in INDEX_TYPES


@dataclass(frozen=True)
class Probe:
    """Result of an anonymous pull check of one tag."""

    status: str  # ready | unbuilt | unavailable
    digest: str | None = None

    @property
    def ready(self) -> bool:
        return self.status == "ready"


class Registry:
    def __init__(self, transport: http.Transport, credentials: dict[str, tuple[str, str]] | None = None):
        self.transport = transport
        self.credentials = credentials or {}
        self._tokens: dict[tuple[str, str], str] = {}

    def _url(self, ref: Reference, path: str) -> str:
        return f"https://{API_HOSTS[ref.registry]}/v2/{ref.repository}/{path}"

    def _token(self, ref: Reference, challenge: str) -> str:
        params = dict(_CHALLENGE.findall(challenge))
        realm = params.get("realm", "")
        parsed = urllib.parse.urlparse(realm)
        if parsed.scheme != "https" or parsed.hostname != TOKEN_HOSTS[ref.registry]:
            raise RegistryError(f"{ref.registry}: unexpected token realm {realm!r}")
        query = {"scope": f"repository:{ref.repository}:pull"}
        if params.get("service"):
            query["service"] = params["service"]
        headers = {}
        if ref.registry in self.credentials:
            user, password = self.credentials[ref.registry]
            raw = base64.b64encode(f"{user}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {raw}"
        resp = self._send("GET", f"{realm}?{urllib.parse.urlencode(query)}", headers)
        if resp.status in (401, 403):
            raise Denied(f"no pull access to {ref.registry}/{ref.repository}")
        if resp.status != 200:
            raise RegistryError(f"{ref.registry} token endpoint returned HTTP {resp.status}")
        try:
            payload = json.loads(resp.body)
            token = payload.get("token") or payload.get("access_token")
        except (ValueError, AttributeError):
            token = None
        if not isinstance(token, str) or not token:
            raise RegistryError(f"{ref.registry} token endpoint returned no token")
        return token

    def _send(self, method: str, url: str, headers: dict[str, str], follow: bool = False) -> http.Response:
        """One request; a timeout or connection error is a RegistryError like any other failed read."""
        try:
            if follow:
                return http.follow(self.transport, method, url, headers)
            return self.transport.request(method, url, headers)
        except RegistryError:
            raise
        except RcError as exc:
            raise RegistryError(str(exc)) from None

    def _request(self, method: str, ref: Reference, path: str, accept: str | None = None) -> http.Response:
        key = (ref.registry, ref.repository)
        headers = {"Accept": accept} if accept else {}
        for attempt in range(2):
            sent = dict(headers)
            if key in self._tokens:
                sent["Authorization"] = f"Bearer {self._tokens[key]}"
            resp = self._send(method, self._url(ref, path), sent, follow=path.startswith("blobs/"))
            if resp.status == 401 and attempt == 0:
                challenge = resp.header("www-authenticate") or ""
                if not challenge.lower().startswith("bearer "):
                    raise Denied(f"{ref}: registry asked for unsupported authentication")
                self._tokens[key] = self._token(ref, challenge)
                continue
            if resp.status in (401, 403):
                raise Denied(f"no pull access to {ref.registry}/{ref.repository}")
            if resp.status == 404:
                raise NotFound(f"{ref} not found")
            if resp.status != 200:
                raise RegistryError(f"{method} {self._url(ref, path)}: HTTP {resp.status}")
            return resp
        raise Denied(f"no pull access to {ref.registry}/{ref.repository}")

    def head(self, ref: Reference) -> tuple[str, str]:
        """Digest and media type of a manifest, without downloading it."""
        resp = self._request("HEAD", ref, f"manifests/{ref.reference}", MANIFEST_ACCEPT)
        digest = resp.header("docker-content-digest") or ""
        media_type = (resp.header("content-type") or "").split(";")[0].strip()
        if not names.DIGEST.match(digest) or not media_type:
            manifest = self.manifest(ref)
            return manifest.digest, manifest.media_type
        return digest, media_type

    def manifest(self, ref: Reference) -> Manifest:
        resp = self._request("GET", ref, f"manifests/{ref.reference}", MANIFEST_ACCEPT)
        digest = "sha256:" + hashlib.sha256(resp.body).hexdigest()
        expected = ref.digest or resp.header("docker-content-digest")
        if expected and expected != digest:
            raise RegistryError(f"{ref}: manifest digest mismatch ({expected} != {digest})")
        try:
            data = json.loads(resp.body)
        except ValueError:
            raise RegistryError(f"{ref}: manifest is not JSON") from None
        media_type = data.get("mediaType") or (resp.header("content-type") or "").split(";")[0].strip()
        return Manifest(digest, media_type, data)

    def index_digest(self, ref: Reference) -> str:
        """Digest of the multi-platform index behind a tag."""
        digest, media_type = self.head(ref)
        if media_type not in INDEX_TYPES:
            raise RegistryError(f"{ref} is a single-platform image ({media_type}); a multi-platform index is required")
        return digest

    def blob(self, ref: Reference, digest: str, limit: int = 1024 * 1024) -> bytes:
        if not names.DIGEST.match(digest):
            raise RegistryError(f"{ref}: bad blob digest {digest!r}")
        body = self._request("GET", ref, f"blobs/{digest}").body
        if len(body) > limit:
            raise RegistryError(f"{ref}: blob {digest} is larger than {limit} bytes")
        if "sha256:" + hashlib.sha256(body).hexdigest() != digest:
            raise RegistryError(f"{ref}: blob {digest} does not match its digest")
        return body

    def platform_manifest(self, ref: Reference, platform: str = "linux/amd64") -> Manifest:
        top = self.manifest(ref)
        if not top.is_index:
            return top
        os_name, _, arch = platform.partition("/")
        for entry in top.data.get("manifests", []):
            plat = entry.get("platform") or {}
            if plat.get("os") == os_name and plat.get("architecture") == arch:
                return self.manifest(ref.with_digest(entry["digest"]))
        raise NotFound(f"{ref} has no {platform} image")

    def annotations(self, ref: Reference) -> dict[str, str]:
        top = self.manifest(ref)
        return dict(top.data.get("annotations") or {})

    def config_labels(self, ref: Reference, platform: str = "linux/amd64") -> dict[str, str]:
        manifest = self.platform_manifest(ref, platform)
        config_digest = (manifest.data.get("config") or {}).get("digest", "")
        config = json.loads(self.blob(ref, config_digest))
        return dict((config.get("config") or {}).get("Labels") or {})

    def tags(self, ref: Reference) -> list[str]:
        """Every tag of a repository, following the registry's pagination."""
        out: list[str] = []
        path = "tags/list?n=1000"
        prefix = f"/v2/{ref.repository}/tags/list"
        for _ in range(TAG_PAGES):
            resp = self._request("GET", ref, path)
            try:
                payload = json.loads(resp.body)
                tags = payload.get("tags") or []
            except (ValueError, AttributeError):
                raise RegistryError(f"{ref}: tags/list is not JSON") from None
            out.extend(t for t in tags if isinstance(t, str) and names.TAG.match(t))
            match = _NEXT.search(resp.header("link") or "")
            if not match:
                return out
            parsed = urllib.parse.urlparse(match.group(1))
            if parsed.netloc not in ("", API_HOSTS[ref.registry]) or parsed.path != prefix:
                raise RegistryError(f"{ref}: unexpected pagination link {match.group(1)!r}")
            path = f"tags/list?{parsed.query}"
        raise RegistryError(f"{ref}: more than {TAG_PAGES} pages of tags")

    def platform_sizes(self, ref: Reference) -> dict[str, int]:
        """Compressed size of each platform image of an index: config plus layers."""
        top = self.manifest(ref)
        sizes: dict[str, int] = {}
        for entry in top.data.get("manifests", []) if top.is_index else []:
            plat = entry.get("platform") or {}
            if plat.get("os") in (None, "unknown"):
                continue
            key = f"{plat.get('os')}/{plat.get('architecture')}"
            if plat.get("variant"):
                key += f"/{plat['variant']}"
            manifest = self.manifest(ref.with_digest(entry["digest"]))
            parts = [manifest.data.get("config") or {}, *(manifest.data.get("layers") or [])]
            sizes[key] = sum(int(p.get("size") or 0) for p in parts)
        return sizes

    def probe(self, ref: Reference) -> Probe:
        """Anonymous pull check: ready, unbuilt (repo readable, tag missing) or unavailable."""
        try:
            digest, _ = self.head(ref)
        except NotFound:
            return Probe("unbuilt")
        except Denied:
            return Probe("unavailable")
        return Probe("ready", digest)
