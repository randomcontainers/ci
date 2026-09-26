import copy

import pytest
import yaml

from conftest import CI_DIR, FIXTURES, ROOT
from rc import config, yamlio
from rc.errors import RcError, ValidationError


def raw(name):
    return yaml.safe_load((FIXTURES / name / "package.yml").read_text())


def problems_for(data, distros):
    with pytest.raises(ValidationError) as exc:
        config.parse_package(data, "package.yml", distros)
    return "\n".join(exc.value.problems)


def test_fixtures_parse(packages):
    assert set(packages) == {"yt-dlp", "ffmpeg", "imagemagick", "ghostscript", "streamlink"}
    ytdlp = packages["yt-dlp"]
    combo = ytdlp.default_combo
    assert combo.name == "yt-dlp-ffmpeg"
    assert combo.members == ("yt-dlp", "ffmpeg")
    assert combo.base == "ffmpeg"
    assert combo.extras_for("alpine").packages == ("deno",)
    assert combo.extras_for("ubuntu").requirements == "requirements-deno.lock"
    assert ytdlp.image.env_map == {"PYTHONUNBUFFERED": "1"}
    assert packages["ffmpeg"].default_combo is None
    assert packages["imagemagick"].default_combo.base == "ghostscript"


def test_distros(distros):
    assert distros.default == "ubuntu"
    assert distros.ids == ("ubuntu", "alpine")
    ubuntu = distros.get("ubuntu")
    assert ubuntu.image == "ubuntu:26.04" and ubuntu.family == "debian" and ubuntu.version == "26.04"
    assert ubuntu.reference == "docker.io/library/ubuntu:26.04"
    assert ubuntu.qualified == "ubuntu26.04"


def test_distro_version_must_be_quoted():
    with pytest.raises(ValidationError) as exc:
        config.parse_distros(
            {"default": "ubuntu", "distros": {"ubuntu": {"image": "ubuntu:26.04", "family": "debian", "version": 26.04}}},
            "distros.yml",
        )
    assert "quote it" in str(exc.value)


def test_package_list():
    assert config.load_package_list(CI_DIR / "packages.yml") == ("yt-dlp", "ffmpeg", "imagemagick", "ghostscript", "streamlink")
    assert set(config.load_package_list(CI_DIR / "packages.yml")) <= set(config.load_package_list(ROOT / "packages.yml"))


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (lambda d: d.update(name="ci"), "reserved name"),
        (lambda d: d.update(name="Yt_Dlp"), "must match"),
        (lambda d: d.update(summary="x" * 161), "at most 160"),
        (lambda d: d.update(homepage="http://example.com"), "https URL"),
        (lambda d: d.update(license="Unlicense AND"), "incomplete"),
        (lambda d: d.update(extra=1), "unknown key 'extra'"),
        (lambda d: d["upstream"].update(version="2026.8.19"), "calendar version"),
        (lambda d: d["upstream"].update(version=2026.0819), "quote it"),
        (lambda d: d["upstream"].update(source="npm"), "must be one of"),
        (lambda d: d["upstream"].pop("publisher"), "trusted publisher"),
        (lambda d: d["upstream"].update(**{"version-template": "{2}"}), "does not have"),
        (lambda d: d["upstream"].update(**{"tag-pattern": "(\\d+"}), "not a valid regular expression"),
        (lambda d: d["upstream"].update(cooldown="1 day"), "duration"),
        (lambda d: d["image"].update(env={"LANG": "C"}), "cannot be overridden"),
        (lambda d: d["image"].update(env={"lower": "x"}), "not a valid variable name"),
        (lambda d: d["image"].update(env={"X": 1}), "must be a string"),
        (lambda d: d["image"].update(env={"X": "a\nb"}), "printable ASCII"),
        (lambda d: d["image"].update(entrypoint=[]), "must not be empty"),
        (lambda d: d.update(test=[]), "must not be empty"),
        (lambda d: d.update(test=[{"grep -q": "x"}]), "read as a mapping"),
        (
            lambda d: d["examples"].append({"title": "t", "command": 'docker run --rm -v "$PWD:/work" randomcontainers.com/yt-dlp -v URL'}),
            "must also pass --user",
        ),
        (lambda d: d["combos"].append(copy.deepcopy(d["combos"][0])), "declared twice"),
        (lambda d: d["combos"][0].update(**{"with": ["yt-dlp"]}), "combined with itself"),
        (lambda d: d["combos"][0].update(base="streamlink"), "must be the package itself"),
        (lambda d: d["combos"][0].pop("extras-license"), "need 'extras-license'"),
        (lambda d: d["combos"][0]["extras"].update(debian={"packages": ["x"]}), "not a distro"),
        (lambda d: d["combos"][0]["extras"]["alpine"].update(packages=["deno;rm"]), "not a valid package name"),
        (lambda d: d["combos"][0]["extras"]["ubuntu"].update(requirements="../x.lock"), "file name"),
        (lambda d: d["combos"][0].update(env={"PYTHONUNBUFFERED": "0"}), "differently from image.env"),
        (lambda d: d["combos"][0].pop("test"), "missing required key 'test'"),
    ],
)
def test_package_problems(distros, mutate, expected):
    data = raw("yt-dlp")
    mutate(data)
    assert expected in problems_for(data, distros)


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (lambda a: a.update(url="https://example.org/{release}.tar.xz"), "unknown placeholders"),
        (lambda a: a.update(url="http://example.org/x.tar.xz"), "https URL"),
        (lambda a: a.update(sha256="ABC"), "sha256"),
        (lambda a: a.pop("sha256"), "missing required key 'sha256'"),
        (lambda a: a.update(checksums={"url": "https://e.org/SUMS", "algorithm": "md5"}), "must be one of"),
    ],
)
def test_artifact_problems(distros, mutate, expected):
    data = raw("ghostscript")
    mutate(data["upstream"]["artifact"])
    assert expected in problems_for(data, distros)


@pytest.mark.parametrize(
    "template,expected",
    [
        ("https://e.org/{version}/{nodots}.tar.gz", "https://e.org/1.18.0/1180.tar.gz"),
        ("https://e.org/Release_{underscored}/x-{version}.tar.gz", "https://e.org/Release_1_18_0/x-1.18.0.tar.gz"),
        ("https://e.org/{major}.{minor}/x.tar.gz", "https://e.org/1.18/x.tar.gz"),
    ],
)
def test_expand_url(template, expected):
    assert config.expand_url(template, "1.18.0") == expected


def test_source_release_needs_artifact(distros):
    data = raw("ffmpeg")
    del data["upstream"]["artifact"]
    assert "source-release: needs upstream.artifact" in problems_for(data, distros)


@pytest.mark.parametrize("license_", ["GPL-3.0-or-later", "MIT AND LGPL-2.1-only", "AGPL-3.0-or-later AND MIT"])
def test_copyleft_tarball_builds_need_a_source_release(distros, license_):
    data = raw("ffmpeg")
    data["license"] = license_
    data["source-release"] = False
    assert "source-release: must be true: a tarball build under" in problems_for(data, distros)
    data["license"] = "MIT AND BSD-3-Clause"
    config.parse_package(data, "package.yml", distros)


def test_two_default_combos(distros):
    data = raw("yt-dlp")
    extra = copy.deepcopy(data["combos"][0])
    extra["with"] = ["ffmpeg", "ghostscript"]
    data["combos"].append(extra)
    assert "at most one combo can be the default" in problems_for(data, distros)


def test_artifact_urls(packages):
    gs = packages["ghostscript"].upstream.artifact
    assert gs.url_for("10.08.0").endswith("/gs10080/ghostscript-10.08.0.tar.xz")
    ff = packages["ffmpeg"].upstream.artifact
    assert ff.signature_for("9.0.2") == "https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz.asc"


def test_duplicate_yaml_keys_are_rejected():
    with pytest.raises(RcError) as exc:
        yamlio.load_text("name: a\nname: b\n", "package.yml")
    assert "duplicate key 'name'" in str(exc.value)


def test_combo_file():
    combo = config.parse_combo_file({"name": "yt-dlp-ffmpeg", "owner": "yt-dlp", "with": ["ffmpeg"]}, "combo.yml")
    assert combo.with_ == ("ffmpeg",)
    with pytest.raises(ValidationError) as exc:
        config.parse_combo_file({"name": "ffmpeg", "owner": "yt-dlp", "with": ["ffmpeg"]}, "combo.yml")
    assert "must be 'yt-dlp-ffmpeg'" in str(exc.value)


def test_extras_package_names_follow_the_distro_family(distros):
    data = raw("yt-dlp")
    data["combos"][0]["extras"]["alpine"]["packages"] = ["deno", "libSvtAv1Enc", "so:libSvtAv1Enc.so.4"]
    package = config.parse_package(data, "package.yml", distros)
    assert package.default_combo.extras_for("alpine").packages == ("deno", "libSvtAv1Enc", "so:libSvtAv1Enc.so.4")

    data["combos"][0]["extras"]["ubuntu"]["packages"] = ["libSvtAv1Enc"]
    assert "'libSvtAv1Enc' is not a valid package name" in problems_for(data, distros)

    data["combos"][0]["extras"]["ubuntu"]["packages"] = ["deno;id"]
    assert "'deno;id' is not a valid package name" in problems_for(data, distros)
