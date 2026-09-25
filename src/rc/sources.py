"""Where package definitions come from.

In CI a member's package.yml is read from the main branch of its
repository. For local work, point --packages-dir at a directory that holds
checkouts named after each package (<dir>/<name>/package.yml).
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

    def add(self, package: Package) -> None:
        self._cache[package.name] = package

    def load(self, name: str) -> Package:
        if name in self._cache:
            return self._cache[name]
        problem = names.check_name(name)
        if problem:
            raise RcError(f"package name {problem}")
        if self.local_dir is not None:
            path = self.local_dir / name / "package.yml"
            if not path.is_file():
                raise RcError(f"no package definition at {path}")
            data, origin = yamlio.load_file(path), str(path)
        elif self.transport is not None:
            url = RAW_PACKAGE_URL.format(org=self.org, name=name)
            text = http.get(self.transport, url).decode("utf-8")
            data, origin = yamlio.load_text(text, url), url
        else:
            raise RcError(f"no source configured for the package definition of {name}")
        package = parse_package(data, origin, self.distros)
        if package.name != name:
            raise RcError(f"{origin} declares name {package.name!r}, expected {name!r}")
        self._cache[name] = package
        return package
