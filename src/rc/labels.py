"""Image labels and index annotations (docs/architecture.md, "The build").

CI sets every key on every image, so nothing is inherited from a base
image or a combo member.
"""

import json

from rc.constants import VENDOR

LABEL_PREFIXES = ("org.opencontainers.image.", "com.randomcontainers.")


def image_labels(
    *,
    title: str,
    description: str,
    repository: str,
    version: str,
    licenses: str,
    revision: str,
    created: str,
    base_name: str,
    base_digest: str,
    package: str,
    variant: str,
    distro: str,
    distro_version: str,
    members: dict[str, dict[str, str]],
) -> dict[str, str]:
    return {
        "org.opencontainers.image.title": title,
        "org.opencontainers.image.description": description,
        "org.opencontainers.image.url": f"https://github.com/{repository}",
        "org.opencontainers.image.source": f"https://github.com/{repository}",
        "org.opencontainers.image.documentation": f"https://github.com/{repository}#readme",
        "org.opencontainers.image.version": version,
        "org.opencontainers.image.licenses": licenses,
        "org.opencontainers.image.revision": revision,
        "org.opencontainers.image.created": created,
        "org.opencontainers.image.vendor": VENDOR,
        "org.opencontainers.image.base.name": base_name,
        "org.opencontainers.image.base.digest": base_digest,
        "com.randomcontainers.package": package,
        "com.randomcontainers.variant": variant,
        "com.randomcontainers.distro": distro,
        "com.randomcontainers.distro-version": distro_version,
        "com.randomcontainers.members": json.dumps(members, separators=(",", ":"), sort_keys=True),
    }


def is_managed(key: str) -> bool:
    return key.startswith(LABEL_PREFIXES)
