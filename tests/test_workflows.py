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


def test_only_the_reconcile_job_uses_the_bot_environment():
    jobs = yaml.safe_load((WORKFLOWS / "reconcile.yml").read_text())["jobs"]
    assert {name: job.get("environment") for name, job in jobs.items()} == {
        "reconcile": {"name": "bot", "deployment": False},
        "publish-status": None,
    }


def test_only_the_setup_step_gets_administration():
    steps = yaml.safe_load((WORKFLOWS / "reconcile.yml").read_text())["jobs"]["reconcile"]["steps"]
    tokens = {s["id"]: s for s in steps if "create-github-app-token" in s.get("uses", "")}
    admin = [i for i, s in tokens.items() if "permission-administration" in s["with"]]
    assert admin == ["admin-token"]
    assert tokens["admin-token"]["if"] == "${{ steps.reconcile.outputs.setup == 'true' }}"
    assert [k for k in tokens["admin-token"]["with"] if k.startswith("permission-")] == ["permission-administration"]
    users = [s for s in steps if "steps.admin-token.outputs.token" in str(s.get("env", {}))]
    assert len(users) == 1 and users[0]["run"].startswith("rc setup-repos ")
    reconcile = next(s for s in steps if s.get("id") == "reconcile")
    assert "--setup-file" in reconcile["run"] and "RC_ADMIN_TOKEN" not in reconcile["env"]


def test_commit_token_covers_only_the_planned_commits():
    steps = yaml.safe_load((WORKFLOWS / "reconcile.yml").read_text())["jobs"]["reconcile"]["steps"]
    tokens = {s["id"]: s for s in steps if "create-github-app-token" in s.get("uses", "")}
    writers = [i for i, s in tokens.items() if "write" in (s["with"].get("permission-contents"), s["with"].get("permission-workflows"))]
    assert writers == ["write-token"]
    token = tokens["write-token"]
    assert token["if"] == "${{ steps.reconcile.outputs.commits == 'true' }}"
    assert token["with"]["repositories"] == "${{ steps.reconcile.outputs.commit-repos }}"
    users = [s for s in steps if "steps.write-token.outputs.token" in str(s.get("env", {}))]
    assert len(users) == 1 and users[0]["run"].startswith("rc apply-commits ")
    reconcile = next(s for s in steps if s.get("id") == "reconcile")
    assert "--commit-file" in reconcile["run"] and set(reconcile["env"]) == {"RC_GITHUB_TOKEN", "RC_CI_TOKEN"}
