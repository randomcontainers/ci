"""Rules that span several packages: combo members, names, env and licenses."""

from collections.abc import Mapping

from rc import names
from rc.config import Combo, Package
from rc.constants import RESERVED_NAMES
from rc.errors import RcError

# GHCR shows at most this many characters of org.opencontainers.image.licenses.
LICENSE_MAX = 256


def combo_members(combo: Combo, packages: Mapping[str, Package]) -> list[Package]:
    missing = [m for m in combo.members if m not in packages]
    if missing:
        raise RcError(f"{combo.name}: no definition for {', '.join(missing)}")
    return [packages[m] for m in combo.members]


def combo_env(combo: Combo, packages: Mapping[str, Package]) -> dict[str, str]:
    """Environment of every member plus the combo's own, sorted by name."""
    env: dict[str, str] = {}
    for member in combo_members(combo, packages):
        env.update(member.image.env)
    env.update(combo.env)
    return dict(sorted(env.items()))


def combo_license(combo: Combo, packages: Mapping[str, Package], distro_id: str) -> str:
    terms = [m.license for m in combo_members(combo, packages)]
    if combo.extras_for(distro_id) and combo.extras_license:
        terms.append(combo.extras_license)
    return names.spdx_and(terms)


def combo_title(combo: Combo, packages: Mapping[str, Package]) -> str:
    return " + ".join(m.title for m in combo_members(combo, packages))


def validate_catalog(packages: Mapping[str, Package], listed: tuple[str, ...]) -> list[str]:
    """Checks that need every package definition at once."""
    return [text for _, text in catalog_problems(packages, listed)]


def catalog_problems(packages: Mapping[str, Package], listed: tuple[str, ...]) -> list[tuple[tuple[str, ...], str]]:
    """Each problem with the names of the combos it is about; none for a package problem."""
    problems: list[tuple[tuple[str, ...], str]] = []
    for name in packages:
        if name not in listed:
            problems.append(((), f"{name}: not listed in packages.yml"))
    combo_owner: dict[str, str] = {}
    member_sets: dict[frozenset, str] = {}
    for package in packages.values():
        for combo in package.combos:
            where = f"{package.name}: combo {combo.name}"
            found: list[str] = []
            for member in combo.with_:
                if member not in listed:
                    found.append(f"{where}: {member} is not listed in packages.yml")
                elif member not in packages:
                    found.append(f"{where}: no package.yml for {member}")
            if combo.name in listed or combo.name in packages:
                found.append(f"{where}: the name is already used by a package")
            if combo.name in RESERVED_NAMES:
                found.append(f"{where}: the name is reserved")
            if combo.name in combo_owner:
                found.append(f"{where}: also declared by {combo_owner[combo.name]}")
            combo_owner.setdefault(combo.name, package.name)
            key = frozenset(combo.members)
            if key in member_sets and member_sets[key] != combo.name:
                problems.append(((combo.name, member_sets[key]), f"{where}: same members as {member_sets[key]}"))
            member_sets.setdefault(key, combo.name)
            if all(m in packages for m in combo.members):
                found.extend(f"{where}: {p}" for p in combo_problems(combo, packages))
            problems.extend(((combo.name,), text) for text in found)
    return problems


def combo_problems(combo: Combo, packages: Mapping[str, Package]) -> list[str]:
    """Problems that only show once the member definitions are known."""
    problems = env_conflicts(combo, packages)
    terms = [m.license for m in combo_members(combo, packages)]
    if combo.extras and combo.extras_license:
        terms.append(combo.extras_license)
    if len(names.spdx_and(terms)) > LICENSE_MAX:
        problems.append(f"the combined license expression is longer than {LICENSE_MAX} characters")
    return problems


def env_conflicts(combo: Combo, packages: Mapping[str, Package]) -> list[str]:
    problems = []
    seen: dict[str, tuple[str, str]] = {}
    sources = [(m.name, m.image.env) for m in combo_members(combo, packages)]
    sources.append((f"combo {combo.name}", combo.env))
    for source, env in sources:
        for key, value in env:
            if key in seen and seen[key][1] != value:
                problems.append(f"{key} is {seen[key][1]!r} in {seen[key][0]} but {value!r} in {source}")
            seen.setdefault(key, (source, value))
    return problems
