import email.message
import ssl
import urllib.error
import urllib.request

import pytest

from rc import http
from rc.errors import RcError, TransientError, http_error


def test_stream_redirects_stay_on_https():
    handler = http._HttpsRedirect()
    req = urllib.request.Request("https://example.org/a.tar.xz")
    headers = email.message.Message()
    with pytest.raises(RcError, match="non-https"):
        handler.redirect_request(req, None, 302, "Found", headers, "http://mirror.example.org/a.tar.xz")
    follow = handler.redirect_request(req, None, 302, "Found", headers, "https://mirror.example.org/a.tar.xz")
    assert follow.full_url == "https://mirror.example.org/a.tar.xz"
    assert any(isinstance(h, http._HttpsRedirect) for h in http._STREAM_OPENER.handlers)
    assert not any(type(h) is urllib.request.HTTPRedirectHandler for h in http._STREAM_OPENER.handlers)


def test_stream_refuses_http():
    with pytest.raises(RcError, match="non-https"):
        http.stream_digests("http://example.org/a.tar.xz", ("sha256",))


def test_outages_are_transient():
    assert isinstance(http._unreachable("GET https://x", urllib.error.URLError(TimeoutError("timed out"))), TransientError)
    assert isinstance(http._unreachable("GET https://x", ConnectionResetError()), TransientError)
    bad_cert = http._unreachable("GET https://x", urllib.error.URLError(ssl.SSLCertVerificationError("self-signed")))
    assert type(bad_cert) is RcError
    assert type(http._unreachable("GET https://x", ssl.SSLCertVerificationError(1, "CERTIFICATE_VERIFY_FAILED"))) is RcError
    assert [type(http_error("x", s)) for s in (404, 429, 500, 503)] == [RcError, TransientError, TransientError, TransientError]
