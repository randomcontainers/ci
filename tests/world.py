"""A small organization in memory: package repositories, upstreams, registries and builds."""

import hashlib
from datetime import timedelta

from conftest import FIXTURES, ROOT, publish_bases
from fakegithub import NOW, FakeGitHub, FakeWeb, Router
from fakes import FakeRegistry
from rc import commits, locks, reposetup, upstream
from rc.config import ComboFile, load_distros, load_package, load_package_list
from rc.github import GitHub
from rc.plan import Planner, RunContext
from rc.reconcile import Reconciler
from rc.registry import Registry
from rc.sources import PackageSource
from rc.state import RegistryState

ORG = "randomcontainers"
LOCKS = {
    "yt-dlp": [
        locks.Recipe("requirements.lock", "yt-dlp", ("default", "curl-cffi"), "3.14", "7 days", (), ("yt-dlp-ejs",)),
        locks.Recipe("requirements-deno.lock", "yt-dlp", ("pin-deno",), "3.14", "7 days", ("yt-dlp",), ("deno",)),
    ],
    "streamlink": [locks.Recipe("requirements.lock", "streamlink", (), "3.14", "7 days", ())],
}


def lock_bytes(recipe: locks.Recipe, version: str, generation: int = 0) -> bytes:
    """What uv would write: the header, then pinned requirements with hashes."""
    pins = [] if recipe.project in recipe.no_emit else [f"{recipe.project}=={version}"]
    pins.append(f"certifi==2026.{generation + 1}.1")
    lines = [locks.HEADER, f"#    {recipe.header_command(version)}"]
    for pin in pins:
        digest = hashlib.sha256(pin.encode()).hexdigest()
        lines += [f"{pin} \\", f"    --hash=sha256:{digest}"]
    return ("\n".join(lines) + "\n").encode()


class World:
    def __init__(self, lock_date=NOW):
        self.now = NOW
        self.registry = FakeRegistry()
        self.github = FakeGitHub(ORG)
        self.github.admin_token = "admin-token"
        self.web = FakeWeb()
        self.router = Router(self.registry, self.github, self.web)
        self.distros = load_distros(ROOT / "distros.yml")
        self.listed = load_package_list(ROOT / "packages.yml")
        self.packages = {p.name: load_package(p / "package.yml", self.distros) for p in FIXTURES.iterdir()}
        self.generation = 0
        self.bases = publish_bases(self.registry)
        self.github.add_repo("ci", {".github/workflows/tick.yml": b"name: Tick\n"}, installed=False)
        for name in self.listed:
            files = {
                "package.yml": (FIXTURES / name / "package.yml").read_bytes(),
                ".github/workflows/build.yml": b"name: Build\n",
            }
            for recipe in LOCKS.get(name, []):
                files[recipe.file] = lock_bytes(recipe, self.packages[name].version)
            self.github.add_repo(name, files, date=lock_date)
        old = NOW - timedelta(days=30)
        self.web.add_release("yt-dlp", "2026.8.19", old, publisher="yt-dlp/yt-dlp")
        self.web.add_release("streamlink", "8.6.1", old, publisher="streamlink/streamlink")
        self.github.tags["FFmpeg/FFmpeg"] = [("n9.0.2", "1" * 40, "tag", old), ("n9.1-dev", "2" * 40, "tag", old)]
        self.github.releases["ImageMagick/ImageMagick"] = [self.release("7.1.2-31", old)]
        self.github.releases["ArtifexSoftware/ghostpdl-downloads"] = [self.release("gs10080", old)]

    @staticmethod
    def release(tag, when, assets=(), prerelease=False):
        return {
            "tag_name": tag,
            "draft": False,
            "prerelease": prerelease,
            "published_at": when.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "assets": [{"name": a, "state": "uploaded"} for a in assets],
        }

    def lock(self, recipe, version):
        return lock_bytes(recipe, version, self.generation)

    def fetch(self, url, algorithms):
        data = self.web.urls[url]
        return {a: hashlib.new(a, data).hexdigest() for a in algorithms}

    def reconciler(self, *, dry_run=True, now=None, **kw) -> Reconciler:
        now = now or self.now
        gh = GitHub(self.router, "dispatch-token")
        return Reconciler(
            ci_dir=ROOT,
            gh=gh,
            ci_gh=GitHub(self.router),
            registry=RegistryState(Registry(self.router), ORG),
            checker=upstream.Checker(gh, self.router, now),
            fetch=self.fetch,
            lock=self.lock,
            now=now,
            dry_run=dry_run,
            **kw,
        )

    def run(self, **kw):
        """One pass as reconcile.yml runs it: rc reconcile, then rc apply-commits and rc setup-repos when it asked for them."""
        outcome = self.reconciler(dry_run=False, **kw).run()
        self.commit(outcome)
        self.setup(outcome)
        return outcome

    def commit(self, outcome) -> list[str]:
        planned = commits.check_all(outcome.commits, self.listed)
        return commits.apply(planned, GitHub(self.router, "write-token"), ORG) if planned else []

    def setup(self, outcome) -> list[str]:
        requests = [reposetup.check(r) for r in outcome.requests]
        return reposetup.apply(requests, GitHub(self.router, "admin-token"), ORG) if requests else []

    def build(self, name: str, *, default_only: bool = False, want: str = "") -> dict:
        """Run what the build workflow does for a repository: plan, then publish every target."""
        repo = f"{ORG}/{name}"
        run = RunContext(
            repository=repo,
            sha=self.github.heads[repo],
            ref="refs/heads/main",
            event_name="workflow_dispatch",
            default_only=default_only,
            want=want,
            now=self.now,
        )
        planner = Planner(self.distros, self.listed, PackageSource(self.distros, local_dir=FIXTURES), Registry(self.registry), run)
        if name in self.packages:
            doc = planner.plan_package(self.packages[name])
        else:
            owner = next(p for p in self.packages.values() if p.combo_by_name(name))
            combo = owner.combo_by_name(name)
            doc = planner.plan_combo(ComboFile(name, owner.name, combo.with_))
        for target in doc["targets"]:
            for tag in target["tags"]:
                self.registry.add_image("ghcr.io", f"{ORG}/{name}", tag, labels=target["labels"], annotations=target["labels"])
        published = tuple(f"{t['flavour']} {t['distro']}" for t in doc["targets"])
        self.github.add_run(repo, f"Build {want}" if want else "Build", published=published)
        return doc

    def run_dispatched(self, since: int = 0) -> list[str]:
        """Build everything dispatched after position `since`, members first."""
        order = ["ffmpeg", "ghostscript", "yt-dlp", "imagemagick", "streamlink"]
        pending = self.github.dispatches[since:]
        key = lambda d: order.index(d[0].split("/")[1]) if d[0].split("/")[1] in order else len(order)
        built = []
        for repo, _, inputs in sorted(pending, key=key):
            name = repo.split("/")[1]
            self.build(name, default_only=inputs.get("default-only") == "true", want=inputs["want"])
            built.append(name)
        return built
