"""An expiring session was answered with the wrong reason.

`AuthMiddleware._validate_token` catches a failed decode and raises what reads
like a clean refusal:

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": "Invalid or expired token"},
    )

`HTTPException` takes `detail`, not `content`; the confusion is understandable
because `dispatch`, thirty lines above, builds three `JSONResponse` objects
that legitimately take `content`. The line therefore raised
`TypeError: HTTPException.__init__() got an unexpected keyword argument
'content'` -- inside the handler whose whole purpose is to turn a bad token
into a polite answer.

Measured, not deduced, because the reading was misleading: `dispatch` catches
anything and answers 401, so the caller never saw a 500. What it saw was the
wrong reason.

    before   401  {"detail": "Invalid authentication credentials"}
    after    401  {"detail": "Invalid or expired token"}

The difference matters to whoever reads it. Sessions last two hours
(`access_token_expire_minutes`), so the common case by far is a token that was
valid and aged out -- and that user was told their credentials were wrong,
which sends them to check a password rather than to sign in again.

It reached production because no test asked what an invalid token *returns*:
the existing ones assert on tokens that decode, and the catch-all above kept
the status code right while the reason drifted.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from fastapi import HTTPException

import apowerb
from apowerb.auth.middleware import AuthMiddleware


@pytest.fixture
def middleware(monkeypatch):
    """A middleware with a usable signing key.

    Without one, `decode_access_token` raises `RuntimeError` and the middleware
    re-raises it on purpose -- a server misconfiguration is not the caller's
    fault. That branch is a different subject, and a test that lands in it
    would be exercising the config failure while claiming to exercise a bad
    token.
    """
    from apowerb.configs.settings import get_settings

    monkeypatch.setattr(get_settings(), "encrypt_key", "k" * 32, raising=False)
    return AuthMiddleware(app=None)


async def test_a_missing_signing_key_still_leaves_this_function(monkeypatch):
    """The neighbouring branch, pinned so the fix above cannot swallow it.

    Scope, measured rather than assumed: this pins what `_validate_token`
    raises, not what the caller receives. `dispatch` ends with a bare
    `except Exception` that answers 401 whatever reaches it, so a server
    with no signing key currently looks to the caller exactly like a bad
    token. That is a separate gap, older than this change and untouched by
    it; claiming otherwise here would be pinning a behaviour the HTTP layer
    does not have.
    """
    from apowerb.configs.settings import get_settings

    monkeypatch.setattr(get_settings(), "encrypt_key", "", raising=False)

    with pytest.raises(RuntimeError, match="ENCRYPT_KEY"):
        await AuthMiddleware(app=None)._validate_token("not-a-jwt")


@pytest.mark.parametrize(
    "token",
    [
        pytest.param("not-a-jwt", id="malformed"),
        pytest.param("", id="empty"),
        pytest.param("a.b.c", id="three-empty-segments"),
        pytest.param(
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ4IiwiZXhwIjoxfQ.bad",
            id="expired-shaped",
        ),
    ],
)
async def test_a_token_that_does_not_decode_is_refused_not_crashed(middleware, token):
    """The refusal itself must not raise. `pytest.raises(HTTPException)` is the
    whole point: a `TypeError` escaping here is what the catch-all above then
    relabelled, losing the reason on the way."""
    with pytest.raises(HTTPException) as raised:
        await middleware._validate_token(token)

    assert raised.value.status_code == 401
    assert raised.value.detail


async def test_the_refusal_says_what_happened(middleware):
    """The message the operator reads. Losing it would leave a bare 401 that
    tells a user with an expired session nothing about reconnecting."""
    with pytest.raises(HTTPException) as raised:
        await middleware._validate_token("not-a-jwt")

    assert "expired" in str(raised.value.detail).lower()


def test_no_http_exception_is_built_with_an_argument_it_does_not_take():
    """The general fault, not just this one line.

    `HTTPException` accepts `status_code`, `detail` and `headers`. Anything
    else raises `TypeError` from inside a `raise` statement. Whether that
    surfaces as a crash or, as here, as a plausible-looking answer with the
    wrong reason depends on what catches it upstream -- which is exactly why it
    should not be left to chance.

    It matches the name as written, so `from fastapi import HTTPException as X`
    followed by `X(...)` would escape it. No such alias exists in the tree
    today; the limit is stated rather than hidden.
    """
    allowed = {"status_code", "detail", "headers"}
    root = pathlib.Path(apowerb.__file__).parent

    offenders = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            if name != "HTTPException":
                continue
            unexpected = [k.arg for k in node.keywords if k.arg not in allowed]
            if unexpected:
                rel = path.relative_to(root.parent)
                offenders.append(f"{rel}:{node.lineno}: {unexpected}")

    assert offenders == []


def test_the_scanner_would_catch_the_argument_that_was_there():
    """Positive control. Without it, a scanner that stopped matching anything
    would let the sweep above pass on an empty result."""
    snippet = "raise HTTPException(status_code=401, content={'detail': 'x'})\n"
    allowed = {"status_code", "detail", "headers"}

    found = [
        k.arg
        for node in ast.walk(ast.parse(snippet))
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "HTTPException"
        for k in node.keywords
        if k.arg not in allowed
    ]

    assert found == ["content"]
