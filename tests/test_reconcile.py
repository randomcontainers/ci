import hashlib
import json
import re
from datetime import timedelta

import pytest
import yaml

from conftest import ROOT
from fakegithub import NOW
from rc import cli, commits, locks, status
from rc.errors import RcError
from rc.github import GitHub, GitHubError
from rc.reconcile import INDEX_STEP, PUBLISH_JOB, backoff
from world import LOCKS, ORG, World, lock_bytes


def dispatched(world, since=0):
    return {repo.split("/")[1]: inputs for repo, _, inputs in world.github.dispatches[since:]}


def kinds(outcome, kind=None):
    return [(a.kind, a.repo.split("/")[1]) for a in outcome.actions if kind in (None, a.kind)]


def outputs(path):
    """Step outputs from a GITHUB_OUTPUT file written as name<<delimiter blocks."""
    lines = path.read_text().splitlines()
    return {lines[i].split("<<")[0]: lines[i + 1] for i in range(0, len(lines), 3)}


def converge(world):
    """The passes and builds that take an empty organization to every image published."""
    world.run()
    world.run_dispatched()
    world.run()
    count = len(world.github.dispatches)
    world.run()
    world.run_dispatched(count)


def test_first_pass_creates_combos_and_builds_packages():
    world = World()
    outcome = world.run()

    assert sorted(c["name"] for c in world.github.created) == ["imagemagick-ghostscript", "streamlink-ffmpeg", "yt-dlp-ffmpeg"]
    created = next(c for c in world.github.created if c["name"] == "yt-dlp-ffmpeg")
    assert created["auto_init"] is True and created["homepage"] == "https://randomcontainers.com/yt-dlp-ffmpeg/"
    assert sorted(world.github.files(f"{ORG}/yt-dlp-ffmpeg")) == ["README.md"]

    # Nothing is published yet: every package is built, no combo can be.
    assert sorted(dispatched(world)) == ["ffmpeg", "ghostscript", "imagemagick", "streamlink", "yt-dlp"]
    assert all(i["default-only"] == "false" and i["want"].startswith("all-") for i in dispatched(world).values())
    blocked = outcome.notes.items["blocked"]
    assert "`yt-dlp-ffmpeg`: `ffmpeg` is not built yet" in blocked
    assert outcome.catalog["images"][0]["public"] is False

    # The next pass, with fresh tokens, writes the files of the new repositories.
    second = world.run()
    combos = ["imagemagick-ghostscript", "streamlink-ffmpeg", "yt-dlp-ffmpeg"]
    assert kinds(second, "commit") == [("commit", name) for name in combos]
    assert world.github.topics[f"{ORG}/yt-dlp-ffmpeg"] == ["randomcontainers", "container-image", "docker", "yt-dlp", "ffmpeg"]
    files = world.github.files(f"{ORG}/yt-dlp-ffmpeg")
    assert yaml.safe_load(files["combo.yml"]) == {"name": "yt-dlp-ffmpeg", "owner": "yt-dlp", "with": ["ffmpeg"]}
    assert {"Dockerfile.alpine", "Dockerfile.ubuntu", ".github/workflows/build.yml", "LICENSE"} <= set(files)
    assert files["README.md"].startswith(b"# yt-dlp-ffmpeg")
    assert "repositories" not in second.notes.items


def test_converges_after_a_successful_cycle():
    world = World()
    world.run()
    assert world.run_dispatched() == ["ffmpeg", "ghostscript", "yt-dlp", "imagemagick", "streamlink"]

    combos = ["imagemagick-ghostscript", "streamlink-ffmpeg", "yt-dlp-ffmpeg"]
    second = world.run()
    assert kinds(second) == [(kind, name) for name in combos for kind in ("commit", "topics")]
    third = world.run()
    assert kinds(third) == [("dispatch", name) for name in combos]
    assert all("default-only" not in i for i in dispatched(world, 5).values())
    world.run_dispatched(5)

    final = world.reconciler(dry_run=True).run()
    assert final.actions == []
    assert not final.notes, final.notes.lines()

    catalog = final.catalog
    images = {i["name"]: i for i in catalog["images"]}
    assert catalog["generated"] == "2026-09-24T12:00:00Z"
    assert all(i["public"] for i in images.values())
    assert images["yt-dlp"]["default_ready"] and not images["yt-dlp"]["stale"]
    assert [(v["flavour"], v["distro"]) for v in images["yt-dlp"]["variants"]] == [
        ("default", "ubuntu"),
        ("default", "alpine"),
        ("slim", "ubuntu"),
        ("slim", "alpine"),
    ]
    ghostscript = images["ghostscript"]
    assert ghostscript["default"] == {"kind": "slim"}
    assert ghostscript["variants"][0]["tags"][:4] == ["latest", "10.08.0", "10.08", "10"]
    assert ghostscript["variants"][0]["digest"] == ghostscript["variants"][2]["digest"]
    assert images["yt-dlp-ffmpeg"]["members"] == ["yt-dlp", "ffmpeg"]
    assert images["yt-dlp-ffmpeg"]["base"] == images["yt-dlp"]["default"]["base"] == "ffmpeg"
    assert images["imagemagick-ghostscript"]["base"] == "ghostscript"
    assert images["ffmpeg"]["source_release"] and not images["yt-dlp"]["source_release"]
    assert images["yt-dlp-ffmpeg"]["variants"][0]["tags"] == ["latest", "2026.08.19", "ubuntu", "2026.08.19-ubuntu", "2026.08.19-ubuntu26.04"]
    size = images["yt-dlp"]["variants"][0]["size"]
    assert sorted(size) == ["linux/amd64", "linux/arm64"] and all(v > 0 for v in size.values())

    # The same state gives the same bytes, so publish-status commits nothing.
    again = world.reconciler(dry_run=True, previous_catalog=catalog).run()
    assert status.dumps(again.catalog) == status.dumps(catalog)


def test_member_update_rebuilds_only_the_default_image():
    world = World()
    converge(world)
    count = len(world.github.dispatches)

    world.github.push(f"{ORG}/ffmpeg", {"README.md": b"new\n"})
    world.build("ffmpeg", want="all-manual")
    outcome = world.run()
    requests = dispatched(world, count)
    assert requests["yt-dlp"]["default-only"] == "true" and requests["yt-dlp"]["want"].startswith("default-")
    assert requests["streamlink"]["default-only"] == "true"
    assert set(requests) == {"yt-dlp", "streamlink", "yt-dlp-ffmpeg", "streamlink-ffmpeg"}
    assert any("new ffmpeg" in a.title for a in outcome.actions)


def test_weekly_rebuild_and_base_update():
    world = World()
    converge(world)

    later = world.reconciler(dry_run=True, now=NOW + timedelta(days=8)).run()
    assert len([a for a in later.actions if a.kind == "dispatch"]) == 8
    assert "built 8 days ago" in later.actions[0].title

    world.registry.add_image("docker.io", "library/alpine", "3.24", labels={"new": "base"})
    rebased = world.reconciler(dry_run=True).run()
    titles = [a.title for a in rebased.actions]
    assert len(titles) == 5 and all("slim-alpine (new base image)" in t for t in titles)


@pytest.mark.parametrize(
    "attempts,elapsed,expected",
    [
        ([("failure", 2)], 2, False),
        ([("failure", 7)], 7, True),
        ([("failure", 7), ("failure", 20)], 7, False),
        ([("failure", 13), ("failure", 30)], 13, True),
        ([("failure", 23), ("failure", 40), ("failure", 60)], 23, False),
        ([("failure", 25), ("failure", 40), ("failure", 60)], 25, True),
    ],
)
def test_backoff(attempts, elapsed, expected):
    world = World()
    plan = world.reconciler(dry_run=True).run()
    want = next(a.inputs["want"] for a in plan.actions if a.repo == f"{ORG}/ffmpeg")
    for conclusion, hours_ago in sorted(attempts, key=lambda a: -a[1]):
        world.github.add_run(f"{ORG}/ffmpeg", f"Build {want}", conclusion=conclusion, when=NOW - timedelta(hours=hours_ago))
    outcome = world.reconciler(dry_run=True).run()
    planned = [a for a in outcome.actions if a.repo == f"{ORG}/ffmpeg"]
    assert bool(planned) is expected
    if not expected:
        note = next(n for n in outcome.notes.items["builds"] if n.startswith("`ffmpeg`"))
        assert f"Build {want}" in note and "Next attempt after" in note


def test_finished_build_that_did_not_converge_waits():
    world = World()
    world.run()
    world.run_dispatched()
    world.github.push(f"{ORG}/ffmpeg", {"README.md": b"new\n"})
    plan = world.reconciler(dry_run=True).run()
    want = next(a.inputs["want"] for a in plan.actions if a.repo == f"{ORG}/ffmpeg")
    world.github.add_run(f"{ORG}/ffmpeg", f"Build {want}", when=NOW - timedelta(hours=3))
    outcome = world.reconciler(dry_run=True).run()
    assert f"{ORG}/ffmpeg" not in {a.repo for a in outcome.actions}
    note = next(n for n in outcome.notes.items["builds"] if n.startswith("`ffmpeg`"))
    assert "finished, but the images still differ" in note and "2026-09-24 15:00 UTC" in note


def test_backoff_steps():
    assert [backoff(n) for n in (1, 2, 3, 9)] == [timedelta(hours=h) for h in (6, 12, 24, 24)]


def test_running_build_is_not_dispatched_again():
    world = World()
    world.github.add_run(f"{ORG}/ffmpeg", "Build", status="in_progress", event="push")
    outcome = world.reconciler(dry_run=True).run()
    assert f"{ORG}/ffmpeg" not in {a.repo for a in outcome.actions}
    assert f"{ORG}/ffmpeg: a build is already queued or running" in outcome.log
    # a pull request build does not hold anything back
    world = World()
    run = world.github.add_run(f"{ORG}/ffmpeg", "Fix the build", status="queued", event="pull_request")
    run["head_branch"] = "fix"
    assert f"{ORG}/ffmpeg" in {a.repo for a in world.reconciler(dry_run=True).run().actions}


def test_pull_request_from_a_fork_main_does_not_hold_back_a_build():
    world = World()
    world.github.add_run(f"{ORG}/ffmpeg", "Fix the build", status="in_progress", event="pull_request")
    outcome = world.reconciler(dry_run=True).run()
    assert f"{ORG}/ffmpeg" in {a.repo for a in outcome.actions}
    assert f"{ORG}/ffmpeg: a build is already queued or running" not in outcome.log


def test_pull_request_titled_like_a_dispatch_is_not_an_attempt():
    world = World()
    plan = world.reconciler(dry_run=True).run()
    want = next(a.inputs["want"] for a in plan.actions if a.repo == f"{ORG}/ffmpeg")
    world.github.add_run(f"{ORG}/ffmpeg", f"Build {want}", conclusion="failure", event="pull_request")
    outcome = world.reconciler(dry_run=True).run()
    assert f"{ORG}/ffmpeg" in {a.repo for a in outcome.actions}
    assert not any("Next attempt after" in n for n in outcome.notes.items.get("builds", []))


def test_pull_request_runs_do_not_hide_a_published_build():
    world = World()
    world.run()
    world.run_dispatched()
    for _ in range(3):
        world.github.add_run(f"{ORG}/ffmpeg", "Fix the build", event="pull_request")
    world.registry.private.add(("ghcr.io", f"{ORG}/ffmpeg"))
    outcome = world.reconciler(dry_run=True).run()
    assert any(n.startswith("`ffmpeg`: built, but") for n in outcome.notes.items["public"])


def test_private_package_is_blocked_not_rebuilt():
    world = World()
    world.run()
    world.run_dispatched()
    world.registry.private.add(("ghcr.io", f"{ORG}/ffmpeg"))
    count = len(world.github.dispatches)
    outcome = world.run()
    assert "ffmpeg" not in dispatched(world, count)
    assert any(n.startswith("`ffmpeg`: built, but") for n in outcome.notes.items["public"])
    assert "`yt-dlp` default image: waiting for `ffmpeg` to be made public" in outcome.notes.items["blocked"]
    # the catalog keeps listing it, as not public
    assert next(i for i in outcome.catalog["images"] if i["name"] == "ffmpeg")["public"] is False


def test_distro_release_mismatch_blocks_combos():
    world = World()
    world.run()
    world.run_dispatched()
    labels = {"org.opencontainers.image.version": "9.0.2", "com.randomcontainers.distro-version": "3.23"}
    world.registry.add_image("ghcr.io", f"{ORG}/ffmpeg", "slim-alpine", labels=labels, annotations=labels)
    outcome = world.reconciler(dry_run=True).run()
    assert "`yt-dlp-ffmpeg` on Alpine 3.24: `ffmpeg` slim-alpine is built on 3.23" in outcome.notes.items["blocked"]


@pytest.mark.parametrize("break_it", ["unreachable", "invalid", "not utf-8"])
def test_unreadable_package_yml_does_not_stop_the_pass(break_it):
    world = World()
    converge(world)
    catalog = world.reconciler(dry_run=True).run().catalog
    count = len(world.github.dispatches)
    repo = f"{ORG}/ffmpeg"
    rec = world.reconciler(dry_run=False, previous_catalog=catalog)
    if break_it == "unreachable":
        original = rec.gh.raw

        def raw(name, sha, path):
            if name == repo:
                raise GitHubError(f"{repo}/{path} at {sha[:12]}: HTTP 503", 503)
            return original(name, sha, path)

        rec.gh.raw = raw
    else:
        world.github.push(repo, {"package.yml": b"name: [ffmpeg\n" if break_it == "invalid" else b"\xff\xfe"})
    outcome = rec.run()

    assert any(n.startswith("`ffmpeg`: ") for n in outcome.notes.items["errors"])
    assert "`yt-dlp` default image: `ffmpeg` has no valid package.yml" in outcome.notes.items["blocked"]
    assert dispatched(world, count) == {}
    # every entry that depends on ffmpeg is kept as it was, so the catalog does not change
    assert status.dumps(outcome.catalog) == status.dumps(catalog)


def test_registry_timeout_keeps_the_previous_entry():
    world = World()
    converge(world)
    catalog = world.reconciler(dry_run=True).run().catalog
    rec = world.reconciler(dry_run=True, previous_catalog=catalog)
    transport = rec.state.registry.transport

    class Flaky:
        def request(self, method, url, headers, body=None):
            if f"/v2/{ORG}/streamlink/" in url:
                raise RcError(f"{method} {url}: timed out")
            return transport.request(method, url, headers, body)

    rec.state.registry.transport = Flaky()
    outcome = rec.run()
    assert any(n.startswith("Registry: ") and "timed out" in n for n in outcome.notes.items["errors"])
    assert "`streamlink-ffmpeg`: `streamlink` could not be read from the registry" in outcome.notes.items["blocked"]
    assert "public" not in outcome.notes.items and outcome.actions == []
    assert status.dumps(outcome.catalog) == status.dumps(catalog)


def test_green_build_that_published_nothing_does_not_count_as_built():
    world = World()
    world.run()
    world.run_dispatched()
    world.run()
    # a build started by hand before the members were public: its plan skipped every target
    world.github.add_run(f"{ORG}/yt-dlp-ffmpeg", "Build")
    count = len(world.github.dispatches)
    outcome = world.run()
    assert "yt-dlp-ffmpeg" in dispatched(world, count)
    assert "public" not in outcome.notes.items


def test_publish_job_names_match_build_workflow():
    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "build.yml").read_text())
    merge = workflow["jobs"]["merge"]
    assert merge["name"].startswith(PUBLISH_JOB)
    assert INDEX_STEP in [step.get("name") for step in merge["steps"]]


# combo repositories


def test_combo_sync_respects_ownership():
    world = World()
    stranger = yaml.safe_dump({"name": "yt-dlp-ffmpeg", "owner": "yt", "with": ["dlp", "ffmpeg"]}).encode()
    world.github.add_repo("yt-dlp-ffmpeg", {"combo.yml": stranger, "README.md": b"mine\n"})
    world.github.add_repo("imagemagick-ghostscript", {"README.md": b"not a combo\n"})
    first = World()
    first.run()
    first.run()
    managed = first.github.files(f"{ORG}/streamlink-ffmpeg")
    world.github.add_repo("streamlink-ffmpeg", {**managed, "README.md": b"old\n"})
    old = yaml.safe_dump({"name": "old-ffmpeg", "owner": "old", "with": ["ffmpeg"]}).encode()
    world.github.add_repo("old-ffmpeg", {"combo.yml": old})
    heads = dict(world.github.heads)

    outcome = world.run()
    assert world.github.created == []
    assert world.github.heads[f"{ORG}/yt-dlp-ffmpeg"] == heads[f"{ORG}/yt-dlp-ffmpeg"]
    assert world.github.heads[f"{ORG}/imagemagick-ghostscript"] == heads[f"{ORG}/imagemagick-ghostscript"]
    assert world.github.heads[f"{ORG}/old-ffmpeg"] == heads[f"{ORG}/old-ffmpeg"]
    commits = [a for a in outcome.actions if a.kind == "commit"]
    assert [(a.repo, sorted(a.files)) for a in commits] == [(f"{ORG}/streamlink-ffmpeg", ["README.md"])]
    assert world.github.files(f"{ORG}/streamlink-ffmpeg")["README.md"] == managed["README.md"]
    notes = outcome.notes.items["repositories"]
    assert f"`{ORG}/yt-dlp-ffmpeg` belongs to `yt`, not `yt-dlp`, so it was left as it is" in notes
    assert any(n.startswith(f"`{ORG}/imagemagick-ghostscript` has no combo.yml") for n in notes)
    assert any(n.startswith(f"`{ORG}/old-ffmpeg` is no longer declared in `old` package.yml") for n in notes)
    # a combo repository that is not ours is never built
    assert "yt-dlp-ffmpeg" not in dispatched(world)


def test_combo_repository_is_set_up_after_a_failed_first_commit():
    world = World()
    world.run()
    repo = f"{ORG}/yt-dlp-ffmpeg"
    outcome = world.reconciler(dry_run=False).run()
    writer = GitHub(world.router, "write-token")
    original = writer.commit

    def commit(name, *args, **kwargs):
        if name == repo:
            raise GitHubError(f"POST /repos/{repo}/git/blobs: HTTP 404", 404)
        return original(name, *args, **kwargs)

    writer.commit = commit
    problems = commits.apply(outcome.commits, writer, ORG)
    assert problems == [f'commit "Add files generated from yt-dlp package.yml" to {repo}: POST /repos/{repo}/git/blobs: HTTP 404']
    world.setup(outcome)
    assert sorted(world.github.files(repo)) == ["README.md"]

    outcome = world.run()
    assert kinds(outcome, "commit") == [("commit", "yt-dlp-ffmpeg")]
    assert "combo.yml" in world.github.files(repo)
    assert world.github.topics[repo][0] == "randomcontainers"
    assert "repositories" not in outcome.notes.items


def test_readme_only_repository_of_someone_else_is_left_alone():
    world = World()
    world.github.add_repo("yt-dlp-ffmpeg", {"README.md": b"# yt-dlp-ffmpeg\n"})
    heads = dict(world.github.heads)
    outcome = world.run()
    assert world.github.heads[f"{ORG}/yt-dlp-ffmpeg"] == heads[f"{ORG}/yt-dlp-ffmpeg"]
    assert any(n.startswith(f"`{ORG}/yt-dlp-ffmpeg` has no combo.yml") for n in outcome.notes.items["repositories"])


def test_combos_with_the_same_members_are_not_created():
    world = World()
    repo = f"{ORG}/ffmpeg"
    text = world.github.files(repo)["package.yml"].decode()
    text += "combos:\n  - with: [yt-dlp]\n    summary: FFmpeg with yt-dlp.\n    test:\n      - yt-dlp --version\n"
    world.github.push(repo, {"package.yml": text.encode()})
    outcome = world.run()
    assert any("same members as yt-dlp-ffmpeg" in n for n in outcome.notes.items["errors"])
    assert sorted(c["name"] for c in world.github.created) == ["imagemagick-ghostscript", "streamlink-ffmpeg"]


def test_default_image_of_a_rejected_combo_is_held_back():
    world = World()
    converge(world)
    count = len(world.github.dispatches)
    repo = f"{ORG}/ffmpeg"
    text = world.github.files(repo)["package.yml"].decode()
    text += "combos:\n  - with: [yt-dlp]\n    summary: FFmpeg with yt-dlp.\n    test:\n      - yt-dlp --version\n"
    world.github.push(repo, {"package.yml": text.encode()})
    world.build("ffmpeg", want="all-manual")

    outcome = world.run()
    assert "`yt-dlp` default image: held back until the errors about `yt-dlp-ffmpeg` are fixed" in outcome.notes.items["blocked"]
    requests = dispatched(world, count)
    assert "yt-dlp" not in requests and "yt-dlp-ffmpeg" not in requests
    assert requests["streamlink"]["default-only"] == "true"


def test_combo_commit_is_never_forced():
    world = World()
    world.run()
    world.run()
    repo = f"{ORG}/streamlink-ffmpeg"
    world.github.push(repo, {"README.md": b"edited\n"})
    rec = world.reconciler(dry_run=False)
    original = rec.gh.head

    def stale_head(name):
        head = original(name)
        if name == repo:
            world.github.push(repo, {"NOTES.md": b"racing commit\n"})
        return head

    rec.gh.head = stale_head
    problems = world.commit(rec.run())
    assert len(problems) == 1 and "Update is not a fast forward" in problems[0]
    assert world.github.files(repo)["NOTES.md"] == b"racing commit\n"


# upstream updates


def test_calver_bump_commits_package_and_locks():
    world = World()
    world.web.add_release("yt-dlp", "2026.9.20", NOW - timedelta(days=2), publisher="yt-dlp/yt-dlp")
    world.web.add_release("yt-dlp", "2026.9.21.123456.dev0", NOW - timedelta(days=2), publisher="yt-dlp/yt-dlp")
    outcome = world.run()
    commit = next(a for a in outcome.actions if a.kind == "commit" and a.repo == f"{ORG}/yt-dlp")
    assert commit.message == "Update yt-dlp to 2026.09.20"
    assert sorted(commit.files) == ["package.yml", "requirements-deno.lock", "requirements.lock"]
    files = world.github.files(f"{ORG}/yt-dlp")
    assert "  version: 2026.09.20\n" in files["package.yml"].decode()
    assert b"'yt-dlp[default,curl-cffi]==2026.09.20'" in files["requirements.lock"]
    assert b"--exclude-newer-package yt-dlp-ejs=false - -o requirements.lock" in files["requirements.lock"]
    # the push starts the build; no dispatch on top of it
    assert "yt-dlp" not in dispatched(world)


def test_provenance_mismatch_is_refused():
    world = World()
    world.web.add_release("yt-dlp", "2026.9.20", NOW - timedelta(days=2), publisher="someone/yt-dlp")
    outcome = world.run()
    assert not [a for a in outcome.actions if a.kind == "commit" and a.repo == f"{ORG}/yt-dlp"]
    assert (
        "`yt-dlp` 2026.09.20 yt_dlp-2026.9.20-py3-none-any.whl was published by GitHub someone/yt-dlp, not yt-dlp/yt-dlp"
        in outcome.notes.items["upstream"]
    )


def test_missing_provenance_is_refused():
    world = World()
    world.web.add_release("streamlink", "8.7.0", NOW - timedelta(days=2), publisher=None)
    outcome = world.reconciler(dry_run=True).run()
    assert "`streamlink` 8.7.0 streamlink-8.7.0-py3-none-any.whl has no provenance" in outcome.notes.items["upstream"]


def test_cooldown_waits_for_new_releases():
    world = World()
    world.web.add_release("streamlink", "8.6.2", NOW - timedelta(days=3), publisher="streamlink/streamlink")
    world.web.add_release("streamlink", "8.7.0", NOW - timedelta(hours=3), publisher="streamlink/streamlink")
    outcome = world.run()
    commit = next(a for a in outcome.actions if a.kind == "commit" and a.repo == f"{ORG}/streamlink")
    assert commit.message == "Update streamlink to 8.6.2"
    assert "streamlink: 8.7.0 was released 3h ago; the cooldown is 24h" in outcome.log


def test_tarball_bump_pins_sha256_after_checksums():
    world = World()
    base = "https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs10090"
    tarball = b"ghostscript 10.09.0 source"
    world.web.urls[f"{base}/ghostscript-10.09.0.tar.xz"] = tarball
    sums = f"{hashlib.sha512(tarball).hexdigest()}  ghostscript-10.09.0.tar.xz\n{'0' * 128}  other.tar.gz\n"
    world.web.urls[f"{base}/SHA512SUMS"] = sums.encode()
    world.github.releases["ArtifexSoftware/ghostpdl-downloads"].insert(
        0, World.release("gs10090", NOW - timedelta(days=2), assets=["ghostscript-10.09.0.tar.xz", "SHA512SUMS"])
    )
    outcome = world.run()
    commit = next(a for a in outcome.actions if a.repo == f"{ORG}/ghostscript" and a.kind == "commit")
    assert commit.message == "Update ghostscript to 10.09.0"
    text = world.github.files(f"{ORG}/ghostscript")["package.yml"].decode()
    assert "  version: 10.09.0\n" in text
    assert f"    sha256: {hashlib.sha256(tarball).hexdigest()}\n" in text

    # a checksum that does not match stops the update
    world = World()
    world.web.urls[f"{base}/ghostscript-10.09.0.tar.xz"] = tarball
    world.web.urls[f"{base}/SHA512SUMS"] = f"{'1' * 128}  ghostscript-10.09.0.tar.xz\n".encode()
    world.github.releases["ArtifexSoftware/ghostpdl-downloads"].insert(
        0, World.release("gs10090", NOW - timedelta(days=2), assets=["ghostscript-10.09.0.tar.xz", "SHA512SUMS"])
    )
    outcome = world.run()
    assert not [a for a in outcome.actions if a.repo == f"{ORG}/ghostscript" and a.kind == "commit"]
    assert any("does not match its sha512" in n for n in outcome.notes.items["errors"])


def test_weekly_relock_commits_only_changed_locks():
    world = World(lock_date=NOW - timedelta(days=8))
    world.generation = 1
    outcome = world.run()
    commits = {a.repo.split("/")[1]: a for a in outcome.actions if a.kind == "commit" and "dependencies" in a.message}
    assert commits["yt-dlp"].message == "Update yt-dlp dependencies"
    assert sorted(commits["yt-dlp"].files) == ["requirements-deno.lock", "requirements.lock"]
    assert sorted(commits["streamlink"].files) == ["requirements.lock"]

    # locks changed less than a week ago are left alone
    world = World(lock_date=NOW - timedelta(days=2))
    world.generation = 1
    outcome = world.run()
    assert not [a for a in outcome.actions if a.kind == "commit" and "dependencies" in a.message]


def test_lock_that_needs_new_license_files_is_not_committed():
    world = World(lock_date=NOW - timedelta(days=8))
    world.generation = 1  # certifi 2026.1.1 -> 2026.2.1
    for name in ("streamlink", "yt-dlp"):
        world.github.push(f"{ORG}/{name}", {"licenses/certifi-2026.1.1/bundled/SOURCES": b"x\n"}, date=NOW - timedelta(days=8))
    heads = dict(world.github.heads)
    outcome = world.run()
    assert world.github.heads[f"{ORG}/streamlink"] == heads[f"{ORG}/streamlink"]
    assert world.github.heads[f"{ORG}/yt-dlp"] == heads[f"{ORG}/yt-dlp"]
    note = next(n for n in outcome.notes.items["upstream"] if n.startswith("`streamlink`"))
    assert '"Update streamlink dependencies" was not committed' in note
    assert "moves certifi 2026.1.1 to 2026.2.1" in note and "scripts/bundled-licenses.py" in note

    # a version bump that keeps the licensed version goes ahead
    world = World()
    world.github.push(f"{ORG}/streamlink", {"licenses/certifi-2026.1.1/bundled/SOURCES": b"x\n"})
    world.web.add_release("streamlink", "8.7.0", NOW - timedelta(days=2), publisher="streamlink/streamlink")
    outcome = world.run()
    assert kinds(outcome, "commit") == [("commit", "streamlink")]
    assert "upstream" not in outcome.notes.items


def test_lock_pins():
    lock = b"""# header
#    echo 'x==1' | uv pip compile ...
curl-cffi==0.16.0 ; implementation_name == 'cpython' \\
    --hash=sha256:00
Lxml==6.1.3 \\
    --hash=sha256:11
numpy==2.0 ; python_version >= '3.14' \\
numpy==1.26 ; python_version < '3.14' \\
"""
    assert locks.pins(lock) == {"curl-cffi": {"0.16.0"}, "lxml": {"6.1.3"}, "numpy": {"2.0", "1.26"}}


def test_unexpected_lock_header_is_an_error():
    world = World(lock_date=NOW - timedelta(days=8))
    repo = f"{ORG}/streamlink"
    bad = lock_bytes(LOCKS["streamlink"][0], "8.6.1").replace(b"--only-binary :all:", b"--index-url https://example.org")
    world.github.push(repo, {"requirements.lock": bad}, date=NOW - timedelta(days=8))
    outcome = world.run()
    assert any("unsupported uv option '--index-url'" in n for n in outcome.notes.items["errors"])


# health and status


def test_disabled_tick_and_default_lag_are_reported():
    world = World()
    world.run()
    world.run_dispatched()
    world.github.states[(f"{ORG}/ci", "tick.yml")] = "disabled_inactivity"
    text = world.github.files(f"{ORG}/streamlink")["package.yml"].decode().replace("version: 8.6.1", "version: 8.6.2")
    world.github.push(f"{ORG}/streamlink", {"package.yml": text.encode()}, date=NOW - timedelta(hours=7))
    outcome = world.reconciler(dry_run=True).run()
    health = outcome.notes.items["health"]
    assert any(n.startswith("`tick.yml` is disabled_inactivity") for n in health)
    assert any(n.startswith("`streamlink:latest` is 8.6.1, but package.yml has asked for 8.6.2") for n in health)
    streamlink = next(i for i in outcome.catalog["images"] if i["name"] == "streamlink")
    assert streamlink["stale"] is True and streamlink["version"] == "8.6.2"


def test_issue_body():
    notes = status.Notes()
    assert status.issue_body(notes, f"{ORG}/ci").endswith("Nothing needs attention.\n")
    notes.add("blocked", "`a` on Alpine 3.24: `b` is not built yet")
    notes.add("blocked", "`a` on Alpine 3.24: `b` is not built yet")
    body = status.issue_body(notes, f"{ORG}/ci")
    assert body.count("- `a` on Alpine") == 1 and "### Blocked" in body


def test_carry_over_adds_back_missing_entries(distros):
    doc = status.document(distros, {}, [], None)
    doc["images"] = [
        {"name": "b", "kind": "package", "updated": "2026-09-01T00:00:00Z"},
        {"name": "b-a", "kind": "combo", "updated": None},
    ]
    kept = [
        {"name": "a", "kind": "package", "updated": "2026-09-20T00:00:00Z"},
        {"name": "a-b", "kind": "combo", "updated": None},
    ]
    out = status.carry_over(doc, {"images": kept + doc["images"]}, {"a", "a-b", "gone"})
    assert [i["name"] for i in out["images"]] == ["a", "b", "a-b", "b-a"]
    assert list(out)[:2] == ["schema", "generated"] and out["generated"] == "2026-09-20T00:00:00Z"


def test_committed_catalog_matches_distros(distros):
    committed = json.loads((ROOT / "status" / "catalog.json").read_text())
    expected = status.empty(distros)
    assert {k: committed[k] for k in ("schema", "registry", "distros", "platforms")} == {
        k: expected[k] for k in ("schema", "registry", "distros", "platforms")
    }
    assert isinstance(committed["images"], list)


# The keys and types that readers of catalog.json can rely on in schema 1.
NAME = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,127}$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def check_catalog_schema(doc):
    assert doc["schema"] == 1 and set(doc["registry"]) == {"alias", "ghcr"}
    assert all(set(d) == {"id", "version", "default"} for d in doc["distros"])
    for image in doc["images"]:
        assert NAME.match(image["name"]) and image["kind"] in ("package", "combo")
        for key in ("title", "summary", "license", "version"):
            assert isinstance(image[key], str)
        assert image["repository"].startswith("https://") and isinstance(image["public"], bool)
        assert image["updated"] is None or isinstance(image["updated"], str)
        for v in image["variants"]:
            assert v["flavour"] in ("default", "slim") and isinstance(v["distro"], str)
            assert all(TAG.match(t) for t in v["tags"]) and DIGEST.match(v["digest"])
            assert all(isinstance(n, int) and n >= 0 for n in v["size"].values())
        if image["kind"] == "package":
            assert image["homepage"].startswith("https://")
            assert all(set(e) == {"title", "command"} for e in image["examples"])
            assert isinstance(image["default_ready"], bool) and isinstance(image["stale"], bool)
            assert isinstance(image["source_release"], bool)
            default = image["default"]
            assert default == {"kind": "slim"} or (
                default["kind"] == "combo" and NAME.match(default["combo"]) and default["with"] and default["summary"] and default["license"]
            )
            if default["kind"] == "combo":
                assert default["base"] in (image["name"], *default["with"])
        else:
            assert NAME.match(image["owner"]) and len(image["members"]) >= 2
            assert image["base"] in image["members"]


def test_catalog_follows_its_schema():
    world = World()
    check_catalog_schema(world.reconciler(dry_run=True).run().catalog)
    converge(world)
    check_catalog_schema(world.reconciler(dry_run=True).run().catalog)


def test_cli_writes_the_status_files(tmp_path, monkeypatch, capsys):
    world = World()
    monkeypatch.setattr(cli, "_reconciler", lambda args, dry_run: world.reconciler(dry_run=dry_run))
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    assert cli.main(["reconcile", "--output-dir", str(tmp_path)]) == 0
    assert (tmp_path / "issue-state").read_text() == "open\n"
    assert "### Blocked" in (tmp_path / "issue.md").read_text()
    assert json.loads((tmp_path / "catalog.json").read_text())["schema"] == 1
    out = capsys.readouterr().out
    assert "5 builds dispatched; 0 commits for rc apply-commits; 3 repository changes for rc setup-repos;" in out
    assert cli.main(["reconcile", "--dry-run", "--output-dir", str(tmp_path)]) == 1
    assert cli.main(["reconcile", "--dry-run", "--setup-file", str(tmp_path / "setup.json")]) == 1


def test_repository_setup_runs_apart_from_the_pass(tmp_path, monkeypatch, capsys):
    world = World()
    monkeypatch.setattr(cli, "_reconciler", lambda args, dry_run: world.reconciler(dry_run=dry_run))
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    setup = tmp_path / "setup.json"
    assert cli.main(["reconcile", "--setup-file", str(setup)]) == 0
    assert outputs(output)["setup"] == "true"
    # the pass itself created nothing: its tokens cannot
    assert world.github.created == []
    requests = json.loads(setup.read_text())
    assert [(r["kind"], r["name"]) for r in requests] == [
        ("create", "imagemagick-ghostscript"),
        ("create", "streamlink-ffmpeg"),
        ("create", "yt-dlp-ffmpeg"),
    ]

    monkeypatch.setattr(cli, "UrllibTransport", lambda: world.router)
    monkeypatch.delenv("RC_ADMIN_TOKEN", raising=False)
    assert cli.main(["setup-repos", str(setup)]) == 1
    monkeypatch.setenv("RC_ADMIN_TOKEN", "write-token")
    assert cli.main(["setup-repos", str(setup)]) == 1
    assert "3 of 3 repository changes made" not in capsys.readouterr().out
    monkeypatch.setenv("RC_ADMIN_TOKEN", "admin-token")
    assert cli.main(["setup-repos", str(setup)]) == 0
    assert "3 of 3 repository changes made" in capsys.readouterr().out
    assert sorted(c["name"] for c in world.github.created) == [r["name"] for r in requests]

    # a pass with nothing to set up writes no file
    setup.unlink()
    world.run()
    output.write_text("")
    assert cli.main(["reconcile", "--setup-file", str(setup)]) == 0
    assert outputs(output)["setup"] == "false" and not setup.exists()


def test_topics_removed_by_hand_are_requested_again():
    world = World()
    world.run()
    world.run()
    repo = f"{ORG}/yt-dlp-ffmpeg"
    world.github.topics[repo] = ["docker", "mine"]
    outcome = world.run()
    assert kinds(outcome, "topics") == [("topics", "yt-dlp-ffmpeg")]
    assert world.github.topics[repo] == ["randomcontainers", "container-image", "docker", "yt-dlp", "ffmpeg", "mine"]
    assert kinds(world.run(), "topics") == []

    # topics missing from the installation listing are read from the repository
    world.github.topics[repo] = []
    rec = world.reconciler(dry_run=False)
    listing = rec.gh.installation_repositories
    rec.gh.installation_repositories = lambda: dict.fromkeys(listing(), None)
    assert kinds(rec.run(), "topics") == [("topics", "yt-dlp-ffmpeg")]
