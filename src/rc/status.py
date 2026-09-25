"""status/catalog.json and the body of the tracking issue.

catalog.json is the machine-readable list of the published images: every
package and combo with its published variants and their tags. Its times
are the build times of the published images, not the time of the pass, so
a pass that finds nothing new produces the same bytes and no commit.
"""

import json
from collections.abc import Mapping
from dataclasses import dataclass, field

from rc import catalog, names, tags, versions
from rc.config import Combo, Distros, Package
from rc.constants import ALIAS, ORG, PLATFORMS, REGISTRY
from rc.state import Published, RegistryState

SCHEMA = 1
ISSUE_TITLE = "Reconciler status"
SECTIONS = (
    ("public", "Waiting to be made public"),
    ("blocked", "Blocked"),
    ("builds", "Builds"),
    ("upstream", "Upstream"),
    ("repositories", "Repositories"),
    ("health", "Health"),
    ("errors", "Errors"),
)


def empty(distros: Distros, org: str = ORG) -> dict:
    return {
        "schema": SCHEMA,
        "registry": {"alias": ALIAS, "ghcr": f"{REGISTRY}/{org}"},
        "distros": [{"id": d.id, "version": d.version, "default": d.id == distros.default} for d in distros.items],
        "platforms": [p.platform for p in PLATFORMS],
        "images": [],
    }


def _variant(state: RegistryState, name: str, flavour: str, distro, published: Published, tag_list, previous) -> dict:
    size = previous.get((name, published.digest))
    if size is None:
        size = state.sizes(name, published.digest)
    return {
        "flavour": flavour,
        "distro": distro.id,
        "version": published.version,
        "tags": tag_list,
        "digest": published.digest,
        "size": size,
    }


def _previous_sizes(previous: dict | None) -> dict[tuple[str, str], dict]:
    out = {}
    for image in (previous or {}).get("images", []):
        for v in image.get("variants", []):
            if isinstance(v.get("size"), dict) and v["size"] and isinstance(v.get("digest"), str):
                out[(image.get("name"), v["digest"])] = v["size"]
    return out


def _updated(published: list[Published]) -> str | None:
    times = sorted(p.created for p in published if p.created)
    return times[-1].strftime("%Y-%m-%dT%H:%M:%SZ") if times else None


def _valid(published: Published | None, versioning: str) -> bool:
    if published is None or not names.DIGEST.match(published.digest):
        return False
    try:
        versions.validate(versioning, published.version)
    except versions.VersionError:
        return False
    return True


def package_entry(
    package: Package,
    packages: Mapping[str, Package],
    distros: Distros,
    state: RegistryState,
    *,
    stale: bool,
    previous: dict,
    org: str = ORG,
) -> dict:
    image = state.image(package.name)
    combo = package.default_combo
    variants: list[dict] = []
    seen: list[Published] = []
    default_ready = False
    for flavour in ("default", "slim"):
        for distro in distros.items:
            tag = distro.id if flavour == "default" else f"slim-{distro.id}"
            published = state.tag(package.name, tag)
            if not _valid(published, package.versioning):
                continue
            tag_list = tags.package_tags(flavour, published.version, package.versioning, distro, distros.default).tags
            variants.append(_variant(state, package.name, flavour, distro, published, tag_list, previous))
            seen.append(published)
            if flavour == "default" and distro.id == distros.default:
                default_ready = True
    if combo:
        members = [packages[m] for m in combo.members if m in packages]
        complete = len(members) == len(combo.members)
        by_distro = [catalog.combo_license(combo, packages, d.id) for d in distros.items] if complete else [package.license]
        default = {
            "kind": "combo",
            "combo": combo.name,
            "with": list(combo.with_),
            "base": combo.base,
            "summary": combo.summary,
            "license": names.spdx_and(by_distro),
        }
    else:
        default = {"kind": "slim"}
    return {
        "name": package.name,
        "kind": "package",
        "title": package.title,
        "summary": package.summary,
        "homepage": package.homepage,
        "repository": f"https://github.com/{org}/{package.name}",
        "license": package.license,
        "version": package.version,
        "versioning": package.versioning,
        "examples": [{"title": e.title, "command": e.command} for e in package.examples],
        "source_release": package.source_release,
        "default": default,
        "combos": [c.name for c in package.combos],
        "public": image.readable,
        "default_ready": default_ready,
        "stale": stale,
        "updated": _updated(seen),
        "variants": variants,
    }


def combo_entry(combo: Combo, packages: Mapping[str, Package], distros: Distros, state: RegistryState, *, previous: dict, org: str = ORG) -> dict:
    owner = packages[combo.owner]
    image = state.image(combo.name)
    variants: list[dict] = []
    seen: list[Published] = []
    for distro in distros.items:
        published = state.tag(combo.name, distro.id)
        if not _valid(published, owner.versioning):
            continue
        tag_list = tags.combo_tags(published.version, distro, distros.default).tags
        variants.append(_variant(state, combo.name, "default", distro, published, tag_list, previous))
        seen.append(published)
    licenses = names.spdx_and([catalog.combo_license(combo, packages, d.id) for d in distros.items])
    return {
        "name": combo.name,
        "kind": "combo",
        "owner": combo.owner,
        "members": list(combo.members),
        "base": combo.base,
        "title": catalog.combo_title(combo, packages),
        "summary": combo.summary,
        "repository": f"https://github.com/{org}/{combo.name}",
        "license": licenses,
        "version": owner.version,
        "public": image.readable,
        "updated": _updated(seen),
        "variants": variants,
    }


def document(
    distros: Distros,
    packages: Mapping[str, Package],
    combos: list[Combo],
    state: RegistryState,
    *,
    stale: set[str] = frozenset(),
    previous: dict | None = None,
    org: str = ORG,
) -> dict:
    sizes = _previous_sizes(previous)
    doc = empty(distros, org)
    images = [
        package_entry(p, packages, distros, state, stale=p.name in stale, previous=sizes, org=org)
        for p in sorted(packages.values(), key=lambda p: p.name)
    ]
    images += [combo_entry(c, packages, distros, state, previous=sizes, org=org) for c in sorted(combos, key=lambda c: c.name)]
    return _with_images(doc, images)


def _with_images(doc: dict, images: list[dict]) -> dict:
    """The document with these images and `generated` set to the newest build time among them."""
    rest = {k: v for k, v in doc.items() if k not in ("schema", "generated", "images")}
    updated = sorted(i["updated"] for i in images if isinstance(i.get("updated"), str) and i["updated"])
    head = {"schema": SCHEMA, "generated": updated[-1]} if updated else {"schema": SCHEMA}
    return {**head, **rest, "images": images}


def carry_over(doc: dict, previous: dict | None, names_: set[str]) -> dict:
    """Keep the previous entries of images that could not be read in this pass.

    An entry is replaced by its previous version, or added back when this
    pass has none, for example because its package.yml could not be read.
    """
    old = {i.get("name"): i for i in (previous or {}).get("images", []) if isinstance(i, dict)}
    images = [old.get(i["name"], i) if i["name"] in names_ else i for i in doc["images"]]
    present = {i["name"] for i in doc["images"]}
    images += [old[n] for n in sorted(names_) if n in old and n not in present]
    return _with_images(doc, sorted(images, key=lambda i: (i.get("kind") != "package", str(i.get("name")))))


def dumps(doc: dict) -> str:
    return json.dumps(doc, indent=2, ensure_ascii=False) + "\n"


@dataclass
class Notes:
    items: dict[str, list[str]] = field(default_factory=dict)

    def add(self, section: str, text: str) -> None:
        if section not in dict(SECTIONS):
            raise KeyError(section)
        bucket = self.items.setdefault(section, [])
        if text not in bucket:
            bucket.append(text)

    def __bool__(self) -> bool:
        return any(self.items.values())

    def lines(self) -> list[str]:
        out = []
        for key, title in SECTIONS:
            for text in self.items.get(key, []):
                out.append(f"{title}: {text}")
        return out


def issue_body(notes: Notes, repository: str) -> str:
    lines = [
        f"The reconcile workflow in [{repository}](https://github.com/{repository}) keeps this issue up to date "
        "and closes it when nothing needs attention. Edits to it are overwritten.",
    ]
    if not notes:
        lines += ["", "Nothing needs attention."]
    for key, title in SECTIONS:
        entries = notes.items.get(key, [])
        if entries:
            lines += ["", f"### {title}", ""]
            lines += [f"- {e}" for e in entries]
    return "\n".join(lines) + "\n"
