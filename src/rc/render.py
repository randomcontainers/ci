"""Generated files: the combo Dockerfile and the files of a combo repository.

The combo Dockerfile has one stage per member. Bake supplies each member
as a named build context with the member's name: the owner's slim target
or a published slim image pinned by digest. Apart from the Dockerfile
frontend on its first line, the file holds no versions, digests or labels.
"""

import json
import re
import string
from collections.abc import Mapping
from importlib import resources

from rc import catalog, names
from rc.config import Combo, Distro, Distros, Package
from rc.constants import ALIAS, IMAGE_USER, METADATA_DIR, ORG, REGISTRY
from rc.errors import RcError

_SIMPLE_ENV = re.compile(r"^[A-Za-z0-9_./:@%+,=-]+$")
_DISTRO_NAMES = {"ubuntu": "Ubuntu", "alpine": "Alpine", "debian": "Debian"}

# Package Dockerfiles pin the same frontend. Keep it on the release that
# ships with the moby/buildkit pinned in build.yml.
DOCKERFILE_SYNTAX = "docker/dockerfile:1.27.0@sha256:bde3983e9c939224420ddaf6b784cc30e09b035a4dea01f581230c50809f372e"


# Records the wheels installed from a combo's requirements file the way the
# owner's build records its own: URL and hash appended to `source`, license
# files copied to licenses/<name>/. Runs with the owner's venv interpreter
# and is passed to it line by line, so it must not contain single quotes.
_RECORD_WHEELS = (
    "import json, pathlib, shutil, sys",
    "from importlib import metadata",
    'NAMES = ("LICENSE", "LICENCE", "COPYING", "NOTICE", "AUTHORS")',
    "report, meta = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])",
    'installed = json.loads(report.read_text())["install"]',
    'with open(meta / "source", "a") as source:',
    "    for item in installed:",
    '        info = item["download_info"]',
    '        print(info["url"] + "#sha256=" + info["archive_info"]["hashes"]["sha256"], file=source)',
    "for item in installed:",
    '    dist = metadata.distribution(item["metadata"]["name"])',
    '    name = dist.metadata["Name"]',
    "    files = [",
    "        p for p in dist.files or ()",
    '        if len(p.parts) > 1 and p.parts[0].endswith(".dist-info")',
    '        and (p.parts[1] == "licenses" or p.name.upper().startswith(NAMES))',
    "    ]",
    "    if not files:",
    '        sys.exit("no license file found for " + name)',
    "    for path in files:",
    '        rel = path.parts[2:] if path.parts[1] == "licenses" else path.parts[1:]',
    '        target = meta.joinpath("licenses", name, *rel)',
    "        target.parent.mkdir(parents=True, exist_ok=True)",
    "        shutil.copyfile(path.locate(), target)",
)


def _safe(value: str, pattern: re.Pattern, what: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise RcError(f"refusing to render {what} {value!r}")
    return value


def env_value(value: str) -> str:
    """Quote an ENV value so the Dockerfile parser reads it back unchanged."""
    if not names.is_printable_ascii(value, 1024):
        raise RcError(f"refusing to render environment value {value!r}")
    if value and _SIMPLE_ENV.match(value):
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
    return f'"{escaped}"'


def _exec_form(items) -> str:
    for item in items:
        if not item or not all(ch.isprintable() for ch in item):
            raise RcError(f"refusing to render exec-form argument {item!r}")
    return json.dumps(list(items), separators=(",", ":"))


def combo_dockerfile(combo: Combo, packages: Mapping[str, Package], distro: Distro, org: str = ORG) -> str:
    members = catalog.combo_members(combo, packages)
    for member in members:
        _safe(member.name, names.NAME, "member name")
    owner = packages[combo.owner]
    base = combo.base or combo.with_[0]
    others = [m.name for m in members if m.name != base]
    extras = combo.extras_for(distro.id)
    for package in extras.packages:
        if not names.is_distro_package(package, distro.family):
            raise RcError(f"refusing to render package name {package!r}")

    base_env = packages[base].image.env_map
    env = {k: v for k, v in catalog.combo_env(combo, packages).items() if base_env.get(k) != v}

    lines = [
        f"# syntax={DOCKERFILE_SYNTAX}",
        f"# Generated from {org}/{combo.owner} package.yml by {org}/ci. Do not edit.",
        f"FROM {base} AS base",
    ]
    lines += [f"FROM {name} AS {name}" for name in others]
    lines += ["FROM base", "USER 0:0"]
    if distro.family == "debian":
        lines.append("ARG DEBIAN_FRONTEND=noninteractive")
    lines += [f"COPY --link --from={name} /usr/local/ /usr/local/" for name in others]

    deps = "/tmp/runtime-deps"
    steps = [
        "set -eu",
        f'for f in {METADATA_DIR}/*/runtime-deps; do cat "$f"; echo; done > {deps}',
        f"sort -u -o {deps} {deps}",
        f"if grep -Ev '{names.distro_package_ere(distro.family)}' {deps} >&2; then"
        " echo 'runtime-deps: invalid package name' >&2; exit 1; fi",
    ]
    extra = "".join(f" {p}" for p in extras.packages)
    if distro.family == "debian":
        steps += ["apt-get update", f"xargs apt-get install -y --no-install-recommends{extra} < {deps}"]
    else:
        steps += [f"xargs apk add --no-cache{extra} < {deps}"]
    temp = [deps]
    if extras.requirements:
        req = _safe(extras.requirements, names.FILE_NAME, "requirements file")
        venv, meta, report = f"/usr/local/lib/{owner.name}", f"{METADATA_DIR}/{owner.name}", "/tmp/extras-report.json"
        program = "".join(f" \\\n        '{line}'" for line in _RECORD_WHEELS)
        steps += [
            f"{venv}/bin/pip install --no-cache-dir --disable-pip-version-check --no-input --no-deps"
            f" --require-hashes --only-binary=:all: --report {report} -r {meta}/{req}",
            f"{venv}/bin/python -c \"$(printf '%s\\n'{program})\" \\\n        {report} {meta}",
        ]
        temp.append(report)
    if distro.family == "debian":
        steps += [f"rm -rf /var/lib/apt/lists/* {' '.join(temp)}", "ldconfig"]
    else:
        steps.append(f"rm -f {' '.join(temp)}")
    lines.append("RUN " + "; \\\n    ".join(steps))

    if env:
        pairs = [f"{_safe(k, names.ENV_KEY, 'variable name')}={env_value(v)}" for k, v in env.items()]
        lines.append("ENV " + " \\\n    ".join(pairs))
    lines += [
        f"USER {IMAGE_USER}",
        f"ENTRYPOINT {_exec_form(['tini', '--', *owner.image.entrypoint])}",
        f"CMD {_exec_form(owner.image.cmd)}",
    ]
    return "\n".join(lines) + "\n"


def _template(name: str) -> str:
    return resources.files("rc").joinpath("templates", "combo", name).read_text(encoding="utf-8")


def _distro_label(distro: Distro) -> str:
    return f"{_DISTRO_NAMES.get(distro.id, distro.id.capitalize())} {distro.version}"


def combo_repo_files(
    combo: Combo, packages: Mapping[str, Package], distros: Distros, org: str = ORG
) -> dict[str, str]:
    """All files of a generated combo repository, keyed by path."""
    members = catalog.combo_members(combo, packages)
    owner = packages[combo.owner]
    files = {
        "combo.yml": string.Template(_template("combo.yml")).substitute(
            org=org,
            owner=combo.owner,
            name=combo.name,
            members=", ".join(combo.with_),
        ),
        ".github/workflows/build.yml": _template("build.yml"),
        ".github/zizmor.yml": _template("zizmor.yml"),
        ".hadolint.yaml": _template("hadolint.yaml"),
        "LICENSE": _template("LICENSE"),
    }
    for distro in distros.items:
        files[f"Dockerfile.{distro.id}"] = combo_dockerfile(combo, packages, distro, org)

    tag_rows = []
    for distro in distros.items:
        tags = ["`latest`", "`<version>`"] if distro.id == distros.default else []
        tags += [f"`{distro.id}`", f"`<version>-{distro.id}`", f"`<version>-{distro.qualified}`"]
        tag_rows.append(f"| {', '.join(tags)} | {_distro_label(distro)} |")

    member_rows = [
        f"| [{m.title}]({_md_url(m.homepage)}) | `{m.license}` | [{org}/{m.name}](https://github.com/{org}/{m.name}) |"
        for m in members
    ]
    extras_lines = []
    for distro in distros.items:
        extras = combo.extras_for(distro.id)
        parts = []
        if extras.packages:
            noun = "distro package " if len(extras.packages) == 1 else "distro packages "
            parts.append(noun + ", ".join(f"`{p}`" for p in extras.packages))
        if extras.requirements:
            parts.append(f"Python packages from {owner.title}'s `{extras.requirements}`")
        if parts:
            extras_lines.append(f"- {_distro_label(distro)}: {'; '.join(parts)} ({combo.extras_license})")
    extras_text = ""
    if extras_lines:
        extras_text = "Also installed:\n\n" + "\n".join(extras_lines) + "\n\n"
    if any(combo.extras_for(d.id).requirements for d in distros.items):
        meta = f"{METADATA_DIR}/{owner.name}"
        extras_text += (
            f"The wheel URL and hash of each of these Python packages are listed in `{meta}/source`,"
            f" and its license files are in `{meta}/licenses/<name>/`.\n\n"
        )

    by_distro = {d.id: catalog.combo_license(combo, packages, d.id) for d in distros.items}
    if len(set(by_distro.values())) == 1:
        license_text = f"`{next(iter(by_distro.values()))}`"
    else:
        license_text = ", ".join(f"`{expr}` on {_distro_label(distros.get(d))}" for d, expr in by_distro.items())

    extra_names = list(dict.fromkeys(p for d in distros.items for p in combo.extras_for(d.id).packages))
    title = _join_words([m.title for m in members])
    if extra_names:
        title += ", plus " + _join_words([f"`{p}`" for p in extra_names])
    elif any(combo.extras_for(d.id) for d in distros.items):
        title += ", plus the Python packages listed under Contents"

    owner_link = f"[{combo.owner}](https://github.com/{org}/{combo.owner})"
    if combo.default:
        intro = (
            f"This image is the default image of {owner_link} (`{REGISTRY}/{org}/{combo.owner}:latest`), published under "
            "its own name. The contents are the same; the digests differ."
        )
    else:
        intro = f"This image adds {_join_words([m.title for m in members[1:]])} to the {owner_link} image."

    agpl = ""
    agpl_members = [m for m in members if _agpl_terms(m.license)]
    if agpl_members and not _agpl_terms(owner.license):
        for m in agpl_members:
            agpl += f" {m.title} is licensed under {_join_words(_agpl_terms(m.license))}."
        agpl += f" Use `{REGISTRY}/{org}/{combo.owner}:slim` if your policy excludes AGPL software."

    sources = [
        f"- {m.title}: [{org}/{m.name}](https://github.com/{org}/{m.name}#licenses) has a `v<version>` release "
        f"with the source of each {m.title} version it builds."
        for m in members
        if m.source_release
    ]
    sources_text = "Corresponding source:\n\n" + "\n".join(sources) + "\n\n" if sources else ""

    files["README.md"] = string.Template(_template("README.md")).substitute(
        name=combo.name,
        intro=intro,
        agpl=agpl,
        quick_start=_quick_start(owner, combo.name, org),
        distro_names=_join_words(list(dict.fromkeys(_distro_label(d).rsplit(" ", 1)[0] for d in distros.items)), "or"),
        meta=METADATA_DIR,
        sources=sources_text,
        title=title,
        summary=combo.summary,
        org=org,
        owner=combo.owner,
        owner_title=owner.title,
        alias=ALIAS,
        registry=REGISTRY,
        tag_rows="\n".join(tag_rows),
        default_distro=_distro_label(distros.get(distros.default)),
        distro_list=_join_words([_distro_label(d) for d in distros.items]),
        distro_suffixes=_join_words([f"`{d.qualified}`" for d in distros.items], "or"),
        member_rows="\n".join(member_rows),
        extras=extras_text,
        license=license_text,
    )
    return files


def _agpl_terms(expression: str) -> list[str]:
    return list(dict.fromkeys(t for t in re.findall(r"[A-Za-z0-9.+-]+", expression) if t.startswith("AGPL-")))


def _quick_start(owner: Package, name: str, org: str) -> str:
    """The owner's first example that runs its image, pointed at the combo image."""
    names_ = rf"(?:{re.escape(ALIAS)}|{re.escape(REGISTRY)}/{re.escape(org)})/{re.escape(owner.name)}"
    image = re.compile(rf"(?<![\w./-]){names_}(?![\w./:@-])")
    for example in owner.examples:
        if image.search(example.command):
            return image.sub(f"{REGISTRY}/{org}/{name}", example.command, count=1)
    return f"docker run --rm {REGISTRY}/{org}/{name}"


def _join_words(words: list[str], conjunction: str = "and") -> str:
    return words[0] if len(words) == 1 else f"{', '.join(words[:-1])} {conjunction} {words[-1]}"


def _md_url(url: str) -> str:
    """Escape the characters that end a Markdown link target."""
    return url.replace("(", "%28").replace(")", "%29").replace(" ", "%20")
