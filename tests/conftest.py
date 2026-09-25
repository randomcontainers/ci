from datetime import UTC, datetime
from pathlib import Path

import pytest

from fakes import FakeRegistry
from rc.config import load_distros, load_package, load_package_list
from rc.plan import Planner, RunContext
from rc.registry import Registry
from rc.sources import PackageSource

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "packages"
GOLDEN = ROOT / "tests" / "golden"
NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)
SHA = "a" * 40


@pytest.fixture(autouse=True)
def outside_actions(monkeypatch):
    # rc writes workflow commands to stdout when GITHUB_ACTIONS is set; tests
    # that need that behaviour set it themselves.
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)


@pytest.fixture
def distros():
    return load_distros(ROOT / "distros.yml")


@pytest.fixture
def listed():
    return load_package_list(ROOT / "packages.yml")


@pytest.fixture
def packages(distros):
    return {p.name: load_package(p / "package.yml", distros) for p in sorted(FIXTURES.iterdir())}


@pytest.fixture
def fake():
    return FakeRegistry()


def slim_labels(name, version, distro_version):
    return {
        "org.opencontainers.image.version": version,
        "com.randomcontainers.distro-version": distro_version,
        "com.randomcontainers.variant": "slim",
    }


def publish_bases(fake):
    """Base images on Docker Hub, as multi-platform indexes."""
    return {
        "ubuntu": fake.add_image("docker.io", "library/ubuntu", "26.04"),
        "alpine": fake.add_image("docker.io", "library/alpine", "3.24"),
    }


def publish_slim(fake, name, version, distros=(("ubuntu", "26.04"), ("alpine", "3.24"))):
    return {
        d: fake.add_image("ghcr.io", f"randomcontainers/{name}", f"slim-{d}", labels=slim_labels(name, version, dv))
        for d, dv in distros
    }


def make_planner(distros, listed, fake, repository, *, event="push", ref="refs/heads/main", default_only=False):
    source = PackageSource(distros, local_dir=FIXTURES)
    run = RunContext(
        repository=repository,
        sha=SHA,
        ref=ref,
        event_name=event,
        default_only=default_only,
        now=NOW,
    )
    return Planner(distros, listed, source, Registry(fake), run)
