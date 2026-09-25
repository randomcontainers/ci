"""In-memory GitHub (REST API and raw files), PyPI and download hosts."""

import base64
import hashlib
import json
import re
import urllib.parse
from datetime import UTC, datetime

from rc.github import blob_sha
from rc.http import Response

NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)


def iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _json(status: int, data) -> Response:
    return Response(status, {"content-type": "application/json"}, json.dumps(data).encode())


class FakeGitHub:
    """Repositories with commits, workflow runs and releases, served like api.github.com."""

    def __init__(self, org: str = "randomcontainers"):
        self.org = org
        self.blobs: dict[str, bytes] = {}
        self.trees: dict[str, dict[str, str]] = {}
        self.commits: dict[str, dict] = {}
        self.heads: dict[str, str] = {}
        self.runs: dict[str, list[dict]] = {}
        self.states: dict[tuple[str, str], str] = {}
        self.releases: dict[str, list[dict]] = {}
        self.tags: dict[str, list[tuple[str, str, str, datetime]]] = {}
        self.installation: set[str] = set()
        self.dispatches: list[tuple[str, str, dict]] = []
        self.created: list[dict] = []
        self.topics: dict[str, list[str]] = {}
        # When set, creating repositories and setting topics need this token,
        # like the Administration permission.
        self.admin_token: str | None = None
        self.meta: dict[str, dict] = {}
        self.requests: list[tuple[str, str, dict, bytes | None]] = []
        self.clock = NOW

    # setup helpers

    def _tree(self, files: dict[str, bytes]) -> str:
        entries = {}
        for path, content in files.items():
            sha = blob_sha(content)
            self.blobs[sha] = content
            entries[path] = sha
        tree_id = hashlib.sha1(json.dumps(entries, sort_keys=True).encode()).hexdigest()  # noqa: S324
        self.trees[tree_id] = entries
        return tree_id

    def _commit(self, tree_id: str, parents: list[str], message: str, date: datetime | None = None) -> str:
        body = json.dumps([tree_id, parents, message, len(self.commits)]).encode()
        sha = hashlib.sha1(body).hexdigest()  # noqa: S324
        self.commits[sha] = {"tree": tree_id, "parents": parents, "message": message, "date": date or self.clock}
        return sha

    def add_repo(self, name: str, files: dict[str, bytes], date: datetime | None = None, installed: bool = True) -> str:
        full = f"{self.org}/{name}" if "/" not in name else name
        sha = self._commit(self._tree(files), [], "Initial commit", date)
        self.heads[full] = sha
        self.runs.setdefault(full, [])
        if installed:
            self.installation.add(full)
        return sha

    def push(self, repo: str, files: dict[str, bytes], message: str = "Change", date: datetime | None = None) -> str:
        head = self.heads[repo]
        merged = {**self.files(repo), **files}
        sha = self._commit(self._tree(merged), [head], message, date)
        self.heads[repo] = sha
        return sha

    def files(self, repo: str, sha: str | None = None) -> dict[str, bytes]:
        commit = self.commits[sha or self.heads[repo]]
        return {p: self.blobs[b] for p, b in self.trees[commit["tree"]].items()}

    def add_run(
        self,
        repo: str,
        title: str,
        conclusion: str = "success",
        status: str = "completed",
        when: datetime | None = None,
        event: str = "workflow_dispatch",
        published: tuple[str, ...] = (),
    ):
        """A run of build.yml; `published` names the "<flavour> <distro>" indexes it created."""
        when = when or self.clock
        jobs = [{"name": "build / Plan", "conclusion": "success", "steps": [{"name": "Plan", "conclusion": "success"}]}]
        for target in published:
            steps = [{"name": "Decide tags", "conclusion": "success"}, {"name": "Create index", "conclusion": "success"}]
            jobs.append({"name": f"build / Publish {target}", "conclusion": "success", "steps": steps})
        run = {
            "id": len(self.runs[repo]) + 1,
            "display_title": title,
            "event": event,
            "head_branch": "main",
            "status": status,
            "conclusion": conclusion if status == "completed" else None,
            "created_at": iso(when),
            "updated_at": iso(when),
            "jobs": jobs,
        }
        self.runs[repo].insert(0, run)
        return run

    # transport

    def request(self, method: str, url: str, headers: dict[str, str], body: bytes | None = None) -> Response:
        self.requests.append((method, url, dict(headers), body))
        parsed = urllib.parse.urlparse(url)
        if parsed.hostname == "raw.githubusercontent.com":
            owner, repo, sha, path = parsed.path.lstrip("/").split("/", 3)
            commit = self.commits.get(sha)
            if f"{owner}/{repo}" not in self.heads or commit is None:
                return Response(404)
            blob = self.trees[commit["tree"]].get(path)
            return Response(200, {}, self.blobs[blob]) if blob else Response(404)
        if parsed.hostname != "api.github.com":
            return Response(404)
        data = json.loads(body) if body else None
        query = dict(urllib.parse.parse_qsl(parsed.query))
        admin = (method == "POST" and re.match(r"^/orgs/[^/]+/repos$", parsed.path)) or (
            method == "PUT" and parsed.path.endswith("/topics")
        )
        if admin and self.admin_token and headers.get("Authorization") != f"Bearer {self.admin_token}":
            return _json(403, {"message": "Resource not accessible by integration"})
        return self._api(method, parsed.path, query, data)

    def _api(self, method, path, query, data) -> Response:
        if path == "/installation/repositories":
            return _json(200, {"repositories": [{"full_name": r, "topics": self.topics.get(r, [])} for r in sorted(self.installation)]})
        m = re.match(r"^/orgs/([^/]+)/repos$", path)
        if m and method == "POST":
            full = f"{m.group(1)}/{data['name']}"
            if full in self.heads:
                return _json(422, {"message": "name already exists on this account"})
            self.created.append(data)
            self.add_repo(full, {"README.md": f"# {data['name']}\n".encode()})
            self.meta[full] = {"homepage": data.get("homepage"), "description": data.get("description")}
            return _json(201, {"full_name": full})
        m = re.match(r"^/repos/([^/]+/[^/]+)(/.*)?$", path)
        if not m:
            return Response(404)
        repo, rest = m.group(1), m.group(2) or ""
        if repo in self.releases or repo in self.tags:
            return self._upstream(repo, rest)
        if repo not in self.heads:
            return _json(404, {"message": "Not Found"})
        if rest == "" and method == "GET":
            return _json(200, {"full_name": repo, **self.meta.get(repo, {}), "topics": self.topics.get(repo, [])})
        if rest == "/commits/main":
            sha = self.heads[repo]
            return _json(200, {"sha": sha, "commit": {"committer": {"date": iso(self.commits[sha]["date"])}}})
        if rest == "/commits" and method == "GET":
            return _json(200, self._history(repo, query.get("path", ""))[:1])
        m = re.match(r"^/git/commits/([0-9a-f]{40})$", rest)
        if m and method == "GET":
            c = self.commits.get(m.group(1))
            if c is None:
                return Response(404)
            return _json(200, {"sha": m.group(1), "tree": {"sha": c["tree"]}, "committer": {"date": iso(c["date"])}})
        m = re.match(r"^/git/trees/([0-9a-f]{40})$", rest)
        if m:
            key = m.group(1)
            tree = self.trees.get(self.commits[key]["tree"] if key in self.commits else key)
            if tree is None:
                return Response(404)
            return _json(200, {"tree": [{"path": p, "type": "blob", "sha": s, "mode": "100644"} for p, s in tree.items()], "truncated": False})
        if rest == "/git/blobs" and method == "POST":
            content = base64.b64decode(data["content"])
            sha = blob_sha(content)
            self.blobs[sha] = content
            return _json(201, {"sha": sha})
        if rest == "/git/trees" and method == "POST":
            base = dict(self.trees[data["base_tree"]])
            for entry in data["tree"]:
                base[entry["path"]] = entry["sha"]
            tree_id = hashlib.sha1(json.dumps(base, sort_keys=True).encode()).hexdigest()  # noqa: S324
            self.trees[tree_id] = base
            return _json(201, {"sha": tree_id})
        if rest == "/git/commits" and method == "POST":
            return _json(201, {"sha": self._commit(data["tree"], data["parents"], data["message"])})
        if rest == "/git/refs/heads/main" and method == "PATCH":
            commit = self.commits.get(data["sha"])
            if commit is None or data.get("force") is not False or commit["parents"] != [self.heads[repo]]:
                return _json(422, {"message": "Update is not a fast forward"})
            self.heads[repo] = data["sha"]
            return _json(200, {"object": {"sha": data["sha"]}})
        if rest == "/topics" and method == "PUT":
            self.topics[repo] = data["names"]
            return _json(200, data)
        m = re.match(r"^/actions/runs/([0-9]+)/jobs$", rest)
        if m and method == "GET":
            run = next((r for r in self.runs[repo] if r["id"] == int(m.group(1))), None)
            if run is None:
                return _json(404, {"message": "Not Found"})
            return _json(200, {"total_count": len(run["jobs"]), "jobs": run["jobs"]})
        m = re.match(r"^/actions/workflows/([^/]+)(/runs|/dispatches)?$", rest)
        if m:
            workflow, sub = m.group(1), m.group(2)
            if f".github/workflows/{workflow}" not in self.files(repo):
                return _json(404, {"message": "Not Found"})
            if sub == "/runs":
                return _json(200, {"workflow_runs": self.runs[repo]})
            if sub == "/dispatches" and method == "POST":
                self.dispatches.append((repo, workflow, data["inputs"]))
                return Response(204)
            return _json(200, {"state": self.states.get((repo, workflow), "active")})
        return _json(404, {"message": f"fake: {method} {path}"})

    def _history(self, repo: str, path: str) -> list[dict]:
        out = []
        sha = self.heads[repo]
        while sha:
            c = self.commits[sha]
            parent = c["parents"][0] if c["parents"] else None
            mine = self.trees[c["tree"]].get(path)
            theirs = self.trees[self.commits[parent]["tree"]].get(path) if parent else None
            if mine and mine != theirs:
                out.append({"sha": sha, "commit": {"committer": {"date": iso(c["date"])}}})
            sha = parent
        return out

    def _upstream(self, repo, rest) -> Response:
        if rest == "/releases":
            return _json(200, self.releases.get(repo, []))
        m = re.match(r"^/git/matching-refs/tags/(.*)$", rest)
        if m:
            prefix = urllib.parse.unquote(m.group(1))
            return _json(
                200,
                [
                    {"ref": f"refs/tags/{name}", "object": {"sha": sha, "type": kind}}
                    for name, sha, kind, _ in self.tags.get(repo, [])
                    if name.startswith(prefix)
                ],
            )
        m = re.match(r"^/git/(tags|commits)/([0-9a-f]{40})$", rest)
        if m:
            for _, sha, kind, date in self.tags.get(repo, []):
                if sha == m.group(2):
                    key = "tagger" if m.group(1) == "tags" else "committer"
                    return _json(200, {key: {"date": iso(date)}})
        return Response(404)


class FakeWeb:
    """PyPI's simple and integrity APIs plus static download URLs."""

    def __init__(self):
        self.projects: dict[str, dict] = {}
        self.provenance: dict[tuple[str, str], str | None] = {}
        self.urls: dict[str, bytes] = {}
        self.requests: list[tuple[str, str]] = []

    def add_release(self, project: str, version: str, when: datetime, *, publisher: str | None, wheel: str = "py3-none-any", yanked: bool = False):
        stem = project.replace("-", "_")
        data = self.projects.setdefault(project, {"versions": [], "files": []})
        if version not in data["versions"]:
            data["versions"].append(version)
        for filename in (f"{stem}-{version}-{wheel}.whl", f"{stem}-{version}.tar.gz"):
            data["files"].append(
                {
                    "filename": filename,
                    "upload-time": iso(when),
                    "yanked": yanked,
                    "provenance": f"https://pypi.org/integrity/{project}/{version}/{filename}/provenance" if publisher else None,
                }
            )
            self.provenance[(version, filename)] = publisher

    def request(self, method: str, url: str, headers: dict[str, str], body: bytes | None = None) -> Response:
        self.requests.append((method, url))
        parsed = urllib.parse.urlparse(url)
        if parsed.hostname == "pypi.org":
            m = re.match(r"^/simple/([^/]+)/$", parsed.path)
            if m and m.group(1) in self.projects:
                return _json(200, self.projects[m.group(1)])
            m = re.match(r"^/integrity/([^/]+)/([^/]+)/([^/]+)/provenance$", parsed.path)
            if m:
                publisher = self.provenance.get((urllib.parse.unquote(m.group(2)), urllib.parse.unquote(m.group(3))))
                if publisher is None:
                    return Response(404)
                bundle = {"publisher": {"kind": "GitHub", "repository": publisher, "workflow": "release.yml"}, "attestations": [{}]}
                return _json(200, {"version": 1, "attestation_bundles": [bundle]})
            return Response(404)
        if url in self.urls:
            return Response(200, {}, b"" if method == "HEAD" else self.urls[url])
        return Response(404)


class Router:
    """One transport for every host: registries, GitHub and the web."""

    def __init__(self, registry, github: FakeGitHub, web: FakeWeb):
        self.registry = registry
        self.github = github
        self.web = web

    def request(self, method: str, url: str, headers: dict[str, str], body: bytes | None = None) -> Response:
        host = urllib.parse.urlparse(url).hostname or ""
        if host in ("api.github.com", "raw.githubusercontent.com"):
            return self.github.request(method, url, headers, body)
        if host in ("pypi.org",) or url in self.web.urls:
            return self.web.request(method, url, headers, body)
        return self.registry.request(method, url, headers, body)
