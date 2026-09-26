"""Patterns every name, path and value from a definition file must match.

These checks run before any value reaches a tag, a file path, a Dockerfile
or a shell, so keep them strict.
"""

import re
import shlex
import urllib.parse

from rc.constants import RESERVED_NAMES

NAME = re.compile(r"^[a-z0-9]+(?:[._-][a-z0-9]+)*$")
NAME_MAX = 64
DISTRO_ID = re.compile(r"^[a-z][a-z0-9]{0,31}$")
DISTRO_IMAGE = re.compile(r"^[a-z0-9]+(?:[._/-][a-z0-9]+)*:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
DISTRO_VERSION = re.compile(r"^[0-9]+(?:\.[0-9]+){0,3}$")
# Package names a combo may install, per distro family. apk names and so:
# names can contain capitals (libSvtAv1Enc, so:libSvtAv1Enc.so.4); Debian
# names cannot. The same expressions go into the grep -E check of the
# rendered Dockerfile, so keep them valid as POSIX EREs too.
_PACKAGE_NAME = {
    "alpine": r"(so:)?[A-Za-z0-9][A-Za-z0-9+._-]*",
    "debian": r"[a-z0-9][a-z0-9+.-]*",
}
DISTRO_PACKAGE = {family: re.compile(f"^{body}$") for family, body in _PACKAGE_NAME.items()}
ENV_KEY = re.compile(r"^[A-Z_][A-Z0-9_]*$")
ABS_PATH = re.compile(r"^/[A-Za-z0-9._/-]+$")
FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
SHA256_HEX = re.compile(r"^[a-f0-9]{64}$")
GIT_SHA = re.compile(r"^[a-f0-9]{40}$")
GITHUB_REPO = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}$")
# owner/repo on a Forgejo server such as Codeberg; neither part may start with a dot.
FORGE_REPO = re.compile(r"^[A-Za-z0-9_](?:[A-Za-z0-9._-]{0,39})/[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$")
# A GitLab project: its numeric id, or its path with every namespace.
GITLAB_PROJECT = re.compile(r"^(?:[1-9][0-9]{0,11}|[A-Za-z0-9_][A-Za-z0-9._-]{0,99}(?:/[A-Za-z0-9_][A-Za-z0-9._-]{0,99}){1,9})$")
# Base URL of a GitLab or Forgejo server: https, optionally a path, no trailing slash.
SERVER_URL = re.compile(r"^https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~-]+)*$")
# A git repository to clone over https: host and path only, no query or credentials.
GIT_URL = re.compile(r"^https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9_~-][A-Za-z0-9._~-]*)+$")
# A tag name that git accepts and that is safe on a command line: components
# separated by single slashes, none starting with a dot or a dash.
GIT_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._+-]*(?:/[A-Za-z0-9_][A-Za-z0-9._+-]*)*$")
# The name of an extra artifact; upper-cased with _ for -, it prefixes the build arguments.
EXTRA_NAME = re.compile(r"^[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*$")
# A version pinned by hand for an extra artifact.
PINNED_VERSION = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._+~-]{0,62}[A-Za-z0-9])?$")
PYPI_PROJECT = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?$")
DURATION = re.compile(r"^[0-9]{1,4}[mhd]$")
HTTPS_URL = re.compile(r"^https://[A-Za-z0-9.-]+(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~!$&'()*+,;=:@%/?#-]*)?$")
WANT = re.compile(r"^[A-Za-z0-9._-]{0,80}$")
TOPIC = re.compile(r"^[a-z0-9][a-z0-9-]{0,49}$")

_SPDX_ID = re.compile(r"^(?:LicenseRef-|DocumentRef-[A-Za-z0-9.-]+:LicenseRef-)?[A-Za-z0-9][A-Za-z0-9.+-]*$")
_SPDX_TOKEN = re.compile(r"\(|\)|[^\s()]+")


def is_git_tag(value: str) -> bool:
    """A tag git accepts: GIT_TAG, at most 128 characters, no "..", no component ending in "." or ".lock"."""
    if not isinstance(value, str) or len(value) > 128 or not GIT_TAG.match(value) or ".." in value:
        return False
    return not any(part.endswith((".", ".lock")) for part in value.split("/"))


def url_file_name(url: str) -> str | None:
    """The file name at the end of a URL's path, or None when it is not a plain file name."""
    if not isinstance(url, str):
        return None
    path = urllib.parse.urlparse(url).path
    name = urllib.parse.unquote(path.rsplit("/", 1)[-1])
    return name if FILE_NAME.match(name) else None


def check_name(value: str) -> str | None:
    """Return a problem description, or None when the name is usable."""
    if not isinstance(value, str) or not NAME.match(value):
        return f"{value!r} must match {NAME.pattern}"
    if len(value) > NAME_MAX:
        return f"{value!r} is longer than {NAME_MAX} characters"
    if value in RESERVED_NAMES:
        return f"{value!r} is a reserved name"
    return None


def is_distro_package(value: str, family: str) -> bool:
    return isinstance(value, str) and DISTRO_PACKAGE[family].fullmatch(value) is not None


def distro_package_ere(family: str) -> str:
    """ERE for one line of a runtime-deps file: a package name or nothing."""
    return f"^({_PACKAGE_NAME[family]})?$"


def is_single_line(value: str, limit: int) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= limit
        and value == value.strip()
        and all(ch.isprintable() for ch in value)
    )


def is_printable_ascii(value: str, limit: int) -> bool:
    return isinstance(value, str) and len(value) <= limit and all(0x20 <= ord(ch) < 0x7F for ch in value)


def check_spdx(expr: str) -> str | None:
    """Light syntax check of an SPDX license expression."""
    if not isinstance(expr, str) or not expr or len(expr) > 256:
        return "must be an SPDX expression of at most 256 characters"
    depth = 0
    expect_operand = True
    for token in _SPDX_TOKEN.findall(expr):
        if token == "(":
            if not expect_operand:
                return f"unexpected '(' in {expr!r}"
            depth += 1
        elif token == ")":
            if expect_operand or depth == 0:
                return f"unbalanced ')' in {expr!r}"
            depth -= 1
        elif token in ("AND", "OR", "WITH"):
            if expect_operand:
                return f"unexpected {token} in {expr!r}"
            expect_operand = True
        else:
            if not expect_operand or not _SPDX_ID.match(token) or token.lower() in ("and", "or", "with"):
                return f"{token!r} is not a license identifier in {expr!r}"
            expect_operand = False
    if expect_operand or depth:
        return f"{expr!r} is incomplete"
    return None


def spdx_and(expressions: list[str]) -> str:
    """Join license expressions with AND, keeping OR terms grouped and dropping repeats."""
    seen: list[str] = []
    for expr in expressions:
        expr = expr.strip()
        if "(" not in expr and " OR " not in f" {expr} ":
            terms = [t.strip() for t in expr.split(" AND ")]
        elif " OR " in f" {expr} " and not _fully_parenthesized(expr):
            terms = [f"({expr})"]
        else:
            terms = [expr]
        for term in terms:
            if term not in seen:
                seen.append(term)
    return " AND ".join(seen)


def _fully_parenthesized(expr: str) -> bool:
    if not (expr.startswith("(") and expr.endswith(")")):
        return False
    depth = 0
    for i, ch in enumerate(expr):
        depth += ch == "("
        depth -= ch == ")"
        if depth == 0 and i < len(expr) - 1:
            return False
    return True


def mounts_without_user(command: str) -> bool:
    """True when a docker command bind-mounts a directory but does not set --user.

    Only the options before the image reference count, so a tool's own -v
    flag after the image is ignored.
    """
    try:
        words = shlex.split(command)
    except ValueError:
        words = command.split()
    options = []
    for word in words:
        if "randomcontainers" in word and not word.startswith("-"):
            break
        options.append(word)
    mounts = any(
        w in ("-v", "--volume", "--mount")
        or w.startswith(("--volume=", "--mount="))
        or (w.startswith("-v") and ":" in w)
        for w in options
    )
    user = any(w in ("--user", "-u", "--userns") or w.startswith(("--user=", "--userns=")) for w in options)
    return mounts and not user
