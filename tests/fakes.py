"""In-memory stand-ins for registries and the GitHub API. No test touches the network."""

import hashlib
import json
import urllib.parse

from rc.http import Response
from rc.registry import DOCKER_LIST, OCI_INDEX, OCI_MANIFEST

HOSTS = {"ghcr.io": ("ghcr.io", "https://ghcr.io/token", "ghcr.io"),
         "docker.io": ("registry-1.docker.io", "https://auth.docker.io/token", "registry.docker.io")}
STORAGE = "https://pkg-containers.githubusercontent.com/blob/"


def digest_of(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


class FakeRegistry:
    """Serves manifests and blobs the way ghcr.io and Docker Hub do, including token auth."""

    def __init__(self):
        self.repos: dict[tuple[str, str], dict] = {}
        self.blobs: dict[str, bytes] = {}
        self.private: set[tuple[str, str]] = set()
        self.requests: list[tuple[str, str, dict]] = []
        self.head_sha = "a" * 40
        self.page_size = 100

    def _repo(self, registry: str, repo: str) -> dict:
        return self.repos.setdefault((registry, repo), {"tags": {}, "manifests": {}})

    def _put_manifest(self, registry: str, repo: str, data: dict, media_type: str) -> str:
        body = json.dumps(data, sort_keys=True).encode()
        digest = digest_of(body)
        self._repo(registry, repo)["manifests"][digest] = (body, media_type)
        return digest

    def add_image(
        self,
        registry: str,
        repo: str,
        tag: str,
        labels: dict | None = None,
        annotations: dict | None = None,
        platforms=("linux/amd64", "linux/arm64"),
        index_type: str = OCI_INDEX,
    ) -> str:
        entries = []
        for platform in platforms:
            os_name, arch = platform.split("/")
            config = json.dumps({"architecture": arch, "os": os_name, "config": {"Labels": labels or {}}}).encode()
            config_digest = digest_of(config)
            self.blobs[config_digest] = config
            manifest = {
                "schemaVersion": 2,
                "mediaType": OCI_MANIFEST,
                "config": {"mediaType": "application/vnd.oci.image.config.v1+json", "digest": config_digest, "size": len(config)},
                "layers": [],
            }
            digest = self._put_manifest(registry, repo, manifest, OCI_MANIFEST)
            entries.append({"mediaType": OCI_MANIFEST, "digest": digest, "platform": {"os": os_name, "architecture": arch}})
        entries.append({"mediaType": OCI_MANIFEST, "digest": "sha256:" + "0" * 64,
                        "platform": {"os": "unknown", "architecture": "unknown"}})
        index = {"schemaVersion": 2, "mediaType": index_type, "manifests": entries}
        if annotations:
            index["annotations"] = annotations
        digest = self._put_manifest(registry, repo, index, index_type)
        self._repo(registry, repo)["tags"][tag] = digest
        return digest

    def add_single(self, registry: str, repo: str, tag: str) -> str:
        manifest = {"schemaVersion": 2, "mediaType": OCI_MANIFEST, "config": {"digest": "sha256:" + "1" * 64}, "layers": []}
        digest = self._put_manifest(registry, repo, manifest, OCI_MANIFEST)
        self._repo(registry, repo)["tags"][tag] = digest
        return digest

    def request(self, method: str, url: str, headers: dict[str, str], body: bytes | None = None) -> Response:
        self.requests.append((method, url, dict(headers)))
        parsed = urllib.parse.urlparse(url)
        if url.startswith(STORAGE):
            if "Authorization" in headers:
                return Response(400, {}, b"storage does not accept registry tokens")
            digest = parsed.path.rsplit("/", 1)[1]
            return Response(200, {}, self.blobs[digest]) if digest in self.blobs else Response(404)
        if parsed.hostname == "api.github.com":
            if parsed.path.endswith("/commits/main"):
                return Response(200, {}, self.head_sha.encode())
            return Response(404)
        for registry, (api, realm, service) in HOSTS.items():
            if url.startswith(realm):
                query = dict(urllib.parse.parse_qsl(parsed.query))
                repo = query["scope"].split(":")[1]
                if (registry, repo) in self.private or (registry, repo) not in self.repos:
                    if "Authorization" not in headers:
                        return Response(403 if registry == "ghcr.io" else 401)
                return Response(200, {}, json.dumps({"token": f"t:{repo}"}).encode())
            if parsed.hostname == api:
                return self._serve(registry, api, realm, service, method, parsed.path, headers, parsed.query)
        return Response(404)

    def _serve(self, registry, api, realm, service, method, path, headers, query="") -> Response:
        rest = path[len("/v2/") :]
        if rest.endswith("/tags/list"):
            repo = rest[: -len("/tags/list")]
            if headers.get("Authorization") != f"Bearer t:{repo}":
                challenge = f'Bearer realm="{realm}",service="{service}",scope="repository:{repo}:pull"'
                return Response(401, {"www-authenticate": challenge})
            data = self.repos.get((registry, repo))
            if data is None:
                return Response(404)
            params = dict(urllib.parse.parse_qsl(query))
            tags = sorted(data["tags"])
            if "last" in params:
                tags = [t for t in tags if t > params["last"]]
            n = min(int(params.get("n", "100")), self.page_size)
            page, more = tags[:n], len(tags) > n
            hdrs = {"content-type": "application/json"}
            if more:
                hdrs["link"] = f'</v2/{repo}/tags/list?last={page[-1]}&n={n}>; rel="next"'
            return Response(200, hdrs, json.dumps({"name": repo, "tags": page}).encode())
        if "/manifests/" in rest:
            repo, _, ref = rest.partition("/manifests/")
            kind = "manifest"
        else:
            repo, _, ref = rest.partition("/blobs/")
            kind = "blob"
        if headers.get("Authorization") != f"Bearer t:{repo}":
            challenge = f'Bearer realm="{realm}",service="{service}",scope="repository:{repo}:pull"'
            return Response(401, {"www-authenticate": challenge})
        data = self.repos.get((registry, repo))
        if data is None:
            return Response(404)
        if kind == "blob":
            return Response(307, {"location": STORAGE + ref})
        digest = data["tags"].get(ref, ref)
        if digest not in data["manifests"]:
            return Response(404)
        body, media_type = data["manifests"][digest]
        hdrs = {"docker-content-digest": digest, "content-type": media_type}
        return Response(200, hdrs, b"" if method == "HEAD" else body)


__all__ = ["FakeRegistry", "digest_of", "DOCKER_LIST", "OCI_INDEX"]
