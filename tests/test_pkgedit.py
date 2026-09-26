import pytest

from conftest import FIXTURES
from rc import pkgedit
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
