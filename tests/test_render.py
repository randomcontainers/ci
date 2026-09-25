import dataclasses
import json
import os
import shlex
import subprocess
import sys

import pytest
import yaml

from conftest import GOLDEN
from rc import render
from rc.config import Example
from rc.errors import RcError

COMBOS = [("yt-dlp", "yt-dlp-ffmpeg"), ("imagemagick", "imagemagick-ghostscript"), ("streamlink", "streamlink-ffmpeg")]


@pytest.mark.parametrize("owner,combo_name", COMBOS)
@pytest.mark.parametrize("distro_id", ["ubuntu", "alpine"])
def test_golden(packages, distros, owner, combo_name, distro_id):
    combo = packages[owner].default_combo
    assert combo.name == combo_name
    text = render.combo_dockerfile(combo, packages, distros.get(distro_id))
    assert text == (GOLDEN / combo_name / f"Dockerfile.{distro_id}").read_text()


def test_shape(packages, distros):
    text = render.combo_dockerfile(packages["yt-dlp"].default_combo, packages, distros.get("ubuntu"))
    lines = text.splitlines()
    assert lines[0] == f"# syntax={render.DOCKERFILE_SYNTAX}"
    assert lines[2:5] == ["FROM ffmpeg AS base", "FROM yt-dlp AS yt-dlp", "FROM base"]
    assert "COPY --link --from=yt-dlp /usr/local/ /usr/local/" in lines
    assert lines[-3:] == ['USER 1000:1000', 'ENTRYPOINT ["tini","--","yt-dlp"]', 'CMD ["--help"]']
    # POSIX sh only: no process substitution or bash arrays.
    assert "<(" not in text and "[[" not in text and "pipefail" not in text
    assert "LABEL" not in text


def test_base_member_env_is_inherited_not_repeated(packages, distros):
    ffmpeg = packages["ffmpeg"]
    env_ffmpeg = dataclasses.replace(ffmpeg, image=dataclasses.replace(ffmpeg.image, env=(("FFREPORT", "level=32"),)))
    pk = dict(packages, ffmpeg=env_ffmpeg)
    text = render.combo_dockerfile(pk["yt-dlp"].default_combo, pk, distros.get("ubuntu"))
    assert "FFREPORT" not in text


@pytest.mark.parametrize(
    "value,expected",
    [
        ("/cache/deno", "/cache/deno"),
        ("1", "1"),
        ("", '""'),
        ("a b", '"a b"'),
        ('say "hi"', '"say \\"hi\\""'),
        ("$HOME/x", '"\\$HOME/x"'),
        ("back\\slash", '"back\\\\slash"'),
    ],
)
def test_env_value(value, expected):
    assert render.env_value(value) == expected


def test_env_value_rejects_control_characters():
    with pytest.raises(RcError):
        render.env_value("a\nb")


def test_refuses_unsafe_values(packages, distros):
    combo = packages["yt-dlp"].default_combo
    bad = dataclasses.replace(combo, extras=(("alpine", dataclasses.replace(combo.extras_for("alpine"), packages=("deno; rm -rf /",))),))
    with pytest.raises(RcError):
        render.combo_dockerfile(bad, packages, distros.get("alpine"))


def test_exec_form_is_json(packages, distros):
    ytdlp = packages["yt-dlp"]
    odd = dataclasses.replace(ytdlp, image=dataclasses.replace(ytdlp.image, cmd=('say "hi"', "a\\b")))
    text = render.combo_dockerfile(odd.default_combo, dict(packages, **{"yt-dlp": odd}), distros.get("ubuntu"))
    assert 'CMD ["say \\"hi\\"","a\\\\b"]' in text


def test_repo_files(packages, distros):
    files = render.combo_repo_files(packages["yt-dlp"].default_combo, packages, distros)
    assert set(files) == {
        "combo.yml",
        "Dockerfile.ubuntu",
        "Dockerfile.alpine",
        "README.md",
        "LICENSE",
        ".github/workflows/build.yml",
        ".github/zizmor.yml",
        ".hadolint.yaml",
    }
    combo = yaml.safe_load(files["combo.yml"])
    assert combo == {"name": "yt-dlp-ffmpeg", "owner": "yt-dlp", "with": ["ffmpeg"]}
    readme = files["README.md"]
    assert "2026.08.19" not in readme and "9.0.2" not in readme
    assert "`Unlicense AND GPL-3.0-or-later AND MIT`" in readme
    assert "$" not in readme.replace("$(id", "").replace("$PWD", "")
    assert "It contains yt-dlp and FFmpeg, plus `deno`, on Ubuntu or Alpine," in readme
    assert "- Alpine 3.24: distro package `deno` (MIT)" in readme
    assert (
        "```sh\ndocker run --rm --user \"$(id -u):$(id -g)\" -v \"$PWD:/work\" ghcr.io/randomcontainers/yt-dlp-ffmpeg "
        '"https://www.youtube.com/watch?v=..."\n```'
    ) in readme
    assert "<arguments>" not in readme and "AGPL" not in readme
    ffmpeg = "- FFmpeg: [randomcontainers/ffmpeg](https://github.com/randomcontainers/ffmpeg#licenses)"
    assert f"{ffmpeg} has a `v<version>` release" in readme
    assert "Ubuntu and Alpine packages keep their own licenses. The SBOM" in readme
    assert "## Files and permissions" in readme and "## Updates" in readme
    assert "## Requesting a tool\n\nTo suggest another tool, use the [Request a tool]" in readme
    assert (
        "gh attestation verify oci://ghcr.io/randomcontainers/yt-dlp-ffmpeg:latest \\\n"
        "  --repo randomcontainers/yt-dlp-ffmpeg --signer-repo randomcontainers/ci\n"
    ) in readme
    assert "`--repo randomcontainers/yt-dlp --signer-repo randomcontainers/ci`" in readme
    assert "tags ending in `ubuntu26.04` or `alpine3.24` stay on that release" in readme
    assert "Only the tags of the yt-dlp version currently in [yt-dlp's package.yml]" in readme
    assert "(https://github.com/orgs/randomcontainers/packages/container/package/yt-dlp-ffmpeg)" in readme
    assert readme.count("randomcontainers.com/yt-dlp-ffmpeg") == 1
    assert "The same images can also be pulled as `randomcontainers.com/yt-dlp-ffmpeg`." in readme
    assert "All tags" not in readme and "existing tags" not in readme
    assert yaml.safe_load(files[".hadolint.yaml"]) == {"ignored": ["DL3006", "DL3008", "DL3018"]}
    assert yaml.safe_load(files[".github/zizmor.yml"])["rules"]["unpinned-uses"]["config"]["policies"] == {
        "randomcontainers/*": "ref-pin",
        "*": "hash-pin",
    }
    assert files["LICENSE"].startswith("MIT License")
    workflow = yaml.safe_load(files[".github/workflows/build.yml"])
    assert workflow["jobs"]["build"]["uses"] == "randomcontainers/ci/.github/workflows/build.yml@v1"
    assert workflow["jobs"]["build"]["with"]["default-only"] is False
    triggers = workflow[True] if True in workflow else workflow["on"]
    assert set(triggers) == {"workflow_dispatch"}
    assert set(triggers["workflow_dispatch"]["inputs"]) == {"want"}


def test_readme_of_a_combo_with_agpl_software(packages, distros):
    readme = render.combo_repo_files(packages["imagemagick"].default_combo, packages, distros)["README.md"]
    assert "It contains ImageMagick and Ghostscript, on Ubuntu or Alpine," in readme
    assert (
        "Ghostscript is licensed under AGPL-3.0-or-later. "
        "Use `ghcr.io/randomcontainers/imagemagick:slim` if your policy excludes AGPL software."
    ) in readme
    assert "- Ghostscript: [randomcontainers/ghostscript](https://github.com/randomcontainers/ghostscript#licenses)" in readme
    assert "- ImageMagick:" not in readme.split("Corresponding source:")[1].split("\n\n")[1]
    assert "Also installed" not in readme and "plus" not in readme.split("\n")[4]


def test_quick_start_uses_the_owner_example(packages):
    ytdlp = packages["yt-dlp"]
    example = lambda command: dataclasses.replace(ytdlp, examples=(Example("x", command),))
    quick = lambda command: render._quick_start(example(command), "yt-dlp-ffmpeg", "randomcontainers")
    combo = "ghcr.io/randomcontainers/yt-dlp-ffmpeg"
    assert quick("docker run --rm ghcr.io/randomcontainers/yt-dlp -F URL") == f"docker run --rm {combo} -F URL"
    assert quick("docker run --rm randomcontainers.com/yt-dlp -F URL") == f"docker run --rm {combo} -F URL"
    for command in ("docker run --rm randomcontainers.com/yt-dlp:slim -F URL", "echo randomcontainers.com/yt-dlp-x"):
        assert quick(command) == f"docker run --rm {combo}"


def test_repo_files_are_stable(packages, distros):
    a = render.combo_repo_files(packages["imagemagick"].default_combo, packages, distros)
    b = render.combo_repo_files(packages["imagemagick"].default_combo, packages, distros)
    assert a == b


def test_markdown_link_targets_are_escaped():
    assert render._md_url("https://example.org/a_(b)") == "https://example.org/a_%28b%29"


def test_runtime_deps_check_follows_the_distro_family(packages, distros):
    combo = packages["yt-dlp"].default_combo
    alpine = render.combo_dockerfile(combo, packages, distros.get("alpine"))
    ubuntu = render.combo_dockerfile(combo, packages, distros.get("ubuntu"))
    assert "grep -Ev '^((so:)?[A-Za-z0-9][A-Za-z0-9+._-]*)?$' /tmp/runtime-deps" in alpine
    assert "grep -Ev '^([a-z0-9][a-z0-9+.-]*)?$' /tmp/runtime-deps" in ubuntu


def test_runtime_deps_check_in_a_shell(packages, distros, tmp_path):
    """Run the rendered check on runtime-deps files like the ones an image carries."""
    text = render.combo_dockerfile(packages["yt-dlp"].default_combo, packages, distros.get("alpine"))
    check = next(line for line in text.splitlines() if "grep -Ev" in line).strip().removesuffix("; \\")
    script = check.replace("/tmp/runtime-deps", str(tmp_path / "deps"))
    for content, ok in [
        ("font-dejavu\nso:libSvtAv1Enc.so.4\nso:libstdc++.so.6\n\n", True),
        ("so:libSvtAv1Enc.so.4\n$(id)\n", False),
        ("deno; rm -rf /\n", False),
        ("-X\n", False),
    ]:
        (tmp_path / "deps").write_text(content)
        result = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
        assert (result.returncode == 0) is ok, (content, result.stderr)


def test_record_wheels_program_fits_in_single_quotes():
    assert not any("'" in line for line in render._RECORD_WHEELS)


def _wheel(site, name, version, license_file):
    info = site / f"{name}-{version}.dist-info"
    (info / "licenses").mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n")
    record = [f"{info.name}/METADATA,,", f"{info.name}/RECORD,,"]
    if license_file:
        (info / "licenses" / license_file).write_text(f"{name} license\n")
        record.append(f"{info.name}/licenses/{license_file},,")
    (info / "RECORD").write_text("\n".join(record) + "\n")
    return {
        "metadata": {"name": name, "version": version},
        "download_info": {
            "url": f"https://files.pythonhosted.org/packages/{name}-{version}-py3-none-any.whl",
            "archive_info": {"hashes": {"sha256": "ab" * 32}},
        },
    }


def test_requirements_extras_are_recorded(packages, distros, tmp_path):
    """The rendered pip step records the wheels in the owner's source file and licenses/."""
    text = render.combo_dockerfile(packages["yt-dlp"].default_combo, packages, distros.get("ubuntu"))
    body = text.split("\nRUN ", 1)[1].split("\nENV ", 1)[0].replace("\\\n", "")
    step = next(s for s in body.split("; ") if "/bin/python -c" in s)

    site, meta = tmp_path / "site", tmp_path / "meta"
    meta.mkdir()
    (meta / "source").write_text("https://files.pythonhosted.org/packages/yt_dlp.whl#sha256=00\n")
    report = tmp_path / "report.json"
    report.write_text(json.dumps({"install": [_wheel(site, "deno", "2.9.5", "LICENSE")]}))
    script = (
        step.replace("/usr/local/lib/yt-dlp/bin/python", shlex.quote(sys.executable))
        .replace("/tmp/extras-report.json", shlex.quote(str(report)))
        .replace("/usr/local/share/randomcontainers/yt-dlp", shlex.quote(str(meta)))
    )
    env = dict(os.environ, PYTHONPATH=str(site))
    result = subprocess.run(["sh", "-eu", "-c", script], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    assert (meta / "source").read_text().splitlines()[1] == (
        f"https://files.pythonhosted.org/packages/deno-2.9.5-py3-none-any.whl#sha256={'ab' * 32}"
    )
    assert (meta / "licenses" / "deno" / "LICENSE").read_text() == "deno license\n"

    report.write_text(json.dumps({"install": [_wheel(site, "nolicense", "1.0", None)]}))
    result = subprocess.run(["sh", "-eu", "-c", script], capture_output=True, text=True, env=env)
    assert result.returncode != 0 and "no license file found for nolicense" in result.stderr


def test_no_requirements_step_without_requirements(packages, distros):
    text = render.combo_dockerfile(packages["yt-dlp"].default_combo, packages, distros.get("alpine"))
    assert "pip install" not in text and "extras-report" not in text


@pytest.mark.parametrize("name", ["deno\n", "Deno"])
def test_refuses_package_names_the_distro_does_not_allow(packages, distros, name):
    combo = packages["yt-dlp"].default_combo
    ubuntu = dataclasses.replace(combo.extras_for("ubuntu"), packages=(name,))
    bad = dataclasses.replace(combo, extras=(("ubuntu", ubuntu),))
    with pytest.raises(RcError, match="refusing to render package name"):
        render.combo_dockerfile(bad, packages, distros.get("ubuntu"))
