"""The newest upstream release a package may move to (docs/architecture.md, "2. Upstream").

A release is taken when all of these hold:

- its tag (or PyPI version) matches `tag-pattern` and the version built from
  it passes the versioning scheme, so nothing unexpected reaches a tag, a
  path or a commit message;
- it is older than `cooldown`;
- its files are published: on PyPI not yanked, with wheels for glibc and
  musl on amd64 and arm64 or a pure-Python wheel, and every file carries a
  PEP 740 provenance from the `publisher` repository; for tarball builds the
  artifact (and signature) answer with HTTP 200.

When a newer release fails a check, older releases that are still newer
than the current version are tried, and the reason is reported.
"""

import json
import re
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from rc import http, names, versions
from rc.config import Package, Upstream, expand_url
from rc.errors import RcError
from rc.github import GitHub, parse_time

PYPI = "https://pypi.org"
SIMPLE_JSON = "application/vnd.pypi.simple.v1+json"
INTEGRITY_JSON = "application/vnd.pypi.integrity.v1+json"
MAX_CANDIDATES = 5
MAX_TAG = 128

_CALVER_PYPI = re.compile(r"^([0-9]{4})\.([0-9]{1,2})\.([0-9]{1,2})$")
_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,254}$")
_PLATFORMS = {("manylinux", "x86_64"), ("manylinux", "aarch64"), ("musllinux", "x86_64"), ("musllinux", "aarch64")}
_CHECKSUM_LINE = re.compile(r"^([A-Fa-f0-9]{64,128})\s+\*?(\S+)$")
_CHECKSUM_BSD = re.compile(r"^[A-Z0-9-]+ \((\S+)\) = ([A-Fa-f0-9]{64,128})$")
_RELEASE_ASSET = re.compile(r"^https://github\.com/([^/]+/[^/]+)/releases/download/([^/]+)/([^/]+)$")


@dataclass
class Candidate:
    version: str
    tag: str
    published: datetime | None = None
    object: tuple[str, str] | None = None
    release: dict | None = None
    files: list[dict] = field(default_factory=list)


@dataclass
class Result:
    package: str
    current: str
    newest: str | None = None
    chosen: Candidate | None = None
    refused: list[str] = field(default_factory=list)
    waiting: list[str] = field(default_factory=list)


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
            raise RcError(f"PyPI Integrity API: HTTP {resp.status} for {filename}")
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

    # candidates

    def pypi(self, upstream: Upstream) -> list[Candidate]:
        url = f"{PYPI}/simple/{pypi_name(upstream.project)}/"
        resp = http.follow(self.web, "GET", url, {"Accept": SIMPLE_JSON})
        if resp.status != 200:
            raise RcError(f"PyPI: HTTP {resp.status} for {upstream.project}")
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

    def candidates(self, upstream: Upstream) -> list[Candidate]:
        source = {"pypi": self.pypi, "github-release": self.github_release, "github-tag": self.github_tag}
        found = source[upstream.source](upstream)
        seen: dict[str, Candidate] = {}
        for c in found:
            seen.setdefault(c.version, c)
        return sorted(seen.values(), key=lambda c: versions.sort_key(upstream.versioning, c.version), reverse=True)

    # checks

    def _published(self, upstream: Upstream, c: Candidate) -> datetime | None:
        if c.published is None and c.object is not None:
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
        artifact = upstream.artifact
        if artifact:
            for url in filter(None, (artifact.url_for(c.version), artifact.signature_for(c.version))):
                if not self._present(upstream, c, url):
                    return "waiting", f"{url} is not available yet"
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
        return result


# Pinning a tarball at bump time


def parse_checksums(text: str, filename: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        m = _CHECKSUM_LINE.match(line)
        if m and m.group(2) == filename:
            return m.group(1).lower()
        m = _CHECKSUM_BSD.match(line)
        if m and m.group(1) == filename:
            return m.group(2).lower()
    return None


Fetch = Callable[[str, tuple[str, ...]], dict[str, str]]


def pin_artifact(package: Package, c: Candidate, web: http.Transport, fetch: Fetch) -> str:
    """Download the release artifact once and return its sha256 after the configured cross-checks.

    At least one check must run: the checksums file, the digest GitHub
    recorded for the release asset, or the signature that the Dockerfile
    verifies. Without any of them the download itself would be the only
    source of the pin.
    """
    artifact = package.upstream.artifact
    url = artifact.url_for(c.version)
    filename = urllib.parse.unquote(url.rsplit("/", 1)[1])
    algorithms = ("sha256",)
    if artifact.checksums and artifact.checksums.algorithm != "sha256":
        algorithms += (artifact.checksums.algorithm,)
    digests = fetch(url, algorithms)
    sha256 = digests["sha256"]
    if not names.SHA256_HEX.match(sha256):
        raise RcError(f"{url}: unexpected digest")
    checked = False
    if artifact.checksums:
        checksums_url = expand_url(artifact.checksums.url, c.version)
        listing = http.get(web, checksums_url).decode("utf-8", "replace")
        expected = parse_checksums(listing, filename)
        if expected is None:
            raise RcError(f"{filename} is not listed in {checksums_url}")
        if digests[artifact.checksums.algorithm] != expected:
            raise RcError(f"{filename} does not match its {artifact.checksums.algorithm} in the checksums file")
        checked = True
    for asset in (c.release or {}).get("assets", []):
        digest = asset.get("digest")
        if asset.get("name") == filename and isinstance(digest, str) and digest.startswith("sha256:"):
            if digest != f"sha256:{sha256}":
                raise RcError(f"{filename} does not match the digest GitHub recorded for the release asset")
            checked = True
    if not checked and not artifact.signature:
        raise RcError(f"{filename}: no checksums file, no signature and no digest recorded by GitHub; pin it by hand")
    return sha256
