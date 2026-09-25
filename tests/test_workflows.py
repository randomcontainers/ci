import re

import yaml

from conftest import ROOT
from rc import render

WORKFLOWS = ROOT / ".github" / "workflows"
HADOLINT = re.compile(r"hadolint/hadolint:v[0-9.]+@sha256:[a-f0-9]{64}")


def test_hadolint_is_pinned_once():
    # build.yml lints the calling repository, test.yml the rendered combos: same version for both.
    build = HADOLINT.findall((WORKFLOWS / "build.yml").read_text())
    test = HADOLINT.findall((WORKFLOWS / "test.yml").read_text())
    assert len(build) == 1 and len(test) == 1
    assert build == test


def test_dockerfile_frontend_is_pinned():
    # BuildKit pulls the frontend named on the syntax line and runs it in the job that pushes.
    assert re.fullmatch(r"docker/dockerfile:1\.[0-9]+\.[0-9]+@sha256:[a-f0-9]{64}", render.DOCKERFILE_SYNTAX)


def test_hadolint_runs_before_the_plan():
    steps = yaml.safe_load((WORKFLOWS / "build.yml").read_text())["jobs"]["plan"]["steps"]
    names = [step.get("name") for step in steps]
    assert names.index("Lint Dockerfiles") < names.index("Plan")
    lint = steps[names.index("Lint Dockerfiles")]
    assert lint["working-directory"] == "src"
    assert "--network none" in lint["run"] and ':/src:ro"' in lint["run"]
