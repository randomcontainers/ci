import pytest

from rc import versions


@pytest.mark.parametrize(
    "scheme,version",
    [
        ("calver", "2026.08.19"),
        ("calver", "2026.09.16.232951"),
        ("semver", "9.0.2"),
        ("semver", "9.0"),
        ("loose", "7.1.2-31"),
        ("loose", "10.08.0"),
        ("loose", "13.36"),
    ],
)
def test_valid(scheme, version):
    versions.validate(scheme, version)


@pytest.mark.parametrize(
    "scheme,version",
    [
        ("calver", "2026.8.19"),
        ("calver", "2026.13.01"),
        ("calver", "v2026.08.19"),
        ("semver", "9"),
        ("semver", "09.0.1"),
        ("semver", "1.2.3-rc1"),
        ("semver", "1.2.3+build"),
        ("loose", "v7.1"),
        ("loose", "7.1 2"),
        ("loose", "7..1"),
        ("loose", "7.1/2"),
        ("semver", 9.0),
    ],
)
def test_invalid(scheme, version):
    with pytest.raises(versions.VersionError):
        versions.validate(scheme, version)


def test_unknown_scheme():
    with pytest.raises(versions.VersionError):
        versions.validate("pep440", "1.0")


@pytest.mark.parametrize(
    "scheme,older,newer",
    [
        ("calver", "2026.07.04", "2026.08.19"),
        ("calver", "2026.08.19", "2026.08.19.1"),
        ("semver", "8.1.3", "9.0.2"),
        ("semver", "9.0.9", "9.0.10"),
        ("semver", "9.0", "9.0.1"),
        ("loose", "7.1.2-9", "7.1.2-31"),
        ("loose", "7.1.2", "7.1.2-1"),
        ("loose", "10.07.0", "10.08.0"),
        ("loose", "9.56a", "9.56"),
    ],
)
def test_ordering(scheme, older, newer):
    assert versions.compare(scheme, older, newer) == -1
    assert versions.compare(scheme, newer, older) == 1
    assert versions.compare(scheme, newer, newer) == 0


def test_semver_short_equals_zero_patch():
    assert versions.compare("semver", "9.0", "9.0.0") == 0


@pytest.mark.parametrize(
    "scheme,version,expected",
    [
        ("semver", "9.0.2", ["9.0", "9"]),
        ("semver", "9.0", ["9"]),
        ("loose", "7.1.2-31", ["7.1.2", "7.1", "7"]),
        ("loose", "10.08.0", ["10.08", "10"]),
        ("loose", "9.56a", ["9"]),
        ("calver", "2026.08.19", []),
    ],
)
def test_shortcuts(scheme, version, expected):
    assert versions.shortcuts(scheme, version) == expected
