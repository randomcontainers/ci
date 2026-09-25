import pytest

from rc import tags
from rc.errors import RcError


def test_ffmpeg_semver(distros):
    ubuntu, alpine = distros.get("ubuntu"), distros.get("alpine")
    slim = tags.package_tags("slim", "9.0.2", "semver", ubuntu, "ubuntu")
    assert slim.tags == [
        "slim",
        "9.0.2-slim",
        "9.0-slim",
        "9-slim",
        "slim-ubuntu",
        "9.0.2-slim-ubuntu",
        "9.0-slim-ubuntu",
        "9-slim-ubuntu",
        "9.0.2-slim-ubuntu26.04",
    ]
    assert set(slim.floating) == {"slim", "9.0-slim", "9-slim", "slim-ubuntu", "9.0-slim-ubuntu", "9-slim-ubuntu"}
    default = tags.package_tags("default", "9.0.2", "semver", alpine, "ubuntu")
    assert default.tags == ["alpine", "9.0.2-alpine", "9.0-alpine", "9-alpine", "9.0.2-alpine3.24"]
    assert default.fixed == ["9.0.2-alpine", "9.0.2-alpine3.24"]


def test_default_on_default_distro(distros):
    ts = tags.package_tags("default", "7.1.2-31", "loose", distros.get("ubuntu"), "ubuntu")
    assert ts.tags == [
        "latest",
        "7.1.2-31",
        "7.1.2",
        "7.1",
        "7",
        "ubuntu",
        "7.1.2-31-ubuntu",
        "7.1.2-ubuntu",
        "7.1-ubuntu",
        "7-ubuntu",
        "7.1.2-31-ubuntu26.04",
    ]
    assert "7.1.2-31" not in ts.floating
    assert "7.1" in ts.floating


def test_calver_has_no_shortcuts(distros):
    ts = tags.package_tags("slim", "2026.08.19", "calver", distros.get("alpine"), "ubuntu")
    assert ts.tags == ["slim-alpine", "2026.08.19-slim-alpine", "2026.08.19-slim-alpine3.24"]


def test_ghostscript_shortcuts(distros):
    ts = tags.package_tags("default", "10.08.0", "loose", distros.get("ubuntu"), "ubuntu")
    assert ts.tags[:4] == ["latest", "10.08.0", "10.08", "10"]


def test_combo_tags(distros):
    ubuntu = tags.combo_tags("2026.08.19", distros.get("ubuntu"), "ubuntu")
    assert ubuntu.tags == ["latest", "2026.08.19", "ubuntu", "2026.08.19-ubuntu", "2026.08.19-ubuntu26.04"]
    assert ubuntu.floating == ["latest", "ubuntu"]
    alpine = tags.combo_tags("2026.08.19", distros.get("alpine"), "ubuntu")
    assert alpine.tags == ["alpine", "2026.08.19-alpine", "2026.08.19-alpine3.24"]


def test_primary_tag(distros):
    assert tags.primary_tag("slim", "9.0.2", distros.get("alpine")) == "9.0.2-slim-alpine3.24"
    assert tags.primary_tag("default", "9.0.2", distros.get("ubuntu")) == "9.0.2-ubuntu26.04"


def test_rejects_invalid_tag():
    ts = tags.TagSet()
    with pytest.raises(RcError):
        ts.add("bad tag", floating=False)
    with pytest.raises(RcError):
        ts.add("x" * 129, floating=False)


def test_extend_keeps_order_and_floating(distros):
    ubuntu = distros.get("ubuntu")
    slim = tags.package_tags("slim", "9.0.2", "semver", ubuntu, "ubuntu")
    slim.extend(tags.package_tags("default", "9.0.2", "semver", ubuntu, "ubuntu"))
    assert slim.tags[0] == "slim" and "latest" in slim.tags and "latest" in slim.floating
    assert len(slim.tags) == len(set(slim.tags))
