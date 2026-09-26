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


SOURCES = {
    "html-index": {
        "source": "html-index",
        "url": "https://poppler.freedesktop.org/releases.html",
        "pattern": r'href="poppler-(?P<version>\d+\.\d+\.\d+)\.tar\.xz">[^<]*</a> \((?P<date>[^)]+)\)',
        "versioning": "loose",
        "version": "26.09.0",
    },
    "gitlab-release": {"source": "gitlab-release", "project": "4207231", "versioning": "semver", "version": "16.1.0"},
    "forgejo-tag": {
        "source": "forgejo-tag",
        "repository": "mbunkus/mkvtoolnix",
        "tag-pattern": r"^release-(\d+\.\d+)$",
        "versioning": "loose",
        "version": "102.0",
    },
}


def with_upstream(source, **changes):
    data = raw("ffmpeg")
    data["upstream"] = {**copy.deepcopy(SOURCES[source]), **changes}
    data["source-release"] = False
    data["license"] = "MIT"
    return data


def test_other_sources(distros):
    html = config.parse_package(with_upstream("html-index"), "package.yml", distros).upstream
    assert html.url == "https://poppler.freedesktop.org/releases.html" and html.index_dated and html.server is None
    gitlab = config.parse_package(with_upstream("gitlab-release"), "package.yml", distros).upstream
    assert gitlab.project == "4207231" and gitlab.server == "https://gitlab.com"
    path = config.parse_package(with_upstream("gitlab-release", project="graphviz/graphviz"), "package.yml", distros)
    assert path.upstream.project == "graphviz/graphviz"
    forgejo = config.parse_package(with_upstream("forgejo-tag"), "package.yml", distros).upstream
    assert forgejo.repository == "mbunkus/mkvtoolnix" and forgejo.server == "https://codeberg.org"
    own = config.parse_package(with_upstream("forgejo-tag", server="https://git.example.org/forge"), "package.yml", distros)
    assert own.upstream.server == "https://git.example.org/forge"
    artifact = {"url": "https://nmap.org/dist/nmap-{version}.tar.bz2", "sha256": "0" * 64}
    undated = with_upstream("html-index", pattern=r'href="nmap-(\d+\.\d+)\.tar\.bz2"', artifact=artifact)
    assert not config.parse_package(undated, "package.yml", distros).upstream.index_dated


@pytest.mark.parametrize(
    "source,changes,expected",
    [
        ("html-index", {"url": None}, "html-index upstreams need 'url'"),
        ("html-index", {"pattern": None}, "html-index upstreams need 'pattern'"),
        ("html-index", {"url": "http://nmap.org/dist/"}, "https URL"),
        ("html-index", {"pattern": "poppler-(\\d+"}, "not a valid regular expression"),
        ("html-index", {"pattern": "poppler-\\d+"}, "needs a group for the version"),
        ("html-index", {"pattern": "(?P<ver>\\d+)"}, "unknown named groups ['ver']"),
        ("html-index", {"pattern": "(?P<date>\\S+) poppler-(\\d+)"}, "name the version group"),
        ("html-index", {"pattern": "poppler-(\\d+)"}, "need 'artifact'; its Last-Modified is the release date"),
        ("html-index", {"pattern": "x" * 513}, "at most 512"),
        ("html-index", {"repository": "a/b"}, "upstream.repository: is only used by github-release, github-tag and forgejo-tag"),
        ("html-index", {"server": "https://a.org"}, "upstream.server: is only used by gitlab-release and forgejo-tag"),
        ("gitlab-release", {"project": None}, "gitlab-release upstreams need 'project'"),
        ("gitlab-release", {"project": 4207231}, "quote it"),
        ("gitlab-release", {"project": "graphviz"}, "GitLab project id or path"),
        ("gitlab-release", {"project": "graphviz/../x"}, "GitLab project id or path"),
        ("gitlab-release", {"project": "0"}, "GitLab project id or path"),
        ("gitlab-release", {"server": "https://gitlab.com/"}, "without a trailing slash"),
        ("gitlab-release", {"server": "http://gitlab.com"}, "without a trailing slash"),
        ("gitlab-release", {"server": "https://gitlab.com/?x=1"}, "without a trailing slash"),
        ("gitlab-release", {"publisher": "a/b"}, "upstream.publisher: is only used by pypi"),
        ("gitlab-release", {"url": "https://a.org/"}, "upstream.url: is only used by html-index"),
        ("forgejo-tag", {"repository": None}, "forgejo-tag upstreams need 'repository'"),
        ("forgejo-tag", {"repository": "mbunkus/.."}, "owner/repo"),
        ("forgejo-tag", {"repository": "../mkvtoolnix"}, "owner/repo"),
        ("forgejo-tag", {"repository": "mbunkus/mkvtoolnix/tags"}, "owner/repo"),
        ("forgejo-tag", {"project": "x"}, "upstream.project: is only used by pypi and gitlab-release"),
        ("forgejo-tag", {"pattern": "(x)"}, "upstream.pattern: is only used by html-index"),
    ],
)
def test_other_source_problems(distros, source, changes, expected):
    data = with_upstream(source, **{k: v for k, v in changes.items() if v is not None})
    for key in [k for k, v in changes.items() if v is None]:
        del data["upstream"][key]
    assert expected in problems_for(data, distros)


def test_github_sources_reject_the_new_keys(distros):
    data = raw("ffmpeg")
    data["upstream"]["server"] = "https://codeberg.org"
    assert "upstream.server: is only used by gitlab-release and forgejo-tag" in problems_for(data, distros)
    data = raw("yt-dlp")
    data["upstream"]["repository"] = "yt-dlp/yt-dlp"
    assert "upstream.repository: is only used by github-release, github-tag and forgejo-tag" in problems_for(data, distros)


WHISPER_GIT = {
    "url": "https://github.com/ggml-org/whisper.cpp.git",
    "tag": "v{version}",
    "commit": "927cfce34f31707e17f2bff35c349632fb9e2c3a",
}


def whisper(**git):
    data = raw("ffmpeg")
    data.update(name="whisper-cpp", title="whisper.cpp", license="MIT")
    data["upstream"] = {
        "source": "github-tag",
        "repository": "ggml-org/whisper.cpp",
        "tag-pattern": r"^v(\d+\.\d+\.\d+)$",
        "versioning": "semver",
        "version": "1.9.4",
        "git": {**WHISPER_GIT, **git},
    }
    data["source-release"] = False
    return data


def test_git_source(distros):
    upstream = config.parse_package(whisper(), "package.yml", distros).upstream
    assert upstream.git == config.GitSource(**WHISPER_GIT)
    assert upstream.git.tag_for("1.9.4") == "v1.9.4"
    assert upstream.git.archive_stem("1.9.4") == "whisper.cpp-1.9.4"
    assert upstream.artifact is None


@pytest.mark.parametrize(
    "git,expected",
    [
        ({"url": "http://github.com/ggml-org/whisper.cpp.git"}, "https URL of a git repository"),
        ({"url": "https://github.com/ggml-org/whisper.cpp.git?x=1"}, "https URL of a git repository"),
        ({"url": "https://github.com/../whisper.cpp.git"}, "https URL of a git repository"),
        ({"url": "https://user@github.com/ggml-org/whisper.cpp.git"}, "https URL of a git repository"),
        ({"url": "https://github.com"}, "https URL of a git repository"),
        ({"tag": "v1.9.4"}, "needs a placeholder such as {version}"),
        ({"tag": "v{release}"}, "unknown placeholders ['release']"),
        ({"tag": "{version}.lock"}, "does not give a valid tag name"),
        ({"tag": "v{version}..x"}, "does not give a valid tag name"),
        ({"tag": "-{version}"}, "does not give a valid tag name"),
        ({"tag": "v{version}^{{}}"}, "does not give a valid tag name"),
        ({"tag": "v {version}"}, "does not give a valid tag name"),
        ({"commit": "927cfce"}, "full commit id"),
        ({"commit": "927CFCE34F31707E17F2BFF35C349632FB9E2C3A"}, "full commit id"),
        ({"branch": "master"}, "unknown key 'branch'"),
    ],
)
def test_git_source_problems(distros, git, expected):
    assert expected in problems_for(whisper(**git), distros)


def test_git_source_replaces_the_artifact(distros):
    data = whisper()
    data["upstream"]["artifact"] = {"url": "https://github.com/ggml-org/whisper.cpp/archive/v{version}.tar.gz", "sha256": "0" * 64}
    assert "upstream: set either 'artifact' or 'git', not both" in problems_for(data, distros)


def test_copyleft_git_builds_need_a_source_release(distros):
    data = whisper()
    data["license"] = "GPL-2.0-only"
    assert "source-release: must be true: a build from a git tag under GPL-2.0-only" in problems_for(data, distros)
    data["source-release"] = True
    assert config.parse_package(data, "package.yml", distros).source_release
    data["upstream"]["git"]["url"] = "https://github.com/ggml-org/~whisper"
    assert "upstream.git.url: must end in a file name" in problems_for(data, distros)


GTS = {
    "version": "0.7.6",
    "url": "https://downloads.sourceforge.net/project/gts/gts/{version}/gts-{version}.tar.gz",
    "sha256": "059c3e13e3e3b796d775ec9f96abdce8f2b3b5144df8514eda0cc12e13e8b81e",
    "distros": ["alpine"],
}
DOCS = {
    "url": "https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs{nodots}/ghostpdl-{version}.tar.xz",
    "checksums": {"url": "https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs{nodots}/SHA512SUMS", "algorithm": "sha512"},
    "sha256": "1" * 64,
}


def with_extras(**extras):
    data = raw("ghostscript")
    data["upstream"]["extra-artifacts"] = extras
    return data


def test_extra_artifacts(distros):
    upstream = config.parse_package(with_extras(gts=GTS, **{"pdf-docs": DOCS}), "package.yml", distros).upstream
    gts, docs = upstream.extra_artifacts
    assert gts.pinned_by_hand and not docs.pinned_by_hand
    assert upstream.extra("pdf-docs") is docs and upstream.extra("x") is None
    assert (gts.used_on("ubuntu"), gts.used_on("alpine"), docs.used_on("ubuntu")) == (False, True, True)
    assert gts.build_args("10.08.0") == {
        "GTS_VERSION": "0.7.6",
        "GTS_URL": "https://downloads.sourceforge.net/project/gts/gts/0.7.6/gts-0.7.6.tar.gz",
        "GTS_SHA256": GTS["sha256"],
    }
    assert docs.build_args("10.08.0") == {
        "PDF_DOCS_VERSION": "10.08.0",
        "PDF_DOCS_URL": "https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs10080/ghostpdl-10.08.0.tar.xz",
        "PDF_DOCS_SHA256": "1" * 64,
    }
    assert docs.artifact.checksums == config.Checksums(DOCS["checksums"]["url"], "sha512")
    assert [n for _, n in config.release_files(upstream, "10.08.0")] == [
        "ghostscript-10.08.0.tar.xz",
        "gts-0.7.6.tar.gz",
        "ghostpdl-10.08.0.tar.xz",
    ]


@pytest.mark.parametrize(
    "extras,expected",
    [
        ({}, "must be a non-empty mapping"),
        ({"GTS": GTS}, "lowercase letters and digits"),
        ({"gts lib": GTS}, "lowercase letters and digits"),
        ({"x" * 33: GTS}, "at most 32 characters"),
        ({"source": GTS}, "'source' is reserved for the main source"),
        ({"pdf-docs": DOCS, "pdf_docs": DOCS}, "gives the same build arguments (PDF_DOCS_*) as 'pdf-docs'"),
        ({"gts": {**GTS, "version": 0.8}}, "must be a string (quote it in YAML)"),
        ({"gts": {**GTS, "version": "0.7.6; id"}}, "not a valid version"),
        ({"gts": {**GTS, "checksums": DOCS["checksums"]}}, "which it does for artifacts without 'version'"),
        ({"gts": {k: v for k, v in GTS.items() if k != "version"} | {"url": "https://e.org/gts.tar.gz"}}, "needs a placeholder such as {version}"),
        ({"gts": {**GTS, "distros": ["debian"]}}, "'debian' is not a distro in distros.yml"),
        ({"gts": {**GTS, "distros": ["alpine", "alpine"]}}, "lists a distro more than once"),
        ({"gts": {**GTS, "distros": []}}, "must not be empty"),
        ({"gts": {**GTS, "sha256": "ABC"}}, "sha256"),
        ({"gts": {**GTS, "url": "http://e.org/{version}.tar.gz"}}, "https URL"),
        ({"gts": {**GTS, "mirror": "x"}}, "unknown key 'mirror'"),
        ({"gts": {**GTS, "url": "https://e.org/gts/{version}/download"}, "docs": {**GTS, "url": "https://e.org/docs/{version}/download"}},
         "gives the release asset download, like upstream.extra-artifacts.gts.url"),
        ({"gts": {**GTS, "url": "https://e.org/ghostscript-{version}.tar.xz", "version": "10.08.0"}},
         "gives the release asset ghostscript-10.08.0.tar.xz, like upstream.artifact.url"),
        ({"gts": {**GTS, "url": "https://e.org/gts/"}}, "must end in a file name"),
    ],
)
def test_extra_artifact_problems(distros, extras, expected):
    assert expected in problems_for(with_extras(**extras), distros)


def test_extra_artifacts_alone_can_make_a_source_release(distros):
    data = raw("yt-dlp")
    data["license"] = "Unlicense AND LGPL-2.1-or-later"
    data["upstream"]["extra-artifacts"] = {"gts": GTS}
    assert "must be true: a build with extra artifacts under LGPL-2.1-or-later" in problems_for(data, distros)
    data["source-release"] = True
    assert config.parse_package(data, "package.yml", distros).upstream.extra("gts").version == "0.7.6"
