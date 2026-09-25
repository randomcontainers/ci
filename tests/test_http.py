import email.message
import urllib.request

import pytest

from rc import http
from rc.errors import RcError


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
