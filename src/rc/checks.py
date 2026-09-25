"""rc check-image: does a built image follow the image contract?

Every check runs against the loaded image before anything is pushed. The
inspection script runs as root with no network in a throwaway container,
so it sees the image exactly as shipped.
"""

import re
from dataclasses import dataclass, field

from rc.constants import COMMON_PACKAGES, METADATA_DIR
from rc.docker import Docker
from rc.errors import RcError
from rc.labels import is_managed

PROBE_SCRIPT = r"""
section() { printf '\n@@section %s\n' "$1"; }
section os-release
cat /etc/os-release 2>/dev/null || true
section alpine-release
cat /etc/alpine-release 2>/dev/null || true
section installed
if [ -f /etc/apk/world ]; then cat /etc/apk/world; elif command -v apt-mark >/dev/null 2>&1; then apt-mark showmanual; fi
section dirs
for d in /work /cache; do
  if [ -d "$d" ]; then printf '%s %s\n' "$d" "$(stat -c %a "$d")"; else printf '%s missing\n' "$d"; fi
done
section meta
for d in METADATA/*/; do
  [ -d "$d" ] || continue
  n=$(basename "$d")
  for f in version runtime-deps source; do
    if [ -f "$d$f" ]; then printf '%s %s present\n' "$n" "$f"; else printf '%s %s missing\n' "$n" "$f"; fi
  done
  printf '%s licenses %s\n' "$n" "$(find "${d}licenses" -type f 2>/dev/null | wc -l)"
  printf '%s version-value %s\n' "$n" "$(head -n 1 "${d}version" 2>/dev/null)"
done
section runtime-deps
for f in METADATA/*/runtime-deps; do [ -f "$f" ] && cat "$f"; echo; done
section bin
for f in /usr/local/bin/* /usr/local/bin/.[!.]*; do
  if [ -L "$f" ] && [ ! -e "$f" ]; then printf 'dangling %s\n' "${f##*/}"
  elif [ -L "$f" ]; then printf 'link %s %s\n' "${f##*/}" "$(readlink -f "$f")"
  elif [ -e "$f" ]; then printf 'file %s\n' "${f##*/}"; fi
done
section elf
find /usr/local -type f \( -perm -100 -o -name '*.so' -o -name '*.so.*' \) | while IFS= read -r f; do
  out=$(ldd "$f" 2>&1) || true
  case "$out" in
    *"not found"*|*"Error loading shared library"*|*"Error relocating"*) printf '@@file %s\n%s\n' "$f" "$out" ;;
  esac
done
""".replace("METADATA", METADATA_DIR)

BASE_SCRIPT = "if [ -f /etc/apk/world ]; then cat /etc/apk/world; else apt-mark showmanual; fi"

FORBIDDEN_BIN = re.compile(r"^(python[0-9.]*|pip[0-9.]*|activate.*|easy_install.*|Activate\.ps1)$")
# musl's ldd reports the CPython API as unresolved in every extension module;
# the interpreter provides those symbols at import time.
_PY_SYMBOL = re.compile(r"Error relocating .*: _?Py[A-Za-z0-9_]*: symbol not found")
_APK_CONSTRAINT = re.compile(r"[<>=~].*$")


@dataclass
class Report:
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def add(self, message: str) -> None:
        self.problems.append(message)


def parse_sections(output: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current = None
    for line in output.splitlines():
        if line.startswith("@@section "):
            current = line[len("@@section ") :].strip()
            sections[current] = []
        elif current is not None and line.strip():
            sections[current].append(line)
    return sections


def check_config(config: dict, expect: dict, labels: dict[str, str], report: Report) -> None:
    if (config.get("Entrypoint") or []) != expect["entrypoint"]:
        report.add(f"entrypoint is {config.get('Entrypoint')}, expected {expect['entrypoint']}")
    if (config.get("Cmd") or []) != expect["cmd"]:
        report.add(f"cmd is {config.get('Cmd')}, expected {expect['cmd']}")
    if config.get("User") != expect["user"]:
        report.add(f"user is {config.get('User')!r}, expected {expect['user']!r}")
    if config.get("WorkingDir") != expect["workdir"]:
        report.add(f"working directory is {config.get('WorkingDir')!r}, expected {expect['workdir']!r}")
    env = dict(item.split("=", 1) for item in config.get("Env") or [] if "=" in item)
    for key, value in expect["env"].items():
        if env.get(key) != value:
            report.add(f"environment {key} is {env.get(key)!r}, expected {value!r}")
    if config.get("Volumes"):
        report.add(f"image declares volumes {sorted(config['Volumes'])}; the contract has no VOLUME")
    actual = config.get("Labels") or {}
    for key, value in labels.items():
        if actual.get(key) != value:
            report.add(f"label {key} is {actual.get(key)!r}, expected {value!r}")
    for key in sorted(actual):
        if key not in labels and key.startswith("com.randomcontainers."):
            report.add(f"unexpected label {key} (inherited from a base image?)")
        elif key not in labels and is_managed(key):
            report.warnings.append(f"label {key}={actual[key]!r} is inherited from the base image")


def check_os(sections: dict[str, list[str]], family: str, version: str, report: Report) -> None:
    if family == "alpine":
        release = "".join(sections.get("alpine-release", [])).strip()
        if release != version and not release.startswith(version + "."):
            report.add(f"/etc/alpine-release is {release!r}, expected {version}.x")
    else:
        found = ""
        for line in sections.get("os-release", []):
            if line.startswith("VERSION_ID="):
                found = line.split("=", 1)[1].strip().strip('"')
        if found != version:
            report.add(f"/etc/os-release VERSION_ID is {found!r}, expected {version!r}")


def _package_set(lines: list[str]) -> set[str]:
    return {_APK_CONSTRAINT.sub("", line.strip()) for line in lines if line.strip()}


def check_packages(sections: dict[str, list[str]], base_set: set[str], extra: list[str], report: Report) -> None:
    ignore = base_set | set(COMMON_PACKAGES)
    installed = _package_set(sections.get("installed", [])) - ignore
    expected = (_package_set(sections.get("runtime-deps", [])) | set(extra)) - ignore
    missing = sorted(expected - installed)
    unexpected = sorted(installed - expected)
    if missing:
        report.add(f"listed in runtime-deps but not installed: {', '.join(missing)}")
    if unexpected:
        report.add(f"installed but not listed in any runtime-deps file: {', '.join(unexpected)}")


def check_metadata(sections: dict[str, list[str]], packages: dict[str, str], report: Report) -> None:
    found: dict[str, dict[str, str]] = {}
    for line in sections.get("meta", []):
        parts = line.split(" ", 2)
        if len(parts) == 3:
            found.setdefault(parts[0], {})[parts[1]] = parts[2]
        elif len(parts) == 2:
            found.setdefault(parts[0], {})[parts[1]] = ""
    for name, version in packages.items():
        meta = found.get(name)
        where = f"{METADATA_DIR}/{name}"
        if meta is None:
            report.add(f"{where} is missing")
            continue
        for f in ("version", "runtime-deps", "source"):
            if meta.get(f) != "present":
                report.add(f"{where}/{f} is missing")
        if meta.get("licenses", "0").strip() in ("", "0"):
            report.add(f"{where}/licenses/ has no license files")
        if meta.get("version-value", "").strip() != version:
            report.add(f"{where}/version is {meta.get('version-value', '').strip()!r}, expected {version!r}")
    for name in sorted(set(found) - set(packages)):
        report.add(f"{METADATA_DIR}/{name} belongs to no package of this image")


def check_dirs(sections: dict[str, list[str]], report: Report) -> None:
    for line in sections.get("dirs", []):
        path, _, mode = line.partition(" ")
        if mode != "1777":
            report.add(f"{path} has mode {mode}, expected 1777")


def check_bin(sections: dict[str, list[str]], report: Report) -> None:
    for line in sections.get("bin", []):
        parts = line.split(" ", 2)
        kind, name = parts[0], parts[1] if len(parts) > 1 else ""
        if FORBIDDEN_BIN.match(name):
            report.add(f"/usr/local/bin/{name} must not be exposed; link only the tool's entry points")
        if kind == "dangling":
            report.add(f"/usr/local/bin/{name} is a dangling symlink")
        elif kind == "link" and len(parts) == 3 and not parts[2].startswith("/usr/local/"):
            report.add(f"/usr/local/bin/{name} points outside /usr/local ({parts[2]})")


def check_elf(sections: dict[str, list[str]], report: Report, limit: int = 20) -> None:
    failures: dict[str, list[str]] = {}
    current = None
    for line in sections.get("elf", []):
        if line.startswith("@@file "):
            current = line[len("@@file ") :]
            continue
        if current is None:
            continue
        if "Error relocating" in line:
            bad = not _PY_SYMBOL.search(line)
        else:
            bad = "not found" in line or "Error loading shared library" in line
        if bad:
            failures.setdefault(current, []).append(line.strip())
    for path, lines in list(failures.items())[:limit]:
        report.add(f"unresolved libraries in {path}: {'; '.join(lines[:3])}")
    if len(failures) > limit:
        report.add(f"... and {len(failures) - limit} more files with unresolved libraries")


def check_image(docker: Docker, image: str, target: dict) -> Report:
    report = Report()
    expect = target["expect"]
    check_config(docker.inspect(image), expect, target["labels"], report)

    result = docker.shell(image, PROBE_SCRIPT)
    if result.returncode != 0:
        raise RcError(f"the inspection script failed in {image}: {result.stderr.strip()[-2000:]}")
    sections = parse_sections(result.stdout)

    base = target["distro_base"]
    if not docker.exists(base):
        docker.pull(base)
    base_result = docker.shell(base, BASE_SCRIPT)
    if base_result.returncode != 0:
        raise RcError(f"cannot list the packages of {base}: {base_result.stderr.strip()}")

    check_os(sections, target["family"], expect["os_version"], report)
    check_packages(sections, _package_set(base_result.stdout.splitlines()), expect["extra_packages"], report)
    check_metadata(sections, expect["packages"], report)
    check_dirs(sections, report)
    check_bin(sections, report)
    check_elf(sections, report)
    return report
