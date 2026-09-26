import yaml

from conftest import FIXTURES
from rc import repofiles
from rc.config import parse_package

GOOD = """# syntax=docker/dockerfile:1
ARG BASE_IMAGE
FROM --platform=$BUILDPLATFORM ${BASE_IMAGE} AS build
ARG VERSION
ARG SOURCE_SHA256
RUN true
FROM ${BASE_IMAGE} AS slim
"""


def write(root, files):
    root.mkdir(exist_ok=True)
    for name, text in files.items():
        (root / name).write_text(text)
    return root


def test_conforming_repository(tmp_path, packages, distros):
    root = write(tmp_path / "ffmpeg", {"Dockerfile.ubuntu": GOOD, "Dockerfile.alpine": GOOD})
    assert repofiles.package_problems(packages["ffmpeg"], root, distros) == []


def test_contract_problems(tmp_path, packages, distros):
    root = write(
        tmp_path / "ffmpeg",
        {"Dockerfile.ubuntu": GOOD.replace("ARG SOURCE_SHA256\n", "").replace("AS slim", "AS runtime")},
    )
    problems = repofiles.package_problems(packages["ffmpeg"], root, distros)
    assert problems == [
        "Dockerfile.ubuntu does not declare ARG SOURCE_SHA256",
        "Dockerfile.ubuntu: the last stage must be named slim",
        "Dockerfile.alpine is missing",
    ]


def test_combo_requirements_file(tmp_path, packages, distros):
    root = write(tmp_path / "yt-dlp", {"Dockerfile.ubuntu": GOOD, "Dockerfile.alpine": GOOD})
    problems = repofiles.package_problems(packages["yt-dlp"], root, distros)
    assert problems == ["combo yt-dlp-ffmpeg: requirements-deno.lock is missing"]
    (root / "requirements-deno.lock").write_text("")
    assert repofiles.package_problems(packages["yt-dlp"], root, distros) == []


def test_distro_releases_follow_distros_yml(tmp_path, distros):
    readme = "| `<version>-alpine3.24` | Alpine 3.24 |\n\nBuilt on Ubuntu 26.04 and Alpine 3.24.\n"
    files = {
        "Dockerfile.ubuntu": GOOD.replace("ARG BASE_IMAGE\n", "ARG BASE_IMAGE=ubuntu:26.04\n"),
        "Dockerfile.alpine": GOOD.replace("ARG BASE_IMAGE\n", "ARG BASE_IMAGE=alpine:3.24\n"),
        "README.md": readme,
    }
    root = write(tmp_path / "ffmpeg", files)
    assert repofiles.distro_problems(root, distros) == []

    files["Dockerfile.alpine"] = GOOD.replace("ARG BASE_IMAGE\n", "ARG BASE_IMAGE=alpine:3.23\n")
    files["README.md"] = readme.replace("alpine3.24", "alpine3.23")
    root = write(tmp_path / "ffmpeg", files)
    assert repofiles.distro_problems(root, distros) == [
        "Dockerfile.alpine: ARG BASE_IMAGE defaults to alpine:3.23, not alpine:3.24 from distros.yml",
        "README.md names alpine 3.23, but distros.yml builds on 3.24",
    ]


def test_git_and_extra_artifact_arguments(tmp_path, distros):
    data = yaml.safe_load((FIXTURES / "ffmpeg" / "package.yml").read_text())
    data["upstream"].pop("artifact")
    data["upstream"]["git"] = {"url": "https://github.com/FFmpeg/FFmpeg.git", "tag": "n{version}", "commit": "c" * 40}
    data["upstream"]["extra-artifacts"] = {
        "gts": {"version": "0.7.6", "url": "https://e.org/gts-{version}.tar.gz", "sha256": "a" * 64, "distros": ["alpine"]},
        "nv-codec": {"version": "13.0", "url": "https://e.org/nv-{version}.tar.gz", "sha256": "b" * 64},
    }
    package = parse_package(data, "package.yml", distros)
    plain = GOOD.replace("ARG SOURCE_SHA256\n", "")
    root = write(tmp_path / "ffmpeg", {"Dockerfile.ubuntu": plain, "Dockerfile.alpine": plain})
    assert repofiles.package_problems(package, root, distros) == [
        "Dockerfile.ubuntu does not declare ARG SOURCE_COMMIT",
        "Dockerfile.ubuntu does not declare ARG NV_CODEC_SHA256",
        "Dockerfile.alpine does not declare ARG SOURCE_COMMIT",
        "Dockerfile.alpine does not declare ARG GTS_SHA256",
        "Dockerfile.alpine does not declare ARG NV_CODEC_SHA256",
    ]
    ubuntu = plain.replace("ARG VERSION\n", "ARG VERSION\nARG SOURCE_COMMIT\nARG NV_CODEC_SHA256\n")
    alpine = ubuntu.replace("ARG VERSION\n", "ARG VERSION\nARG GTS_SHA256\n")
    root = write(tmp_path / "ffmpeg", {"Dockerfile.ubuntu": ubuntu, "Dockerfile.alpine": alpine})
    assert repofiles.package_problems(package, root, distros) == []
