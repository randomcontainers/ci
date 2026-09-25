from rc import repofiles

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
