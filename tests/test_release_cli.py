import copy
import hashlib
import json
import os
import shutil
import subprocess
import tarfile

import pytest

from conftest import CI_DIR, FIXTURES
from rc import cli, release
from rc.errors import RcError

RELEASE = {
    "tag": "v9.0.2",
    "title": "FFmpeg 9.0.2 source",
    "url": "https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz",
    "sha256": hashlib.sha256(b"tarball").hexdigest(),
    "signature": "https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz.asc",
}


def fake_download(contents):
    def download(url, dest, max_bytes=0):
        data = contents[url]
        dest.write_bytes(data)
        return hashlib.sha256(data).hexdigest()

    return download


def test_fetch_verifies_and_keeps_signature(tmp_path):
    files = release.fetch(
        RELEASE,
        tmp_path,
        download=fake_download({RELEASE["url"]: b"tarball", RELEASE["signature"]: b"sig"}),
    )
    assert [f.path.name for f in files] == ["ffmpeg-9.0.2.tar.xz", "ffmpeg-9.0.2.tar.xz.asc"]
    notes = release.notes({"image": "ghcr.io/randomcontainers/ffmpeg"}, RELEASE, files)
    assert notes == (
        "Upstream source of FFmpeg 9.0.2, as built into the images from this repository.\n\n"
        f"- `ffmpeg-9.0.2.tar.xz`: sha256 `{RELEASE['sha256']}`, from https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz\n"
        "- `ffmpeg-9.0.2.tar.xz.asc`: upstream signature, from https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz.asc\n\n"
        "Images: `ghcr.io/randomcontainers/ffmpeg`\n"
    )


def test_fetch_rejects_wrong_tarball(tmp_path):
    with pytest.raises(RcError, match="package.yml pins"):
        release.fetch(RELEASE, tmp_path, download=fake_download({RELEASE["url"]: b"other"}))
    assert not (tmp_path / "assets" / "ffmpeg-9.0.2.tar.xz").exists()


def test_asset_name():
    assert release.asset_name("https://e.org/a/b/ghostscript-10.08.0.tar.xz") == "ghostscript-10.08.0.tar.xz"
    with pytest.raises(RcError):
        release.asset_name("https://e.org/")


def test_cli_validate_catalog(capsys):
    code = cli.main(["--ci-dir", str(CI_DIR), "validate", *map(str, sorted(FIXTURES.iterdir())), "--catalog",
                     "--packages-dir", str(FIXTURES)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "ok: catalog of 5 packages" in out


def test_cli_validate_reports_problems(tmp_path, capsys):
    bad = tmp_path / "package.yml"
    bad.write_text((FIXTURES / "ffmpeg" / "package.yml").read_text().replace("name: ffmpeg", "name: ci"))
    assert cli.main(["--ci-dir", str(CI_DIR), "validate", str(bad)]) == 1
    assert "reserved name" in capsys.readouterr().err


def test_cli_render_repo_files(tmp_path, capsys):
    out = tmp_path / "combo"
    code = cli.main(["--ci-dir", str(CI_DIR), "render", "--package-file", str(FIXTURES / "yt-dlp" / "package.yml"),
                     "--packages-dir", str(FIXTURES), "--repo-files", str(out)])
    assert code == 0
    assert (out / "Dockerfile.alpine").is_file() and (out / ".github" / "workflows" / "build.yml").is_file()
    code = cli.main(["--ci-dir", str(CI_DIR), "render", "--combo-file", str(out / "combo.yml"),
                     "--packages-dir", str(FIXTURES), "--distro", "alpine"])
    assert code == 0
    assert capsys.readouterr().out.endswith((out / "Dockerfile.alpine").read_text())


def test_cli_tags_json(capsys):
    code = cli.main(["--ci-dir", str(CI_DIR), "tags", "--package-file", str(FIXTURES / "ffmpeg" / "package.yml"),
                     "--distro", "ubuntu", "--json"])
    assert code == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[0]["flavour"] == "slim" and "latest" in rows[0]["tags"]


def package_checkout(root, name):
    """A package repository with the fixture package.yml and minimal contract files."""
    root.mkdir(parents=True)
    (root / "package.yml").write_text((FIXTURES / name / "package.yml").read_text())
    for distro in ("ubuntu", "alpine"):
        (root / f"Dockerfile.{distro}").write_text("ARG BASE_IMAGE\nFROM ${BASE_IMAGE} AS slim\nARG VERSION\n")
    (root / "requirements-deno.lock").write_text("")
    return root


def test_cli_plan_checks_repository_files(tmp_path, capsys):
    source = package_checkout(tmp_path / "src", "yt-dlp")
    (source / "Dockerfile.alpine").unlink()
    code = cli.main(["--ci-dir", str(CI_DIR), "plan", "--source", str(source), "--offline",
                     "--packages-dir", str(FIXTURES)])
    assert code == 1
    assert "Dockerfile.alpine is missing" in capsys.readouterr().err


def test_old_distro_release_warns_in_plan_and_fails_validate(tmp_path, monkeypatch, capsys):
    for key in ("GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_OUTPUT", "GITHUB_SHA", "GITHUB_REF", "GITHUB_EVENT_NAME"):
        monkeypatch.delenv(key, raising=False)
    source = package_checkout(tmp_path / "src", "yt-dlp")
    (source / "README.md").write_text("Built on Ubuntu 26.04 and Alpine 3.23.\n")
    code = cli.main(["--ci-dir", str(CI_DIR), "plan", "--source", str(source), "--offline",
                     "--packages-dir", str(FIXTURES), "--member-image", "ffmpeg=rclocal/ffmpeg:slim"])
    assert code == 0
    assert "warning: README.md names alpine 3.23, but distros.yml builds on 3.24" in capsys.readouterr().err
    assert cli.main(["--ci-dir", str(CI_DIR), "validate", str(source), "--files"]) == 1
    assert "README.md names alpine 3.23" in capsys.readouterr().err


def test_cli_offline_plan_and_bake(tmp_path, monkeypatch, capsys):
    for key in ("GITHUB_REPOSITORY", "GITHUB_OUTPUT", "GITHUB_SHA", "GITHUB_REF", "GITHUB_EVENT_NAME"):
        monkeypatch.delenv(key, raising=False)
    plan_file = tmp_path / "plan.json"
    source = package_checkout(tmp_path / "src", "yt-dlp")
    code = cli.main(["--ci-dir", str(CI_DIR), "plan", "--source", str(source), "--offline",
                     "--packages-dir", str(FIXTURES), "--member-image", "ffmpeg=rclocal/ffmpeg:slim",
                     "--output-file", str(plan_file)])
    assert code == 0
    out = capsys.readouterr().out
    assert "build=true" in out and "publish=false" in out
    monkeypatch.chdir(tmp_path)
    code = cli.main(["--ci-dir", str(CI_DIR), "bake", "--plan", str(plan_file), "--distro", "ubuntu",
                     "--platform", "linux/amd64", "--mode", "load", "--output", "bake.json"])
    assert code == 0
    doc = json.loads((tmp_path / "bake.json").read_text())
    assert doc["target"]["default"]["contexts"]["ffmpeg"] == "docker-image://rclocal/ffmpeg:slim"
    assert (tmp_path / ".rc-build" / "ubuntu-default" / "Dockerfile").is_file()


def test_cli_errors_are_reported(capsys):
    assert cli.main(["--ci-dir", str(CI_DIR), "check-image", "--plan", "/nonexistent.json", "--target", "slim",
                     "--distro", "ubuntu"]) == 1
    assert "cannot read plan" in capsys.readouterr().err


GIT_RELEASE = {
    "tag": "v1.9.4",
    "title": "whisper.cpp 1.9.4 source",
    "url": None,
    "sha256": None,
    "signature": None,
    "git": {"url": "https://github.com/ggml-org/whisper.cpp.git", "tag": "v1.9.4", "commit": "c" * 40, "archive": "whisper.cpp-1.9.4"},
    "extras": [
        {
            "name": "gts",
            "version": "0.7.6",
            "url": "https://downloads.sourceforge.net/project/gts/gts/0.7.6/gts-0.7.6.tar.gz",
            "sha256": hashlib.sha256(b"gts").hexdigest(),
            "signature": None,
            "distros": ["alpine"],
            "pinned_by_hand": True,
        },
        {
            "name": "docs",
            "version": "1.9.4",
            "url": "https://example.org/docs-1.9.4.tar.gz",
            "sha256": hashlib.sha256(b"docs").hexdigest(),
            "signature": "https://example.org/docs-1.9.4.tar.gz.asc",
            "distros": [],
            "pinned_by_hand": False,
        },
    ],
}
DOWNLOADS = {
    "https://downloads.sourceforge.net/project/gts/gts/0.7.6/gts-0.7.6.tar.gz": b"gts",
    "https://example.org/docs-1.9.4.tar.gz": b"docs",
    "https://example.org/docs-1.9.4.tar.gz.asc": b"sig",
}


def fake_archive(calls):
    def archive(url, tag, commit, stem, dest, work):
        calls.append((url, tag, commit, stem, dest.name))
        dest.write_bytes(b"archive")
        return hashlib.sha256(b"archive").hexdigest()

    return archive


def test_fetch_git_archive_and_extra_artifacts(tmp_path):
    calls = []
    files = release.fetch(GIT_RELEASE, tmp_path, download=fake_download(DOWNLOADS), archive=fake_archive(calls))
    assert calls == [("https://github.com/ggml-org/whisper.cpp.git", "v1.9.4", "c" * 40, "whisper.cpp-1.9.4", "whisper.cpp-1.9.4.tar.gz")]
    assert [f.path.name for f in files] == ["whisper.cpp-1.9.4.tar.gz", "gts-0.7.6.tar.gz", "docs-1.9.4.tar.gz", "docs-1.9.4.tar.gz.asc"]
    assert all(f.path.parent == tmp_path / "assets" for f in files)
    notes = release.notes({"image": "ghcr.io/randomcontainers/whisper-cpp"}, GIT_RELEASE, files)
    assert notes == (
        "Upstream source of whisper.cpp 1.9.4, as built into the images from this repository.\n\n"
        f"- `whisper.cpp-1.9.4.tar.gz`: sha256 `{hashlib.sha256(b'archive').hexdigest()}`, `git archive` of commit `{'c' * 40}`, "
        "tag `v1.9.4` of https://github.com/ggml-org/whisper.cpp.git\n"
        f"- `gts-0.7.6.tar.gz`: sha256 `{hashlib.sha256(b'gts').hexdigest()}`, gts 0.7.6, only in the Alpine images, "
        "from https://downloads.sourceforge.net/project/gts/gts/0.7.6/gts-0.7.6.tar.gz\n"
        f"- `docs-1.9.4.tar.gz`: sha256 `{hashlib.sha256(b'docs').hexdigest()}`, docs 1.9.4, from https://example.org/docs-1.9.4.tar.gz\n"
        "- `docs-1.9.4.tar.gz.asc`: upstream signature of docs, from https://example.org/docs-1.9.4.tar.gz.asc\n\n"
        "The list is from the newest build of this version. Files attached for earlier builds stay attached.\n\n"
        "Images: `ghcr.io/randomcontainers/whisper-cpp`\n"
    )


def test_fetch_rejects_a_changed_extra_artifact(tmp_path):
    downloads = {**DOWNLOADS, "https://example.org/docs-1.9.4.tar.gz": b"other"}
    with pytest.raises(RcError, match="docs-1.9.4.tar.gz has sha256 .*, but package.yml pins"):
        release.fetch(GIT_RELEASE, tmp_path, download=fake_download(downloads), archive=fake_archive([]))
    assert not (tmp_path / "assets" / "docs-1.9.4.tar.gz").exists()


@pytest.mark.parametrize(
    "change,message",
    [
        (lambda r: r["git"].update(url="https://github.com/ggml-org/whisper.cpp.git --upload-pack=x"), "invalid git source"),
        (lambda r: r["git"].update(tag="--upload-pack=x"), "invalid git source"),
        (lambda r: r["git"].update(commit="HEAD"), "invalid git source"),
        (lambda r: r["git"].update(archive="../x"), "invalid git source"),
        (lambda r: r["extras"][0].update(sha256=None), "invalid sha256"),
        (lambda r: r["extras"][1].update(url="https://example.org/gts-0.7.6.tar.gz"), "both be named gts-0.7.6.tar.gz"),
        (lambda r: r["extras"][1].update(url="https://example.org/whisper.cpp-1.9.4.tar.gz"), "both be named whisper.cpp-1.9.4.tar.gz"),
    ],
)
def test_fetch_checks_the_plan_first(tmp_path, change, message):
    info = copy.deepcopy(GIT_RELEASE)
    change(info)
    calls = []
    with pytest.raises(RcError, match=message):
        release.fetch(info, tmp_path, download=fake_download(DOWNLOADS), archive=fake_archive(calls))
    assert calls == []


def git(repo, *args):
    env = {"PATH": os.environ["PATH"], "HOME": str(repo), "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org"}
    return subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True).stdout.strip()


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_git_archive_of_the_pinned_commit(tmp_path, monkeypatch):
    upstream_repo = tmp_path / "upstream"
    upstream_repo.mkdir()
    git(upstream_repo, "init", "--quiet", "--initial-branch=main")
    (upstream_repo / "README").write_text("tool\n")
    (upstream_repo / "tests").mkdir()
    (upstream_repo / "tests" / "data.txt").write_text("$Format:%H$\n")
    (upstream_repo / ".gitattributes").write_text("tests export-ignore\n*.txt export-subst\n")
    git(upstream_repo, "add", ".")
    git(upstream_repo, "commit", "--quiet", "-m", "one")
    git(upstream_repo, "tag", "-a", "v1.0", "-m", "1.0")
    commit = git(upstream_repo, "rev-parse", "HEAD")
    monkeypatch.setattr(release, "GIT_PROTOCOL", "file")
    url = f"file://{upstream_repo}"

    dest = tmp_path / "tool-1.0.tar.gz"
    digest = release.git_archive(url, "v1.0", commit, "tool-1.0", dest, tmp_path / "work")
    assert digest == hashlib.sha256(dest.read_bytes()).hexdigest()
    with tarfile.open(dest) as tar:
        members = {m.name: m for m in tar.getmembers()}
        assert {"tool-1.0/README", "tool-1.0/.gitattributes", "tool-1.0/tests/data.txt"} <= set(members)
        # export-subst is off, so the file is the one in the commit
        assert tar.extractfile(members["tool-1.0/tests/data.txt"]).read() == b"$Format:%H$\n"

    # a tag moved to another commit is refused
    (upstream_repo / "README").write_text("changed\n")
    git(upstream_repo, "commit", "--quiet", "-am", "two")
    git(upstream_repo, "tag", "-f", "-a", "v1.0", "-m", "moved")
    with pytest.raises(RcError, match=f"tag v1.0 at {url} is commit [0-9a-f]{{40}}, but package.yml pins {commit}"):
        release.git_archive(url, "v1.0", commit, "tool-1.0", dest, tmp_path / "work")
    with pytest.raises(RcError, match="git fetch failed"):
        release.git_archive(url, "v2.0", commit, "tool-2.0", dest, tmp_path / "work")
    # only the configured protocol is allowed
    monkeypatch.setattr(release, "GIT_PROTOCOL", "https")
    with pytest.raises(RcError, match="transport 'file' not allowed"):
        release.git_archive(url, "v1.0", commit, "tool-1.0", dest, tmp_path / "work")
