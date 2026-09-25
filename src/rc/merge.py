"""rc merge: decide what one merge job publishes, and with which arguments.

Guards, in order:
1. every platform image of this flavour and distro was pushed;
2. the run is on refs/heads/main and its commit is still the head of main,
   so a re-run of an old run or a late run never publishes;
3. a floating tag (latest, slim, 9.0, ...) never moves to an older version
   than the one it points at now. Version tags are always written.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from rc import http, names, versions
from rc.errors import RcError
from rc.registry import NotFound, Registry, RegistryError, parse_reference

GITHUB_API = "https://api.github.com"


@dataclass
class Decision:
    skip: bool
    reason: str = ""
    ref: str = ""
    tags: list[str] = field(default_factory=list)
    held: list[str] = field(default_factory=list)
    digests: list[str] = field(default_factory=list)
    args: list[str] = field(default_factory=list)


def platform_digests(plan: dict, flavour: str, digests_dir: Path) -> tuple[list[str], list[str]]:
    """Digests found for each platform, and the platforms that have none."""
    found, missing = [], []
    for platform in plan["platforms"]:
        path = digests_dir / f"{flavour}-{platform['arch']}"
        try:
            digest = path.read_text(encoding="utf-8").strip()
        except OSError:
            missing.append(platform["platform"])
            continue
        if not names.DIGEST.match(digest):
            raise RcError(f"{path} does not hold a sha256 digest")
        found.append(digest)
    return found, missing


def main_head(transport: http.Transport, repository: str, token: str | None) -> str:
    if not names.GITHUB_REPO.match(repository):
        raise RcError(f"bad repository name {repository!r}")
    headers = {"Accept": "application/vnd.github.sha", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = transport.request("GET", f"{GITHUB_API}/repos/{repository}/commits/main", headers)
    if resp.status != 200:
        raise RcError(f"cannot read the head of main in {repository}: HTTP {resp.status}")
    sha = resp.body.decode("ascii", "replace").strip()
    if not names.GIT_SHA.match(sha):
        raise RcError(f"unexpected commit id from the GitHub API: {sha[:80]!r}")
    return sha


def current_version(registry: Registry, image_ref: str) -> str | None:
    """Version a tag points at now, or None when the tag does not exist."""
    ref = parse_reference(image_ref)
    try:
        annotations = registry.annotations(ref)
        version = annotations.get("org.opencontainers.image.version")
        if not version:
            version = registry.config_labels(ref).get("org.opencontainers.image.version")
    except NotFound:
        return None
    return version or None


def imagetools_args(image: str, tags: list[str], labels: dict[str, str], digests: list[str]) -> list[str]:
    args: list[str] = []
    for tag in tags:
        args += ["--tag", f"{image}:{tag}"]
    for key, value in labels.items():
        if "\n" in value or "\r" in value:
            raise RcError(f"annotation {key} contains a line break")
        args += ["--annotation", f"index:{key}={value}"]
    args += [f"{image}@{digest}" for digest in digests]
    return args


def decide(
    plan: dict,
    target: dict,
    digests_dir: Path,
    *,
    event_name: str,
    ref: str,
    sha: str,
    head: Callable[[], str],
    registry: Registry,
) -> Decision:
    image = plan["image"]
    decision = Decision(skip=True, ref=f"{image}:{target['primary']}")
    digests, missing = platform_digests(plan, target["flavour"], digests_dir)
    if missing:
        decision.reason = f"no image for {', '.join(missing)}, so the {target['flavour']} {target['distro']} tags stay as they are"
        return decision
    if event_name == "pull_request" or ref != "refs/heads/main":
        decision.reason = f"images are published only from refs/heads/main (this run: {event_name} on {ref})"
        return decision
    current_head = head()
    if current_head != sha:
        decision.reason = f"main moved on to {current_head[:12]} since {sha[:12]}; the newer run publishes"
        return decision

    version = target["version"]
    for tag in target["tags"]:
        if tag not in target["floating"]:
            decision.tags.append(tag)
            continue
        try:
            current = current_version(registry, f"{image}:{tag}")
        except RegistryError as exc:
            raise RcError(f"cannot read {image}:{tag} to check it is not moved backwards: {exc}") from None
        newer = False
        if current:
            try:
                newer = versions.compare(plan["versioning"], current, version) > 0
            except versions.VersionError:
                newer = False
        if newer:
            decision.held.append(f"{tag} (at {current})")
        else:
            decision.tags.append(tag)

    decision.skip = False
    decision.digests = digests
    decision.args = imagetools_args(image, decision.tags, target["labels"], digests)
    return decision
