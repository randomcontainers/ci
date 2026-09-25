import json
from datetime import timedelta

import pytest

from fakegithub import NOW, FakeGitHub
from rc import cli, commits
from rc.errors import RcError
from rc.github import GitHub
from world import ORG, World

PACKAGES = ("yt-dlp", "ffmpeg")
PARENT = "a" * 40


def outputs(path):
    lines = path.read_text().splitlines()
    return {lines[i].split("<<")[0]: lines[i + 1] for i in range(0, len(lines), 3)}


def test_commits_round_trip(tmp_path):
    planned = [
        commits.planned("yt-dlp", PARENT, "Update yt-dlp to 2026.09.20", {"package.yml": b"a\n", "requirements.lock": b"b\n"}),
        commits.planned("yt-dlp-ffmpeg", PARENT, "Update files", {".github/workflows/build.yml": b"name: Build\n"}),
    ]
    path = tmp_path / "commits.json"
    commits.dump(planned, path, PACKAGES)
    assert commits.load(path, PACKAGES) == planned
    assert commits.repositories(planned) == "yt-dlp,yt-dlp-ffmpeg"


@pytest.mark.parametrize(
    "commit",
    [
        commits.planned("yt-dlp", PARENT, "Update", {".github/workflows/build.yml": b"x"}),
        commits.planned("yt-dlp", PARENT, "Update", {"scripts/metadata.py": b"x"}),
        commits.planned("yt-dlp-ffmpeg", PARENT, "Update", {"../x": b"x"}),
        commits.planned("yt-dlp-ffmpeg", PARENT, "Update", {"/etc/x": b"x"}),
        commits.planned("yt-dlp-ffmpeg", PARENT, "Update", {}),
        commits.planned("ci", PARENT, "Update", {"README.md": b"x"}),
        commits.planned("../ci", PARENT, "Update", {"README.md": b"x"}),
        commits.planned("yt-dlp", "main", "Update", {"package.yml": b"x"}),
        commits.planned("yt-dlp", PARENT, "Update\n\nmore", {"package.yml": b"x"}),
        commits.planned("yt-dlp", PARENT, "x" * 121, {"package.yml": b"x"}),
        {**commits.planned("yt-dlp", PARENT, "Update", {"package.yml": b"x"}), "force": True},
        {**commits.planned("yt-dlp", PARENT, "Update", {}), "files": {"package.yml": "not base64!"}},
        "commit yt-dlp",
    ],
)
def test_unexpected_commits_are_refused(tmp_path, commit):
    path = tmp_path / "commits.json"
    path.write_text(json.dumps([commit]))
    with pytest.raises(RcError):
        commits.load(path, PACKAGES)


def test_one_commit_per_repository(tmp_path):
    one = commits.planned("yt-dlp", PARENT, "Update", {"package.yml": b"x"})
    path = tmp_path / "commits.json"
    path.write_text(json.dumps([one, one]))
    with pytest.raises(RcError, match="same repository"):
        commits.load(path, PACKAGES)


def test_one_failed_commit_does_not_stop_the_others():
    fake = FakeGitHub()
    head = fake.add_repo("a-b", {"README.md": b"old\n"})
    fake.add_repo("c-d", {"README.md": b"old\n"})
    planned = [
        commits.planned("a-b", head, "Update a-b", {"README.md": b"new\n"}),
        commits.planned("c-d", head, "Update c-d", {"README.md": b"new\n"}),
    ]
    problems = commits.apply(planned, GitHub(fake, "token"), ORG)
    assert len(problems) == 1 and problems[0].startswith(f'commit "Update c-d" to {ORG}/c-d:')
    assert fake.files(f"{ORG}/a-b")["README.md"] == b"new\n"
    assert fake.files(f"{ORG}/c-d")["README.md"] == b"old\n"


def test_reconcile_writes_the_commits_and_apply_commits_makes_them(tmp_path, monkeypatch, capsys):
    world = World()
    world.run()
    world.run_dispatched()
    monkeypatch.setattr(cli, "_reconciler", lambda args, dry_run: world.reconciler(dry_run=dry_run))
    monkeypatch.setattr(cli, "UrllibTransport", lambda: world.router)
    output = tmp_path / "output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.delenv("RC_WRITE_TOKEN", raising=False)
    path = tmp_path / "commits.json"
    heads = dict(world.github.heads)

    # The pass itself holds no write token and changes no repository.
    assert cli.main(["reconcile", "--commit-file", str(path)]) == 0
    assert world.github.heads == heads
    assert outputs(output) == {"commits": "true", "commit-repos": "imagemagick-ghostscript,streamlink-ffmpeg,yt-dlp-ffmpeg"}
    assert "0 builds dispatched; 3 commits for rc apply-commits;" in capsys.readouterr().out

    assert cli.main(["apply-commits", str(path)]) == 1
    monkeypatch.setenv("RC_WRITE_TOKEN", "write-token")
    assert cli.main(["apply-commits", str(path)]) == 0
    assert "3 of 3 commits made" in capsys.readouterr().out
    assert "combo.yml" in world.github.files(f"{ORG}/yt-dlp-ffmpeg")

    # a pass with nothing to commit writes no file
    path.unlink()
    output.write_text("")
    assert cli.main(["reconcile", "--commit-file", str(path)]) == 0
    assert outputs(output) == {"commits": "false", "commit-repos": ""} and not path.exists()


def test_package_update_is_not_dispatched_on_top_of_its_commit():
    world = World()
    world.web.add_release("streamlink", "8.7.0", NOW - timedelta(days=2), publisher="streamlink/streamlink")
    outcome = world.run()
    assert [a.message for a in outcome.actions if a.kind == "commit"] == ["Update streamlink to 8.7.0"]
    assert f"{ORG}/streamlink: the commit planned in this pass starts a build" in outcome.log
    assert f"{ORG}/streamlink" not in {repo for repo, _, _ in world.github.dispatches}


def test_changed_combo_files_are_built_on_the_next_pass():
    world = World()
    world.run()
    world.run_dispatched()
    world.run()
    world.run()
    world.run_dispatched(5)
    repo = f"{ORG}/streamlink-ffmpeg"
    world.github.push(repo, {"README.md": b"edited\n"})
    count = len(world.github.dispatches)

    first = world.run()
    assert [(a.kind, a.repo) for a in first.actions] == [("commit", repo)]
    assert f"{repo}: its build is dispatched on the next pass, after the commit planned in this pass" in first.log
    second = world.run()
    assert [(a.kind, a.repo) for a in second.actions] == [("dispatch", repo)]
    assert [r for r, _, _ in world.github.dispatches[count:]] == [repo]


def test_no_commit_to_a_repository_outside_the_installation():
    world = World()
    world.github.installation.discard(f"{ORG}/streamlink")
    world.web.add_release("streamlink", "8.7.0", NOW - timedelta(days=2), publisher="streamlink/streamlink")
    outcome = world.run()
    assert not [a for a in outcome.actions if a.kind == "commit"]
    note = f'`{ORG}/streamlink` is not in the App installation, so "Update streamlink to 8.7.0" was not committed'
    assert note in outcome.notes.items["repositories"]
