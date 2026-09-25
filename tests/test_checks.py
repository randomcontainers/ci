from rc import checks
from rc.checks import Report

EXPECT = {
    "entrypoint": ["tini", "--", "yt-dlp"],
    "cmd": ["--help"],
    "env": {"LANG": "C.UTF-8", "XDG_CACHE_HOME": "/cache", "PYTHONUNBUFFERED": "1"},
    "user": "1000:1000",
    "workdir": "/work",
    "packages": {"yt-dlp": "2026.08.19"},
    "extra_packages": [],
    "os_version": "26.04",
}
LABELS = {"org.opencontainers.image.version": "2026.08.19", "com.randomcontainers.variant": "slim"}

PROBE = """
@@section os-release
PRETTY_NAME="Ubuntu Resolute"
VERSION_ID="26.04"

@@section alpine-release

@@section installed
apt
bash
ca-certificates
python3
tini

@@section dirs
/work 1777
/cache 1777

@@section meta
yt-dlp version present
yt-dlp runtime-deps present
yt-dlp source present
yt-dlp licenses 3
yt-dlp version-value 2026.08.19

@@section runtime-deps
python3

@@section bin
link yt-dlp /usr/local/lib/yt-dlp/bin/yt-dlp

@@section elf
"""


def config(**over):
    base = {
        "Entrypoint": ["tini", "--", "yt-dlp"],
        "Cmd": ["--help"],
        "Env": ["PATH=/usr/bin", "LANG=C.UTF-8", "XDG_CACHE_HOME=/cache", "PYTHONUNBUFFERED=1"],
        "User": "1000:1000",
        "WorkingDir": "/work",
        "Labels": dict(LABELS),
    }
    base.update(over)
    return base


def run_all(probe=PROBE, base_set=frozenset({"apt", "bash"}), extra=(), packages=None):
    report = Report()
    sections = checks.parse_sections(probe)
    checks.check_os(sections, "debian", "26.04", report)
    checks.check_packages(sections, set(base_set), list(extra), report)
    checks.check_metadata(sections, packages or EXPECT["packages"], report)
    checks.check_dirs(sections, report)
    checks.check_bin(sections, report)
    checks.check_elf(sections, report)
    return report


def test_clean_image_passes():
    report = Report()
    checks.check_config(config(), EXPECT, LABELS, report)
    assert report.problems == []
    assert run_all().problems == []


def test_config_mismatches():
    report = Report()
    labels = dict(LABELS, **{"com.randomcontainers.package": "ffmpeg"})
    checks.check_config(
        config(
            Entrypoint=["yt-dlp"],
            User="root",
            Env=["LANG=C"],
            Volumes={"/work": {}},
            Labels=labels,
        ),
        EXPECT,
        LABELS,
        report,
    )
    text = "\n".join(report.problems)
    assert "entrypoint" in text and "user is 'root'" in text and "environment LANG" in text
    assert "volumes" in text and "unexpected label com.randomcontainers.package" in text


def test_inherited_oci_label_is_a_warning():
    report = Report()
    checks.check_config(config(Labels=dict(LABELS, **{"org.opencontainers.image.ref.name": "ubuntu"})), EXPECT, LABELS, report)
    assert report.problems == [] and report.warnings


def test_os_version():
    report = Report()
    checks.check_os(checks.parse_sections("@@section alpine-release\n3.23.4\n"), "alpine", "3.24", report)
    assert report.problems
    report = Report()
    checks.check_os(checks.parse_sections("@@section alpine-release\n3.24.2\n"), "alpine", "3.24", report)
    assert report.problems == []


def test_packages_must_match_runtime_deps():
    probe = PROBE.replace("python3\ntini", "python3\ntini\ncurl").replace("@@section runtime-deps\npython3", "@@section runtime-deps\npython3\nlibfoo1")
    text = "\n".join(run_all(probe).problems)
    assert "not installed: libfoo1" in text
    assert "not listed in any runtime-deps file: curl" in text


def test_apk_world_constraints_are_ignored():
    probe = PROBE.replace("python3\ntini", "python3>=3.14\ntini")
    assert run_all(probe).problems == []


def test_extra_packages_count_as_expected():
    probe = PROBE.replace("python3\ntini", "python3\ntini\ndeno")
    assert run_all(probe, extra=["deno"]).problems == []


def test_metadata_problems():
    probe = PROBE.replace("yt-dlp source present", "yt-dlp source missing").replace("licenses 3", "licenses 0")
    probe = probe.replace("version-value 2026.08.19", "version-value 2026.07.04")
    probe = probe.replace("@@section runtime-deps", "ffmpeg version present\n@@section runtime-deps")
    text = "\n".join(run_all(probe).problems)
    assert "yt-dlp/source is missing" in text
    assert "no license files" in text
    assert "expected '2026.08.19'" in text
    assert "ffmpeg belongs to no package" in text
    assert "yt-dlp/version" in text


def test_missing_member_metadata():
    text = "\n".join(run_all(packages={"yt-dlp": "2026.08.19", "ffmpeg": "9.0.2"}).problems)
    assert "randomcontainers/ffmpeg is missing" in text


def test_dirs():
    assert run_all(PROBE.replace("/cache 1777", "/cache 755")).problems == ["/cache has mode 755, expected 1777"]


def test_bin_rules():
    probe = PROBE.replace(
        "link yt-dlp /usr/local/lib/yt-dlp/bin/yt-dlp",
        "link yt-dlp /usr/local/lib/yt-dlp/bin/yt-dlp\nlink python3 /usr/bin/python3.14\nfile pip\ndangling old",
    )
    text = "\n".join(run_all(probe).problems)
    assert "python3 must not be exposed" in text
    assert "points outside /usr/local" in text
    assert "pip must not be exposed" in text
    assert "old is a dangling symlink" in text


def test_elf_failures_but_not_python_symbols():
    probe = PROBE + (
        "@@file /usr/local/lib/yt-dlp/lib/python3.14/site-packages/lxml/etree.so\n"
        "\t/lib/ld-musl-x86_64.so.1 (0x7f)\n"
        "Error relocating /usr/local/lib/yt-dlp/lib/python3.14/site-packages/lxml/etree.so: PyDict_Next: symbol not found\n"
        "Error relocating /usr/local/lib/yt-dlp/lib/python3.14/site-packages/lxml/etree.so: _PyObject_GC_New: symbol not found\n"
        "@@file /usr/local/bin/ffmpeg\n"
        "\tlibx264.so.164 => not found\n"
        "@@file /usr/local/lib/ImageMagick-7/modules/coders/heic.so\n"
        "Error relocating /usr/local/lib/ImageMagick-7/modules/coders/heic.so: heif_init: symbol not found\n"
        "@@file /usr/local/lib/libfoo.so\n"
        "Error loading shared library libbar.so.2: No such file or directory (needed by /usr/local/lib/libfoo.so)\n"
    )
    problems = run_all(probe).problems
    assert len(problems) == 3
    assert not any("lxml" in p for p in problems)
    assert any("ffmpeg" in p and "libx264" in p for p in problems)
    assert any("heif_init" in p for p in problems)


def test_probe_script_is_posix_sh():
    script = checks.PROBE_SCRIPT
    assert "[[" not in script and "<(" not in script and "pipefail" not in script
    assert "/usr/local/share/randomcontainers/*/" in script
