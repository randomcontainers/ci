from pathlib import Path

import pytest

from fakes import FakeRegistry
from rc.config import load_distros, load_package, load_package_list

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "packages"


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
