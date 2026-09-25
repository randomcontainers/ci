"""Values fixed by the image contract."""

from dataclasses import dataclass

ORG = "randomcontainers"
REGISTRY = "ghcr.io"
SITE = "https://randomcontainers.com"
ALIAS = "randomcontainers.com"
VENDOR = "randomcontainers"

METADATA_DIR = "/usr/local/share/randomcontainers"
COMMON_PACKAGES = ("ca-certificates", "tini")
COMMON_ENV = {"LANG": "C.UTF-8", "XDG_CACHE_HOME": "/cache"}
IMAGE_USER = "1000:1000"
WORKDIR = "/work"
CACHE_DIR = "/cache"

RAW_PACKAGE_URL = "https://raw.githubusercontent.com/{org}/{name}/main/package.yml"

# Names reserved for project paths, which no package or combo can use.
RESERVED_NAMES = frozenset(
    {
        "ci",
        ".github",
        "about",
        "containers",
        "licenses",
        "how-it-works",
        "default-vs-slim",
        "v2",
    }
)


@dataclass(frozen=True)
class Platform:
    platform: str
    arch: str
    runner: str


PLATFORMS = (
    Platform("linux/amd64", "amd64", "ubuntu-24.04"),
    Platform("linux/arm64", "arm64", "ubuntu-24.04-arm"),
)


def platform_by_name(name: str) -> Platform:
    for p in PLATFORMS:
        if name in (p.platform, p.arch):
            return p
    raise KeyError(name)
