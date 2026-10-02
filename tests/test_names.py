import re

import pytest

from rc import names


@pytest.mark.parametrize(
    "expr",
    ["MIT", "GPL-3.0-or-later", "Unlicense AND GPL-3.0-or-later AND MIT", "(MIT OR Apache-2.0) AND BSD-3-Clause",
     "GPL-2.0-or-later WITH Classpath-exception-2.0", "LicenseRef-ImageMagick", "ImageMagick"],
)
def test_spdx_valid(expr):
    assert names.check_spdx(expr) is None


@pytest.mark.parametrize("expr", ["", "MIT AND", "AND MIT", "(MIT", "MIT)", "MIT OR OR BSD", "MIT; rm -rf", "and"])
def test_spdx_invalid(expr):
    assert names.check_spdx(expr) is not None


def test_spdx_and():
    assert names.spdx_and(["Unlicense", "GPL-3.0-or-later", "MIT"]) == "Unlicense AND GPL-3.0-or-later AND MIT"
    assert names.spdx_and(["MIT", "MIT OR Apache-2.0"]) == "MIT AND (MIT OR Apache-2.0)"
    assert names.spdx_and(["ImageMagick", "AGPL-3.0-or-later AND MIT", "MIT"]) == "ImageMagick AND AGPL-3.0-or-later AND MIT"
    assert names.spdx_and(["(MIT OR BSD-2-Clause)"]) == "(MIT OR BSD-2-Clause)"


@pytest.mark.parametrize(
    "command,bad",
    [
        ('docker run --rm -v "$PWD:/work" ghcr.io/randomcontainers/yt-dlp URL', True),
        ('docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/work" ghcr.io/randomcontainers/yt-dlp URL', False),
        ('podman run --rm --userns=keep-id -v "$PWD:/work" ghcr.io/randomcontainers/ffmpeg -version', False),
        ('docker run --rm --mount type=bind,src=.,dst=/work ghcr.io/randomcontainers/ffmpeg -i a b', True),
        ("docker run --rm ghcr.io/randomcontainers/ffmpeg -version", False),
        ("docker run --rm ghcr.io/randomcontainers/yt-dlp -v URL", False),
        ('docker run --rm -v"$PWD:/work" ghcr.io/randomcontainers/yt-dlp URL', True),
    ],
)
def test_mounts_without_user(command, bad):
    assert names.mounts_without_user(command) is bad


def test_check_name():
    assert names.check_name("yt-dlp") is None
    assert "reserved" in names.check_name("ci")
    assert names.check_name("a" * 65) is not None
    assert names.check_name("-x") is not None


@pytest.mark.parametrize(
    "family,value",
    [
        ("alpine", "deno"),
        ("alpine", "libSvtAv1Enc"),
        ("alpine", "so:libSvtAv1Enc.so.4"),
        ("alpine", "so:libstdc++.so.6"),
        ("alpine", "py3-pip_extra"),
        ("debian", "fonts-dejavu-core"),
        ("debian", "libstdc++6"),
        ("debian", "libavcodec61t64"),
    ],
)
def test_distro_package_valid(family, value):
    assert names.is_distro_package(value, family)
    assert re.fullmatch(names.distro_package_ere(family), value)


@pytest.mark.parametrize(
    "family,value",
    [
        ("debian", "libSvtAv1Enc"),
        ("debian", "so:libfoo.so.1"),
        ("debian", "lib_foo"),
        ("alpine", "so:"),
        ("alpine", "cmd:ffmpeg"),
    ]
    + [
        (family, value)
        for family in ("alpine", "debian")
        for value in (
            "",
            "-rf",
            "so:-rf",
            "deno; rm -rf /",
            "deno && id",
            "deno|id",
            "$(id)",
            "`id`",
            "a b",
            "deno\n",
            "deno\nid",
            "x>y",
            "deno=1.0",
            "deno:amd64",
        )
    ],
)
def test_distro_package_invalid(family, value):
    assert not names.is_distro_package(value, family)
    if value:
        assert not re.fullmatch(names.distro_package_ere(family), value)


def test_runtime_deps_ere_allows_blank_lines():
    assert re.fullmatch(names.distro_package_ere("alpine"), "")
    assert re.fullmatch(names.distro_package_ere("debian"), "")
