import dataclasses
import hashlib
from datetime import timedelta

import pytest

from fakegithub import NOW, FakeGitHub, FakeWeb, Router
from fakes import FakeRegistry
from rc import upstream
from rc.errors import RcError
from rc.github import GitHub
from world import World

OLD = NOW - timedelta(days=10)


def checker(github: FakeGitHub, web: FakeWeb | None = None, now=NOW):
    router = Router(FakeRegistry(), github, web or FakeWeb())
    return upstream.Checker(GitHub(router), router, now)


def with_version(package, version):
    return dataclasses.replace(package, upstream=dataclasses.replace(package.upstream, version=version))


def test_ghostscript_release_tags(packages):
    gh = FakeGitHub()
    gh.releases["ArtifexSoftware/ghostpdl-downloads"] = [
        World.release("gs10100rc1", OLD, prerelease=True),
        World.release("gs10090rc1", OLD),
        World.release("gs10090", OLD, assets=["ghostscript-10.09.0.tar.xz", "SHA512SUMS"]),
        World.release("gs10080", OLD),
        World.release("gs9560", OLD),
    ]
    result = checker(gh).latest(packages["ghostscript"])
    assert result.chosen.version == "10.09.0" and result.chosen.tag == "gs10090"
    assert [c.version for c in checker(gh).candidates(packages["ghostscript"].upstream)] == ["10.09.0", "10.08.0", "9.56.0"]


def test_ffmpeg_tags_skip_development_tags(packages):
    gh = FakeGitHub()
    gh.tags["FFmpeg/FFmpeg"] = [
        ("n9.0.2", "1" * 40, "tag", OLD),
        ("n9.1-dev", "2" * 40, "tag", OLD),
        ("n9.0.10", "3" * 40, "tag", OLD),
        ("n10.0-dev", "4" * 40, "tag", OLD),
        ("n8.1.3", "5" * 40, "commit", OLD),
        ("v9.9", "6" * 40, "tag", OLD),
    ]
    web = FakeWeb()
    web.urls["https://ffmpeg.org/releases/ffmpeg-9.0.10.tar.xz"] = b"x"
    web.urls["https://ffmpeg.org/releases/ffmpeg-9.0.10.tar.xz.asc"] = b"x"
    result = checker(gh, web).latest(packages["ffmpeg"])
    assert result.newest == "9.0.10" and result.chosen.version == "9.0.10"
    # the listing is narrowed to tags starting with "n"
    assert any(u.endswith("/git/matching-refs/tags/n") for _, u, _, _ in gh.requests)


def test_tarball_must_be_published(packages):
    gh = FakeGitHub()
    gh.tags["FFmpeg/FFmpeg"] = [("n9.1", "1" * 40, "tag", OLD)]
    result = checker(gh).latest(packages["ffmpeg"])
    assert result.chosen is None
    assert result.waiting == ["9.1 https://ffmpeg.org/releases/ffmpeg-9.1.tar.xz is not available yet"]


def test_imagemagick_patch_levels_sort_numerically(packages):
    gh = FakeGitHub()
    names = ["7.1.2-9", "7.1.2-31", "7.1.2-100", "7.1.10-1", "6.9.13-40"]
    gh.releases["ImageMagick/ImageMagick"] = [
        World.release(n, OLD, assets=[f"ImageMagick-{n}.tar.xz"]) for n in names
    ]
    got = [c.version for c in checker(gh).candidates(packages["imagemagick"].upstream)]
    assert got == ["7.1.10-1", "7.1.2-100", "7.1.2-31", "7.1.2-9"]
    result = checker(gh).latest(with_version(packages["imagemagick"], "7.1.2-31"))
    assert result.chosen.version == "7.1.10-1"


def test_release_asset_missing_waits(packages):
    gh = FakeGitHub()
    gh.releases["ImageMagick/ImageMagick"] = [World.release("7.1.2-32", OLD, assets=["ImageMagick-7.1.2-32.7z"])]
    result = checker(gh).latest(packages["imagemagick"])
    assert result.chosen is None and "is not available yet" in result.waiting[0]


def test_calver_from_pypi(packages):
    web = FakeWeb()
    for version in ("2026.8.19", "2026.9.20", "2026.9.21.123456.dev0", "2026.10.1rc1"):
        web.add_release("yt-dlp", version, OLD, publisher="yt-dlp/yt-dlp")
    result = checker(FakeGitHub(), web).latest(packages["yt-dlp"])
    assert result.chosen.version == "2026.09.20" and result.chosen.tag == "2026.9.20"


def test_semver_from_pypi(packages):
    web = FakeWeb()
    for version in ("8.6.1", "8.10.0", "9.0.0rc1", "9.0.0.post1"):
        web.add_release("streamlink", version, OLD, publisher="streamlink/streamlink")
    result = checker(FakeGitHub(), web).latest(packages["streamlink"])
    assert result.chosen.version == "8.10.0"


def test_pypi_needs_wheels_for_every_platform(packages):
    web = FakeWeb()
    web.add_release("streamlink", "8.7.0", OLD, publisher="streamlink/streamlink", wheel="cp314-cp314-manylinux_2_28_x86_64")
    result = checker(FakeGitHub(), web).latest(packages["streamlink"])
    assert result.chosen is None
    assert result.refused == ["8.7.0 has no wheels for manylinux aarch64, musllinux aarch64, musllinux x86_64"]


def test_yanked_release_is_skipped(packages):
    web = FakeWeb()
    web.add_release("streamlink", "8.6.2", OLD, publisher="streamlink/streamlink")
    web.add_release("streamlink", "8.7.0", OLD, publisher="streamlink/streamlink", yanked=True)
    result = checker(FakeGitHub(), web).latest(packages["streamlink"])
    assert result.chosen.version == "8.6.2" and result.refused == ["8.7.0 is yanked"]


def test_cooldown(packages):
    gh = FakeGitHub()
    gh.releases["ImageMagick/ImageMagick"] = [
        World.release("7.1.2-33", NOW - timedelta(hours=5), assets=["ImageMagick-7.1.2-33.tar.xz"]),
        World.release("7.1.2-32", NOW - timedelta(hours=30), assets=["ImageMagick-7.1.2-32.tar.xz"]),
    ]
    result = checker(gh).latest(packages["imagemagick"])
    assert result.chosen.version == "7.1.2-32"
    assert result.waiting == ["7.1.2-33 was released 5h ago; the cooldown is 24h"]
    later = checker(gh, now=NOW + timedelta(hours=20)).latest(packages["imagemagick"])
    assert later.chosen.version == "7.1.2-33"


@pytest.mark.parametrize(
    "tag",
    [
        "n9.0.3;rm -rf /",
        "n9.0.3\n",
        "n9.0.3 ",
        "n$(id).0",
        "n9.0.3`id`",
        "n٩.0.3",
        "n9.０.3",
        "n9.0.3/../../x",
        "n" + "9" * 200 + ".0",
        "n09.0.3",
    ],
)
def test_malicious_tags_are_rejected(packages, tag):
    assert upstream.match_version(packages["ffmpeg"].upstream, tag) is None


@pytest.mark.parametrize(
    "raw,version",
    [
        ("2026.8.19", "2026.08.19"),
        ("2026.10.1", "2026.10.01"),
        ("2026.12.31", "2026.12.31"),
        ("2026.08.19", "2026.08.19"),
        ("2026.8.19.1", None),
        ("2026.9.21.123456.dev0", None),
        ("2026.10.1rc1", None),
    ],
)
def test_pypi_calendar_versions_are_zero_padded_before_the_tag_pattern(packages, raw, version):
    # yt-dlp's tag-pattern needs two-digit months and days; PyPI drops the zeros.
    assert packages["yt-dlp"].upstream.tag_pattern == r"^(\d{4}\.\d{2}\.\d{2})$"
    assert upstream.match_version(packages["yt-dlp"].upstream, raw, pypi=True) == version


@pytest.mark.parametrize("raw", ["2026.9.20'; echo x #", "2026.9.20\r", "2026.9.32", "2026.13.1"])
def test_malicious_or_invalid_pypi_versions(packages, raw):
    assert upstream.match_version(packages["yt-dlp"].upstream, raw, pypi=True) is None


def test_literal_prefix():
    assert upstream.literal_prefix(r"^n(\d+\.\d+(?:\.\d+)?)$") == "n"
    assert upstream.literal_prefix(r"^gs(\d+)(\d{2})(\d)$") == "gs"
    assert upstream.literal_prefix(r"^(7\.\d+\.\d+-\d+)$") == ""
    assert upstream.literal_prefix(r"^v?(\d+)$") == ""
    assert upstream.literal_prefix(r"^release-1\.(\d+)$") == "release-1."
    assert upstream.literal_prefix(r"^a|^b$") == ""


def test_checksums_file_formats():
    text = f"{'a' * 128}  ghostscript-10.09.0.tar.xz\n{'b' * 128} *other.tar.gz\nSHA512 (x.tar) = {'c' * 128}\n"
    assert upstream.parse_checksums(text, "ghostscript-10.09.0.tar.xz") == "a" * 128
    assert upstream.parse_checksums(text, "other.tar.gz") == "b" * 128
    assert upstream.parse_checksums(text, "x.tar") == "c" * 128
    assert upstream.parse_checksums(text, "missing.tar") is None


def test_release_asset_digest_is_cross_checked(packages):
    tarball = b"source"
    candidate = upstream.Candidate(
        "7.1.2-32",
        "7.1.2-32",
        release={"assets": [{"name": "ImageMagick-7.1.2-32.tar.xz", "digest": "sha256:" + "0" * 64}]},
    )

    def fetch(url, algorithms):
        return {a: hashlib.new(a, tarball).hexdigest() for a in algorithms}

    with pytest.raises(RcError, match="does not match the digest GitHub recorded"):
        upstream.pin_artifact(packages["imagemagick"], candidate, FakeWeb(), fetch)


def test_pin_needs_a_cross_check(packages):
    tarball = b"source"
    sha256 = hashlib.sha256(tarball).hexdigest()

    def fetch(url, algorithms):
        return {a: hashlib.new(a, tarball).hexdigest() for a in algorithms}

    name = "ImageMagick-7.1.2-32.tar.xz"
    for assets in ([], [{"name": name}], [{"name": name, "digest": None}], [{"name": "other.tar.xz", "digest": f"sha256:{sha256}"}]):
        candidate = upstream.Candidate("7.1.2-32", "7.1.2-32", release={"assets": assets})
        with pytest.raises(RcError, match="no checksums file, no signature and no digest recorded by GitHub"):
            upstream.pin_artifact(packages["imagemagick"], candidate, FakeWeb(), fetch)
    candidate = upstream.Candidate("7.1.2-32", "7.1.2-32", release={"assets": [{"name": name, "digest": f"sha256:{sha256}"}]})
    assert upstream.pin_artifact(packages["imagemagick"], candidate, FakeWeb(), fetch) == sha256
    # ffmpeg has a signature, which the Dockerfile verifies
    assert upstream.pin_artifact(packages["ffmpeg"], upstream.Candidate("9.0.3", "n9.0.3"), FakeWeb(), fetch) == sha256


def test_duration():
    assert upstream.duration("24h") == timedelta(hours=24)
    assert upstream.duration("3d") == timedelta(days=3)
    with pytest.raises(RcError):
        upstream.duration("1w")


def test_openssl_checksum_lines():
    # exiftool.org/checksums.txt
    text = (
        f"SHA2-256(Image-ExifTool-13.59.tar.gz)= {'a' * 64}\n"
        f"SHA2-256(exiftool-13.59_64.zip)= {'b' * 64}\n"
        f"SHA1(Image-ExifTool-13.59.tar.gz)= {'c' * 40}\n"
        f"MD5 (Image-ExifTool-13.59.tar.gz) = {'d' * 32}\n"
        f"SHA256(older.tar.gz)={'E' * 64}\n"
        f"SHA2-512(Image-ExifTool-13.59.tar.gz)= {'f' * 128}\n"
    )
    assert upstream.parse_checksums(text, "Image-ExifTool-13.59.tar.gz") == "a" * 64
    assert upstream.parse_checksums(text, "Image-ExifTool-13.59.tar.gz", "sha256") == "a" * 64
    assert upstream.parse_checksums(text, "Image-ExifTool-13.59.tar.gz", "sha512") == "f" * 128
    assert upstream.parse_checksums(text, "older.tar.gz", "sha256") == "e" * 64
    assert upstream.parse_checksums(text, "exiftool-13.59_64.zip", "sha512") is None


def test_checksums_of_another_algorithm_are_skipped():
    text = f"SHA512 (a.tar) = {'1' * 128}\nSHA256 (a.tar) = {'2' * 64}\n{'3' * 128}  b.tar\n{'4' * 64}  b.tar\n"
    assert upstream.parse_checksums(text, "a.tar", "sha256") == "2" * 64
    assert upstream.parse_checksums(text, "a.tar", "sha512") == "1" * 128
    assert upstream.parse_checksums(text, "b.tar", "sha256") == "4" * 64
    assert upstream.parse_checksums(text, "b.tar", "sha512") == "3" * 128
    # a label that does not fit its digest is not trusted
    assert upstream.parse_checksums(f"SHA256 (c.tar) = {'5' * 128}\n", "c.tar", "sha256") is None
