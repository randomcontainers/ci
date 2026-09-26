import fnmatch
import json
import os
import re
import subprocess
import sys

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


def test_documented_dockerfile_snippets_are_linted():
    doc = (ROOT / "docs" / "adding-a-package.md").read_text()
    snippets = [b.split("```")[0] for b in doc.split("```dockerfile\n")[1:]]
    assert len(snippets) == 2
    for distro in ("ubuntu", "alpine"):
        fixture = (ROOT / "tests" / "fixtures" / "docs" / f"Dockerfile.{distro}").read_text()
        assert all(snippet in fixture for snippet in snippets)
        assert not re.search(r"^SHELL", fixture, re.M)
    steps = yaml.safe_load((WORKFLOWS / "test.yml").read_text())["jobs"]["render"]["steps"]
    lint = next(step for step in steps if step.get("name") == "Lint the rendered and documented Dockerfiles")
    assert '"$PWD/tests/fixtures/docs/"' in lint["run"]


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


def test_plan_reaches_the_other_jobs_as_an_artifact():
    text = (WORKFLOWS / "build.yml").read_text()
    jobs = yaml.safe_load(text)["jobs"]
    assert "plan" not in jobs["plan"]["outputs"]
    read = set(re.findall(r"needs\.plan\.outputs\.([A-Za-z0-9_-]+)", text))
    assert read and read <= set(jobs["plan"]["outputs"])
    steps = jobs["plan"]["steps"]
    names = [step.get("name") for step in steps]
    upload = steps[names.index("Upload plan")]
    assert names.index("Plan") < names.index("Upload plan")
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert upload["with"] == {
        "name": "plan",
        "path": "${{ runner.temp }}/plan.json",
        "if-no-files-found": "error",
        "retention-days": 1,
    }
    readers = [name for name, job in jobs.items() if "plan.json" in str(job.get("steps")) and name != "plan"]
    assert readers == ["build", "merge", "release"]
    for name in readers:
        steps = jobs[name]["steps"]
        download = [s for s in steps if s.get("name") == "Download plan"]
        assert len(download) == 1
        assert download[0]["uses"].startswith("actions/download-artifact@")
        assert download[0]["with"] == {"name": "plan", "path": "${{ runner.temp }}"}
        first = next(i for i, s in enumerate(steps) if "plan.json" in s.get("run", ""))
        assert steps.index(download[0]) < first
    # The merge job downloads digests by pattern, which must not pick up the plan.
    patterns = [s["with"]["pattern"] for s in jobs["merge"]["steps"] if "pattern" in s.get("with", {})]
    assert patterns
    assert not any(fnmatch.fnmatchcase("plan", re.sub(r"\$\{\{.*?\}\}", "*", p)) for p in patterns)
    uploads = [s["with"]["name"] for job in jobs.values() for s in job["steps"] if "upload-artifact" in s.get("uses", "")]
    assert uploads.count("plan") == 1 and all(n.startswith("digests-") for n in uploads if n != "plan")


def test_artifact_actions_share_one_pin():
    text = (WORKFLOWS / "build.yml").read_text()
    for action in ("upload-artifact", "download-artifact"):
        pins = set(re.findall(rf"actions/{action}@[a-f0-9]{{40}} # v[0-9.]+", text))
        assert len(pins) == 1, pins


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


FAKE_GH = """#!{python}
import json, os, sys
path = os.environ["FAKE_GH_STATE"]
state = json.load(open(path))
args = sys.argv[1:]
state["calls"].append(" ".join(args[:2]))
tag = args[2]
if args[:2] == ["release", "view"]:
    if tag not in state["releases"]:
        sys.exit(1)
    if "--json" in args:
        print("\\n".join(state["releases"][tag]))
elif args[:2] == ["release", "create"]:
    state["releases"][tag] = []
elif args[:2] == ["release", "upload"]:
    state["releases"][tag].append(os.path.basename(args[3]))
json.dump(state, open(path, "w"))
"""


def publish_release(tmp_path, releases):
    """Run the "Publish release" step of the release job against a fake gh."""
    steps = yaml.safe_load((WORKFLOWS / "build.yml").read_text())["jobs"]["release"]["steps"]
    script = next(s for s in steps if s.get("name") == "Publish release")["run"]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "gh").write_text(FAKE_GH.format(python=sys.executable))
    (bin_dir / "gh").chmod(0o755)
    assets = tmp_path / "source" / "assets"
    assets.mkdir(parents=True)
    for name in ("tool-1.0.tar.gz", "gts-0.7.6.tar.gz"):
        (assets / name).write_bytes(b"x")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"releases": releases, "calls": []}))
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_GH_STATE": str(state),
        "RUNNER_TEMP": str(tmp_path),
        "REPO": "randomcontainers/tool",
        "SHA": "a" * 40,
        "TAG": "v1.0",
        "TITLE": "Tool 1.0 source",
        "NOTES": str(tmp_path / "NOTES.md"),
    }
    subprocess.run(["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script], env=env, check=True, capture_output=True)
    return json.loads(state.read_text())


def test_source_release_uploads_only_missing_files(tmp_path):
    new = publish_release(tmp_path / "new", {})
    assert new["releases"]["v1.0"] == ["gts-0.7.6.tar.gz", "tool-1.0.tar.gz"]
    assert "release edit" not in new["calls"]
    same = publish_release(tmp_path / "same", {"v1.0": ["gts-0.7.6.tar.gz", "tool-1.0.tar.gz"]})
    assert "release upload" not in same["calls"] and "release edit" not in same["calls"]
    added = publish_release(tmp_path / "added", {"v1.0": ["tool-1.0.tar.gz"]})
    assert added["releases"]["v1.0"] == ["tool-1.0.tar.gz", "gts-0.7.6.tar.gz"]
    assert added["calls"].count("release upload") == 1 and added["calls"][-1] == "release edit"
