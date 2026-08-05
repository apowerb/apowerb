"""URL shape allowlist: the host we validate must be the host we reach.

`_validate_url_not_internal` resolves the hostname and rejects private ranges,
which is the substantive control. It reads the host through `urlparse`, while
the request is made by httpx -- and the two do not parse identically. Where
they disagree, the check inspects one host and the connection goes to another,
so the DNS check is bypassed without ever failing.

These tests pin the shapes that create that disagreement, and pin the return
contract: a guard that only raises leaves the raw URL in scope for the caller
to request, which is both a real gap and what CodeQL reports as py/full-ssrf.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from apowerb.routers.rag.validators import _validate_url_shape


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/a.pdf",
        "http://example.com",
        "https://example.com/",
        "https://sub.domain.example.co.uk/path/to/doc.pdf?v=2#frag",
        "https://example.com:8443/doc.pdf",
        "https://192.0.2.10/doc.pdf",
        "https://[2001:db8::1]/doc.pdf",
        "https://example.com/a%20file.pdf",
        "HTTPS://Example.COM/a.pdf",
        "https://my-host.example.com/a.pdf",
    ],
)
def test_ordinary_urls_pass(url):
    assert _validate_url_shape(url) == url


@pytest.mark.parametrize(
    "url",
    [
        # Userinfo: urlparse reads the host as attacker.com, but the shape is
        # built to look like example.com to anything reading it by eye or by a
        # laxer parser.
        "https://example.com@attacker.com/doc.pdf",
        "https://user:pass@attacker.com/doc.pdf",
        # Backslash: some parsers normalise it to "/", changing where the
        # authority ends.
        "https://example.com\\@attacker.com/doc.pdf",
        "https://attacker.com\\.example.com/",
        # Embedded control characters and whitespace -- request smuggling and
        # header injection, and another parser differential.
        "https://example.com\r\nHost: internal/doc.pdf",
        "https://example.com\n/doc.pdf",
        "https://example.com\t/doc.pdf",
        "https://exam ple.com/doc.pdf",
        # Non-HTTP schemes, including the classic SSRF vectors.
        "file:///etc/passwd",
        "gopher://127.0.0.1:11211/_stats",
        "ftp://example.com/doc.pdf",
        # Protocol-relative and scheme-less.
        "//example.com/doc.pdf",
        "example.com/doc.pdf",
        "",
    ],
)
def test_shapes_that_break_parser_agreement_are_rejected(url):
    with pytest.raises(HTTPException) as exc:
        _validate_url_shape(url)
    assert exc.value.status_code == 400


def test_it_returns_the_checked_url_not_just_raises():
    """The caller must request the value the guard handed back.

    A guard that only raises leaves the raw URL in scope; that raw value is
    what reaches `client.get`, so nothing was actually narrowed.
    """
    assert _validate_url_shape("https://example.com/a.pdf") == "https://example.com/a.pdf"


def test_the_message_says_what_is_unacceptable():
    with pytest.raises(HTTPException) as exc:
        _validate_url_shape("https://user:pass@example.com/")
    assert "credentials" in exc.value.detail
