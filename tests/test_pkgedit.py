import pytest
import yaml

from conftest import FIXTURES, ROOT
from rc import pkgedit, yamlio
from rc.config import parse_package
from rc.errors import RcError


def text(name):
    return (FIXTURES / name / "package.yml").read_text()


def test_version_and_sha256(distros):
    before = text("ghostscript")
    after = pkgedit.update_upstream(before, distros, version="10.09.0", sha256="f" * 64)
    changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines(), strict=True) if a != b]
    assert changed == [("  version: 10.08.0", "  version: 10.09.0"), (f"    sha256: {before.split('sha256: ')[1][:64]}", f"    sha256: {'f' * 64}")]


def test_numbers_that_yaml_would_read_as_floats_are_quoted(distros):
    after = pkgedit.update_upstream(text("ffmpeg"), distros, version="9.1")
    assert '  version: "9.1"\n' in after


def test_only_the_upstream_version_changes(distros):
    before = text("yt-dlp")
    after = pkgedit.update_upstream(before, distros, version="2026.09.20")
    assert after.count("2026.09.20") == 1 and after.replace("2026.09.20", "2026.08.19") == before


@pytest.mark.parametrize("version", ["9.1; x", "9.1\nname: evil", "latest"])
def test_refuses_invalid_versions(distros, version):
    with pytest.raises(RcError):
        pkgedit.update_upstream(text("ffmpeg"), distros, version=version)


def test_refuses_bad_digest(distros):
    with pytest.raises(RcError):
        pkgedit.update_upstream(text("ffmpeg"), distros, sha256="F" * 64)


def test_trailing_comments_are_kept(distros):
    before = text("ffmpeg").replace("  version: 9.0.2\n", "  version: 9.0.2        # updated by the reconciler\n")
    after = pkgedit.update_upstream(before, distros, version="9.1")
    assert '  version: "9.1"        # updated by the reconciler\n' in after
    # a longer value pushes the comment to the right
    after = pkgedit.update_upstream(before.replace("9.0.2        #", "9.0.2 #"), distros, version="10.10.10")
    assert "  version: 10.10.10 # updated by the reconciler\n" in after
    quoted = before.replace("9.0.2", "'9.0.2'")
    assert '  version: "9.1"' in pkgedit.update_upstream(quoted, distros, version="9.1")


def test_refuses_files_it_cannot_edit_safely(distros):
    for odd in ("  version: &v 9.0.2", "  version: !!str 9.0.2", "  version: '9.0.2'#x"):
        with pytest.raises(RcError, match="cannot edit"):
            pkgedit.update_upstream(text("ffmpeg").replace("  version: 9.0.2", odd), distros, version="9.1")
    missing = text("streamlink").replace("  version: 8.6.1\n", "  version:\n    8.6.1\n")
    with pytest.raises(RcError):
        pkgedit.update_upstream(missing, distros, version="8.7.0")


def test_other_sources_keep_their_settings(distros):
    before = text("ghostscript").replace(
        "  source: github-release\n  repository: ArtifexSoftware/ghostpdl-downloads\n",
        "  source: html-index\n  url: https://ghostscript.com/releases/\n"
        "  pattern: 'href=\"ghostscript-(?P<version>[0-9.]+)\\.tar\\.xz\"[^>]*> *(?P<date>[0-9-]+)'\n",
    )
    assert "html-index" in before
    after = pkgedit.update_upstream(before, distros, version="10.09.0", sha256="f" * 64)
    assert "  version: 10.09.0\n" in after and after.count("f" * 64) == 1
    assert after.replace("10.09.0", "10.08.0").replace("f" * 64, before.split("sha256: ")[1][:64]) == before


GIT = """  git:
    url: https://github.com/FFmpeg/FFmpeg.git
    tag: n{version}
    commit: aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
"""
EXTRAS = """  extra-artifacts:
    gts:
      version: 0.7.6   # pinned by hand
      url: https://downloads.sourceforge.net/project/gts/gts/{version}/gts-{version}.tar.gz
      sha256: bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
      distros: [alpine]
    ghostpdl:
      url: https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs{nodots}/ghostpdl-{version}.tar.xz
      # follows the package version
      sha256: cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc
"""


def git_text():
    before = text("ffmpeg")
    start = before.index("  artifact:\n")
    end = before.index("image:\n")
    return before[:start] + GIT + before[end:]


def test_commit(distros):
    before = git_text()
    after = pkgedit.update_upstream(before, distros, version="9.1", commit="4" * 40)
    changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines(), strict=True) if a != b]
    # a commit id of digits only would be read as a number, so it is quoted
    assert changed == [("  version: 9.0.2", '  version: "9.1"'), (f"    commit: {'a' * 40}", f'    commit: "{"4" * 40}"')]
    after = pkgedit.update_upstream(before, distros, commit="4" * 39 + "b")
    assert f"    commit: {'4' * 39}b\n" in after
    with pytest.raises(RcError, match="refusing to write commit"):
        pkgedit.update_upstream(before, distros, commit="4" * 39)
    with pytest.raises(RcError, match="no upstream.git.commit"):
        pkgedit.update_upstream(text("ffmpeg"), distros, commit="4" * 40)


def test_extra_artifacts_that_follow_the_version(distros):
    before = text("ghostscript").replace("image:\n", EXTRAS + "image:\n", 1)
    after = pkgedit.update_upstream(before, distros, version="10.09.0", sha256="f" * 64, extras={"ghostpdl": "e" * 64})
    changed = [(a.strip(), b.strip()) for a, b in zip(before.splitlines(), after.splitlines(), strict=True) if a != b]
    assert [b for _, b in changed] == ["version: 10.09.0", f"sha256: {'f' * 64}", f"sha256: {'e' * 64}"]
    assert "sha256: " + "b" * 64 in after
    with pytest.raises(RcError, match="refusing to pin 'gts'"):
        pkgedit.update_upstream(before, distros, extras={"gts": "e" * 64})
    with pytest.raises(RcError, match="refusing to pin 'docs'"):
        pkgedit.update_upstream(before, distros, extras={"docs": "e" * 64})
    with pytest.raises(RcError, match="refusing to write sha256"):
        pkgedit.update_upstream(before, distros, extras={"ghostpdl": "E" * 64})
    flow = before.replace(
        "    ghostpdl:\n      url: https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs{nodots}/ghostpdl-{version}.tar.xz\n"
        "      # follows the package version\n      sha256: " + "c" * 64 + "\n",
        "    ghostpdl: {url: 'https://github.com/ArtifexSoftware/ghostpdl-downloads/releases/download/gs{nodots}/ghostpdl-{version}.tar.xz', "
        "sha256: " + "c" * 64 + "}\n",
    )
    assert flow != before
    with pytest.raises(RcError, match="no upstream.extra-artifacts.ghostpdl.sha256"):
        pkgedit.update_upstream(flow, distros, extras={"ghostpdl": "e" * 64})


def doc_blocks():
    doc = (ROOT / "docs" / "adding-a-package.md").read_text()
    return [b.split("```")[0] for b in doc.split("```yaml\n")[1:]]


def documented_package(block):
    """A package.yml around a YAML block of docs/adding-a-package.md, framed by the ghostscript fixture."""
    head, rest = text("ghostscript").split("upstream:\n", 1)
    head = head.replace("source-release: true\n", "")
    body, tail = rest.split("image:\n", 1)
    tail = "image:\n" + tail
    block = block.replace("<hex>", "b" * 64).replace("https://...", "https://example.org/")
    if block.startswith("name:"):
        return block
    if block.startswith("  artifact:"):
        return head + "upstream:\n" + body[: body.index("  artifact:\n")] + block + tail
    block = block.replace("  # source, project, artifact ...\n", body)
    if {"artifact", "git", "extra-artifacts"} & set(yaml.safe_load(block)["upstream"]):
        head += "source-release: true\n"
    return head + block + tail


NEW_VERSION = {"semver": "99.1.0", "loose": "99.1.0", "calver": "2099.01.01"}


@pytest.mark.parametrize("index", range(len(doc_blocks())))
def test_documented_examples_can_be_updated(distros, index):
    before = documented_package(doc_blocks()[index])
    upstream = parse_package(yamlio.load_text(before, "package.yml"), "package.yml", distros).upstream
    changes = {"version": NEW_VERSION[upstream.versioning]}
    if upstream.artifact:
        changes["sha256"] = "c" * 64
    if upstream.git:
        changes["commit"] = "d" * 40
    extras = {e.name: "e" * 64 for e in upstream.extra_artifacts if not e.pinned_by_hand}
    after = pkgedit.update_upstream(before, distros, **changes, extras=extras)
    changed = [(a, b) for a, b in zip(before.splitlines(), after.splitlines(), strict=True) if a != b]
    assert len(changed) == len(changes) + len(extras)
    for a, b in changed:
        assert ("#" in a) == ("#" in b)
        if "#" in a:
            assert (a.index("#"), a.split("#", 1)[1]) == (b.index("#"), b.split("#", 1)[1])


def test_every_documented_example_is_covered():
    blocks = doc_blocks()
    assert len(blocks) >= 8
    assert sum("# pinned by the reconciler" in b or "# updated by the reconciler" in b for b in blocks) >= 4
