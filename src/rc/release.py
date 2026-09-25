"""rc source-release: fetch and verify the upstream source for the v<version> release.

The tarball must match the sha256 pinned in package.yml, the same value
the Dockerfile verifies. A detached signature, when the package lists one,
is attached as published upstream so users can check it themselves.
"""

import urllib.parse
from pathlib import Path

from rc import http, names
from rc.errors import RcError


def asset_name(url: str) -> str:
    name = Path(urllib.parse.urlparse(url).path).name
    if not names.FILE_NAME.match(name):
        raise RcError(f"cannot derive a file name from {url}")
    return name


def fetch(release: dict, out_dir: Path, download=http.download) -> list[Path]:
    assets = out_dir / "assets"
    assets.mkdir(parents=True, exist_ok=True)
    tarball = assets / asset_name(release["url"])
    digest = download(release["url"], tarball)
    if digest != release["sha256"]:
        tarball.unlink(missing_ok=True)
        raise RcError(f"{release['url']} has sha256 {digest}, but package.yml pins {release['sha256']}")
    files = [tarball]
    if release.get("signature"):
        signature = assets / asset_name(release["signature"])
        download(release["signature"], signature, max_bytes=1024 * 1024)
        files.append(signature)
    return files


def notes(plan: dict, release: dict, files: list[Path]) -> str:
    lines = [
        f"Upstream source of {release['title'].removesuffix(' source')}, as built into the images from this repository.",
        "",
        f"- `{files[0].name}`: sha256 `{release['sha256']}`, from {release['url']}",
    ]
    if len(files) > 1:
        lines.append(f"- `{files[1].name}`: upstream signature, from {release['signature']}")
    lines += ["", f"Images: `{plan['image']}`"]
    return "\n".join(lines) + "\n"
