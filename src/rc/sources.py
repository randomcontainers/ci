"""Where package definitions come from.

In CI a package.yml is read from the package's repository on GitHub: from
main, or from the commit a published image was built from. For local work,
point --packages-dir at a directory that holds checkouts named after each
package (<dir>/<name>/package.yml); those are used whatever the commit.
"""

from pathlib import Path

from rc import http, names, yamlio
from rc.config import Distros, Package, parse_package
from rc.constants import ORG, RAW_PACKAGE_URL
from rc.errors import RcError


class PackageSource:
    def __init__(
        self,
        distros: Distros,
        local_dir: Path | None = None,
        transport: http.Transport | None = None,
        org: str = ORG,
        preloaded: dict[str, Package] | None = None,
    ):
        self.distros = distros
        self.local_dir = local_dir
        self.transport = transport
        self.org = org
        self._cache: dict[str, Package] = dict(preloaded or {})
        self._at: dict[tuple[str, str], Package] = {}

    def add(self, package: Package) -> None:
        self._cache[package.name] = package

    def load(self, name: str) -> Package:
        if name in self._cache:
            return self._cache[name]
        _check_name(name)
        if self.local_dir is not None:
            path = self.local_dir / name / "package.yml"
            if not path.is_file():
                raise RcError(f"no package definition at {path}")
            package = self._parse(yamlio.load_file(path), str(path), name)
        elif self.transport is not None:
            package = self._fetch(name, "main")
        else:
            raise RcError(f"no source configured for the package definition of {name}")
        self._cache[name] = package
        return package

    def load_at(self, name: str, revision: str) -> Package:
        """The package definition at a commit of the package's repository."""
        _check_name(name)
        if not names.GIT_SHA.match(revision):
            raise RcError(f"{revision[:80]!r} is not a full commit id")
        if self.local_dir is not None or self.transport is None:
            return self.load(name)
        key = (name, revision)
        if key not in self._at:
            self._at[key] = self._fetch(name, revision)
        return self._at[key]

    def _fetch(self, name: str, ref: str) -> Package:
        url = RAW_PACKAGE_URL.format(org=self.org, name=name, ref=ref)
        try:
            text = http.get(self.transport, url).decode("utf-8")
        except UnicodeDecodeError:
            raise RcError(f"{url} is not UTF-8") from None
        return self._parse(yamlio.load_text(text, url), url, name)

    def _parse(self, data, origin: str, name: str) -> Package:
        package = parse_package(data, origin, self.distros)
        if package.name != name:
            raise RcError(f"{origin} declares name {package.name!r}, expected {name!r}")
        return package


def _check_name(name: str) -> None:
    problem = names.check_name(name)
    if problem:
        raise RcError(f"package name {problem}")
