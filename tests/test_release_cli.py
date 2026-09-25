import hashlib
import json

import pytest

from conftest import FIXTURES, ROOT
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
    assert [f.name for f in files] == ["ffmpeg-9.0.2.tar.xz", "ffmpeg-9.0.2.tar.xz.asc"]
    notes = release.notes({"image": "ghcr.io/randomcontainers/ffmpeg"}, RELEASE, files)
    assert RELEASE["sha256"] in notes and "FFmpeg 9.0.2" in notes


def test_fetch_rejects_wrong_tarball(tmp_path):
    with pytest.raises(RcError, match="package.yml pins"):
        release.fetch(RELEASE, tmp_path, download=fake_download({RELEASE["url"]: b"other"}))
    assert not (tmp_path / "assets" / "ffmpeg-9.0.2.tar.xz").exists()


def test_asset_name():
    assert release.asset_name("https://e.org/a/b/ghostscript-10.08.0.tar.xz") == "ghostscript-10.08.0.tar.xz"
    with pytest.raises(RcError):
        release.asset_name("https://e.org/")


def test_cli_validate_catalog(capsys):
    code = cli.main(["--ci-dir", str(ROOT), "validate", *map(str, sorted(FIXTURES.iterdir())), "--catalog",
                     "--packages-dir", str(FIXTURES)])
    out = capsys.readouterr().out
    assert code == 0, out
    assert "ok: catalog of 5 packages" in out


def test_cli_validate_reports_problems(tmp_path, capsys):
    bad = tmp_path / "package.yml"
    bad.write_text((FIXTURES / "ffmpeg" / "package.yml").read_text().replace("name: ffmpeg", "name: ci"))
    assert cli.main(["--ci-dir", str(ROOT), "validate", str(bad)]) == 1
    assert "reserved name" in capsys.readouterr().err


def test_cli_render_repo_files(tmp_path, capsys):
    out = tmp_path / "combo"
    code = cli.main(["--ci-dir", str(ROOT), "render", "--package-file", str(FIXTURES / "yt-dlp" / "package.yml"),
                     "--packages-dir", str(FIXTURES), "--repo-files", str(out)])
    assert code == 0
    assert (out / "Dockerfile.alpine").is_file() and (out / ".github" / "workflows" / "build.yml").is_file()
    code = cli.main(["--ci-dir", str(ROOT), "render", "--combo-file", str(out / "combo.yml"),
                     "--packages-dir", str(FIXTURES), "--distro", "alpine"])
    assert code == 0
    assert capsys.readouterr().out.endswith((out / "Dockerfile.alpine").read_text())


def test_cli_tags_json(capsys):
    code = cli.main(["--ci-dir", str(ROOT), "tags", "--package-file", str(FIXTURES / "ffmpeg" / "package.yml"),
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
    code = cli.main(["--ci-dir", str(ROOT), "plan", "--source", str(source), "--offline",
                     "--packages-dir", str(FIXTURES)])
    assert code == 1
    assert "Dockerfile.alpine is missing" in capsys.readouterr().err


def test_old_distro_release_warns_in_plan_and_fails_validate(tmp_path, monkeypatch, capsys):
    for key in ("GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_OUTPUT", "GITHUB_SHA", "GITHUB_REF", "GITHUB_EVENT_NAME"):
        monkeypatch.delenv(key, raising=False)
    source = package_checkout(tmp_path / "src", "yt-dlp")
    (source / "README.md").write_text("Built on Ubuntu 26.04 and Alpine 3.23.\n")
    code = cli.main(["--ci-dir", str(ROOT), "plan", "--source", str(source), "--offline",
                     "--packages-dir", str(FIXTURES), "--member-image", "ffmpeg=rclocal/ffmpeg:slim"])
    assert code == 0
    assert "warning: README.md names alpine 3.23, but distros.yml builds on 3.24" in capsys.readouterr().err
    assert cli.main(["--ci-dir", str(ROOT), "validate", str(source), "--files"]) == 1
    assert "README.md names alpine 3.23" in capsys.readouterr().err


def test_cli_offline_plan_and_bake(tmp_path, monkeypatch, capsys):
    for key in ("GITHUB_REPOSITORY", "GITHUB_OUTPUT", "GITHUB_SHA", "GITHUB_REF", "GITHUB_EVENT_NAME"):
        monkeypatch.delenv(key, raising=False)
    plan_file = tmp_path / "plan.json"
    source = package_checkout(tmp_path / "src", "yt-dlp")
    code = cli.main(["--ci-dir", str(ROOT), "plan", "--source", str(source), "--offline",
                     "--packages-dir", str(FIXTURES), "--member-image", "ffmpeg=rclocal/ffmpeg:slim",
                     "--output-file", str(plan_file)])
    assert code == 0
    out = capsys.readouterr().out
    assert "build=true" in out and "publish=false" in out
    monkeypatch.chdir(tmp_path)
    code = cli.main(["--ci-dir", str(ROOT), "bake", "--plan", str(plan_file), "--distro", "ubuntu",
                     "--platform", "linux/amd64", "--mode", "load", "--output", "bake.json"])
    assert code == 0
    doc = json.loads((tmp_path / "bake.json").read_text())
    assert doc["target"]["default"]["contexts"]["ffmpeg"] == "docker-image://rclocal/ffmpeg:slim"
    assert (tmp_path / ".rc-build" / "ubuntu-default" / "Dockerfile").is_file()


def test_cli_errors_are_reported(capsys):
    assert cli.main(["--ci-dir", str(ROOT), "check-image", "--plan", "/nonexistent.json", "--target", "slim",
                     "--distro", "ubuntu"]) == 1
    assert "cannot read plan" in capsys.readouterr().err
