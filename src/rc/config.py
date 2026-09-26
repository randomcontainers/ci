"""Definition files: distros.yml, packages.yml, package.yml and combo.yml.

Every file is parsed into frozen dataclasses. Parsing collects all problems
with their location and raises one ValidationError, so a single run of
`rc validate` shows everything that needs fixing.
"""

import re
import string
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rc import names, versions, yamlio
from rc.constants import COMMON_ENV
from rc.errors import ValidationError

SOURCES = ("pypi", "github-release", "github-tag", "gitlab-release", "forgejo-tag", "html-index")
# The upstream keys each source reads; the first ones listed are required.
SOURCE_KEYS = {
    "pypi": ("project", "publisher"),
    "github-release": ("repository",),
    "github-tag": ("repository",),
    "gitlab-release": ("project", "server"),
    "forgejo-tag": ("repository", "server"),
    "html-index": ("url", "pattern"),
}
SOURCE_REQUIRED = {
    "pypi": ("project", "publisher"),
    "github-release": ("repository",),
    "github-tag": ("repository",),
    "gitlab-release": ("project",),
    "forgejo-tag": ("repository",),
    "html-index": ("url", "pattern"),
}
DEFAULT_SERVERS = {"gitlab-release": "https://gitlab.com", "forgejo-tag": "https://codeberg.org"}
INDEX_GROUPS = ("version", "date")
FAMILIES = ("debian", "alpine")
CHECKSUM_ALGORITHMS = ("sha256", "sha512")
URL_FIELDS = ("version", "nodots", "underscored", "major", "minor")


@dataclass(frozen=True)
class Distro:
    id: str
    image: str
    family: str
    version: str

    @property
    def qualified(self) -> str:
        """Distro plus version as used in tags, for example ubuntu26.04."""
        return f"{self.id}{self.version}"

    @property
    def reference(self) -> str:
        """Fully qualified image reference, for example docker.io/library/ubuntu:26.04."""
        repo, _, tag = self.image.rpartition(":")
        if "/" not in repo:
            repo = f"library/{repo}"
        if repo.split("/", 1)[0].count(".") == 0:
            repo = f"docker.io/{repo}"
        return f"{repo}:{tag}"


@dataclass(frozen=True)
class Distros:
    default: str
    items: tuple[Distro, ...]

    def get(self, distro_id: str) -> Distro:
        for d in self.items:
            if d.id == distro_id:
                return d
        raise KeyError(distro_id)

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(d.id for d in self.items)


@dataclass(frozen=True)
class Checksums:
    url: str
    algorithm: str


@dataclass(frozen=True)
class Artifact:
    url: str
    sha256: str
    checksums: Checksums | None = None
    signature: str | None = None

    def url_for(self, version: str) -> str:
        return expand_url(self.url, version)

    def signature_for(self, version: str) -> str | None:
        return expand_url(self.signature, version) if self.signature else None


@dataclass(frozen=True)
class Upstream:
    source: str
    versioning: str
    version: str
    project: str | None = None
    repository: str | None = None
    tag_pattern: str | None = None
    version_template: str | None = None
    cooldown: str = "24h"
    publisher: str | None = None
    artifact: Artifact | None = None
    server: str | None = None
    url: str | None = None
    pattern: str | None = None

    @property
    def index_dated(self) -> bool:
        return index_dated(self.pattern)


@dataclass(frozen=True)
class ImageSpec:
    entrypoint: tuple[str, ...]
    cmd: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()

    @property
    def env_map(self) -> dict[str, str]:
        return dict(self.env)


@dataclass(frozen=True)
class Example:
    title: str
    command: str


@dataclass(frozen=True)
class Extras:
    packages: tuple[str, ...] = ()
    requirements: str | None = None

    def __bool__(self) -> bool:
        return bool(self.packages or self.requirements)


@dataclass(frozen=True)
class Combo:
    owner: str
    with_: tuple[str, ...]
    summary: str
    test: tuple[str, ...]
    default: bool = False
    base: str = ""
    extras: tuple[tuple[str, Extras], ...] = ()
    extras_license: str | None = None
    env: tuple[tuple[str, str], ...] = ()

    @property
    def name(self) -> str:
        return "-".join((self.owner, *self.with_))

    @property
    def members(self) -> tuple[str, ...]:
        return (self.owner, *self.with_)

    @property
    def env_map(self) -> dict[str, str]:
        return dict(self.env)

    def extras_for(self, distro_id: str) -> Extras:
        return dict(self.extras).get(distro_id, Extras())


@dataclass(frozen=True)
class Package:
    name: str
    title: str
    summary: str
    homepage: str
    license: str
    upstream: Upstream
    image: ImageSpec
    test: tuple[str, ...]
    examples: tuple[Example, ...] = ()
    combos: tuple[Combo, ...] = ()
    source_release: bool = False

    @property
    def version(self) -> str:
        return self.upstream.version

    @property
    def versioning(self) -> str:
        return self.upstream.versioning

    @property
    def default_combo(self) -> Combo | None:
        for combo in self.combos:
            if combo.default:
                return combo
        return None

    def combo_by_name(self, name: str) -> Combo | None:
        for combo in self.combos:
            if combo.name == name:
                return combo
        return None


@dataclass(frozen=True)
class ComboFile:
    name: str
    owner: str
    with_: tuple[str, ...]


def index_dated(pattern: str | None) -> bool:
    """True when an html-index pattern reads the release date from the page."""
    return pattern is not None and "date" in re.compile(pattern, re.ASCII).groupindex


def expand_url(template: str, version: str) -> str:
    parts = version.replace("-", ".").split(".")
    values = {
        "version": version,
        "nodots": versions.nodots(version),
        "underscored": version.replace(".", "_"),
        "major": parts[0],
        "minor": parts[1] if len(parts) > 1 else "0",
    }
    return template.format(**values)


@dataclass
class _Reader:
    """Typed access to a parsed YAML mapping that records problems with their location."""

    problems: list[str] = field(default_factory=list)

    def add(self, path: str, message: str) -> None:
        self.problems.append(f"{path}: {message}" if path else message)

    def mapping(self, value: Any, path: str, allowed: Iterable[str], required: Iterable[str] = ()) -> dict:
        if not isinstance(value, dict):
            self.add(path, "must be a mapping")
            return {}
        allowed = set(allowed)
        for key in value:
            if not isinstance(key, str) or key not in allowed:
                self.add(path, f"unknown key {key!r}")
        for key in required:
            if key not in value:
                self.add(path, f"missing required key {key!r}")
        return value

    def text(self, data: dict, key: str, path: str, limit: int = 256, required: bool = True) -> str | None:
        where = f"{path}.{key}" if path else key
        if key not in data:
            return None
        value = data[key]
        if not isinstance(value, str):
            hint = " (quote it in YAML)" if isinstance(value, (int, float)) else ""
            self.add(where, f"must be a string{hint}")
            return None
        if not names.is_single_line(value, limit):
            self.add(where, f"must be one line of at most {limit} printable characters")
            return None
        return value

    def pattern(self, data: dict, key: str, path: str, regex: re.Pattern, what: str) -> str | None:
        value = self.text(data, key, path)
        if value is not None and not regex.match(value):
            self.add(f"{path}.{key}" if path else key, f"{value!r} is not a valid {what}")
            return None
        return value

    def boolean(self, data: dict, key: str, path: str, default: bool = False) -> bool:
        if key not in data:
            return default
        value = data[key]
        if not isinstance(value, bool):
            self.add(f"{path}.{key}" if path else key, "must be true or false")
            return default
        return value

    def str_list(self, data: dict, key: str, path: str, *, required: bool = False, nonempty: bool = False) -> list[str]:
        where = f"{path}.{key}" if path else key
        if key not in data:
            if required:
                self.add(where, "is required")
            return []
        value = data[key]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            hint = ""
            if isinstance(value, list) and any(isinstance(v, dict) for v in value):
                hint = "; an entry containing ': ' was read as a mapping, so quote it or use a | block"
            self.add(where, f"must be a list of strings{hint}")
            return []
        if nonempty and not value:
            self.add(where, "must not be empty")
        return value

    def env(self, data: dict, key: str, path: str) -> tuple[tuple[str, str], ...]:
        where = f"{path}.{key}" if path else key
        if key not in data:
            return ()
        value = data[key]
        if not isinstance(value, dict):
            self.add(where, "must be a mapping of variable names to strings")
            return ()
        out = []
        for k, v in value.items():
            if not isinstance(k, str) or not names.ENV_KEY.match(k):
                self.add(where, f"{k!r} is not a valid variable name")
                continue
            if k in COMMON_ENV:
                self.add(where, f"{k} is set by every image and cannot be overridden")
                continue
            if not isinstance(v, str):
                self.add(f"{where}.{k}", "must be a string (quote it in YAML)")
                continue
            if not names.is_printable_ascii(v, 1024):
                self.add(f"{where}.{k}", "must be printable ASCII of at most 1024 characters")
                continue
            out.append((k, v))
        return tuple(out)

    def commands(self, data: dict, key: str, path: str, *, required: bool) -> tuple[str, ...]:
        where = f"{path}.{key}" if path else key
        items = self.str_list(data, key, path, required=required, nonempty=required)
        for i, cmd in enumerate(items):
            if not cmd.strip() or "\x00" in cmd or len(cmd) > 4096:
                self.add(f"{where}[{i}]", "must be a non-empty shell command of at most 4096 characters")
        return tuple(items)


def load_distros(path: Path) -> Distros:
    return parse_distros(yamlio.load_file(path), str(path))


def parse_distros(data: Any, origin: str) -> Distros:
    r = _Reader()
    data = r.mapping(data, "", ("default", "distros"), ("default", "distros"))
    items = []
    raw = data.get("distros", {})
    if not isinstance(raw, dict) or not raw:
        r.add("distros", "must be a non-empty mapping")
        raw = {}
    for distro_id, spec in raw.items():
        path = f"distros.{distro_id}"
        if not isinstance(distro_id, str) or not names.DISTRO_ID.match(distro_id):
            r.add(path, "distro id must be lowercase letters and digits")
            continue
        spec = r.mapping(spec, path, ("image", "family", "version"), ("image", "family", "version"))
        image = r.pattern(spec, "image", path, names.DISTRO_IMAGE, "image reference")
        family = r.text(spec, "family", path)
        version = r.pattern(spec, "version", path, names.DISTRO_VERSION, "distro version")
        if family is not None and family not in FAMILIES:
            r.add(f"{path}.family", f"must be one of {', '.join(FAMILIES)}")
        if image and family in FAMILIES and version:
            items.append(Distro(distro_id, image, family, version))
    default = r.text(data, "default", "")
    if default is not None and default not in raw:
        r.add("default", f"{default!r} is not one of the listed distros")
    if r.problems:
        raise ValidationError(origin, r.problems)
    return Distros(default, tuple(items))


def load_package_list(path: Path) -> tuple[str, ...]:
    data = yamlio.load_file(path)
    r = _Reader()
    data = r.mapping(data, "", ("packages",), ("packages",))
    items = r.str_list(data, "packages", "", required=True, nonempty=True)
    for i, name in enumerate(items):
        problem = names.check_name(name)
        if problem:
            r.add(f"packages[{i}]", problem)
    if len(set(items)) != len(items):
        r.add("packages", "lists a name more than once")
    if r.problems:
        raise ValidationError(str(path), r.problems)
    return tuple(items)


PACKAGE_KEYS = (
    "name",
    "title",
    "summary",
    "homepage",
    "license",
    "upstream",
    "image",
    "test",
    "examples",
    "combos",
    "source-release",
)
PACKAGE_REQUIRED = ("name", "title", "summary", "homepage", "license", "upstream", "image", "test")
UPSTREAM_KEYS = (
    "source",
    "project",
    "repository",
    "tag-pattern",
    "version-template",
    "versioning",
    "version",
    "cooldown",
    "publisher",
    "artifact",
    "server",
    "url",
    "pattern",
)
COMBO_KEYS = ("with", "default", "base", "summary", "extras", "extras-license", "env", "test")


def load_package(path: Path, distros: Distros) -> Package:
    return parse_package(yamlio.load_file(path), str(path), distros)


def parse_package(data: Any, origin: str, distros: Distros) -> Package:
    r = _Reader()
    data = r.mapping(data, "", PACKAGE_KEYS, PACKAGE_REQUIRED)
    name = data.get("name")
    problem = names.check_name(name) if "name" in data else None
    if problem:
        r.add("name", problem)
        name = None
    title = r.text(data, "title", "", limit=64)
    summary = r.text(data, "summary", "", limit=160)
    homepage = r.pattern(data, "homepage", "", names.HTTPS_URL, "https URL")
    license_ = r.text(data, "license", "")
    if license_ is not None:
        spdx = names.check_spdx(license_)
        if spdx:
            r.add("license", spdx)
    upstream = _parse_upstream(r, data.get("upstream"), "upstream") if "upstream" in data else None

    image = None
    if "image" in data:
        raw = r.mapping(data["image"], "image", ("entrypoint", "cmd", "env"), ("entrypoint",))
        entrypoint = r.str_list(raw, "entrypoint", "image", required=True, nonempty=True)
        cmd = r.str_list(raw, "cmd", "image")
        for key, items in (("entrypoint", entrypoint), ("cmd", cmd)):
            for i, item in enumerate(items):
                if not item or not all(ch.isprintable() for ch in item) or len(item) > 256:
                    r.add(f"image.{key}[{i}]", "must be printable and at most 256 characters")
        image = ImageSpec(tuple(entrypoint), tuple(cmd), r.env(raw, "env", "image"))

    test = r.commands(data, "test", "", required=True)

    examples = []
    raw_examples = data.get("examples", [])
    if not isinstance(raw_examples, list):
        r.add("examples", "must be a list")
        raw_examples = []
    for i, ex in enumerate(raw_examples):
        path = f"examples[{i}]"
        ex = r.mapping(ex, path, ("title", "command"), ("title", "command"))
        ex_title = r.text(ex, "title", path, limit=120)
        command = ex.get("command")
        if not isinstance(command, str) or not command.strip() or len(command) > 1024 or "\x00" in command:
            r.add(f"{path}.command", "must be a non-empty string of at most 1024 characters")
            continue
        if names.mounts_without_user(command):
            r.add(f"{path}.command", "mounts a directory with -v, so it must also pass --user")
        if ex_title:
            examples.append(Example(ex_title, command))

    source_release = r.boolean(data, "source-release", "")
    if source_release and upstream is not None and upstream.artifact is None:
        r.add("source-release", "needs upstream.artifact with a pinned sha256")
    copyleft = sorted({t for t in re.findall(r"[A-Za-z0-9.+-]+", license_ or "") if t.startswith(("GPL-", "LGPL-", "AGPL-"))})
    if copyleft and upstream is not None and upstream.artifact is not None and not source_release:
        r.add(
            "source-release",
            f"must be true: a tarball build under {', '.join(copyleft)} has to publish the source it compiled",
        )

    combos = []
    raw_combos = data.get("combos", [])
    if not isinstance(raw_combos, list):
        r.add("combos", "must be a list")
        raw_combos = []
    for i, raw in enumerate(raw_combos):
        combo = _parse_combo(r, raw, f"combos[{i}]", name or "", distros)
        if combo:
            combos.append(combo)
    _check_combos(r, name, combos, image)

    if r.problems:
        raise ValidationError(origin, r.problems)
    return Package(
        name=name,
        title=title,
        summary=summary,
        homepage=homepage,
        license=license_,
        upstream=upstream,
        image=image,
        test=test,
        examples=tuple(examples),
        combos=tuple(combos),
        source_release=source_release,
    )


def _parse_upstream(r: _Reader, raw: Any, path: str) -> Upstream | None:
    raw = r.mapping(raw, path, UPSTREAM_KEYS, ("source", "versioning", "version"))
    source = r.text(raw, "source", path)
    if source is not None and source not in SOURCES:
        r.add(f"{path}.source", f"must be one of {', '.join(SOURCES)}")
        source = None
    project = repository = publisher = server = url = pattern = None
    if source is not None:
        for key in SOURCE_REQUIRED[source]:
            if key not in raw:
                what = {"publisher": "'publisher', the trusted publisher repository"}.get(key, repr(key))
                r.add(path, f"{source} upstreams need {what}")
        for key in ("project", "publisher", "repository", "server", "url", "pattern"):
            if key in raw and key not in SOURCE_KEYS[source]:
                users = [s for s in SOURCES if key in SOURCE_KEYS[s]]
                listed = f"{', '.join(users[:-1])} and {users[-1]}" if len(users) > 1 else users[0]
                r.add(f"{path}.{key}", f"is only used by {listed}")
    if source == "pypi":
        project = r.pattern(raw, "project", path, names.PYPI_PROJECT, "PyPI project name")
        publisher = r.pattern(raw, "publisher", path, names.GITHUB_REPO, "owner/repo")
    elif source in ("github-release", "github-tag"):
        repository = r.pattern(raw, "repository", path, names.GITHUB_REPO, "owner/repo")
    elif source == "gitlab-release":
        project = r.pattern(raw, "project", path, names.GITLAB_PROJECT, "GitLab project id or path (quote an id in YAML)")
    elif source == "forgejo-tag":
        repository = r.pattern(raw, "repository", path, names.FORGE_REPO, "owner/repo")
    elif source == "html-index":
        url = r.pattern(raw, "url", path, names.HTTPS_URL, "https URL")
        pattern = _index_pattern(r, r.text(raw, "pattern", path, limit=512), f"{path}.pattern")
    if source in DEFAULT_SERVERS:
        server = r.pattern(raw, "server", path, names.SERVER_URL, "https base URL without a trailing slash")
        server = server or DEFAULT_SERVERS[source]

    tag_pattern = r.text(raw, "tag-pattern", path)
    groups = 0
    if tag_pattern is not None:
        try:
            compiled = re.compile(tag_pattern)
            groups = compiled.groups
            if not tag_pattern.startswith("^") or not tag_pattern.endswith("$"):
                r.add(f"{path}.tag-pattern", "must be anchored with ^ and $")
        except re.error as exc:
            r.add(f"{path}.tag-pattern", f"is not a valid regular expression: {exc}")
    template = r.text(raw, "version-template", path)
    if template is not None:
        if tag_pattern is None:
            r.add(f"{path}.version-template", "needs tag-pattern")
        refs = [int(m) for m in re.findall(r"\{(\d+)\}", template)]
        leftover = re.sub(r"\{\d+\}", "", template)
        if not refs or "{" in leftover or "}" in leftover:
            r.add(f"{path}.version-template", "must reference groups as {1}, {2}, ...")
        elif max(refs) > groups or min(refs) < 1:
            r.add(f"{path}.version-template", f"references a group that tag-pattern does not have ({groups} groups)")

    versioning = r.text(raw, "versioning", path)
    if versioning is not None and versioning not in versions.SCHEMES:
        r.add(f"{path}.versioning", f"must be one of {', '.join(versions.SCHEMES)}")
        versioning = None
    version = r.text(raw, "version", path, limit=64)
    if version is not None and versioning is not None:
        try:
            versions.validate(versioning, version)
        except versions.VersionError as exc:
            r.add(f"{path}.version", str(exc))
            version = None
    cooldown = r.pattern(raw, "cooldown", path, names.DURATION, "duration like 24h") or "24h"

    artifact = None
    if "artifact" in raw:
        artifact = _parse_artifact(r, raw["artifact"], f"{path}.artifact")
    if source == "html-index" and pattern is not None and not index_dated(pattern) and "artifact" not in raw:
        r.add(path, "html-index upstreams without a 'date' group in 'pattern' need 'artifact'; its Last-Modified is the release date")

    if source is None or versioning is None or version is None:
        return None
    return Upstream(
        source=source,
        versioning=versioning,
        version=version,
        project=project,
        repository=repository,
        tag_pattern=tag_pattern,
        version_template=template,
        cooldown=cooldown,
        publisher=publisher,
        artifact=artifact,
        server=server,
        url=url,
        pattern=pattern,
    )


def _index_pattern(r: _Reader, value: str | None, path: str) -> str | None:
    """The regular expression an html-index source applies to each line of its page."""
    if value is None:
        return None
    try:
        compiled = re.compile(value, re.ASCII)
    except re.error as exc:
        r.add(path, f"is not a valid regular expression: {exc}")
        return None
    unknown = sorted(set(compiled.groupindex) - set(INDEX_GROUPS))
    if unknown:
        r.add(path, f"has unknown named groups {unknown}; allowed: {', '.join(INDEX_GROUPS)}")
        return None
    if not compiled.groups:
        r.add(path, "needs a group for the version: (?P<version>...) or the first group")
        return None
    if "version" not in compiled.groupindex and compiled.groupindex.get("date") == 1:
        r.add(path, "reads the date from its first group, so name the version group (?P<version>...)")
        return None
    return value


def _check_url_template(r: _Reader, value: str | None, path: str) -> str | None:
    if value is None:
        return None
    try:
        fields = [f for _, f, _, _ in string.Formatter().parse(value) if f is not None]
    except ValueError as exc:
        r.add(path, f"is not a valid URL template: {exc}")
        return None
    unknown = [f for f in fields if f not in URL_FIELDS]
    if unknown:
        r.add(path, f"uses unknown placeholders {unknown}; allowed: {', '.join(URL_FIELDS)}")
        return None
    sample = expand_url(value, "1.2.3")
    if not names.HTTPS_URL.match(sample):
        r.add(path, "must be an https URL")
        return None
    return value


def _parse_artifact(r: _Reader, raw: Any, path: str) -> Artifact | None:
    raw = r.mapping(raw, path, ("url", "checksums", "sha256", "signature"), ("url", "sha256"))
    url = _check_url_template(r, r.text(raw, "url", path, limit=512), f"{path}.url")
    signature = _check_url_template(r, r.text(raw, "signature", path, limit=512), f"{path}.signature")
    sha256 = r.pattern(raw, "sha256", path, names.SHA256_HEX, "lowercase sha256 hex digest")
    checksums = None
    if "checksums" in raw:
        cpath = f"{path}.checksums"
        craw = r.mapping(raw["checksums"], cpath, ("url", "algorithm"), ("url", "algorithm"))
        curl = _check_url_template(r, r.text(craw, "url", cpath, limit=512), f"{cpath}.url")
        algorithm = r.text(craw, "algorithm", cpath)
        if algorithm is not None and algorithm not in CHECKSUM_ALGORITHMS:
            r.add(f"{cpath}.algorithm", f"must be one of {', '.join(CHECKSUM_ALGORITHMS)}")
        elif curl and algorithm:
            checksums = Checksums(curl, algorithm)
    if url and sha256:
        return Artifact(url=url, sha256=sha256, checksums=checksums, signature=signature)
    return None


def _parse_combo(r: _Reader, raw: Any, path: str, owner: str, distros: Distros) -> Combo | None:
    raw = r.mapping(raw, path, COMBO_KEYS, ("with", "summary", "test"))
    with_ = r.str_list(raw, "with", path, required=True, nonempty=True)
    for i, member in enumerate(with_):
        problem = names.check_name(member)
        if problem:
            r.add(f"{path}.with[{i}]", problem)
        elif member == owner:
            r.add(f"{path}.with[{i}]", "a package cannot be combined with itself")
    if len(set(with_)) != len(with_):
        r.add(f"{path}.with", "lists a member more than once")
    default = r.boolean(raw, "default", path)
    base = raw.get("base", with_[0] if with_ else "")
    if "base" in raw and (not isinstance(base, str) or base not in (owner, *with_)):
        r.add(f"{path}.base", "must be the package itself or one of the members in 'with'")
    summary = r.text(raw, "summary", path, limit=160)

    extras = []
    if "extras" in raw:
        epath = f"{path}.extras"
        eraw = raw["extras"]
        if not isinstance(eraw, dict):
            r.add(epath, "must be a mapping of distro to extras")
            eraw = {}
        for distro_id, spec in eraw.items():
            dpath = f"{epath}.{distro_id}"
            if distro_id not in distros.ids:
                r.add(dpath, f"{distro_id!r} is not a distro in distros.yml")
                continue
            spec = r.mapping(spec, dpath, ("packages", "requirements"))
            packages = r.str_list(spec, "packages", dpath)
            family = distros.get(distro_id).family
            for i, pkg in enumerate(packages):
                if not names.is_distro_package(pkg, family):
                    r.add(f"{dpath}.packages[{i}]", f"{pkg!r} is not a valid package name")
            requirements = r.pattern(spec, "requirements", dpath, names.FILE_NAME, "file name")
            item = Extras(tuple(packages), requirements)
            if item:
                extras.append((distro_id, item))
    extras_license = r.text(raw, "extras-license", path)
    if extras_license is not None:
        spdx = names.check_spdx(extras_license)
        if spdx:
            r.add(f"{path}.extras-license", spdx)
    if extras and extras_license is None:
        r.add(path, "combos with extras need 'extras-license'")
    env = r.env(raw, "env", path)
    test = r.commands(raw, "test", path, required=True)
    if not with_ or summary is None:
        return None
    return Combo(
        owner=owner,
        with_=tuple(with_),
        summary=summary,
        test=test,
        default=default,
        base=base if isinstance(base, str) else "",
        extras=tuple(extras),
        extras_license=extras_license,
        env=env,
    )


def _check_combos(r: _Reader, owner: str | None, combos: list[Combo], image: ImageSpec | None) -> None:
    if sum(1 for c in combos if c.default) > 1:
        r.add("combos", "at most one combo can be the default")
    seen_names: set[str] = set()
    seen_sets: set[frozenset] = set()
    for combo in combos:
        if combo.name in seen_names:
            r.add("combos", f"{combo.name} is declared twice")
        seen_names.add(combo.name)
        members = frozenset(combo.members)
        if members in seen_sets:
            r.add("combos", f"{combo.name} has the same members as another combo")
        seen_sets.add(members)
        problem = names.check_name(combo.name)
        if problem:
            r.add("combos", f"combo name {problem}")
        if image is not None:
            own = image.env_map
            for key, value in combo.env:
                if key in own and own[key] != value:
                    r.add("combos", f"{combo.name} sets {key} differently from image.env")


def load_combo_file(path: Path) -> ComboFile:
    return parse_combo_file(yamlio.load_file(path), str(path))


def parse_combo_file(data: Any, origin: str) -> ComboFile:
    r = _Reader()
    data = r.mapping(data, "", ("name", "owner", "with"), ("name", "owner", "with"))
    values = {}
    for key in ("name", "owner"):
        value = data.get(key)
        problem = names.check_name(value) if key in data else None
        if problem:
            r.add(key, problem)
        values[key] = value
    with_ = r.str_list(data, "with", "", required=True, nonempty=True)
    for i, member in enumerate(with_):
        problem = names.check_name(member)
        if problem:
            r.add(f"with[{i}]", problem)
    if not r.problems and values["name"] != "-".join((values["owner"], *with_)):
        r.add("name", f"must be {'-'.join((values['owner'], *with_))!r}, the owner followed by the members")
    if r.problems:
        raise ValidationError(origin, r.problems)
    return ComboFile(values["name"], values["owner"], tuple(with_))
