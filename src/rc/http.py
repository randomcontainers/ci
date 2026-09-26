"""A small HTTPS client with an injectable transport, so tests never touch the network."""

import hashlib
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from rc import __version__
from rc.errors import RcError, TransientError, http_error

USER_AGENT = f"randomcontainers-rc/{__version__}"
MAX_BODY = 16 * 1024 * 1024
REDIRECTS = (301, 302, 303, 307, 308)


@dataclass
class Response:
    status: int
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""

    def header(self, name: str) -> str | None:
        return self.headers.get(name.lower())


class Transport(Protocol):
    def request(self, method: str, url: str, headers: dict[str, str], body: bytes | None = None) -> Response: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class _HttpsRedirect(urllib.request.HTTPRedirectHandler):
    """Follow redirects only to https URLs; checking the final URL would miss an http hop in between."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise RcError(f"{req.full_url} redirected to a non-https URL")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_STREAM_OPENER = urllib.request.build_opener(_HttpsRedirect())


class UrllibTransport:
    """HTTPS requests with retries of GET and HEAD on 429 and 5xx. Redirects are returned, never followed."""

    def __init__(self, timeout: float = 30.0, retries: int = 2, max_body: int = MAX_BODY):
        self.timeout = timeout
        self.retries = retries
        self.max_body = max_body
        self._opener = urllib.request.build_opener(_NoRedirect())

    def request(self, method: str, url: str, headers: dict[str, str], body: bytes | None = None) -> Response:
        if not url.startswith("https://"):
            raise RcError(f"refusing non-https URL {url}")
        attempt = 0
        retries = self.retries if method in ("GET", "HEAD") else 0
        while True:
            response = self._once(method, url, headers, data=body)
            if response.status in (429, 500, 502, 503, 504) and attempt < retries:
                attempt += 1
                time.sleep(2**attempt)
                continue
            return response

    def _once(self, method: str, url: str, headers: dict[str, str], data: bytes | None) -> Response:
        req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": USER_AGENT, **headers})
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                body = resp.read(self.max_body + 1) if method != "HEAD" else b""
                status = resp.status
                hdrs = {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as exc:
            status = exc.code
            hdrs = {k.lower(): v for k, v in (exc.headers or {}).items()}
            body = exc.read(65536) if method != "HEAD" else b""
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise _unreachable(f"{method} {url}", exc) from None
        if len(body) > self.max_body:
            raise RcError(f"{method} {url}: response larger than {self.max_body} bytes")
        return Response(status, hdrs, body)


def _unreachable(what: str, exc: Exception) -> RcError:
    """A connection failure is transient, except a certificate that does not verify."""
    reason = getattr(exc, "reason", exc)
    bad_certificate = isinstance(exc, ssl.SSLCertVerificationError) or isinstance(reason, ssl.SSLCertVerificationError)
    return (RcError if bad_certificate else TransientError)(f"{what}: {reason}")


def follow(transport: Transport, method: str, url: str, headers: dict[str, str] | None = None) -> Response:
    """Send a request and follow up to five https redirects.

    Headers are dropped after the first hop, so a registry token never
    reaches a storage host.
    """
    current = url
    sent = dict(headers or {})
    for _ in range(6):
        resp = transport.request(method, current, sent)
        if resp.status not in REDIRECTS:
            return resp
        location = resp.header("location")
        if not location:
            return resp
        current = urllib.parse.urljoin(current, location)
        sent = {}
    raise RcError(f"{method} {url}: too many redirects")


def get(transport: Transport, url: str, headers: dict[str, str] | None = None) -> bytes:
    resp = follow(transport, "GET", url, headers)
    if resp.status != 200:
        raise http_error(f"GET {url}", resp.status)
    return resp.body


def download(url: str, dest: Path, max_bytes: int = 1024**3, timeout: float = 120.0) -> str:
    """Stream a URL to a file and return its sha256 hex digest."""
    with dest.open("wb") as out:
        return stream_digests(url, ("sha256",), out=out, max_bytes=max_bytes, timeout=timeout)["sha256"]


def stream_digests(
    url: str, algorithms: tuple[str, ...], *, out=None, max_bytes: int = 1024**3, timeout: float = 120.0
) -> dict[str, str]:
    """Read a URL once and return its hex digests; optionally copy it to a file object."""
    if not url.startswith("https://"):
        raise RcError(f"refusing non-https URL {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    hashes = {name: hashlib.new(name) for name in algorithms}
    size = 0
    try:
        with _STREAM_OPENER.open(req, timeout=timeout) as resp:
            while chunk := resp.read(1024 * 1024):
                size += len(chunk)
                if size > max_bytes:
                    raise RcError(f"{url} is larger than {max_bytes} bytes")
                for h in hashes.values():
                    h.update(chunk)
                if out is not None:
                    out.write(chunk)
    except urllib.error.HTTPError as exc:
        raise http_error(f"GET {url}", exc.code) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise _unreachable(f"GET {url}", exc) from None
    return {name: h.hexdigest() for name, h in hashes.items()}
