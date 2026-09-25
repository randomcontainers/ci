import dataclasses

from rc import catalog


def test_catalog_of_fixtures_is_clean(packages, listed):
    assert catalog.validate_catalog(packages, listed) == []


def test_combo_helpers(packages):
    combo = packages["yt-dlp"].default_combo
    assert catalog.combo_title(combo, packages) == "yt-dlp + FFmpeg"
    assert catalog.combo_license(combo, packages, "ubuntu") == "Unlicense AND GPL-3.0-or-later AND MIT"
    assert catalog.combo_env(combo, packages) == {"DENO_DIR": "/cache/deno", "PYTHONUNBUFFERED": "1"}
    im = packages["imagemagick"].default_combo
    assert catalog.combo_license(im, packages, "alpine") == "ImageMagick AND AGPL-3.0-or-later"


def test_combo_name_clashes_with_package(packages, listed):
    ytdlp = packages["yt-dlp"]
    clash = dataclasses.replace(ytdlp.default_combo, owner="ffmpeg", with_=("ghostscript",), default=False)
    bad = dict(packages)
    bad["ffmpeg"] = dataclasses.replace(packages["ffmpeg"], combos=(clash,))
    bad["ghostscript"] = dataclasses.replace(packages["ghostscript"])
    listed = listed + ("ffmpeg-ghostscript",)
    problems = catalog.validate_catalog(bad, listed)
    assert any("already used by a package" in p for p in problems)


def test_same_members_under_two_owners(packages, listed):
    swapped = dataclasses.replace(packages["yt-dlp"].default_combo, owner="ffmpeg", with_=("yt-dlp",), default=False)
    bad = dict(packages)
    bad["ffmpeg"] = dataclasses.replace(packages["ffmpeg"], combos=(swapped,))
    problems = catalog.validate_catalog(bad, listed)
    assert any("same members as" in p for p in problems)
    # neither combo is created or built until one of them is removed
    assert {c for combos, _ in catalog.catalog_problems(bad, listed) for c in combos} == {"yt-dlp-ffmpeg", "ffmpeg-yt-dlp"}


def test_unknown_member(packages, listed):
    combo = dataclasses.replace(packages["yt-dlp"].default_combo, with_=("exiftool",))
    bad = dict(packages)
    bad["yt-dlp"] = dataclasses.replace(packages["yt-dlp"], combos=(combo,))
    problems = catalog.validate_catalog(bad, listed)
    assert any("exiftool is not listed" in p for p in problems)


def test_env_conflict_between_members(packages, listed):
    ffmpeg = dataclasses.replace(
        packages["ffmpeg"], image=dataclasses.replace(packages["ffmpeg"].image, env=(("PYTHONUNBUFFERED", "0"),))
    )
    bad = dict(packages, ffmpeg=ffmpeg)
    problems = catalog.validate_catalog(bad, listed)
    assert any("PYTHONUNBUFFERED" in p for p in problems)


def test_unlisted_package(packages):
    problems = catalog.validate_catalog(packages, ("yt-dlp", "ffmpeg"))
    assert any(p.startswith("imagemagick: not listed") for p in problems)


def test_combined_license_must_fit_ghcr(packages, listed):
    long_license = " AND ".join(f"LicenseRef-Part{i}" for i in range(20))
    ffmpeg = dataclasses.replace(packages["ffmpeg"], license=long_license)
    problems = catalog.validate_catalog(dict(packages, ffmpeg=ffmpeg), listed)
    assert any("longer than 256 characters" in p for p in problems)
