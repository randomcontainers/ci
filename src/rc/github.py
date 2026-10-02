"""The parts of the GitHub REST API the reconciler uses.

Reads work without a token for public repositories (60 requests an hour).
Files are read from raw.githubusercontent.com at a commit id, which is
immutable, so its cache never returns stale content and it does not count
against the API rate limit. Commits are made with the Git Data API as one
commit per change, and a branch is only ever moved forward.
"""

import base64
import hashlib
import json
import re
import urllib.parse
from dataclasses import dataclass
from datetime import datetime

from rc import http, names
from rc.errors import RcError

API = "https://api.github.com"
RAW = "https://raw.githubusercontent.com"
API_VERSION = "2022-11-28"
PAGES = 10

_NEXT = re.compile(r'<(https://api\.github\.com/[^>]+)>;\s*rel="next"')
_PATH = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_WORKFLOW = re.compile(r"^[A-Za-z0-9._-]+\.ya?ml$")


class GitHubError(RcError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Commit:
    sha: str
    date: datetime | None


@dataclass(frozen=True)
class Run:
    id: int
    title: str
    event: str
    branch: str
    status: str
    conclusion: str
    created: datetime | None
    updated: datetime | None

    @property
    def active(self) -> bool:
        return self.status not in ("completed", "")


@dataclass(frozen=True)
class Job:
    name: str
    conclusion: str
    steps: dict[str, str]  # step name to conclusion


def parse_time(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def check_repo(repo: str) -> str:
    if not isinstance(repo, str) or not names.GITHUB_REPO.match(repo):
        raise RcError(f"bad repository name {repo!r}")
    return repo


def safe_path(path: str) -> bool:
    return bool(_PATH.match(path)) and not any(part in (".", "..") for part in path.split("/"))


def blob_sha(content: bytes) -> str:
    """The git object id of a file, as the tree API reports it."""
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()  # noqa: S324 - git object ids are sha1


class GitHub:
    def __init__(self, transport: http.Transport, token: str | None = None):
        self.transport = transport
        self.token = token
        self.requests = 0

    # plumbing

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        headers = {"Accept": accept, "X-GitHub-Api-Version": API_VERSION}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def call(self, method: str, path: str, body: dict | None = None, ok=(200,)) -> http.Response:
        url = path if path.startswith(API + "/") else API + path
        headers = self._headers()
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        self.requests += 1
        resp = self.transport.request(method, url, headers, data)
        if resp.status not in ok:
            message = ""
            try:
                message = str(json.loads(resp.body).get("message", ""))[:200]
            except (ValueError, AttributeError):
                pass
            short = url[len(API) :] if url.startswith(API) else url
            raise GitHubError(f"{method} {short}: HTTP {resp.status}{': ' + message if message else ''}", resp.status)
        return resp

    def get(self, path: str, missing: tuple[int, ...] = ()):
        """JSON of a GET, or None when the status is one of `missing`."""
        try:
            resp = self.call("GET", path)
        except GitHubError as exc:
            if exc.status in missing:
                return None
            raise
        try:
            return json.loads(resp.body)
        except ValueError:
            raise GitHubError(f"GET {path}: response is not JSON") from None

    def pages(self, path: str, key: str | None = None, limit: int = PAGES) -> list:
        out: list = []
        url = path
        for _ in range(limit):
            resp = self.call("GET", url)
            try:
                payload = json.loads(resp.body)
            except ValueError:
                raise GitHubError(f"GET {path}: response is not JSON") from None
            items = payload.get(key, []) if key else payload
            if not isinstance(items, list):
                raise GitHubError(f"GET {path}: unexpected response")
            out.extend(items)
            match = _NEXT.search(resp.header("link") or "")
            if not match:
                break
            url = match.group(1)
        return out

    # reads

    def repository(self, repo: str) -> dict | None:
        return self.get(f"/repos/{check_repo(repo)}", missing=(404,))

    def head(self, repo: str, branch: str = "main") -> Commit | None:
        """Head commit of a branch, or None when the repository or branch does not exist."""
        data = self.get(f"/repos/{check_repo(repo)}/commits/{branch}", missing=(404, 409, 422))
        if data is None:
            return None
        sha = data.get("sha", "")
        if not names.GIT_SHA.match(sha):
            raise GitHubError(f"{repo}: unexpected commit id {sha[:80]!r}")
        date = parse_time(((data.get("commit") or {}).get("committer") or {}).get("date"))
        return Commit(sha, date)

    def raw(self, repo: str, sha: str, path: str) -> bytes | None:
        """A file at a commit, or None when it does not exist."""
        if not names.GIT_SHA.match(sha) or not safe_path(path):
            raise RcError(f"refusing to read {path!r} at {sha!r}")
        resp = http.follow(self.transport, "GET", f"{RAW}/{check_repo(repo)}/{sha}/{path}")
        if resp.status == 404:
            return None
        if resp.status != 200:
            raise GitHubError(f"{repo}/{path} at {sha[:12]}: HTTP {resp.status}", resp.status)
        return resp.body

    def tree(self, repo: str, sha: str) -> dict[str, str]:
        """Path to blob id of every file at a commit."""
        if not names.GIT_SHA.match(sha):
            raise RcError(f"bad commit id {sha!r}")
        data = self.get(f"/repos/{check_repo(repo)}/git/trees/{sha}?recursive=1")
        if data.get("truncated"):
            raise GitHubError(f"{repo}: the file tree is too large to compare")
        return {e["path"]: e["sha"] for e in data.get("tree", []) if e.get("type") == "blob"}

    def last_change(self, repo: str, path: str, branch: str = "main") -> datetime | None:
        """Date of the newest commit on a branch that touched a file."""
        query = urllib.parse.urlencode({"path": path, "sha": branch, "per_page": 1})
        data = self.get(f"/repos/{check_repo(repo)}/commits?{query}", missing=(404, 409))
        if not data:
            return None
        return parse_time(((data[0].get("commit") or {}).get("committer") or {}).get("date"))

    def runs(self, repo: str, workflow: str) -> list[Run] | None:
        """Recent runs of a workflow, newest first, or None when it does not exist."""
        if not _WORKFLOW.match(workflow):
            raise RcError(f"bad workflow file name {workflow!r}")
        data = self.get(f"/repos/{check_repo(repo)}/actions/workflows/{workflow}/runs?per_page=100", missing=(404,))
        if data is None:
            return None
        out = []
        for r in data.get("workflow_runs", []):
            out.append(
                Run(
                    id=int(r.get("id") or 0),
                    title=str(r.get("display_title") or ""),
                    event=str(r.get("event") or ""),
                    branch=str(r.get("head_branch") or ""),
                    status=str(r.get("status") or ""),
                    conclusion=str(r.get("conclusion") or ""),
                    created=parse_time(r.get("created_at")),
                    updated=parse_time(r.get("updated_at")),
                )
            )
        return out

    def jobs(self, repo: str, run_id: int) -> list[Job]:
        """Jobs of the latest attempt of a workflow run."""
        if not isinstance(run_id, int) or run_id <= 0:
            raise RcError(f"bad run id {run_id!r}")
        data = self.get(f"/repos/{check_repo(repo)}/actions/runs/{run_id}/jobs?filter=latest&per_page=100")
        out = []
        for j in data.get("jobs", []) if isinstance(data, dict) else []:
            steps = {
                str(s.get("name") or ""): str(s.get("conclusion") or "") for s in j.get("steps") or [] if isinstance(s, dict)
            }
            out.append(Job(str(j.get("name") or ""), str(j.get("conclusion") or ""), steps))
        return out

    def workflow_state(self, repo: str, workflow: str) -> str | None:
        if not _WORKFLOW.match(workflow):
            raise RcError(f"bad workflow file name {workflow!r}")
        data = self.get(f"/repos/{check_repo(repo)}/actions/workflows/{workflow}", missing=(404,))
        return None if data is None else str(data.get("state", ""))

    def installation_repositories(self) -> dict[str, list[str] | None]:
        """Full name of every repository of the App installation, with its topics (None when not listed)."""
        items = self.pages("/installation/repositories?per_page=100", key="repositories")
        out: dict[str, list[str] | None] = {}
        for r in items:
            if isinstance(r, dict) and isinstance(r.get("full_name"), str):
                topics = r.get("topics")
                out[r["full_name"]] = [t for t in topics if isinstance(t, str)] if isinstance(topics, list) else None
        return out

    def releases(self, repo: str) -> list[dict]:
        """The newest 100 releases; drafts are only visible with push access and are skipped."""
        data = self.get(f"/repos/{check_repo(repo)}/releases?per_page=100")
        return [r for r in data if isinstance(r, dict) and not r.get("draft")]

    def tags(self, repo: str, prefix: str = "") -> list[tuple[str, str, str]]:
        """(name, object id, object type) of every tag starting with prefix."""
        if prefix and not re.match(r"^[A-Za-z0-9._-]+$", prefix):
            prefix = ""
        data = self.get(f"/repos/{check_repo(repo)}/git/matching-refs/tags/{urllib.parse.quote(prefix)}")
        out = []
        for ref in data if isinstance(data, list) else []:
            name = str(ref.get("ref", ""))
            obj = ref.get("object") or {}
            if name.startswith("refs/tags/") and names.GIT_SHA.match(str(obj.get("sha", ""))):
                out.append((name[len("refs/tags/") :], obj["sha"], str(obj.get("type", ""))))
        return out

    def tag_date(self, repo: str, sha: str, kind: str) -> datetime | None:
        """When a tag was made: the tagger date of an annotated tag, else the commit date."""
        if not names.GIT_SHA.match(sha):
            raise RcError(f"bad object id {sha!r}")
        if kind == "tag":
            data = self.get(f"/repos/{check_repo(repo)}/git/tags/{sha}")
            return parse_time((data.get("tagger") or {}).get("date"))
        data = self.get(f"/repos/{check_repo(repo)}/git/commits/{sha}")
        return parse_time((data.get("committer") or {}).get("date"))

    # writes

    def dispatch(self, repo: str, workflow: str, inputs: dict[str, str], ref: str = "main") -> None:
        if not _WORKFLOW.match(workflow):
            raise RcError(f"bad workflow file name {workflow!r}")
        self.call(
            "POST",
            f"/repos/{check_repo(repo)}/actions/workflows/{workflow}/dispatches",
            {"ref": ref, "inputs": inputs},
            ok=(200, 204),
        )

    def create_repository(self, org: str, name: str, description: str) -> None:
        """A public repository with a README on main and no homepage."""
        if not names.NAME.match(name) or not names.NAME.match(org):
            raise RcError(f"bad repository name {org}/{name}")
        self.call(
            "POST",
            f"/orgs/{org}/repos",
            {
                "name": name,
                "description": description,
                "visibility": "public",
                "auto_init": True,
                "has_issues": False,
                "has_projects": False,
                "has_wiki": False,
            },
            ok=(201,),
        )

    def set_topics(self, repo: str, topics: list[str]) -> None:
        self.call("PUT", f"/repos/{check_repo(repo)}/topics", {"names": [t for t in topics if names.TOPIC.match(t)]}, ok=(200,))

    def commit(self, repo: str, parent: str, files: dict[str, bytes], message: str, branch: str = "main") -> str:
        """Write files as one commit on top of parent and move the branch to it (never forced)."""
        check_repo(repo)
        if not names.GIT_SHA.match(parent):
            raise RcError(f"bad parent commit {parent!r}")
        base = self.get(f"/repos/{repo}/git/commits/{parent}")
        entries = []
        for path, content in sorted(files.items()):
            if not safe_path(path):
                raise RcError(f"refusing to write {path!r}")
            blob = json.loads(
                self.call(
                    "POST",
                    f"/repos/{repo}/git/blobs",
                    {"content": base64.b64encode(content).decode(), "encoding": "base64"},
                    ok=(201,),
                ).body
            )
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        tree = json.loads(
            self.call(
                "POST",
                f"/repos/{repo}/git/trees",
                {"base_tree": (base.get("tree") or {}).get("sha"), "tree": entries},
                ok=(201,),
            ).body
        )
        commit = json.loads(
            self.call(
                "POST",
                f"/repos/{repo}/git/commits",
                {"message": message, "tree": tree["sha"], "parents": [parent]},
                ok=(201,),
            ).body
        )
        sha = commit.get("sha", "")
        if not names.GIT_SHA.match(sha):
            raise GitHubError(f"{repo}: unexpected commit id {sha[:80]!r}")
        self.call("PATCH", f"/repos/{repo}/git/refs/heads/{branch}", {"sha": sha, "force": False}, ok=(200,))
        return sha
