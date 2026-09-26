"""The newest upstream release a package may move to (docs/architecture.md, "2. Upstream").

A release is taken when all of these hold:

- its tag (or PyPI version) matches `tag-pattern` and the version built from
  it passes the versioning scheme, so nothing unexpected reaches a tag, a
  path or a commit message;
- it is older than `cooldown`;
- its files are published: on PyPI not yanked, with wheels for glibc and
  musl on amd64 and arm64 or a pure-Python wheel, and every file carries a
  PEP 740 provenance from the `publisher` repository; for tarball builds the
  artifact (and signature) answer with HTTP 200, and so do the extra
  artifacts that follow the package version.

Besides PyPI and GitHub, versions can come from GitLab releases, Forgejo
tags (Codeberg) and a regular expression over an index page (html-index).

A package built from a git tag (`upstream.git`) also needs the tag at its
git URL. The tag is resolved there to its commit, the same way `git clone`
sees it, and the commit is pinned in package.yml together with the version.

When a newer release fails a check, older releases that are still newer
than the current version are tried, and the reason is reported.
"""

import json
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone

from rc import http, names, versions
from rc.config import TAG_SOURCES, Artifact, Package, Upstream, expand_url
from rc.errors import RcError, http_error
from rc.github import GitHub, parse_time

PYPI = "https://pypi.org"
SIMPLE_JSON = "application/vnd.pypi.simple.v1+json"
INTEGRITY_JSON = "application/vnd.pypi.integrity.v1+json"
MAX_CANDIDATES = 5
# A version still waiting this long after its cooldown is reported: its URL or
# tag template is most likely wrong.
STALLED = timedelta(days=7)
MAX_TAG = 128
MAX_INDEX = 4 * 1024 * 1024
MAX_LINE = 16 * 1024
FORGEJO_LIMIT = 50
FORGEJO_PAGES = 10
GIT_ADVERTISEMENT = "application/x-git-upload-pack-advertisement"

_CALVER_PYPI = re.compile(r"^([0-9]{4})\.([0-9]{1,2})\.([0-9]{1,2})$")
_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,254}$")
_PLATFORMS = {("manylinux", "x86_64"), ("manylinux", "aarch64"), ("musllinux", "x86_64"), ("musllinux", "aarch64")}
_CHECKSUM_LINE = re.compile(r"^([A-Fa-f0-9]{64,128})\s+\*?(\S+)$")
# BSD `SHA256 (file) = hex` and OpenSSL `SHA2-256(file)= hex`
_CHECKSUM_TAGGED = (
    re.compile(r"^([A-Z0-9-]+) \((\S+)\) = ([A-Fa-f0-9]{64,128})$"),
    re.compile(r"^([A-Z0-9-]+)\((\S+)\)= ?([A-Fa-f0-9]{64,128})$"),
)
_CHECKSUM_LABELS = {"SHA256": "sha256", "SHA-256": "sha256", "SHA2-256": "sha256", "SHA512": "sha512", "SHA-512": "sha512", "SHA2-512": "sha512"}
_HEX_LENGTH = {"sha256": 64, "sha512": 128}
_RELEASE_ASSET = re.compile(r"^https://github\.com/([^/]+/[^/]+)/releases/download/([^/]+)/([^/]+)$")


@dataclass
class Candidate:
    version: str
    tag: str
    published: datetime | None = None
    object: tuple[str, str] | None = None
    release: dict | None = None
    files: list[dict] = field(default_factory=list)
    commit: str | None = None


@dataclass
class Result:
    package: str
    current: str
    newest: str | None = None
    chosen: Candidate | None = None
    refused: list[str] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)
    stalled: list[str] = field(default_factory=list)


_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_MONTH_NAMES = ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december")
_CLOCK = r"(?P<H>\d{2}):(?P<M>\d{2})(?::(?P<S>\d{2})(?:\.\d{1,9})?)?"
_ZONE = r"(?:\s?(?P<tz>Z|UTC|GMT|[+-]\d{2}:?\d{2}))?"
_WEEKDAY = r"(?:[A-Za-z]{3,9},? )?"
_DATES = tuple(
    re.compile(p, re.ASCII)
    for p in (
        rf"(?P<y>\d{{4}})-(?P<m>\d{{2}})-(?P<d>\d{{2}})(?:[T ]{_CLOCK}{_ZONE})?",  # 2026-08-05 14:27, ISO 8601
        rf"(?P<d>\d{{1,2}})-(?P<b>[A-Za-z]{{3}})-(?P<y>\d{{4}})(?: {_CLOCK}{_ZONE})?",  # 05-Aug-2026 14:27
        rf"{_WEEKDAY}(?P<b>[A-Za-z]{{3,9}})\.? (?P<d>\d{{1,2}}),? (?P<y>\d{{4}})",  # Thu Sep 3, 2026
        rf"{_WEEKDAY}(?P<d>\d{{1,2}}) (?P<b>[A-Za-z]{{3,9}})\.? (?P<y>\d{{4}})(?: {_CLOCK}{_ZONE})?",  # Fri, 04 Sep 2026 14:42:08 GMT
    )
)
# A date without a zone is read as the latest moment it can stand for: in
# UTC-12, and at the end of the day when it has no time.
_LATEST_ZONE = timedelta(hours=12)


def release_date(text: str | None) -> datetime | None:
    """A date from an index page or a Last-Modified header, or None when it is not one of the known formats."""
    if not isinstance(text, str) or len(text) > 64:
        return None
    text = " ".join(text.split())
    m = next((m for m in (p.fullmatch(text) for p in _DATES) if m), None)
    if m is None:
        return None
    parts = m.groupdict()
    month = parts.get("m")
    if month is None:
        name = parts["b"].lower()
        if name[:3] not in _MONTHS or (len(name) > 3 and name not in _MONTH_NAMES and name != "sept"):
            return None
        month = _MONTHS.index(name[:3]) + 1
    zone = parts.get("tz")
    try:
        value = datetime(
            int(parts["y"]), int(month), int(parts["d"]), int(parts.get("H") or 0), int(parts.get("M") or 0), int(parts.get("S") or 0)
        )
        if zone in ("Z", "UTC", "GMT"):
            return value.replace(tzinfo=UTC)
        if zone:
            digits = zone[1:].replace(":", "")
            offset = timedelta(hours=int(digits[:2]), minutes=int(digits[2:]))
            if offset > timedelta(hours=14) or int(digits[2:]) >= 60:
                return None
            return value.replace(tzinfo=timezone(offset if zone[0] == "+" else -offset)).astimezone(UTC)
        if parts.get("H") is None:
            value += timedelta(days=1)
        return (value + _LATEST_ZONE).replace(tzinfo=UTC)
    except (ValueError, OverflowError):
        return None


def api_time(value) -> datetime | None:
    """A timestamp from an upstream API; one without a time zone is not trusted."""
    parsed = parse_time(value)
    return parsed if parsed is not None and parsed.tzinfo is not None else None


def duration(text: str) -> timedelta:
    if not names.DURATION.match(text):
        raise RcError(f"bad duration {text!r}")
    unit = {"m": "minutes", "h": "hours", "d": "days"}[text[-1]]
    return timedelta(**{unit: int(text[:-1])})


def pypi_name(project: str) -> str:
    return re.sub(r"[-_.]+", "-", project).lower()


def match_version(upstream: Upstream, raw: str, *, pypi: bool = False) -> str | None:
    """The version a tag or PyPI version stands for, or None when it does not qualify."""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_TAG or not names.is_printable_ascii(raw, MAX_TAG):
        return None
    text = raw
    if pypi and upstream.versioning == "calver":
        # PyPI normalizes 2026.08.19 to 2026.8.19; tags and our versions keep the zeros.
        m = _CALVER_PYPI.match(raw)
        if m:
            text = f"{m.group(1)}.{int(m.group(2)):02d}.{int(m.group(3)):02d}"
    if upstream.tag_pattern:
        m = re.fullmatch(upstream.tag_pattern, text, flags=re.ASCII)
        if not m:
            return None
        template = upstream.version_template or ("{1}" if m.re.groups else "{0}")
        version = re.sub(r"\{(\d+)\}", lambda g: m.group(int(g.group(1))) or "", template)
    else:
        version = text
    try:
        versions.validate(upstream.versioning, version)
    except versions.VersionError:
        return None
    return version


def literal_prefix(pattern: str | None) -> str:
    """Leading characters every matching tag starts with, to narrow the tag listing."""
    if not pattern or not pattern.startswith("^") or "|" in pattern:
        return ""
    out, i = "", 1
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and i + 1 < len(pattern) and pattern[i + 1] in ".-_":
            ch, step = pattern[i + 1], 2
        elif c.isascii() and (c.isalnum() or c in "-_"):
            ch, step = c, 1
        else:
            break
        if i + step < len(pattern) and pattern[i + step] in "?*{+":
            break
        out += ch
        i += step
    return out


def parse_advertisement(body: bytes) -> dict[str, str]:
    """Ref name to object id from a git smart HTTP ref advertisement (the reply to info/refs)."""
    refs: dict[str, str] = {}
    pos = 0
    while pos < len(body):
        head = body[pos : pos + 4]
        if len(head) < 4 or not re.fullmatch(rb"[0-9a-fA-F]{4}", head):
            raise RcError("not a git ref advertisement")
        length = int(head, 16)
        if length == 0:
            pos += 4
            continue
        if length < 4 or pos + length > len(body):
            raise RcError("truncated git ref advertisement")
        line = body[pos + 4 : pos + length].rstrip(b"\n").split(b"\0", 1)[0]
        pos += length
        if line.startswith(b"ERR "):
            raise RcError(f"the server answered: {line[4:200].decode('ascii', 'replace')}")
        if line.startswith(b"#"):
            continue
        sha, _, ref = line.decode("ascii", "replace").partition(" ")
        if names.GIT_SHA.match(sha) and ref.startswith("refs/"):
            refs[ref] = sha
    return refs


def tag_commit(web: http.Transport, url: str, tag: str, refs: dict[str, str] | None = None) -> str | None:
    """The commit a tag points to at a git URL, or None when the repository has no such tag.

    Reads the ref advertisement that `git clone` reads first, unless refs
    from an earlier call are given. An annotated tag is listed twice, the
    second time peeled (`^{}`) to its commit.
    """
    if not names.GIT_URL.match(url) or not names.is_git_tag(tag):
        raise RcError(f"refusing to look up tag {tag!r} at {url!r}")
    if refs is None:
        refs = git_refs(web, url)
    return refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")


def git_refs(web: http.Transport, url: str) -> dict[str, str]:
    """Every ref a git repository served over smart HTTP advertises."""
    if not names.GIT_URL.match(url):
        raise RcError(f"refusing to read the refs of {url!r}")
    resp = http.follow(web, "GET", f"{url}/info/refs?service=git-upload-pack")
    if resp.status != 200:
        raise http_error(url, resp.status)
    if (resp.header("content-type") or "").split(";")[0].strip() != GIT_ADVERTISEMENT:
        raise RcError(f"{url}: not a git repository served over smart HTTP")
    try:
        return parse_advertisement(resp.body)
    except RcError as exc:
        raise RcError(f"{url}: {exc}") from None


def wheel_problem(files: list[dict]) -> str | None:
    wheels = [f["filename"] for f in files if f["filename"].endswith(".whl")]
    if not wheels:
        return "has no wheels"
    found = set()
    for wheel in wheels:
        for tag in wheel[: -len(".whl")].split("-")[-1].split("."):
            if tag == "any":
                return None
            libc = "manylinux" if tag.startswith("manylinux") else "musllinux" if tag.startswith("musllinux") else ""
            arch = "x86_64" if tag.endswith("x86_64") else "aarch64" if tag.endswith("aarch64") else ""
            if libc and arch:
                found.add((libc, arch))
    missing = sorted(f"{libc} {arch}" for libc, arch in _PLATFORMS - found)
    return f"has no wheels for {', '.join(missing)}" if missing else None


def provenance_problem(web: http.Transport, project: str, raw_version: str, files: list[dict], publisher: str) -> str | None:
    """Check the PEP 740 provenance of every file of a release through the PyPI Integrity API."""
    for f in files:
        filename = f["filename"]
        if not _FILENAME.match(filename):
            return f"has a file with an unexpected name {filename[:60]!r}"
        if not f.get("provenance"):
            return f"{filename} has no provenance"
        url = f"{PYPI}/integrity/{pypi_name(project)}/{urllib.parse.quote(raw_version)}/{urllib.parse.quote(filename)}/provenance"
        resp = web.request("GET", url, {"Accept": INTEGRITY_JSON})
        if resp.status == 404:
            return f"{filename} has no provenance"
        if resp.status != 200:
            raise http_error(f"PyPI Integrity API, {filename}", resp.status)
        try:
            bundles = json.loads(resp.body).get("attestation_bundles") or []
        except (ValueError, AttributeError):
            raise RcError(f"PyPI Integrity API: unreadable provenance for {filename}") from None
        if not bundles:
            return f"{filename} has no attestations"
        for bundle in bundles:
            pub = bundle.get("publisher") or {}
            kind, repo = pub.get("kind"), str(pub.get("repository") or "")
            if kind != "GitHub" or repo.lower() != publisher.lower():
                who = f"GitHub {repo}" if kind == "GitHub" and names.GITHUB_REPO.match(repo) else "another publisher"
                return f"{filename} was published by {who}, not {publisher}"
    return None


class Checker:
    def __init__(
        self,
        gh: GitHub,
        web: http.Transport,
        now: datetime,
        max_candidates: int = MAX_CANDIDATES,
    ):
        self.gh = gh
        self.web = web
        self.now = now
        self.max_candidates = max_candidates
        self._refs: dict[str, dict[str, str]] = {}

    # candidates

    def pypi(self, upstream: Upstream) -> list[Candidate]:
        url = f"{PYPI}/simple/{pypi_name(upstream.project)}/"
        resp = http.follow(self.web, "GET", url, {"Accept": SIMPLE_JSON})
        if resp.status != 200:
            raise http_error(f"PyPI, {upstream.project}", resp.status)
        try:
            data = json.loads(resp.body)
        except ValueError:
            raise RcError(f"PyPI: unreadable index for {upstream.project}") from None
        by_version: dict[str, list[dict]] = {}
        stem = pypi_name(upstream.project).replace("-", "_")
        for f in data.get("files", []):
            name = f.get("filename", "")
            if not isinstance(name, str):
                continue
            if name.endswith(".whl"):
                parts = name.split("-")
                raw = parts[1] if len(parts) >= 5 else ""
            elif name.endswith((".tar.gz", ".zip")):
                base = name[: -len(".tar.gz")] if name.endswith(".tar.gz") else name[: -len(".zip")]
                head, _, raw = base.rpartition("-")
                if head.lower().replace("-", "_").replace(".", "_") != stem:
                    raw = ""
            else:
                raw = ""
            if raw:
                by_version.setdefault(raw, []).append(f)
        out = []
        for raw in data.get("versions", []):
            version = match_version(upstream, raw, pypi=True)
            files = by_version.get(raw, [])
            if version and files:
                times = [parse_time(f.get("upload-time")) for f in files]
                published = max((t for t in times if t), default=None)
                out.append(Candidate(version, raw, published=published, files=files))
        return out

    def github_release(self, upstream: Upstream) -> list[Candidate]:
        out = []
        for release in self.gh.releases(upstream.repository):
            if release.get("prerelease"):
                continue
            tag = release.get("tag_name", "")
            version = match_version(upstream, tag)
            if version:
                out.append(Candidate(version, tag, published=parse_time(release.get("published_at")), release=release))
        return out

    def github_tag(self, upstream: Upstream) -> list[Candidate]:
        out = []
        for tag, sha, kind in self.gh.tags(upstream.repository, literal_prefix(upstream.tag_pattern)):
            version = match_version(upstream, tag)
            if version:
                out.append(Candidate(version, tag, object=(sha, kind)))
        return out

    def _get_json(self, url: str, what: str) -> tuple[http.Response, object]:
        resp = http.follow(self.web, "GET", url, {"Accept": "application/json"})
        if resp.status != 200:
            raise http_error(what, resp.status)
        try:
            return resp, json.loads(resp.body)
        except ValueError:
            raise RcError(f"{what}: response is not JSON") from None

    def gitlab_release(self, upstream: Upstream) -> list[Candidate]:
        """The newest 100 releases; upcoming releases are skipped.

        released_at can be set to any date when a release is made, so the
        later of released_at and created_at counts.
        """
        project = urllib.parse.quote(upstream.project, safe="")
        url = f"{upstream.server}/api/v4/projects/{project}/releases?per_page=100"
        _, data = self._get_json(url, f"GitLab releases of {upstream.project}")
        if not isinstance(data, list):
            raise RcError(f"GitLab releases of {upstream.project}: unexpected response")
        out = []
        for release in data:
            if not isinstance(release, dict) or release.get("upcoming_release"):
                continue
            tag = release.get("tag_name", "")
            version = match_version(upstream, tag)
            if version:
                dates = [d for d in (api_time(release.get("released_at")), api_time(release.get("created_at"))) if d]
                out.append(Candidate(version, tag, published=max(dates, default=None)))
        return out

    def _forgejo_api(self, upstream: Upstream) -> str:
        owner, repo = (urllib.parse.quote(part, safe="") for part in upstream.repository.split("/"))
        return f"{upstream.server}/api/v1/repos/{owner}/{repo}"

    def forgejo_tag(self, upstream: Upstream) -> list[Candidate]:
        """Tags from the Forgejo API, newest first.

        Paging stops after a page that lists a tag no newer than the current
        version. The object is the tag's id and whether it is an annotated
        tag, whose tagger date is read only for the candidates that get
        checked.
        """
        base = f"{self._forgejo_api(upstream)}/tags"
        out = []
        for page in range(1, FORGEJO_PAGES + 1):
            resp, data = self._get_json(f"{base}?page={page}&limit={FORGEJO_LIMIT}", f"tags of {upstream.repository}")
            if not isinstance(data, list):
                raise RcError(f"tags of {upstream.repository}: unexpected response")
            reached = False
            for tag in data:
                if not isinstance(tag, dict):
                    continue
                name = tag.get("name", "")
                version = match_version(upstream, name)
                if version:
                    commit = tag.get("commit") if isinstance(tag.get("commit"), dict) else {}
                    tag_id = str(tag.get("id") or "")
                    kind = "commit" if tag_id == commit.get("sha") else "tag"
                    out.append(Candidate(version, name, published=api_time(commit.get("created")), object=(tag_id, kind)))
                    reached = reached or versions.compare(upstream.versioning, version, upstream.version) <= 0
            if reached or not data or 'rel="next"' not in (resp.header("link") or ""):
                break
        return out

    def _forgejo_date(self, upstream: Upstream, c: Candidate) -> datetime | None:
        """The later of the commit date and, for an annotated tag, the tagger date."""
        tag_id, kind = c.object
        if c.published is None or not names.GIT_SHA.match(tag_id):
            return None
        if kind == "commit":
            return c.published
        _, data = self._get_json(f"{self._forgejo_api(upstream)}/git/tags/{tag_id}", f"tag {c.tag} of {upstream.repository}")
        if not isinstance(data, dict) or data.get("sha") != tag_id or not isinstance(data.get("tagger"), dict):
            return None
        tagged = api_time(data["tagger"].get("date"))
        return max(c.published, tagged) if tagged else None

    def html_index(self, upstream: Upstream) -> list[Candidate]:
        """Versions matched by `pattern` on each line of an index page.

        A version listed more than once takes the latest date found for it.
        A pattern that matches nothing means the page changed, which is an
        error rather than "no new version".
        """
        resp = http.follow(self.web, "GET", upstream.url)
        if resp.status != 200:
            raise http_error(upstream.url, resp.status)
        if len(resp.body) > MAX_INDEX:
            raise RcError(f"{upstream.url}: larger than {MAX_INDEX} bytes")
        pattern = re.compile(upstream.pattern, re.ASCII)
        group = "version" if "version" in pattern.groupindex else 1
        dated = upstream.index_dated
        found: dict[str, Candidate] = {}
        matched = False
        for line in resp.body.decode("utf-8", "replace").splitlines():
            if len(line) > MAX_LINE:
                continue
            for m in pattern.finditer(line):
                matched = True
                raw = m.group(group)
                version = match_version(upstream, raw) if raw else None
                if not version:
                    continue
                c = found.setdefault(version, Candidate(version, raw))
                published = release_date(m.group("date")) if dated else None
                if published is not None and (c.published is None or published > c.published):
                    c.published = published
        if not matched:
            raise RcError(f"{upstream.url}: the pattern matches no line; the page may have changed")
        return list(found.values())

    def candidates(self, upstream: Upstream) -> list[Candidate]:
        source = {
            "pypi": self.pypi,
            "github-release": self.github_release,
            "github-tag": self.github_tag,
            "gitlab-release": self.gitlab_release,
            "forgejo-tag": self.forgejo_tag,
            "html-index": self.html_index,
        }
        found = source[upstream.source](upstream)
        seen: dict[str, Candidate] = {}
        for c in found:
            seen.setdefault(c.version, c)
        return sorted(seen.values(), key=lambda c: versions.sort_key(upstream.versioning, c.version), reverse=True)

    # checks

    def _published(self, upstream: Upstream, c: Candidate) -> datetime | None:
        if upstream.source == "forgejo-tag":
            if c.object is not None:
                c.published, c.object = self._forgejo_date(upstream, c), None
        elif c.published is None and c.object is not None:
            c.published = self.gh.tag_date(upstream.repository, *c.object)
        return c.published

    def _present(self, upstream: Upstream, c: Candidate, url: str) -> bool:
        m = _RELEASE_ASSET.match(url)
        if c.release is not None and m and m.group(1).lower() == (upstream.repository or "").lower():
            if urllib.parse.unquote(m.group(2)) == c.tag:
                name = urllib.parse.unquote(m.group(3))
                return any(a.get("name") == name and a.get("state", "uploaded") == "uploaded" for a in c.release.get("assets", []))
        return http.follow(self.web, "HEAD", url).status == 200

    def check(self, upstream: Upstream, c: Candidate) -> tuple[str, str] | None:
        """None when the candidate can be used, else (kind, reason) with kind waiting or refused."""
        if upstream.source == "html-index" and not upstream.index_dated and c.published is None:
            url = upstream.artifact.url_for(c.version)
            resp = http.follow(self.web, "HEAD", url)
            if resp.status != 200:
                return "waiting", f"{url} is not available yet"
            c.published = release_date(resp.header("last-modified"))
        published = self._published(upstream, c)
        if published is None:
            return "refused", "has no release date"
        age = self.now - published
        cooldown = duration(upstream.cooldown)
        if age < cooldown:
            hours = max(0, int(age.total_seconds() // 3600))
            return "waiting", f"was released {hours}h ago; the cooldown is {upstream.cooldown}"
        if upstream.source == "pypi":
            if any(f.get("yanked") for f in c.files):
                return "refused", "is yanked"
            problem = wheel_problem(c.files)
            if problem:
                return "refused", problem
            problem = provenance_problem(self.web, upstream.project, c.tag, c.files, upstream.publisher)
            if problem:
                return "refused", problem
        urls = []
        if upstream.artifact:
            urls += [upstream.artifact.url_for(c.version), upstream.artifact.signature_for(c.version)]
        for extra in upstream.extra_artifacts:
            if not extra.pinned_by_hand:
                urls += [extra.url_for(c.version), extra.signature_for(c.version)]
        for url in filter(None, urls):
            if not self._present(upstream, c, url):
                return "waiting", f"{url} is not available yet"
        if upstream.git:
            tag = upstream.git.tag_for(c.version)
            if upstream.source in TAG_SOURCES and tag != c.tag:
                return "refused", f"is tagged {c.tag}, but git.tag gives {tag}"
            if not names.is_git_tag(tag):
                return "refused", f"gives the tag name {tag!r}, which is not valid"
            url = upstream.git.url
            if url not in self._refs:
                self._refs[url] = git_refs(self.web, url)
            c.commit = tag_commit(self.web, url, tag, self._refs[url])
            if c.commit is None:
                return "waiting", f"has no tag {tag} at {upstream.git.url} yet"
        return None

    def latest(self, package: Package) -> Result:
        upstream = package.upstream
        result = Result(package.name, upstream.version)
        candidates = self.candidates(upstream)
        if candidates:
            result.newest = candidates[0].version
        newer = [c for c in candidates if versions.compare(upstream.versioning, c.version, upstream.version) > 0]
        for c in newer[: self.max_candidates]:
            verdict = self.check(upstream, c)
            if verdict is None:
                result.chosen = c
                break
            kind, reason = verdict
            (result.waiting if kind == "waiting" else result.refused).append(f"{c.version} {reason}")
            age = self.now - c.published if c.published else None
            if kind == "waiting" and age is not None and age > duration(upstream.cooldown) + STALLED:
                result.stalled.append(f"{c.version} {reason}, {age.days} days after its release")
        return result


# Pinning a tarball at bump time


def parse_checksums(text: str, filename: str, algorithm: str | None = None) -> str | None:
    """The digest of filename in a checksums file.

    Reads GNU (`hex  file`), BSD (`SHA256 (file) = hex`) and OpenSSL
    (`SHA2-256(file)= hex`) lines. With an algorithm, only lines of that
    algorithm count: labelled lines by their label, GNU lines by the length
    of the digest.
    """
    length = _HEX_LENGTH.get(algorithm) if algorithm else None
    for line in text.splitlines():
        line = line.strip()
        m = _CHECKSUM_LINE.match(line)
        if m and m.group(2) == filename and length in (None, len(m.group(1))):
            return m.group(1).lower()
        for tagged in _CHECKSUM_TAGGED:
            m = tagged.match(line)
            if not m or m.group(2) != filename:
                continue
            if algorithm and (_CHECKSUM_LABELS.get(m.group(1)) != algorithm or len(m.group(3)) != length):
                continue
            return m.group(3).lower()
    return None


Fetch = Callable[[str, tuple[str, ...]], dict[str, str]]


class HandPin(RcError):
    """A new version with a file that nothing can check, so a person has to pin it."""


def _filename(url: str) -> str:
    return urllib.parse.unquote(url.rsplit("/", 1)[1])


def _recorded_digests(c: Candidate, filename: str) -> list[str]:
    """The sha256 digests GitHub recorded for the release assets named filename."""
    digests = []
    for asset in (c.release or {}).get("assets", []):
        digest = asset.get("digest")
        if asset.get("name") == filename and isinstance(digest, str) and digest.startswith("sha256:"):
            digests.append(digest)
    return digests


def _checkable(artifact: Artifact, version: str, c: Candidate) -> bool:
    return bool(artifact.checksums or artifact.signature or _recorded_digests(c, _filename(artifact.url_for(version))))


def hand_pins(package: Package, c: Candidate) -> list[str]:
    """The files of version c that no checksums file, signature or GitHub digest checks.

    The reconciler cannot pin such a version, and it says so without
    downloading anything.
    """
    source = package.upstream
    artifacts = [source.artifact] if source.artifact else []
    artifacts += [extra.artifact for extra in source.extra_artifacts if not extra.pinned_by_hand]
    return [_filename(a.url_for(c.version)) for a in artifacts if not _checkable(a, c.version, c)]


def pin_artifact(package: Package, c: Candidate, web: http.Transport, fetch: Fetch) -> str:
    """Download the release artifact once and return its sha256 after the configured cross-checks."""
    return pin_file(package.upstream.artifact, c.version, c, web, fetch)


def pin_extras(package: Package, c: Candidate, web: http.Transport, fetch: Fetch) -> dict[str, str]:
    """The sha256 of every extra artifact that follows the package version, checked like the main artifact."""
    return {
        extra.name: pin_file(extra.artifact, c.version, c, web, fetch)
        for extra in package.upstream.extra_artifacts
        if not extra.pinned_by_hand
    }


def pin_file(artifact: Artifact, version: str, c: Candidate, web: http.Transport, fetch: Fetch) -> str:
    """Download a file once and return its sha256 after the configured cross-checks.

    At least one check must run: the checksums file, the digest GitHub
    recorded for the release asset, or the signature that the Dockerfile
    verifies. Without any of them the download itself would be the only
    source of the pin, so a file that none of them covers is not downloaded.
    """
    url = artifact.url_for(version)
    filename = _filename(url)
    if not _checkable(artifact, version, c):
        raise HandPin(f"{filename}: no checksums file, no signature and no digest recorded by GitHub; pin it by hand")
    algorithms = ("sha256",)
    if artifact.checksums and artifact.checksums.algorithm != "sha256":
        algorithms += (artifact.checksums.algorithm,)
    digests = fetch(url, algorithms)
    sha256 = digests["sha256"]
    if not names.SHA256_HEX.match(sha256):
        raise RcError(f"{url}: unexpected digest")
    if artifact.checksums:
        checksums_url = expand_url(artifact.checksums.url, version)
        listing = http.get(web, checksums_url).decode("utf-8", "replace")
        expected = parse_checksums(listing, filename, artifact.checksums.algorithm)
        if expected is None:
            raise RcError(f"{filename} is not listed in {checksums_url}")
        if digests[artifact.checksums.algorithm] != expected:
            raise RcError(f"{filename} does not match its {artifact.checksums.algorithm} in the checksums file")
    for digest in _recorded_digests(c, filename):
        if digest != f"sha256:{sha256}":
            raise RcError(f"{filename} does not match the digest GitHub recorded for the release asset")
    return sha256
