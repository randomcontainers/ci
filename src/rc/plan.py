"""The build plan: everything later jobs need, decided once per run.

`rc plan` resolves each base image tag and each member's slim image to an
index digest exactly once, checks that members are public and on the
current distro release, and writes one JSON document. The build, merge
and release jobs only read that document, so every job of a run works
from the same digests.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from rc import catalog, names, render, tags, versions
from rc.config import Combo, ComboFile, Distro, Distros, Package
from rc.constants import COMMON_ENV, IMAGE_USER, ORG, PLATFORMS, REGISTRY, WORKDIR
from rc.errors import RcError
from rc.labels import image_labels
from rc.registry import Registry, RegistryError, parse_reference
from rc.sources import PackageSource

SCHEMA = 1


@dataclass(frozen=True)
class RunContext:
    repository: str
    sha: str
    ref: str
    event_name: str
    default_only: bool = False
    want: str = ""
    now: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def publish(self) -> bool:
        return self.event_name != "pull_request" and self.ref == "refs/heads/main"


@dataclass
class MemberState:
    name: str
    ready: bool
    reason: str = ""
    digest: str = ""
    version: str = ""
    context: str = ""


class Planner:
    """Builds the plan for a package repository or a combo repository.

    With a registry, member images and base images are resolved to
    digests. Without one (offline mode for local work), members come from
    `member_images` and bases stay as plain tags.
    """

    def __init__(
        self,
        distros: Distros,
        listed: tuple[str, ...],
        source: PackageSource,
        registry: Registry | None,
        run: RunContext,
        org: str = ORG,
        member_images: dict[str, str] | None = None,
    ):
        self.distros = distros
        self.listed = listed
        self.source = source
        self.registry = registry
        self.run = run
        self.org = org
        self.member_images = member_images or {}
        self._base_digests: dict[str, str] = {}
        self.notices: list[str] = []
        self.warnings: list[str] = []
        self.errors: list[str] = []

    @property
    def offline(self) -> bool:
        return self.registry is None

    def image_ref(self, name: str) -> str:
        return f"{REGISTRY}/{self.org}/{name}"

    # Resolution

    def base_ref(self, distro: Distro) -> tuple[str, str]:
        """(BASE_IMAGE value, index digest) for a distro, resolved once per plan."""
        if self.offline:
            return distro.image, ""
        if distro.id not in self._base_digests:
            self._base_digests[distro.id] = self.registry.index_digest(parse_reference(distro.image))
        digest = self._base_digests[distro.id]
        return f"{distro.image}@{digest}", digest

    def resolve_member(self, name: str, distro: Distro) -> MemberState:
        package = self.source.load(name)
        if self.offline:
            image = self.member_images.get(name, "").replace("{distro}", distro.id)
            if not image:
                return MemberState(name, False, f"no local image given for {name} (--member-image {name}=REF)")
            return MemberState(name, True, version=package.version, context=f"docker-image://{image}")
        ref = parse_reference(f"{self.image_ref(name)}:slim-{distro.id}")
        probe = self.registry.probe(ref)
        if probe.status == "unavailable":
            return MemberState(name, False, f"{ref} cannot be pulled anonymously (private or not published yet)")
        if probe.status == "unbuilt":
            return MemberState(name, False, f"{ref} does not exist yet")
        try:
            digest = self.registry.index_digest(ref)
            labels = self.registry.config_labels(ref.with_digest(digest))
        except RegistryError as exc:
            return MemberState(name, False, f"cannot read {ref}: {exc}")
        version = labels.get("org.opencontainers.image.version", "")
        distro_version = labels.get("com.randomcontainers.distro-version", "")
        try:
            versions.validate(package.versioning, version)
        except versions.VersionError:
            return MemberState(name, False, f"{ref} has no valid version label ({version!r})")
        if distro_version != distro.version:
            return MemberState(
                name,
                False,
                f"{ref} is built on {distro.id} {distro_version or '(unknown)'}, not {distro.version}",
            )
        return MemberState(
            name,
            True,
            digest=digest,
            version=version,
            context=f"docker-image://{self.image_ref(name)}@{digest}",
        )

    # Targets

    def _common(self, distro: Distro, version: str) -> dict:
        return {
            "distro": distro.id,
            "family": distro.family,
            "distro_version": distro.version,
            "version": version,
        }

    def _labels(self, **kw) -> dict[str, str]:
        return image_labels(
            repository=self.run.repository,
            revision=self.run.sha,
            created=self.run.now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            **kw,
        )

    def _expect(self, package_env: dict[str, str], entrypoint, cmd, installed: dict[str, str], extras, distro) -> dict:
        env = dict(COMMON_ENV)
        env.update(package_env)
        return {
            "entrypoint": ["tini", "--", *entrypoint],
            "cmd": list(cmd),
            "env": env,
            "user": IMAGE_USER,
            "workdir": WORKDIR,
            "packages": installed,
            "extra_packages": list(extras),
            "os_version": distro.version,
        }

    def slim_target(self, package: Package, distro: Distro, with_default_tags: bool) -> dict:
        base_image, base_digest = self.base_ref(distro)
        ts = tags.package_tags("slim", package.version, package.versioning, distro, self.distros.default)
        if with_default_tags:
            ts.extend(tags.package_tags("default", package.version, package.versioning, distro, self.distros.default))
        args = {"BASE_IMAGE": base_image, "VERSION": package.version}
        if package.upstream.artifact:
            args["SOURCE_SHA256"] = package.upstream.artifact.sha256
        return {
            "id": f"slim-{distro.id}",
            "flavour": "slim",
            "variant": "slim",
            **self._common(distro, package.version),
            "tags": ts.tags,
            "floating": ts.floating,
            "primary": tags.primary_tag("slim", package.version, distro),
            "labels": self._labels(
                name=package.name,
                title=package.title,
                description=package.summary,
                version=package.version,
                licenses=package.license,
                base_name=distro.reference,
                base_digest=base_digest,
                package=package.name,
                variant="slim",
                distro=distro.id,
                distro_version=distro.version,
                members={},
            ),
            "build": {"dockerfile": f"Dockerfile.{distro.id}", "target": "slim", "args": args},
            "distro_base": base_image,
            "expect": self._expect(
                package.image.env_map,
                package.image.entrypoint,
                package.image.cmd,
                {package.name: package.version},
                [],
                distro,
            ),
            "tests": [
                {"package": package.name, "version": package.version, "commands": list(package.test), "require_output": True}
            ],
        }

    def default_target(self, combo: Combo, distro: Distro, *, in_repo: bool) -> dict | None:
        """The combo image for one distro, or None when a member is not ready.

        in_repo: built in the owner's repository next to its slim target
        (the owner comes from `target:slim` unless default-only is set).
        """
        owner = self.source.load(combo.owner)
        packages = {m: self.source.load(m) for m in combo.members}
        states: dict[str, MemberState] = {}
        for name in combo.members:
            if name == owner.name and in_repo and not self.run.default_only:
                states[name] = MemberState(name, True, version=owner.version, context="target:slim")
                continue
            states[name] = self.resolve_member(name, distro)
        blocked = [s for s in states.values() if not s.ready]
        if blocked:
            what = f"{owner.name} default image" if in_repo else combo.name
            for state in blocked:
                self.notices.append(f"Skipping the {what} on {distro.id}: {state.reason}")
            return None

        version = states[owner.name].version
        if version != owner.version:
            self.notices.append(
                f"{combo.name} on {distro.id} uses {owner.name} {version}; package.yml is at {owner.version}"
            )
        if in_repo:
            ts = tags.package_tags("default", version, owner.versioning, distro, self.distros.default)
            primary = tags.primary_tag("default", version, distro)
            name, title, variant = owner.name, owner.title, "default"
        else:
            ts = tags.combo_tags(version, distro, self.distros.default)
            primary = tags.primary_tag("default", version, distro)
            name, title, variant = combo.name, catalog.combo_title(combo, packages), "combo"

        external = {n: s for n, s in states.items() if not (in_repo and n == owner.name)}
        members_label = {n: {"version": s.version, "digest": s.digest} for n, s in external.items()}
        # base.digest names what the image is built FROM: the base member's
        # slim index, or the distro image when that member is built in this run.
        base = combo.base or combo.with_[0]
        _, distro_digest = self.base_ref(distro)
        if states[base].digest:
            base_name, base_digest = f"{self.image_ref(base)}:slim-{distro.id}", states[base].digest
        else:
            base_name, base_digest = distro.reference, distro_digest

        env = catalog.combo_env(combo, packages)
        extras = combo.extras_for(distro.id)
        return {
            "id": f"default-{distro.id}",
            "flavour": "default",
            "variant": variant,
            **self._common(distro, version),
            "tags": ts.tags,
            "floating": ts.floating,
            "primary": primary,
            "labels": self._labels(
                name=name,
                title=title,
                description=combo.summary,
                version=version,
                licenses=catalog.combo_license(combo, packages, distro.id),
                base_name=base_name,
                base_digest=base_digest,
                package=owner.name,
                variant=variant,
                distro=distro.id,
                distro_version=distro.version,
                members=members_label,
            ),
            "build": {
                "dockerfile_text": render.combo_dockerfile(combo, packages, distro, self.org),
                "contexts": {n: s.context for n, s in states.items()},
                "args": {},
            },
            "distro_base": f"{distro.image}@{distro_digest}" if distro_digest else distro.image,
            "expect": self._expect(
                env,
                owner.image.entrypoint,
                owner.image.cmd,
                {n: s.version for n, s in states.items()},
                extras.packages,
                distro,
            ),
            "tests": [
                {
                    "package": n,
                    "version": states[n].version,
                    "commands": list(packages[n].test),
                    "require_output": True,
                }
                for n in combo.members
            ]
            + [{"package": combo.name, "version": version, "commands": list(combo.test), "require_output": False}],
        }

    # Whole plans

    def _check_catalog(self, packages: dict[str, Package], combo: Combo | None) -> list[str]:
        """The catalog problems of a combo, checked against every listed package.

        The reconciler holds back combos with such problems; checking them
        here too keeps a manual run from building one.
        """
        for name in packages:
            if name not in self.listed:
                self.warnings.append(f"{name} is not listed in packages.yml")
        if combo is None:
            return []
        everything = dict(packages)
        for name in self.listed:
            if name not in everything:
                try:
                    everything[name] = self.source.load(name)
                except RcError as exc:
                    self.warnings.append(f"{combo.name} was not checked against {name}: {exc}")
        return [text for combos, text in catalog.catalog_problems(everything, self.listed) if combo.name in combos]

    def plan_package(self, package: Package) -> dict:
        expected = self.run.repository.rsplit("/", 1)[-1].lower()
        if package.name != expected:
            raise RcError(f"package.yml name {package.name!r} does not match the repository name {expected!r}")
        self.source.add(package)
        combo = package.default_combo
        packages = {package.name: package}
        if combo:
            packages.update({m: self.source.load(m) for m in combo.with_})
        problems = self._check_catalog(packages, combo)
        if problems:
            # The slim images do not depend on the combo, so they are still built.
            self.errors.append(f"Not building the {package.name} default image: " + "; ".join(problems))

        targets, skipped = [], []
        for distro in self.distros.items:
            if not self.run.default_only:
                targets.append(self.slim_target(package, distro, with_default_tags=combo is None))
            if combo:
                target = None if problems else self.default_target(combo, distro, in_repo=True)
                if target:
                    targets.append(target)
                else:
                    skipped.append({"flavour": "default", "distro": distro.id})
        if self.run.default_only and not combo:
            self.notices.append(f"{package.name} has no default combo; nothing to rebuild with default-only")

        release = None
        if package.source_release and self.run.publish and not self.run.default_only and package.upstream.artifact:
            artifact = package.upstream.artifact
            release = {
                "tag": f"v{package.version}",
                "title": f"{package.title} {package.version} source",
                "url": artifact.url_for(package.version),
                "sha256": artifact.sha256,
                "signature": artifact.signature_for(package.version),
            }
        return self._document("package", package.name, package, targets, skipped, release)

    def plan_combo(self, combo_file: ComboFile) -> dict:
        expected = self.run.repository.rsplit("/", 1)[-1].lower()
        if combo_file.name != expected:
            raise RcError(f"combo.yml name {combo_file.name!r} does not match the repository name {expected!r}")
        owner = self.source.load(combo_file.owner)
        combo = owner.combo_by_name(combo_file.name)
        if combo is None or combo.with_ != combo_file.with_:
            raise RcError(f"{combo_file.owner}/package.yml no longer declares the combo {combo_file.name}")
        packages = {m: self.source.load(m) for m in combo.members}
        problems = self._check_catalog(packages, combo)
        if problems:
            raise RcError(f"refusing to build {combo.name}: " + "; ".join(problems))
        targets, skipped = [], []
        for distro in self.distros.items:
            target = self.default_target(combo, distro, in_repo=False)
            if target:
                targets.append(target)
            else:
                skipped.append({"flavour": "default", "distro": distro.id})
        return self._document("combo", combo.name, owner, targets, skipped, None)

    def _document(self, kind, name, owner: Package, targets, skipped, release) -> dict:
        return {
            "schema": SCHEMA,
            "kind": kind,
            "name": name,
            "owner": owner.name,
            "org": self.org,
            "image": self.image_ref(name),
            "repository": self.run.repository,
            "revision": self.run.sha,
            "created": self.run.now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "epoch": str(int(self.run.now.timestamp())),
            "publish": self.run.publish,
            "default_only": self.run.default_only,
            "want": self.run.want,
            "versioning": owner.versioning,
            "version": owner.version,
            "platforms": [{"platform": p.platform, "arch": p.arch, "runner": p.runner} for p in PLATFORMS],
            "release": release,
            "targets": targets,
            "skipped": skipped,
        }


# Reading a plan back


def build_matrix(plan: dict) -> dict:
    legs = []
    for distro_id in dict.fromkeys(t["distro"] for t in plan["targets"]):
        flavours = {t["flavour"] for t in plan["targets"] if t["distro"] == distro_id}
        for p in plan["platforms"]:
            legs.append(
                {
                    "distro": distro_id,
                    "platform": p["platform"],
                    "arch": p["arch"],
                    "runner": p["runner"],
                    "slim": "slim" in flavours,
                    "default": "default" in flavours,
                }
            )
    return {"include": legs}


def merge_matrix(plan: dict) -> dict:
    return {
        "include": [
            {"flavour": t["flavour"], "distro": t["distro"], "ref": f"{plan['image']}:{t['primary']}"}
            for t in plan["targets"]
        ]
    }


def load(path: Path) -> dict:
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RcError(f"cannot read plan {path}: {exc}") from None
    check(plan)
    return plan


def check(plan: dict) -> None:
    """Re-validate the values that end up in commands, tags and paths."""
    if not isinstance(plan, dict) or plan.get("schema") != SCHEMA:
        raise RcError("unsupported plan document")
    if not names.NAME.match(plan.get("name", "")) or not names.NAME.match(plan.get("owner", "")):
        raise RcError("plan has an invalid image name")
    if plan.get("image") != f"{REGISTRY}/{plan.get('org')}/{plan['name']}":
        raise RcError("plan has an unexpected image reference")
    for target in plan.get("targets", []):
        if target.get("flavour") not in ("slim", "default"):
            raise RcError("plan has an unknown flavour")
        if not names.DISTRO_ID.match(target.get("distro", "")):
            raise RcError("plan has an invalid distro")
        for tag in target.get("tags", []):
            if not names.TAG.match(tag):
                raise RcError(f"plan has an invalid tag {tag!r}")
        for context in target.get("build", {}).get("contexts", {}).values():
            if not (context == "target:slim" or context.startswith("docker-image://")):
                raise RcError(f"plan has an unexpected build context {context!r}")


def find_target(plan: dict, flavour: str, distro: str) -> dict:
    for target in plan["targets"]:
        if target["flavour"] == flavour and target["distro"] == distro:
            return target
    raise RcError(f"the plan has no {flavour} image for {distro}")


def summary_markdown(plan: dict, notices: list[str]) -> str:
    lines = [f"### {plan['image']} {plan['version']}", ""]
    if plan["want"]:
        lines += [f"Request `{plan['want']}`", ""]
    lines += ["| Image | Distro | Version | Tags |", "|---|---|---|---|"]
    for t in plan["targets"]:
        lines.append(f"| {t['variant']} | {t['distro']} | {t['version']} | {', '.join(f'`{x}`' for x in t['tags'])} |")
    if not plan["targets"]:
        lines.append("| nothing to build | | | |")
    for n in notices:
        lines += ["", f"- {n}"]
    if not plan["publish"]:
        lines += ["", "Build and test only: images are published from the main branch."]
    return "\n".join(lines) + "\n"
