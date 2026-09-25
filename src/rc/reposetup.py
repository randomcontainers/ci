"""Repository changes that need the App's Administration permission.

`rc reconcile` does not hold a token with that permission. It writes the
combo repositories to create and the topics to set to a file, and
`rc setup-repos` makes those changes in a separate step, with a token that
is minted only when the file is not empty. The step reads nothing but this
file, and checks every value again before it is used.
"""

import json
from pathlib import Path

from rc import names
from rc.constants import SITE
from rc.errors import RcError
from rc.github import GitHub

KINDS = ("create", "topics")
MAX_REQUESTS = 50
MAX_TOPICS = 20
MAX_DESCRIPTION = 160


def create(name: str, description: str) -> dict:
    return {"kind": "create", "name": name, "description": description, "homepage": f"{SITE}/{name}/"}


def topics(name: str, values: list[str]) -> dict:
    return {"kind": "topics", "name": name, "topics": list(values)}


def check(request) -> dict:
    """Return the request when every value is what the reconciler writes, or raise RcError."""
    if not isinstance(request, dict) or request.get("kind") not in KINDS:
        raise RcError(f"unexpected request {str(request)[:80]!r}")
    name = request.get("name")
    problem = names.check_name(name)
    if problem:
        raise RcError(problem)
    if request["kind"] == "create":
        if set(request) != {"kind", "name", "description", "homepage"}:
            raise RcError(f"{name}: unexpected keys in the create request")
        description = request["description"]
        if not isinstance(description, str) or not description or len(description) > MAX_DESCRIPTION:
            raise RcError(f"{name}: the description must be 1 to {MAX_DESCRIPTION} characters")
        if any(ord(c) < 32 for c in description):
            raise RcError(f"{name}: the description contains control characters")
        if request["homepage"] != f"{SITE}/{name}/":
            raise RcError(f"{name}: the homepage must be {SITE}/{name}/")
    else:
        if set(request) != {"kind", "name", "topics"}:
            raise RcError(f"{name}: unexpected keys in the topics request")
        values = request["topics"]
        if not isinstance(values, list) or not values or len(values) > MAX_TOPICS:
            raise RcError(f"{name}: expected 1 to {MAX_TOPICS} topics")
        for value in values:
            if not isinstance(value, str) or not names.TOPIC.match(value):
                raise RcError(f"{name}: {str(value)[:60]!r} is not a valid topic")
    return request


def load(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RcError(f"cannot read {path}: {exc}") from None
    if not isinstance(data, list) or len(data) > MAX_REQUESTS:
        raise RcError(f"{path}: expected a list of at most {MAX_REQUESTS} requests")
    return [check(r) for r in data]


def dump(requests: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([check(r) for r in requests], indent=2) + "\n", encoding="utf-8")


def apply(requests: list[dict], admin: GitHub, org: str) -> list[str]:
    """Make each change; return the problems. One failed request does not stop the others."""
    problems = []
    for request in requests:
        name = request["name"]
        try:
            if request["kind"] == "create":
                admin.create_repository(org, name, request["description"], request["homepage"])
            else:
                admin.set_topics(f"{org}/{name}", request["topics"])
        except RcError as exc:
            problems.append(f"{request['kind']} {org}/{name}: {exc}")
    return problems
