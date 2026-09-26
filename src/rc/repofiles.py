"""Files a package repository needs next to its package.yml (docs/adding-a-package.md, "Files").

These are light text checks that fail early with a clear message; the
build and check-image cover the rest.
"""

import re
from pathlib import Path

from rc.config import Distros, Package

_ARG = re.compile(r"^\s*ARG\s+([A-Za-z_][A-Za-z0-9_]*)", re.M | re.I)
_BASE_DEFAULT = re.compile(r"^\s*ARG\s+BASE_IMAGE\s*=\s*(\S+)", re.M | re.I)
_FROM = re.compile(r"^\s*FROM\s+(?:--\S+\s+)*\S+(?:\s+AS\s+(\S+))?\s*$", re.M | re.I)


def package_problems(package: Package, directory: Path, distros: Distros) -> list[str]:
    problems = []
    common = ["BASE_IMAGE", "VERSION"]
    if package.upstream.artifact:
        common.append("SOURCE_SHA256")
    if package.upstream.git:
        common.append("SOURCE_COMMIT")
    for distro in distros.items:
        name = f"Dockerfile.{distro.id}"
        path = directory / name
        if not path.is_file():
            problems.append(f"{name} is missing")
            continue
        text = path.read_text(encoding="utf-8")
        declared = set(_ARG.findall(text))
        extras = [e for e in package.upstream.extra_artifacts if e.used_on(distro.id)]
        required = common + [f"{e.prefix}_SHA256" for e in extras]
        for arg in required:
            if arg not in declared:
                problems.append(f"{name} does not declare ARG {arg}")
        stages = _FROM.findall(text)
        if not stages or stages[-1].lower() != "slim":
            problems.append(f"{name}: the last stage must be named slim")
    for combo in package.combos:
        for _, extras in combo.extras:
            if extras.requirements and not (directory / extras.requirements).is_file():
                problems.append(f"combo {combo.name}: {extras.requirements} is missing")
    return problems


def distro_problems(directory: Path, distros: Distros) -> list[str]:
    """Distro releases named in the Dockerfiles and README.md that distros.yml does not build on.

    The build always passes BASE_IMAGE, so a stale default or README only
    shows when someone builds locally or reads the tag table.
    """
    problems = []
    for distro in distros.items:
        path = directory / f"Dockerfile.{distro.id}"
        if not path.is_file():
            continue
        for value in _BASE_DEFAULT.findall(path.read_text(encoding="utf-8")):
            if value.strip("\"'") != distro.image:
                problems.append(f"{path.name}: ARG BASE_IMAGE defaults to {value}, not {distro.image} from distros.yml")
    readme = directory / "README.md"
    if readme.is_file():
        ids = "|".join(re.escape(d.id) for d in distros.items)
        named = re.findall(rf"\b({ids}) ?([0-9]+\.[0-9]+)\b", readme.read_text(encoding="utf-8"), re.I)
        for distro_id, version in sorted({(d.lower(), v) for d, v in named}):
            current = distros.get(distro_id).version
            if version != current:
                problems.append(f"README.md names {distro_id} {version}, but distros.yml builds on {current}")
    return problems
