"""rc reconcile: bring the published images in line with the package definitions.

Each pass starts from nothing but the repositories, PyPI, GitHub releases
and the registry, so it can be run as often as needed and a pass that finds
everything current does nothing. A pass:

1. reads packages.yml and each listed package's package.yml at the head of
   its main branch;
2. moves a package to a newer upstream release, or re-locks its Python
   dependencies once a week, in one commit per repository;
3. creates or updates the generated combo repositories;
4. compares what each image should be (version, commit, base image digest
   and member digests) with the annotations of what is published, and
   dispatches a build where they differ;
5. checks that the tick schedule is still enabled and that `latest` follows
   package.yml;
6. writes status/catalog.json and the body of the tracking issue.

A build is dispatched with `want`, a hash of what it should produce; the
build shows it in its run name, "Build <want>". A want that already has a
queued or running build is not dispatched again, and one whose last run
ended less than 6, 12 or 24 hours ago (after one, two, or three or more
runs) waits. Only runs of main started by a push or a dispatch count. An
image that cannot be read anonymously is never treated as out of date: it
is private, or it was never built.

One package, combo or repository that cannot be read or handled only
adds an error to the tracking issue; the rest of the pass goes on.

The pass holds no token that can write to a repository. It records the
commits it plans, which `rc apply-commits` makes after the pass (see
rc.commits), and the repositories to create and the topics to set, which
`rc setup-repos` makes (see rc.reposetup).
"""

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rc import catalog, commits, locks, names, pkgedit, render, reposetup, status, upstream, yamlio
from rc.config import Combo, Distro, Distros, Package, load_distros, load_package_list, parse_combo_file, parse_package
from rc.constants import ORG, RESERVED_NAMES
from rc.errors import RcError, TransientError
from rc.github import Commit, GitHub, Job, Run, blob_sha
from rc.state import RegistryState

BUILD_WORKFLOW = "build.yml"
TICK_WORKFLOW = "tick.yml"
BACKOFF = (timedelta(hours=6), timedelta(hours=12), timedelta(hours=24))
MAX_AGE = timedelta(days=7)
RELOCK_EVERY = timedelta(days=7)
LAG = timedelta(hours=6)
TOPICS = ("randomcontainers", "container-image", "docker")
# A build published an image when one of its "Publish <flavour> <distro>"
# jobs (the merge job of build.yml) created an index.
PUBLISH_JOB = "Publish "
INDEX_STEP = "Create index"
CHECKED_RUNS = 3
# A pull request run never publishes, and one from a fork can use any branch
# name and title, including "main" and "Build <want>". Neither of these
# events can come from a fork.
BUILD_EVENTS = ("push", "workflow_dispatch")

Lock = Callable[[locks.Recipe, str], bytes]


@dataclass
class Action:
    kind: str  # dispatch, commit for rc apply-commits, or create and topics for rc setup-repos
    repo: str
    title: str
    files: dict[str, bytes] = field(default_factory=dict)
    message: str = ""
    parent: str = ""
    inputs: dict[str, str] = field(default_factory=dict)
    request: dict = field(default_factory=dict)
    done: bool = False


@dataclass
class Repo:
    name: str
    repo: str
    head: Commit | None = None
    exists: bool = False
    package: Package | None = None
    text: bytes | None = None
    committing: bool = False
    created: bool = False
    planned: bool = False
    runs: list[Run] | None = None
    runs_loaded: bool = False
    built: bool | None = None
    built_error: str = ""


@dataclass
class Target:
    """One image of one repository on one distro, with what it should be."""

    label: str
    tag: str
    desired: dict
    reasons: list[str]


@dataclass
class Outcome:
    actions: list[Action]
    notes: status.Notes
    catalog: dict | None
    log: list[str]

    @property
    def requests(self) -> list[dict]:
        """The changes left to rc setup-repos."""
        return [a.request for a in self.actions if a.kind in reposetup.KINDS]

    @property
    def commits(self) -> list[dict]:
        """The commits left to rc apply-commits."""
        return [commits.planned(a.repo.split("/")[1], a.parent, a.message, a.files) for a in self.actions if a.kind == "commit"]


def want_id(scope: str, repo: str, targets: list[Target]) -> str:
    payload = json.dumps(
        {"repo": repo, "scope": scope, "targets": {t.label: t.desired for t in targets}},
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"{scope}-{hashlib.sha256(payload.encode()).hexdigest()[:20]}"


def backoff(attempts: int) -> timedelta:
    return BACKOFF[min(attempts, len(BACKOFF)) - 1]


def _utc(value: datetime | None) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if value else "unknown"


def _distro_label(distro: Distro) -> str:
    return f"{distro.id.capitalize() if distro.id != 'ubuntu' else 'Ubuntu'} {distro.version}"


def _why(exc: Exception) -> str:
    return str(exc) if isinstance(exc, RcError) else f"{type(exc).__name__}: {exc}"


def _published_index(job: Job) -> bool:
    return (
        job.conclusion == "success"
        and job.name.rpartition(" / ")[2].startswith(PUBLISH_JOB)
        and job.steps.get(INDEX_STEP) == "success"
    )


class Reconciler:
    def __init__(
        self,
        *,
        ci_dir: Path,
        gh: GitHub,
        ci_gh: GitHub,
        registry: RegistryState,
        checker: upstream.Checker,
        fetch: upstream.Fetch,
        lock: Lock,
        now: datetime,
        dry_run: bool = True,
        packages_dir: Path | None = None,
        previous_catalog: dict | None = None,
        org: str = ORG,
    ):
        self.ci_dir = ci_dir
        self.gh = gh
        self.ci_gh = ci_gh
        self.state = registry
        self.checker = checker
        self.fetch = fetch
        self.lock = lock
        self.now = now
        self.dry_run = dry_run
        self.packages_dir = packages_dir
        self.previous = previous_catalog
        self.org = org
        self.notes = status.Notes()
        self.actions: list[Action] = []
        self.log: list[str] = []
        self.repos: dict[str, Repo] = {}
        self.packages: dict[str, Package] = {}
        self.combos: dict[str, Combo] = {}
        self.combo_repos: dict[str, Repo] = {}
        self.stale: set[str] = set()
        self.distros: Distros | None = None
        self.listed: tuple[str, ...] = ()
        self.unloaded: set[str] = set()
        self.rejected: set[str] = set()
        self.installed: dict[str, list[str] | None] | None = None
        self.installed_loaded = False

    # helpers

    def say(self, text: str) -> None:
        self.log.append(text)

    def _act(self, action: Action, run: Callable[[], None]) -> bool:
        self.actions.append(action)
        if self.dry_run:
            self.say(f"would {action.title}")
            return False
        try:
            run()
        except RcError as exc:
            self.notes.add("errors", f"Could not {action.title}: {exc}")
            self.say(f"failed to {action.title}: {exc}")
            return False
        action.done = True
        self.say(action.title)
        return True

    def _request(self, action: Action) -> None:
        self.actions.append(action)
        self.say(f"would {action.title}" if self.dry_run else f"requested: {action.title}")

    def _commit(self, repo: Repo, action: Action) -> bool:
        """Plan a commit for rc apply-commits, unless its token could not cover the repository."""
        installed = self._installed()
        if installed is not None and not any(k.lower() == repo.repo.lower() for k in installed):
            self.notes.add(
                "repositories",
                f"`{repo.repo}` is not in the App installation, so \"{action.message}\" was not committed",
            )
            return False
        self._request(action)
        repo.committing = True
        return True

    def _runs(self, repo: Repo) -> list[Run] | None:
        """Recent builds of main started by a push or a dispatch, newest first."""
        if not repo.runs_loaded:
            repo.runs_loaded = True
            if repo.exists:
                try:
                    runs = self.gh.runs(repo.repo, BUILD_WORKFLOW)
                except RcError as exc:
                    self.notes.add("errors", f"Cannot list the builds of `{repo.repo}`: {exc}")
                else:
                    if runs is not None:
                        repo.runs = [r for r in runs if r.event in BUILD_EVENTS and r.branch == "main"]
        return repo.runs

    def _ever_built(self, repo: Repo) -> bool:
        """Whether one of the newest successful builds on main published an image.

        A green run is not enough: its plan may have skipped every target, or
        its merge may have given way to a newer commit.
        """
        if repo.built_error:
            raise RcError(repo.built_error)
        if repo.built is None:
            runs = [r for r in self._runs(repo) or [] if r.conclusion == "success"]
            try:
                repo.built = any(
                    any(_published_index(job) for job in self.gh.jobs(repo.repo, run.id)) for run in runs[:CHECKED_RUNS]
                )
            except RcError as exc:
                repo.built_error = f"cannot read the builds of `{repo.repo}`: {exc}"
                raise RcError(repo.built_error) from None
        return repo.built

    def _unreadable(self, name: str, repo: Repo) -> str:
        if self._ever_built(repo):
            self.notes.add(
                "public",
                f"`{name}`: built, but ghcr.io/{self.org}/{name} cannot be pulled anonymously. "
                "Make it public under Package settings, Danger zone, Change visibility.",
            )
            return "private"
        return "unbuilt"

    def _member_reason(self, member, distro: Distro) -> str:
        if member.reason == "private":
            repo = self.repos.get(member.name)
            try:
                built = bool(repo) and self._ever_built(repo)
            except RcError:
                return f"`{member.name}` cannot be pulled anonymously"
            if built:
                return f"waiting for `{member.name}` to be made public"
            return f"`{member.name}` is not built yet"
        if member.reason == "unreadable":
            return f"`{member.name}` could not be read from the registry"
        if member.reason == "missing":
            return f"`{member.name}` has no slim-{distro.id} image yet"
        return f"`{member.name}` {member.reason}"

    # 1. definitions

    def load(self) -> None:
        self.distros = load_distros(self.ci_dir / "distros.yml")
        self.listed = load_package_list(self.ci_dir / "packages.yml")
        for name in self.listed:
            repo = Repo(name, f"{self.org}/{name}")
            self.repos[name] = repo
            try:
                repo.head = self.gh.head(repo.repo)
                repo.exists = repo.head is not None or (
                    self.packages_dir is None and self.gh.repository(repo.repo) is not None
                )
                if self.packages_dir is not None:
                    path = self.packages_dir / name / "package.yml"
                    repo.text = path.read_bytes() if path.is_file() else None
                    origin = str(path)
                elif repo.head is not None:
                    repo.text = self.gh.raw(repo.repo, repo.head.sha, "package.yml")
                    origin = f"{repo.repo}/package.yml"
                if repo.head is None:
                    self.notes.add("repositories", f"`{repo.repo}` does not exist or has no main branch")
                if repo.text is None:
                    if self.packages_dir is not None or repo.head is not None:
                        self.notes.add("errors", f"`{repo.repo}` has no package.yml")
                        self.unloaded.add(name)
                    continue
                package = parse_package(yamlio.load_text(repo.text.decode("utf-8"), origin), origin, self.distros)
                if package.name != name:
                    raise RcError(f"{origin} declares the name {package.name!r}")
                repo.package = package
                self.packages[name] = package
            except Exception as exc:
                self.notes.add("errors", f"`{name}`: {_why(exc)}")
                self.unloaded.add(name)
        for combos, problem in catalog.catalog_problems(self.packages, self.listed):
            self.notes.add("errors", problem)
            self.rejected.update(combos)
        for package in self.packages.values():
            for combo in package.combos:
                if combo.name not in self.rejected and all(m in self.packages for m in combo.members):
                    self.combos.setdefault(combo.name, combo)

    # 2. upstream

    def _lock_files(self, repo: Repo) -> dict[str, bytes]:
        package = repo.package
        files = ["requirements.lock"]
        for combo in package.combos:
            for _, extras in combo.extras:
                if extras.requirements and extras.requirements not in files:
                    files.append(extras.requirements)
        out = {}
        for name in files:
            if self.packages_dir is not None:
                path = self.packages_dir / repo.name / name
                data = path.read_bytes() if path.is_file() else None
            else:
                data = self.gh.raw(repo.repo, repo.head.sha, name)
            if data is not None:
                out[name] = data
        return out

    def _relock(self, repo: Repo, version: str, current: dict[str, bytes]) -> dict[str, bytes]:
        out = {}
        for name, data in current.items():
            recipe = locks.parse_recipe(data.decode("utf-8"), name, repo.package.upstream.project)
            try:
                out[name] = self.lock(recipe, version)
            except RcError as exc:
                if not self.dry_run:
                    raise
                self.say(f"{repo.name}: would regenerate {name} for {version} ({exc})")
        return out

    def check_upstream(self) -> None:
        for name, repo in self.repos.items():
            package = repo.package
            if package is None or (repo.head is None and self.packages_dir is None):
                continue
            try:
                result = self.checker.latest(package)
            except TransientError as exc:
                self.say(f"{name}: upstream did not answer, trying again on the next pass: {exc}")
                continue
            except RcError as exc:
                self.notes.add("errors", f"Cannot check upstream for `{name}`: {exc}")
                continue
            for text in result.refused + result.stalled:
                self.notes.add("upstream", f"`{name}` {text}")
            for text in result.waiting:
                self.say(f"{name}: {text}")
            try:
                self._update_package(repo, result)
            except TransientError as exc:
                self.say(f"{name}: a download did not answer, trying again on the next pass: {exc}")
            except Exception as exc:
                self.notes.add("errors", f"Cannot update `{name}`: {_why(exc)}")

    def _update_package(self, repo: Repo, result: upstream.Result) -> None:
        package = repo.package
        pypi = package.upstream.source == "pypi"
        chosen = result.chosen
        unchecked = upstream.hand_pins(package, chosen) if chosen is not None else []
        if unchecked:
            # A download that nothing checks cannot be pinned, so none is made.
            self.notes.add(
                "upstream",
                f"`{repo.name}` {chosen.version} needs a hand pin: no checksums file, signature or digest recorded by "
                f"GitHub checks {', '.join(unchecked)}",
            )
            chosen = None
        elif chosen is None:
            self.say(f"{repo.name}: {package.version} is current (newest upstream: {result.newest or 'none found'})")
        files: dict[str, bytes] = {}
        message = ""
        if chosen is not None:
            message = f"Update {repo.name} to {chosen.version}"
            self.say(f"{repo.name}: {chosen.version} is available (package.yml is at {package.version})")
            source = package.upstream
            sha256, extras, commit = None, {}, None
            if self.dry_run:
                urls = [source.artifact.url_for(chosen.version)] if source.artifact else []
                urls += [e.url_for(chosen.version) for e in source.extra_artifacts if not e.pinned_by_hand]
                for url in urls:
                    self.say(f"{repo.name}: would download {url} and pin its sha256")
            else:
                if source.artifact:
                    sha256 = upstream.pin_artifact(package, chosen, self.checker.web, self.fetch)
                extras = upstream.pin_extras(package, chosen, self.checker.web, self.fetch)
            if source.git:
                commit = chosen.commit
                if not commit:
                    raise RcError(f"tag {source.git.tag_for(chosen.version)} was not resolved to a commit")
                self.say(f"{repo.name}: tag {source.git.tag_for(chosen.version)} at {source.git.url} is commit {commit}")
            text = pkgedit.update_upstream(
                repo.text.decode("utf-8"), self.distros, version=chosen.version, sha256=sha256, commit=commit, extras=extras
            )
            files["package.yml"] = text.encode("utf-8")
            if pypi:
                files.update(self._relock(repo, chosen.version, self._lock_files(repo)))
        elif pypi and repo.head is not None and self._relock_due(repo):
            current = self._lock_files(repo)
            fresh = self._relock(repo, package.version, current)
            files = {k: v for k, v in fresh.items() if current.get(k) != v}
            message = f"Update {repo.name} dependencies"
            if not files:
                self.say(f"{repo.name}: dependencies are current")
        if not files:
            return
        if repo.head is None:
            self.say(f"{repo.name}: {message} (not committed: the repository does not exist)")
            return
        moved = self._license_moves(repo, files.get("requirements.lock"))
        if moved:
            self.notes.add(
                "upstream",
                f"`{repo.name}`: \"{message}\" was not committed. The new requirements.lock moves {', '.join(moved)}, "
                "whose license files are kept in `licenses/`, and the build fails until they match. In the package "
                "repository, regenerate the lock with `rc lock requirements.lock`, run `scripts/bundled-licenses.py`, "
                "and commit both.",
            )
            return
        action = Action(
            "commit",
            repo.repo,
            f"commit \"{message}\" to {repo.repo} ({', '.join(sorted(files))})",
            files=files,
            message=message,
            parent=repo.head.sha,
        )
        self._commit(repo, action)

    def _license_moves(self, repo: Repo, lock: bytes | None) -> list[str]:
        """Packages of licenses/<name>-<version>/ that a new requirements.lock pins at another version.

        The package's build copies those directories into the image and fails
        when the installed version differs, so such a lock is never committed.
        """
        if lock is None:
            return []
        if self.packages_dir is not None:
            base = self.packages_dir / repo.name / "licenses"
            dirs = {p.name for p in base.iterdir() if p.is_dir()} if base.is_dir() else set()
        else:
            tree = self.gh.tree(repo.repo, repo.head.sha)
            dirs = {path.split("/")[1] for path in tree if path.startswith("licenses/") and path.count("/") >= 2}
        pinned = locks.pins(lock)
        moved = []
        for directory in sorted(dirs):
            name, _, version = directory.rpartition("-")
            if not name:
                continue
            new = pinned.get(locks.normalize(name), set())
            if new != {version}:
                moved.append(f"{name} {version} to {' and '.join(sorted(new)) or 'nothing (no longer locked)'}")
        return moved

    def _relock_due(self, repo: Repo) -> bool:
        changed = self.gh.last_change(repo.repo, "requirements.lock")
        return changed is None or self.now - changed >= RELOCK_EVERY

    # 3. combo repositories

    def sync_combos(self) -> None:
        installed = self._installed()
        for name in sorted(self.combos):
            combo = self.combos[name]
            repo = Repo(name, f"{self.org}/{name}")
            self.combo_repos[name] = repo
            try:
                self._sync_combo(combo, repo)
                if installed is not None:
                    self._topics(combo, repo, installed)
            except Exception as exc:
                self.notes.add("errors", f"Cannot update `{repo.repo}`: {_why(exc)}")
        if installed is not None:
            self._removed_combos(installed)

    def _sync_combo(self, combo: Combo, repo: Repo) -> None:
        files = {p: s.encode("utf-8") for p, s in render.combo_repo_files(combo, self.packages, self.distros, self.org).items()}
        message = f"Update files generated from {combo.owner} package.yml"
        repo.head = self.gh.head(repo.repo)
        if repo.head is None:
            if self.gh.repository(repo.repo) is not None:
                self.notes.add("repositories", f"`{repo.repo}` exists but has no main branch; it was left as it is")
                return
            # rc setup-repos creates it after this pass. The next pass writes
            # the files, with a token minted after the repository joined the
            # App installation.
            request = reposetup.create(combo.name, combo.summary)
            self._request(Action("create", repo.repo, f"create {repo.repo}", request=request))
            repo.planned = True
            repo.created = not self.dry_run
            return
        repo.exists = True
        tree = self.gh.tree(repo.repo, repo.head.sha)
        if "combo.yml" not in tree:
            if self._created_here(combo, repo, tree):
                self._set_up(combo, repo, files)
                return
            self.notes.add("repositories", f"`{repo.repo}` has no combo.yml, so it is not managed here; it was left as it is")
            repo.exists = False
            return
        raw = self.gh.raw(repo.repo, repo.head.sha, "combo.yml") or b""
        try:
            current = parse_combo_file(yamlio.load_text(raw.decode("utf-8"), f"{repo.repo}/combo.yml"), f"{repo.repo}/combo.yml")
        except RcError as exc:
            self.notes.add("repositories", f"`{repo.repo}` has an unreadable combo.yml, so it was left as it is: {exc}")
            repo.exists = False
            return
        if current.owner != combo.owner or current.name != combo.name:
            self.notes.add(
                "repositories",
                f"`{repo.repo}` belongs to `{current.owner}`, not `{combo.owner}`, so it was left as it is",
            )
            repo.exists = False
            return
        changed = {p: c for p, c in files.items() if tree.get(p) != blob_sha(c)}
        if not changed:
            self.say(f"{repo.repo}: generated files are current")
            return
        action = Action(
            "commit",
            repo.repo,
            f"commit \"{message}\" to {repo.repo} ({', '.join(sorted(changed))})",
            files=changed,
            message=message,
            parent=repo.head.sha,
        )
        self._commit(repo, action)

    def _created_here(self, combo: Combo, repo: Repo, tree: dict[str, str]) -> bool:
        """Whether an earlier pass created this repository and its files were never written."""
        if set(tree) != {"README.md"}:
            return False
        info = self.gh.repository(repo.repo) or {}
        return info.get("description") == combo.summary or TOPICS[0] in (info.get("topics") or [])

    def _set_up(self, combo: Combo, repo: Repo, files: dict[str, bytes]) -> None:
        message = f"Add files generated from {combo.owner} package.yml"
        title = f"commit \"{message}\" to {repo.repo} (all files)"
        action = Action("commit", repo.repo, title, files=files, message=message, parent=repo.head.sha)
        if self._commit(repo, action):
            repo.created = True
        else:
            repo.exists = False

    def _topics(self, combo: Combo, repo: Repo, installed: dict[str, list[str] | None]) -> None:
        """Request the topics a managed combo repository is missing; other topics are kept."""
        key = next((k for k in installed if k.lower() == repo.repo.lower()), None)
        if not repo.exists or key is None:
            return
        current = installed[key]
        if current is None:
            topics = (self.gh.repository(repo.repo) or {}).get("topics")
            current = [t for t in topics if isinstance(t, str)] if isinstance(topics, list) else []
        wanted = [*TOPICS, *combo.members]
        if all(t in current for t in wanted):
            return
        values = [*wanted, *(t for t in current if t not in wanted)][: reposetup.MAX_TOPICS]
        request = reposetup.topics(combo.name, values)
        self._request(Action("topics", repo.repo, f"set the topics of {repo.repo} to {', '.join(values)}", request=request))

    def _installed(self) -> dict[str, list[str] | None] | None:
        """The repositories of the App installation and their topics, or None when they cannot be listed."""
        if not self.installed_loaded:
            self.installed_loaded = True
            if not self.gh.token:
                self.say("App installation: not listed without a token")
            else:
                try:
                    self.installed = self.gh.installation_repositories()
                except RcError as exc:
                    self.notes.add("errors", f"Cannot list the repositories of the App installation: {exc}")
        return self.installed

    def _removed_combos(self, installed: dict[str, list[str] | None]) -> None:
        declared = {c.name for p in self.packages.values() for c in p.combos}
        known = {f"{self.org}/{n}".lower() for n in (*self.listed, *declared, *RESERVED_NAMES)}
        for full in installed:
            if full.lower() in known or not full.lower().startswith(f"{self.org}/"):
                continue
            try:
                self._removed_combo(full)
            except Exception as exc:
                self.notes.add("errors", f"Cannot read `{full}`: {_why(exc)}")

    def _removed_combo(self, full: str) -> None:
        head = self.gh.head(full)
        raw = self.gh.raw(full, head.sha, "combo.yml") if head else None
        if raw is None:
            return
        try:
            combo_file = parse_combo_file(yamlio.load_text(raw.decode("utf-8"), full), full)
        except (RcError, UnicodeDecodeError):
            return
        if combo_file.owner in self.listed and combo_file.owner not in self.packages:
            return
        self.notes.add(
            "repositories",
            f"`{full}` is no longer declared in `{combo_file.owner}` package.yml. "
            "It is not built any more; archive or delete it by hand.",
        )

    # 4. published images

    def _compare(self, published, desired: dict) -> list[str]:
        if published is None:
            return ["not built yet"]
        reasons = []
        if published.version != desired["version"]:
            reasons.append(f"version {published.version or '?'} -> {desired['version']}")
        if desired.get("revision") and published.revision != desired["revision"]:
            reasons.append(f"commit {published.revision[:12] or '?'} -> {desired['revision'][:12]}")
        if desired.get("base") and published.base_digest != desired["base"]:
            reasons.append("new base image")
        if "members" in desired and published.members != desired["members"]:
            changed = sorted(
                m for m in set(desired["members"]) | set(published.members)
                if desired["members"].get(m) != published.members.get(m)
            )
            reasons.append(f"new {', '.join(changed)}")
        created = published.created
        if created is None or self.now - created > MAX_AGE:
            reasons.append(f"built {(self.now - created).days} days ago" if created else "no build date")
        return reasons

    def compare(self) -> None:
        for name, repo in self.repos.items():
            if repo.package is not None:
                try:
                    self._compare_package(repo)
                except Exception as exc:
                    self.notes.add("errors", f"Cannot compare `{name}`: {_why(exc)}")
        for name, repo in self.combo_repos.items():
            try:
                self._compare_combo(self.combos[name], repo)
            except Exception as exc:
                self.notes.add("errors", f"Cannot compare `{name}`: {_why(exc)}")

    def _members(self, names_, distro: Distro) -> tuple[dict, list[str]]:
        ready, blocked = {}, []
        for member in names_:
            if member not in self.packages:
                blocked.append(f"`{member}` has no valid package.yml")
                continue
            state = self.state.member(member, distro, self.packages[member].versioning)
            if state.ready:
                ready[member] = state
            else:
                blocked.append(self._member_reason(state, distro))
        return ready, blocked

    def _blocked(self, what: str, reasons: dict[str, list[str]]) -> None:
        for reason, where in reasons.items():
            scope = "" if len(where) == len(self.distros.items) else f" on {', '.join(where)}"
            self.notes.add("blocked", f"{what}{scope}: {reason}")

    def _compare_package(self, repo: Repo) -> None:
        package = repo.package
        image = self.state.image(package.name)
        head = repo.head.sha if repo.head else ""
        if image.errors:
            self.say(f"{package.name}: not compared, the registry could not be read")
            return
        if not image.readable:
            if self._unreadable(package.name, repo) == "private":
                self.say(f"{package.name}: cannot be pulled anonymously; waiting for it to be made public")
                return
        combo = package.default_combo
        slim: list[Target] = []
        default: list[Target] = []
        blocked: dict[str, list[str]] = {}
        for distro in self.distros.items:
            desired = {"version": package.version, "revision": head, "base": self.state.base_digest(distro)}
            published = self.state.tag(package.name, f"slim-{distro.id}")
            slim.append(Target(f"slim-{distro.id}", f"slim-{distro.id}", desired, self._compare(published, desired)))
            if combo is None:
                continue
            ready, reasons = self._members(combo.with_, distro)
            if not reasons and combo.name in self.rejected:
                # rc plan skips it too, so a dispatch would only build the slim images.
                reasons = [f"held back until the errors about `{combo.name}` are fixed"]
            if reasons:
                for reason in reasons:
                    blocked.setdefault(reason, []).append(_distro_label(distro))
                continue
            base = combo.base or combo.with_[0]
            desired = {
                "version": package.version,
                "revision": head,
                "base": ready[base].digest if base in ready else "",
                "members": {m: {"version": s.version, "digest": s.digest} for m, s in ready.items()},
            }
            published = self.state.tag(package.name, distro.id)
            default.append(Target(f"default-{distro.id}", distro.id, desired, self._compare(published, desired)))
        self._blocked(f"`{package.name}` default image", blocked)
        for target in slim + default:
            status_text = "; ".join(target.reasons) if target.reasons else "current"
            self.say(f"{package.name}:{target.tag}: {status_text}")
        if any(t.reasons for t in slim):
            self._dispatch(repo, "all", slim + default, {"default-only": "false"})
        elif any(t.reasons for t in default):
            self._dispatch(repo, "default", default, {"default-only": "true"})

    def _compare_combo(self, combo: Combo, repo: Repo) -> None:
        if not repo.exists and not repo.planned:
            return
        image = self.state.image(combo.name)
        if image.errors:
            self.say(f"{combo.name}: not compared, the registry could not be read")
            return
        if not image.readable and self._unreadable(combo.name, repo) == "private":
            return
        head = repo.head.sha if repo.head else ""
        targets: list[Target] = []
        blocked: dict[str, list[str]] = {}
        for distro in self.distros.items:
            ready, reasons = self._members(combo.members, distro)
            if reasons:
                for reason in reasons:
                    blocked.setdefault(reason, []).append(_distro_label(distro))
                continue
            base = combo.base or combo.with_[0]
            desired = {
                "version": ready[combo.owner].version,
                "revision": head,
                "base": ready[base].digest,
                "members": {m: {"version": s.version, "digest": s.digest} for m, s in ready.items()},
            }
            published = self.state.tag(combo.name, distro.id)
            target = Target(distro.id, distro.id, desired, self._compare(published, desired))
            self.say(f"{combo.name}:{distro.id}: {'; '.join(target.reasons) or 'current'}")
            targets.append(target)
        self._blocked(f"`{combo.name}`", blocked)
        if any(t.reasons for t in targets):
            self._dispatch(repo, "combo", targets, {})

    def _dispatch(self, repo: Repo, scope: str, targets: list[Target], inputs: dict[str, str]) -> None:
        differing = [f"{t.tag} ({'; '.join(t.reasons)})" for t in targets if t.reasons]
        if repo.committing:
            if repo.package is not None:
                self.say(f"{repo.repo}: the commit planned in this pass starts a build")
            else:
                self.say(f"{repo.repo}: its build is dispatched on the next pass, after the commit planned in this pass")
            return
        if repo.created:
            self.say(f"{repo.repo}: new; its first build is dispatched once its files are on main")
            return
        if repo.head is None:
            self.say(f"{repo.repo}: would build {', '.join(differing)} once the repository exists")
            return
        want = want_id(scope, repo.repo, targets)
        if not names.WANT.match(want):
            raise RcError(f"bad request id {want!r}")
        runs = self._runs(repo)
        if runs is None:
            self.notes.add("errors", f"`{repo.repo}` has no {BUILD_WORKFLOW} workflow")
            return
        if any(r.active for r in runs):
            self.say(f"{repo.repo}: a build is already queued or running")
            return
        title = f"Build {want}"
        attempts = [r for r in runs if r.event == "workflow_dispatch" and r.title == title]
        if attempts:
            last = max((r.updated or r.created for r in attempts if r.updated or r.created), default=None)
            retry = last + backoff(len(attempts)) if last else None
            if retry and self.now < retry:
                runs_url = f"https://github.com/{repo.repo}/actions/workflows/{BUILD_WORKFLOW}"
                failed = sum(1 for r in attempts if r.conclusion != "success")
                what = f"failed {failed} of {len(attempts)} times" if failed else "finished, but the images still differ"
                self.notes.add(
                    "builds",
                    f"`{repo.name}`: [{title}]({runs_url}) {what}: {', '.join(differing)}. Next attempt after {_utc(retry)}.",
                )
                return
        action = Action(
            "dispatch",
            repo.repo,
            f"dispatch {BUILD_WORKFLOW} in {repo.repo} as \"{title}\"{' (default image only)' if scope == 'default' else ''}: "
            + ", ".join(differing),
            inputs={**inputs, "want": want},
        )
        self._act(action, lambda: self.gh.dispatch(repo.repo, BUILD_WORKFLOW, action.inputs))

    # 5. health

    def health(self) -> None:
        try:
            state = self.ci_gh.workflow_state(f"{self.org}/ci", TICK_WORKFLOW)
        except RcError as exc:
            state = None
            self.say(f"tick.yml: cannot read its state: {exc}")
        if state and state != "active":
            self.notes.add(
                "health",
                f"`{TICK_WORKFLOW}` is {state}, so nothing runs on schedule. Enable it in the Actions tab of "
                f"{self.org}/ci or with `gh workflow enable {TICK_WORKFLOW} --repo {self.org}/ci`.",
            )
        default = self.distros.get(self.distros.default)
        for name, repo in self.repos.items():
            package = repo.package
            if package is None:
                continue
            try:
                published = self.state.tag(name, default.id)
            except RcError as exc:
                self.notes.add("errors", f"Cannot read `{name}:{default.id}`: {exc}")
                continue
            if published is None or published.version == package.version:
                continue
            since = repo.head.date if repo.head else None
            if since is not None and self.now - since > LAG:
                self.stale.add(name)
                self.notes.add(
                    "health",
                    f"`{name}:latest` is {published.version}, but package.yml has asked for {package.version} "
                    f"since {_utc(since)} or earlier.",
                )

    # 6. status

    def build_catalog(self) -> dict:
        combos = list(self.combos.values())
        doc = status.document(self.distros, self.packages, combos, self.state, stale=self.stale, previous=self.previous, org=self.org)
        broken = self.state.failed()
        for name in sorted(broken):
            for error in broken[name]:
                self.notes.add("errors", f"Registry: {error}")
        return status.carry_over(doc, self.previous, set(broken) | self._undefined())

    def _undefined(self) -> set[str]:
        """Images whose definitions could not be used in this pass, and the entries that depend on them."""
        names_ = self.unloaded | self.rejected
        for image in (self.previous or {}).get("images", []) if self.unloaded else []:
            if not isinstance(image, dict):
                continue
            default = image.get("default") if isinstance(image.get("default"), dict) else {}
            related = {image.get("owner"), *(image.get("members") or []), *(default.get("with") or [])}
            if related & self.unloaded:
                names_.add(image.get("name"))
        return names_

    def run(self) -> Outcome:
        self.load()
        self.check_upstream()
        self.sync_combos()
        self.compare()
        self.health()
        return Outcome(self.actions, self.notes, self.build_catalog(), self.log)

    def catalog_only(self) -> dict:
        self.load()
        self.health()
        return self.build_catalog()
