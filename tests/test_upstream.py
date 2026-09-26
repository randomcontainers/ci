import dataclasses
import hashlib
import json
from datetime import UTC, timedelta, timezone

import pytest

from fakegithub import NOW, FakeGitHub, FakeWeb, Router, iso
from fakes import FakeRegistry
from rc import config, upstream
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


@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-09-04T14:43:25.618Z", "2026-09-04T14:43:25"),
        ("2026-09-14T15:56:29+02:00", "2026-09-14T13:56:29"),
        ("Fri, 04 Sep 2026 14:42:08 GMT", "2026-09-04T14:42:08"),
        ("2026-09-03 12:00 -0730", "2026-09-03T19:30:00"),
        # no zone: the latest moment it can mean, in UTC-12
        ("2026-08-05 14:27", "2026-08-06T02:27:00"),
        ("05-Aug-2026 14:27", "2026-08-06T02:27:00"),
        # no time: the end of that day in UTC-12
        ("2026-09-03", "2026-09-04T12:00:00"),
        ("Thu Sep 3, 2026", "2026-09-04T12:00:00"),
        ("September 3, 2026", "2026-09-04T12:00:00"),
        ("3 Sep 2026", "2026-09-04T12:00:00"),
        ("Sep 31, 2026", None),
        ("Foo 3, 2026", None),
        ("2026-09-03 25:00", None),
        ("2026-09-03 12:00 +1500", None),
        ("9999-12-31", None),
        ("yesterday", None),
        (None, None),
        ("2026-09-03" + " " * 80, None),
    ],
)
def test_release_date(text, expected):
    value = upstream.release_date(text)
    assert (value.astimezone(UTC).replace(tzinfo=None).isoformat() if value else None) == expected


# Sources outside PyPI and GitHub

BASE = {
    "name": "tool",
    "title": "Tool",
    "summary": "A tool.",
    "homepage": "https://example.org/",
    "license": "MIT",
    "image": {"entrypoint": ["tool"]},
    "test": ["tool --version"],
}
POPPLER_PAGE = b"""<h2>Poppler 26.09 Releases</h2>
<pre>
<p><a href="poppler-26.09.0.tar.xz">poppler-26.09.0.tar.xz</a> (Thu Sep 3, 2026):</p>
        core:
         * Subset fonts when saving changes
</pre>
<p><a href="poppler-26.08.0.tar.xz">poppler-26.08.0.tar.xz</a> (Sun Aug 2, 2026):</p>
<p><a href="poppler-26.07.0.tar.xz">poppler-26.07.0.tar.xz</a> (Thu Jul 2, 2026):</p>
"""
POPPLER = {
    "source": "html-index",
    "url": "https://poppler.freedesktop.org/releases.html",
    "pattern": r'href="poppler-(?P<version>\d+\.\d+\.\d+)\.tar\.xz">[^<]*</a> \((?P<date>[A-Za-z]{3} [A-Za-z]{3} \d{1,2}, \d{4})\)',
    "versioning": "loose",
    "version": "26.08.0",
}
NMAP_PAGE = (
    b'<li><a class="feature" href="/dist/nmap-7.991.tar.bz2">Nmap 7.991 source code</a>\n'
    b'<tr><td class="indexcolname"><a href="nmap-7.991.tar.bz2">nmap-7.991.tar.bz2</a></td><td>2026-08-05 14:27</td></tr>\n'
    b'<tr><td class="indexcolname"><a href="nmap-7.99.tar.bz2">nmap-7.99.tar.bz2</a></td><td>2026-03-26 13:55</td></tr>\n'
    b'<tr><td class="indexcolname"><a href="nmap-7.99RC1.tar.bz2">nmap-7.99RC1.tar.bz2</a></td></tr>\n'
)
NMAP = {
    "source": "html-index",
    "url": "https://nmap.org/dist/",
    "pattern": r'href="nmap-(\d+\.\d+)\.tar\.bz2"',
    "versioning": "loose",
    "version": "7.99",
    "artifact": {
        "url": "https://nmap.org/dist/nmap-{version}.tar.bz2",
        "signature": "https://nmap.org/dist/sigs/nmap-{version}.tar.bz2.asc",
        "sha256": "0" * 64,
    },
}
GRAPHVIZ = {
    "source": "gitlab-release",
    "project": "4207231",
    "tag-pattern": r"^(\d+\.\d+\.\d+)$",
    "versioning": "semver",
    "version": "16.0.0",
}
GITLAB_RELEASES = "https://gitlab.com/api/v4/projects/4207231/releases?per_page=100"
MKVTOOLNIX = {
    "source": "forgejo-tag",
    "repository": "mbunkus/mkvtoolnix",
    "tag-pattern": r"^release-(\d+\.\d+)$",
    "versioning": "loose",
    "version": "101.0",
}
CODEBERG_TAGS = "https://codeberg.org/api/v1/repos/mbunkus/mkvtoolnix/tags?page={}&limit=50"


def package_with(distros, source, **changes):
    return config.parse_package({**BASE, "upstream": {**source, **changes}}, "package.yml", distros)


def web_json(web, url, data, headers=None):
    web.urls[url] = json.dumps(data).encode()
    web.headers[url] = headers or {}


def gitlab_release(tag, released, created=None, upcoming=False):
    return {"tag_name": tag, "released_at": iso(released), "created_at": iso(created or released), "upcoming_release": upcoming}


def forgejo_tag(name, created, tag_id=None):
    """A tag as the Forgejo API lists it; with tag_id, an annotated tag."""
    sha = hashlib.sha1(name.encode()).hexdigest()
    return {"name": name, "id": tag_id or sha, "commit": {"sha": sha, "created": created.astimezone(timezone(timedelta(hours=2))).isoformat()}}


def forgejo_annotated(web, tag_id, tagged):
    url = f"https://codeberg.org/api/v1/repos/mbunkus/mkvtoolnix/git/tags/{tag_id}"
    web_json(web, url, {"tag": "x", "sha": tag_id, "tagger": {"name": "a", "date": iso(tagged) if tagged else None}})
    return url


def test_html_index_with_dates(distros):
    web = FakeWeb()
    web.urls[POPPLER["url"]] = POPPLER_PAGE
    package = package_with(distros, POPPLER)
    got = checker(FakeGitHub(), web).candidates(package.upstream)
    assert [(c.version, c.published.isoformat()) for c in got] == [
        ("26.09.0", "2026-09-04T12:00:00+00:00"),
        ("26.08.0", "2026-08-03T12:00:00+00:00"),
        ("26.07.0", "2026-07-03T12:00:00+00:00"),
    ]
    result = checker(FakeGitHub(), web).latest(package)
    assert result.chosen.version == "26.09.0" and result.chosen.tag == "26.09.0"


def test_html_index_date_is_read_conservatively(distros):
    web = FakeWeb()
    web.urls[POPPLER["url"]] = POPPLER_PAGE.replace(b"Thu Sep 3, 2026", b"Wed Sep 23, 2026")
    package = package_with(distros, POPPLER)
    # 23 September ends at 12:00 UTC on the 24th, which is NOW
    result = checker(FakeGitHub(), web).latest(package)
    assert result.chosen is None and result.waiting == ["26.09.0 was released 0h ago; the cooldown is 24h"]
    later = checker(FakeGitHub(), web, now=NOW + timedelta(hours=24)).latest(package)
    assert later.chosen.version == "26.09.0"


def test_html_index_unreadable_date_is_refused(distros):
    web = FakeWeb()
    web.urls[POPPLER["url"]] = POPPLER_PAGE.replace(b"Thu Sep 3, 2026", b"Thu Sep 33, 2026")
    result = checker(FakeGitHub(), web).latest(package_with(distros, POPPLER))
    assert result.chosen is None and result.refused == ["26.09.0 has no release date"]


def test_html_index_repeated_version_takes_the_latest_date(distros):
    web = FakeWeb()
    web.urls[POPPLER["url"]] = POPPLER_PAGE + b'<p><a href="poppler-26.09.0.tar.xz">again</a> (Wed Sep 23, 2026):</p>\n'
    got = checker(FakeGitHub(), web).candidates(package_with(distros, POPPLER).upstream)
    assert got[0].version == "26.09.0" and got[0].published.isoformat() == "2026-09-24T12:00:00+00:00"


def test_html_index_dated_by_last_modified(distros):
    web = FakeWeb()
    web.urls[NMAP["url"]] = NMAP_PAGE
    tarball = "https://nmap.org/dist/nmap-7.991.tar.bz2"
    web.urls[tarball] = b"x"
    web.urls["https://nmap.org/dist/sigs/nmap-7.991.tar.bz2.asc"] = b"x"
    web.headers[tarball] = {"last-modified": "Wed, 23 Sep 2026 21:27:13 GMT"}
    package = package_with(distros, NMAP)
    assert [c.version for c in checker(FakeGitHub(), web).candidates(package.upstream)] == ["7.991", "7.99"]
    result = checker(FakeGitHub(), web).latest(package)
    assert result.chosen is None and result.waiting == ["7.991 was released 14h ago; the cooldown is 24h"]
    later = checker(FakeGitHub(), web, now=NOW + timedelta(hours=10)).latest(package)
    assert later.chosen.version == "7.991" and later.chosen.tag == "7.991"


def test_html_index_waits_for_the_artifact(distros):
    web = FakeWeb()
    web.urls[NMAP["url"]] = NMAP_PAGE
    result = checker(FakeGitHub(), web).latest(package_with(distros, NMAP))
    assert result.chosen is None
    assert result.waiting == ["7.991 https://nmap.org/dist/nmap-7.991.tar.bz2 is not available yet"]


def test_html_index_without_last_modified_is_refused(distros):
    web = FakeWeb()
    web.urls[NMAP["url"]] = NMAP_PAGE
    web.urls["https://nmap.org/dist/nmap-7.991.tar.bz2"] = b"x"
    result = checker(FakeGitHub(), web).latest(package_with(distros, NMAP))
    assert result.refused == ["7.991 has no release date"]


def test_html_index_page_that_no_longer_matches_is_an_error(distros):
    web = FakeWeb()
    web.urls[NMAP["url"]] = b"<html><body>Moved to a new layout</body></html>\n"
    with pytest.raises(RcError, match="the pattern matches no line"):
        checker(FakeGitHub(), web).candidates(package_with(distros, NMAP).upstream)
    with pytest.raises(RcError, match="HTTP 404"):
        checker(FakeGitHub(), FakeWeb()).candidates(package_with(distros, NMAP).upstream)


def test_html_index_limits(distros, monkeypatch):
    web = FakeWeb()
    web.urls[NMAP["url"]] = b'<a href="nmap-8.0.tar.bz2">' + b"x" * upstream.MAX_LINE + b"\n" + NMAP_PAGE
    got = checker(FakeGitHub(), web).candidates(package_with(distros, NMAP).upstream)
    assert [c.version for c in got] == ["7.991", "7.99"]
    monkeypatch.setattr(upstream, "MAX_INDEX", 100)
    with pytest.raises(RcError, match="larger than 100 bytes"):
        checker(FakeGitHub(), web).candidates(package_with(distros, NMAP).upstream)


@pytest.mark.parametrize(
    "version",
    ["7.992;id", "7.992$(id)", "7.992`id`", "7.992/../../x", "7.99 2", "7.992\\n", "7." + "9" * 40, "٧.992"],
)
def test_html_index_versions_must_pass_the_scheme(distros, version):
    web = FakeWeb()
    web.urls[NMAP["url"]] = f'<a href="nmap-{version}.tar.bz2">\n'.encode() + NMAP_PAGE
    package = package_with(distros, NMAP, pattern=r'href="nmap-([^"]+)\.tar\.bz2"')
    got = checker(FakeGitHub(), web).candidates(package.upstream)
    assert [c.version for c in got] == ["7.991", "7.99", "7.99RC1"]


def test_gitlab_releases(distros):
    web = FakeWeb()
    web_json(
        web,
        GITLAB_RELEASES,
        [
            gitlab_release("17.0.0", NOW + timedelta(days=30), created=OLD, upcoming=True),
            gitlab_release("16.2.0", OLD, created=NOW - timedelta(hours=3)),
            gitlab_release("16.1.0", OLD),
            gitlab_release("16.1.0-rc1", OLD),
            gitlab_release("16.0.0", OLD),
        ],
    )
    package = package_with(distros, GRAPHVIZ)
    assert [c.version for c in checker(FakeGitHub(), web).candidates(package.upstream)] == ["16.2.0", "16.1.0", "16.0.0"]
    # a release backdated with released_at still waits for its cooldown from created_at
    result = checker(FakeGitHub(), web).latest(package)
    assert result.chosen.version == "16.1.0" and result.waiting == ["16.2.0 was released 3h ago; the cooldown is 24h"]


def test_gitlab_project_path_and_server(distros):
    web = FakeWeb()
    web_json(web, "https://gitlab.example.org/api/v4/projects/graphviz%2Fgraphviz/releases?per_page=100", [gitlab_release("16.1.0", OLD)])
    package = package_with(distros, GRAPHVIZ, project="graphviz/graphviz", server="https://gitlab.example.org")
    assert checker(FakeGitHub(), web).latest(package).chosen.version == "16.1.0"


def test_gitlab_bad_responses(distros):
    package = package_with(distros, GRAPHVIZ)
    with pytest.raises(RcError, match="HTTP 404"):
        checker(FakeGitHub(), FakeWeb()).candidates(package.upstream)
    web = FakeWeb()
    web_json(web, GITLAB_RELEASES, {"message": "401 Unauthorized"})
    with pytest.raises(RcError, match="unexpected response"):
        checker(FakeGitHub(), web).candidates(package.upstream)
    web.urls[GITLAB_RELEASES] = b"<html>"
    with pytest.raises(RcError, match="not JSON"):
        checker(FakeGitHub(), web).candidates(package.upstream)
    web_json(web, GITLAB_RELEASES, ["x", {"tag_name": 16}, {"tag_name": "16.1.0", "released_at": "2026-09-01T00:00:00"}])
    result = checker(FakeGitHub(), web).latest(package)
    assert result.chosen is None and result.refused == ["16.1.0 has no release date"]


def test_forgejo_tags_across_pages(distros):
    web = FakeWeb()
    next_page = {"link": '<https://codeberg.org/api/v1/repos/mbunkus/mkvtoolnix/tags?limit=50&page=2>; rel="next"'}
    web_json(web, CODEBERG_TAGS.format(1), [forgejo_tag("release-102.0", NOW - timedelta(hours=5))], next_page)
    # 99.0 is older than the current version, so page 3 is not read
    page = [forgejo_tag("release-101.1", OLD), forgejo_tag("release-9.9.0", OLD), {"name": None}, forgejo_tag("release-99.0", OLD)]
    web_json(web, CODEBERG_TAGS.format(2), page, next_page)
    web_json(web, CODEBERG_TAGS.format(3), [forgejo_tag("release-200.0", OLD)])
    package = package_with(distros, MKVTOOLNIX)
    got = checker(FakeGitHub(), web).candidates(package.upstream)
    assert [(c.version, c.tag) for c in got] == [("102.0", "release-102.0"), ("101.1", "release-101.1"), ("99.0", "release-99.0")]
    result = checker(FakeGitHub(), web).latest(package)
    assert result.chosen.version == "101.1" and result.waiting == ["102.0 was released 5h ago; the cooldown is 24h"]
    assert CODEBERG_TAGS.format(3) not in [u for _, u in web.requests]


def test_forgejo_page_limit_and_server(distros, monkeypatch):
    monkeypatch.setattr(upstream, "FORGEJO_PAGES", 2)
    web = FakeWeb()
    tags = "https://git.example.org/forge/api/v1/repos/mbunkus/mkvtoolnix/tags?page={}&limit=50"
    for page in (1, 2, 3):
        web_json(web, tags.format(page), [forgejo_tag(f"release-10{page}.0", OLD)], {"link": 'rel="next"'})
    package = package_with(distros, MKVTOOLNIX, server="https://git.example.org/forge", version="100.0")
    assert [c.version for c in checker(FakeGitHub(), web).candidates(package.upstream)] == ["102.0", "101.0"]


def test_forgejo_annotated_tags_are_dated_by_the_tagger(distros):
    web = FakeWeb()
    fresh, old, undated, skipped = ("1" * 40, "3" * 40, "5" * 40, "7" * 40)
    tags = [
        # an annotated tag made today on an old commit
        forgejo_tag("release-103.0", OLD, tag_id=fresh),
        forgejo_tag("release-102.0", OLD, tag_id=undated),
        forgejo_tag("release-101.1", OLD - timedelta(days=5), tag_id=old),
        forgejo_tag("release-100.0", OLD, tag_id=skipped),
    ]
    web_json(web, CODEBERG_TAGS.format(1), tags)
    urls = [
        forgejo_annotated(web, fresh, NOW - timedelta(hours=2)),
        forgejo_annotated(web, undated, None),
        forgejo_annotated(web, old, OLD),
        forgejo_annotated(web, skipped, OLD),
    ]
    result = checker(FakeGitHub(), web).latest(package_with(distros, MKVTOOLNIX))
    assert result.waiting == ["103.0 was released 2h ago; the cooldown is 24h"]
    assert result.refused == ["102.0 has no release date"]
    assert result.chosen.version == "101.1"
    requested = [u for _, u in web.requests]
    assert all(u in requested for u in urls[:3]) and urls[3] not in requested


def test_pin_from_a_gitlab_checksums_file(distros):
    tarball = b"graphviz source"
    sha256 = hashlib.sha256(tarball).hexdigest()
    base = "https://gitlab.com/api/v4/projects/4207231/packages/generic/graphviz-releases/{version}/graphviz-{version}.tar.xz"
    artifact = {"url": base, "checksums": {"url": base + ".sha256", "algorithm": "sha256"}, "sha256": "0" * 64}
    package = package_with(distros, GRAPHVIZ, artifact=artifact)
    web = FakeWeb()
    web.urls[base.format(version="16.1.0") + ".sha256"] = f"{sha256}  graphviz-16.1.0.tar.xz\n".encode()

    def fetch(url, algorithms):
        return {a: hashlib.new(a, tarball).hexdigest() for a in algorithms}

    assert upstream.pin_artifact(package, upstream.Candidate("16.1.0", "16.1.0"), web, fetch) == sha256


def test_pin_from_an_openssl_checksums_file(distros):
    tarball = b"exiftool source"
    sha256 = hashlib.sha256(tarball).hexdigest()
    artifact = {
        "url": "https://downloads.sourceforge.net/project/exiftool/Image-ExifTool-{version}.tar.gz",
        "checksums": {"url": "https://exiftool.org/checksums.txt", "algorithm": "sha256"},
        "sha256": "0" * 64,
    }
    package = package_with(distros, GRAPHVIZ, artifact=artifact)
    web = FakeWeb()
    web.urls["https://exiftool.org/checksums.txt"] = (
        f"SHA2-256(Image-ExifTool-13.59.tar.gz)= {sha256}\nSHA1(Image-ExifTool-13.59.tar.gz)= {'0' * 40}\n"
    ).encode()

    def fetch(url, algorithms):
        return {a: hashlib.new(a, tarball).hexdigest() for a in algorithms}

    assert upstream.pin_artifact(package, upstream.Candidate("13.59", "13.59"), web, fetch) == sha256
    with pytest.raises(RcError, match="is not listed"):
        upstream.pin_artifact(package, upstream.Candidate("13.60", "13.60"), web, fetch)
