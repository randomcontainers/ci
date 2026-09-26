import dataclasses
import json

import pytest
import yaml

from conftest import FIXTURES, SHA, make_planner, publish_bases, publish_slim
from rc import plan as planmod
from rc.config import ComboFile, parse_package
from rc.errors import RcError


def targets_by_id(document):
    return {t["id"]: t for t in document["targets"]}


def test_package_with_ready_members(distros, listed, packages, fake):
    bases = publish_bases(fake)
    ffmpeg = publish_slim(fake, "ffmpeg", "9.0.2")
    planner = make_planner(distros, listed, fake, "randomcontainers/yt-dlp")
    doc = planner.plan_package(packages["yt-dlp"])
    planmod.check(doc)
    t = targets_by_id(doc)
    assert list(t) == ["slim-ubuntu", "default-ubuntu", "slim-alpine", "default-alpine"]
    assert doc["publish"] is True and doc["release"] is None

    slim = t["slim-ubuntu"]
    assert slim["build"]["args"]["BASE_IMAGE"] == f"ubuntu:26.04@{bases['ubuntu']}"
    assert slim["build"]["args"]["VERSION"] == "2026.08.19"
    assert slim["labels"]["org.opencontainers.image.base.digest"] == bases["ubuntu"]
    assert slim["labels"]["org.opencontainers.image.base.name"] == "docker.io/library/ubuntu:26.04"
    assert slim["labels"]["com.randomcontainers.variant"] == "slim"
    assert slim["labels"]["org.opencontainers.image.revision"] == SHA
    assert slim["labels"]["org.opencontainers.image.created"] == "2026-09-24T12:00:00Z"
    assert slim["tags"][0] == "slim" and "latest" not in slim["tags"]
    assert slim["expect"]["env"] == {"LANG": "C.UTF-8", "XDG_CACHE_HOME": "/cache", "PYTHONUNBUFFERED": "1"}
    assert slim["tests"][0]["require_output"] is True

    default = t["default-alpine"]
    assert default["build"]["contexts"] == {
        "yt-dlp": "target:slim",
        "ffmpeg": f"docker-image://ghcr.io/randomcontainers/ffmpeg@{ffmpeg['alpine']}",
    }
    labels = default["labels"]
    assert labels["org.opencontainers.image.licenses"] == "Unlicense AND GPL-3.0-or-later AND MIT"
    assert labels["com.randomcontainers.variant"] == "default"
    assert labels["org.opencontainers.image.base.name"] == "ghcr.io/randomcontainers/ffmpeg:slim-alpine"
    assert labels["org.opencontainers.image.base.digest"] == ffmpeg["alpine"]
    assert json.loads(labels["com.randomcontainers.members"]) == {"ffmpeg": {"version": "9.0.2", "digest": ffmpeg["alpine"]}}
    assert default["expect"]["packages"] == {"yt-dlp": "2026.08.19", "ffmpeg": "9.0.2"}
    assert default["expect"]["extra_packages"] == ["deno"]
    assert [g["package"] for g in default["tests"]] == ["yt-dlp", "ffmpeg", "yt-dlp-ffmpeg"]
    assert default["tests"][1]["version"] == "9.0.2"
    assert default["tests"][2]["require_output"] is False
    assert default["tags"] == ["alpine", "2026.08.19-alpine", "2026.08.19-alpine3.24"]
    assert t["default-ubuntu"]["tags"][:2] == ["latest", "2026.08.19"]
    assert "FROM ffmpeg AS base" in default["build"]["dockerfile_text"]

    # each base tag was resolved once
    heads = [u for m, u, h in fake.requests if m == "HEAD" and "library/ubuntu" in u and "Authorization" in h]
    assert len(heads) == 1


def test_member_not_public_skips_default(distros, listed, packages, fake):
    publish_bases(fake)
    publish_slim(fake, "ffmpeg", "9.0.2")
    fake.private.add(("ghcr.io", "randomcontainers/ffmpeg"))
    planner = make_planner(distros, listed, fake, "randomcontainers/yt-dlp")
    doc = planner.plan_package(packages["yt-dlp"])
    assert [t["id"] for t in doc["targets"]] == ["slim-ubuntu", "slim-alpine"]
    assert doc["skipped"] == [{"flavour": "default", "distro": "ubuntu"}, {"flavour": "default", "distro": "alpine"}]
    assert any("cannot be pulled anonymously" in n for n in planner.notices)


def test_distro_gate(distros, listed, packages, fake):
    publish_bases(fake)
    publish_slim(fake, "ffmpeg", "9.0.2", distros=(("ubuntu", "26.04"), ("alpine", "3.23")))
    planner = make_planner(distros, listed, fake, "randomcontainers/yt-dlp")
    doc = planner.plan_package(packages["yt-dlp"])
    assert [t["id"] for t in doc["targets"]] == ["slim-ubuntu", "default-ubuntu", "slim-alpine"]
    assert any("built on alpine 3.23, not 3.24" in n for n in planner.notices)


def test_member_not_built_for_distro(distros, listed, packages, fake):
    publish_bases(fake)
    publish_slim(fake, "ffmpeg", "9.0.2", distros=(("ubuntu", "26.04"),))
    planner = make_planner(distros, listed, fake, "randomcontainers/yt-dlp")
    doc = planner.plan_package(packages["yt-dlp"])
    assert "default-alpine" not in targets_by_id(doc)
    assert any("does not exist yet" in n for n in planner.notices)


def test_package_without_default_combo(distros, listed, packages, fake):
    publish_bases(fake)
    planner = make_planner(distros, listed, fake, "randomcontainers/ffmpeg")
    doc = planner.plan_package(packages["ffmpeg"])
    t = targets_by_id(doc)
    assert list(t) == ["slim-ubuntu", "slim-alpine"]
    ubuntu = t["slim-ubuntu"]["tags"]
    assert {"latest", "9.0.2", "9.0", "9", "ubuntu", "slim", "9.0.2-slim-ubuntu26.04", "9.0.2-ubuntu26.04"} <= set(ubuntu)
    assert t["slim-ubuntu"]["primary"] == "9.0.2-slim-ubuntu26.04"
    assert t["slim-ubuntu"]["build"]["args"]["SOURCE_SHA256"] == packages["ffmpeg"].upstream.artifact.sha256
    assert doc["release"] == {
        "tag": "v9.0.2",
        "title": "FFmpeg 9.0.2 source",
        "url": "https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz",
        "sha256": packages["ffmpeg"].upstream.artifact.sha256,
        "signature": "https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz.asc",
        "git": None,
        "extras": [],
    }


def test_pull_request_builds_but_does_not_publish(distros, listed, packages, fake):
    publish_bases(fake)
    planner = make_planner(distros, listed, fake, "randomcontainers/ffmpeg", event="pull_request", ref="refs/pull/1/merge")
    doc = planner.plan_package(packages["ffmpeg"])
    assert doc["publish"] is False and doc["release"] is None and doc["targets"]


def test_default_only(distros, listed, packages, fake):
    publish_bases(fake)
    ffmpeg = publish_slim(fake, "ffmpeg", "9.0.2")
    own = publish_slim(fake, "yt-dlp", "2026.07.04")
    planner = make_planner(distros, listed, fake, "randomcontainers/yt-dlp", event="workflow_dispatch", default_only=True)
    doc = planner.plan_package(packages["yt-dlp"])
    t = targets_by_id(doc)
    assert list(t) == ["default-ubuntu", "default-alpine"]
    ubuntu = t["default-ubuntu"]
    assert ubuntu["build"]["contexts"]["yt-dlp"] == f"docker-image://ghcr.io/randomcontainers/yt-dlp@{own['ubuntu']}"
    # the default image carries the version of the slim image it is built on
    assert ubuntu["version"] == "2026.07.04"
    assert "2026.07.04" in ubuntu["tags"]
    assert json.loads(ubuntu["labels"]["com.randomcontainers.members"]) == {
        "ffmpeg": {"version": "9.0.2", "digest": ffmpeg["ubuntu"]}
    }
    assert any("package.yml is at 2026.08.19" in n for n in planner.notices)


def test_default_only_without_combo(distros, listed, packages, fake):
    publish_bases(fake)
    planner = make_planner(distros, listed, fake, "randomcontainers/ffmpeg", default_only=True)
    doc = planner.plan_package(packages["ffmpeg"])
    assert doc["targets"] == []
    assert planmod.build_matrix(doc) == {"include": []}


def test_combo_repository(distros, listed, packages, fake):
    publish_bases(fake)
    im = publish_slim(fake, "imagemagick", "7.1.2-31")
    gs = publish_slim(fake, "ghostscript", "10.08.0")
    planner = make_planner(distros, listed, fake, "randomcontainers/imagemagick-ghostscript", event="workflow_dispatch")
    doc = planner.plan_combo(ComboFile("imagemagick-ghostscript", "imagemagick", ("ghostscript",)))
    assert doc["kind"] == "combo" and doc["image"] == "ghcr.io/randomcontainers/imagemagick-ghostscript"
    t = targets_by_id(doc)
    ubuntu = t["default-ubuntu"]
    assert ubuntu["tags"] == ["latest", "7.1.2-31", "ubuntu", "7.1.2-31-ubuntu", "7.1.2-31-ubuntu26.04"]
    assert ubuntu["build"]["contexts"] == {
        "imagemagick": f"docker-image://ghcr.io/randomcontainers/imagemagick@{im['ubuntu']}",
        "ghostscript": f"docker-image://ghcr.io/randomcontainers/ghostscript@{gs['ubuntu']}",
    }
    labels = ubuntu["labels"]
    assert labels["com.randomcontainers.variant"] == "combo"
    assert labels["com.randomcontainers.package"] == "imagemagick"
    assert labels["org.opencontainers.image.title"] == "ImageMagick + Ghostscript"
    assert labels["org.opencontainers.image.url"] == "https://randomcontainers.com/imagemagick-ghostscript/"
    assert set(json.loads(labels["com.randomcontainers.members"])) == {"imagemagick", "ghostscript"}
    assert labels["org.opencontainers.image.licenses"] == "ImageMagick AND AGPL-3.0-or-later"


def test_combo_repository_checks_its_name(distros, listed, fake):
    planner = make_planner(distros, listed, fake, "randomcontainers/something-else")
    with pytest.raises(RcError, match="does not match the repository name"):
        planner.plan_combo(ComboFile("imagemagick-ghostscript", "imagemagick", ("ghostscript",)))


def test_removed_combo(distros, listed, fake):
    planner = make_planner(distros, listed, fake, "randomcontainers/ffmpeg-ghostscript")
    with pytest.raises(RcError, match="no longer declares"):
        planner.plan_combo(ComboFile("ffmpeg-ghostscript", "ffmpeg", ("ghostscript",)))


def test_package_name_must_match_repository(distros, listed, packages, fake):
    planner = make_planner(distros, listed, fake, "randomcontainers/not-ffmpeg")
    with pytest.raises(RcError, match="does not match the repository name"):
        planner.plan_package(packages["ffmpeg"])


def test_matrices(distros, listed, packages, fake):
    publish_bases(fake)
    publish_slim(fake, "ffmpeg", "9.0.2", distros=(("ubuntu", "26.04"),))
    doc = make_planner(distros, listed, fake, "randomcontainers/yt-dlp").plan_package(packages["yt-dlp"])
    legs = planmod.build_matrix(doc)["include"]
    assert [(leg["distro"], leg["arch"], leg["runner"], leg["slim"], leg["default"]) for leg in legs] == [
        ("ubuntu", "amd64", "ubuntu-24.04", True, True),
        ("ubuntu", "arm64", "ubuntu-24.04-arm", True, True),
        ("alpine", "amd64", "ubuntu-24.04", True, False),
        ("alpine", "arm64", "ubuntu-24.04-arm", True, False),
    ]
    merges = planmod.merge_matrix(doc)["include"]
    assert merges[0] == {
        "flavour": "slim",
        "distro": "ubuntu",
        "ref": "ghcr.io/randomcontainers/yt-dlp:2026.08.19-slim-ubuntu26.04",
    }
    assert len(merges) == 3


def test_offline_plan(distros, listed, packages):
    from conftest import FIXTURES, NOW
    from rc.plan import Planner, RunContext
    from rc.sources import PackageSource

    run = RunContext("randomcontainers/yt-dlp", SHA, "refs/heads/local", "local", now=NOW)
    planner = Planner(
        distros, listed, PackageSource(distros, local_dir=FIXTURES), None, run, member_images={"ffmpeg": "rclocal/ffmpeg:slim"}
    )
    doc = planner.plan_package(packages["yt-dlp"])
    t = targets_by_id(doc)
    assert t["slim-ubuntu"]["build"]["args"]["BASE_IMAGE"] == "ubuntu:26.04"
    assert t["default-ubuntu"]["build"]["contexts"]["ffmpeg"] == "docker-image://rclocal/ffmpeg:slim"
    assert doc["publish"] is False


def test_check_rejects_tampered_plan(distros, listed, packages, fake):
    publish_bases(fake)
    doc = make_planner(distros, listed, fake, "randomcontainers/ffmpeg").plan_package(packages["ffmpeg"])
    doc["targets"][0]["tags"].append("bad tag")
    with pytest.raises(RcError):
        planmod.check(doc)
    doc["targets"][0]["tags"].pop()
    doc["image"] = "ghcr.io/elsewhere/ffmpeg"
    with pytest.raises(RcError):
        planmod.check(doc)


def test_load_reports_a_missing_or_empty_plan(tmp_path):
    with pytest.raises(RcError, match="no plan at"):
        planmod.load(tmp_path / "plan.json")
    (tmp_path / "plan.json").write_text("\n")
    with pytest.raises(RcError, match="is empty"):
        planmod.load(tmp_path / "plan.json")
    (tmp_path / "plan.json").write_text("{")
    with pytest.raises(RcError, match="cannot read plan"):
        planmod.load(tmp_path / "plan.json")


def ffmpeg_with_combo(distros, combo):
    data = yaml.safe_load((FIXTURES / "ffmpeg" / "package.yml").read_text())
    data["combos"] = [combo]
    return parse_package(data, "ffmpeg/package.yml", distros)


def test_combo_with_catalog_problems_is_refused(distros, listed, fake):
    publish_bases(fake)
    publish_slim(fake, "yt-dlp", "2026.08.19")
    publish_slim(fake, "ffmpeg", "9.0.2")
    planner = make_planner(distros, listed, fake, "randomcontainers/yt-dlp-ffmpeg", event="workflow_dispatch")
    # Another package now declares a combo of the same two packages.
    planner.source.add(ffmpeg_with_combo(distros, {"with": ["yt-dlp"], "summary": "FFmpeg and yt-dlp.", "test": ["true"]}))
    with pytest.raises(RcError, match=r"refusing to build yt-dlp-ffmpeg: .*same members as"):
        planner.plan_combo(ComboFile("yt-dlp-ffmpeg", "yt-dlp", ("ffmpeg",)))


def test_combo_env_conflict_is_refused(distros, listed, fake):
    planner = make_planner(distros, listed, fake, "randomcontainers/yt-dlp-ffmpeg", event="workflow_dispatch")
    ffmpeg = planner.source.load("ffmpeg")
    planner.source.add(dataclasses.replace(ffmpeg, image=dataclasses.replace(ffmpeg.image, env=(("DENO_DIR", "/tmp"),))))
    with pytest.raises(RcError, match="refusing to build yt-dlp-ffmpeg: .*DENO_DIR"):
        planner.plan_combo(ComboFile("yt-dlp-ffmpeg", "yt-dlp", ("ffmpeg",)))


def test_package_default_with_catalog_problems_is_skipped(distros, listed, packages, fake):
    publish_bases(fake)
    publish_slim(fake, "ffmpeg", "9.0.2")
    planner = make_planner(distros, listed, fake, "randomcontainers/yt-dlp")
    planner.source.add(ffmpeg_with_combo(distros, {"with": ["yt-dlp"], "summary": "FFmpeg and yt-dlp.", "test": ["true"]}))
    doc = planner.plan_package(packages["yt-dlp"])
    assert [t["id"] for t in doc["targets"]] == ["slim-ubuntu", "slim-alpine"]
    assert doc["skipped"] == [{"flavour": "default", "distro": "ubuntu"}, {"flavour": "default", "distro": "alpine"}]
    # The slim images keep their own tags; latest stays with the default image.
    assert "latest" not in targets_by_id(doc)["slim-ubuntu"]["tags"]
    assert len(planner.errors) == 1
    assert planner.errors[0].startswith("Not building the yt-dlp default image: ")
    assert "same members as" in planner.errors[0]


def test_unreadable_listed_package_is_a_warning(distros, packages, fake):
    publish_bases(fake)
    publish_slim(fake, "ffmpeg", "9.0.2")
    listed = ("yt-dlp", "ffmpeg", "not-there")
    planner = make_planner(distros, listed, fake, "randomcontainers/yt-dlp")
    doc = planner.plan_package(packages["yt-dlp"])
    assert "default-ubuntu" in targets_by_id(doc)
    assert any(w.startswith("yt-dlp-ffmpeg was not checked against not-there") for w in planner.warnings)
    assert planner.errors == []


def test_git_source_and_extra_artifacts(distros, listed, packages, fake):
    publish_bases(fake)
    data = yaml.safe_load((FIXTURES / "ffmpeg" / "package.yml").read_text())
    data["upstream"].pop("artifact")
    data["upstream"]["git"] = {"url": "https://github.com/FFmpeg/FFmpeg.git", "tag": "n{version}", "commit": "c" * 40}
    data["upstream"]["extra-artifacts"] = {
        "gts": {
            "version": "0.7.6",
            "url": "https://downloads.sourceforge.net/project/gts/gts/{version}/gts-{version}.tar.gz",
            "signature": "https://example.org/gts-{version}.tar.gz.sig",
            "sha256": "a" * 64,
            "distros": ["alpine"],
        },
        "nv-codec": {"url": "https://example.org/nv-codec-{version}.tar.gz", "signature": "https://example.org/nv-codec-{version}.tar.gz.asc", "sha256": "b" * 64},
    }
    package = parse_package(data, "package.yml", distros)
    doc = make_planner(distros, listed, fake, "randomcontainers/ffmpeg").plan_package(package)
    t = targets_by_id(doc)
    common = {"VERSION": "9.0.2", "SOURCE_COMMIT": "c" * 40, "NV_CODEC_VERSION": "9.0.2",
              "NV_CODEC_URL": "https://example.org/nv-codec-9.0.2.tar.gz", "NV_CODEC_SHA256": "b" * 64}
    ubuntu = t["slim-ubuntu"]["build"]["args"]
    alpine = t["slim-alpine"]["build"]["args"]
    assert {k: v for k, v in ubuntu.items() if k != "BASE_IMAGE"} == common
    assert {k: v for k, v in alpine.items() if k != "BASE_IMAGE"} == common | {
        "GTS_VERSION": "0.7.6",
        "GTS_URL": "https://downloads.sourceforge.net/project/gts/gts/0.7.6/gts-0.7.6.tar.gz",
        "GTS_SHA256": "a" * 64,
    }
    assert "SOURCE_SHA256" not in ubuntu
    assert doc["release"] == {
        "tag": "v9.0.2",
        "title": "FFmpeg 9.0.2 source",
        "url": None,
        "sha256": None,
        "signature": None,
        "git": {"url": "https://github.com/FFmpeg/FFmpeg.git", "tag": "n9.0.2", "commit": "c" * 40, "archive": "FFmpeg-9.0.2"},
        "extras": [
            {
                "name": "gts",
                "version": "0.7.6",
                "url": "https://downloads.sourceforge.net/project/gts/gts/0.7.6/gts-0.7.6.tar.gz",
                "sha256": "a" * 64,
                "signature": "https://example.org/gts-0.7.6.tar.gz.sig",
                "distros": ["alpine"],
                "pinned_by_hand": True,
            },
            {
                "name": "nv-codec",
                "version": "9.0.2",
                "url": "https://example.org/nv-codec-9.0.2.tar.gz",
                "sha256": "b" * 64,
                "signature": "https://example.org/nv-codec-9.0.2.tar.gz.asc",
                "distros": [],
                "pinned_by_hand": False,
            },
        ],
    }
    planmod.check(doc)
