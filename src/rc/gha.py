"""GitHub Actions workflow commands, step outputs and the job summary.

Outside Actions, outputs are printed as name=value lines so the commands
stay usable on a workstation.
"""

import os
import secrets
import sys


def _escape_data(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def _escape_property(text: str) -> str:
    return _escape_data(text).replace(":", "%3A").replace(",", "%2C")


def _command(kind: str, message: str, title: str | None = None) -> None:
    if not in_actions():
        print(f"{kind}: {message}", file=sys.stderr, flush=True)
        return
    props = f" title={_escape_property(title)}" if title else ""
    print(f"::{kind}{props}::{_escape_data(message)}", flush=True)


def notice(message: str, title: str | None = None) -> None:
    _command("notice", message, title)


def warning(message: str, title: str | None = None) -> None:
    _command("warning", message, title)


def error(message: str, title: str | None = None) -> None:
    _command("error", message, title)


def in_actions() -> bool:
    return os.environ.get("GITHUB_ACTIONS") == "true"


def group(title: str) -> None:
    if in_actions():
        print(f"::group::{_escape_data(title)}", flush=True)
    else:
        print(f"--- {title}", flush=True)


def endgroup() -> None:
    if in_actions():
        print("::endgroup::", flush=True)


def set_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        print(f"{name}={value}")
        return
    delimiter = f"rc_{secrets.token_hex(16)}"
    if delimiter in value:
        raise RuntimeError("output value contains the delimiter")
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(f"{name}<<{delimiter}\n{value}\n{delimiter}\n")


def summary(markdown: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(markdown.rstrip("\n") + "\n")


def fail(message: str) -> None:
    if in_actions():
        error(message)
    else:
        print(f"rc: {message}", file=sys.stderr, flush=True)
