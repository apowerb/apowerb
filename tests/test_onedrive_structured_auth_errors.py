"""``onedrive_core._get_access_token`` must raise ``IntegrationStatusError``
with the right code so the tools propagate the structured payload to the
LLM (rather than the previous free-form ``RuntimeError``).

Mapping:
  - missing credentials .................... INTEGRATION_MISSING
  - ``invalid_grant`` after auto-heal ...... INTEGRATION_EXPIRED
  - non-200 from the Microsoft endpoint .... INTEGRATION_ERROR
  - 200 OK without ``access_token`` ........ INTEGRATION_ERROR

End-to-end: a tool call (e.g. ``onedrive_read.tool_list_files``) must
return the structured ``_integration_status`` dict when auth fails.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from apowerb.tools_store.portfolio.integration_status import (
    INTEGRATION_ERROR,
    INTEGRATION_EXPIRED,
    INTEGRATION_MISSING,
    IntegrationStatusError,
)


PROVIDER = "microsoft_onedrive"


@pytest.fixture(autouse=True)
def _isolate_token_state(monkeypatch):
    """Each test starts with a clean cache and no token in env, so behaviour
    is deterministic (the helper has module-level state)."""
    from apowerb.tools_store.portfolio import onedrive_core

    onedrive_core._token_cache.clear()
    onedrive_core._integration_loaded_for = None
    monkeypatch.delenv("ONEDRIVE_REFRESH_TOKEN", raising=False)
    monkeypatch.delenv("ONEDRIVE_CLIENT_ID", raising=False)
    monkeypatch.delenv("ONEDRIVE_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("AGENT_OWNER", raising=False)
    yield
    onedrive_core._token_cache.clear()
    onedrive_core._integration_loaded_for = None


def _fake_post(status_code: int, text: str = "", json_body: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json.return_value = json_body or {}
    return resp


# ---------------------------------------------------------------------------
# 1. INTEGRATION_MISSING — credentials not configured at all
# ---------------------------------------------------------------------------


class TestMissingCredentials:
    def test_no_refresh_token_raises_missing(self, monkeypatch):
        from apowerb.tools_store.portfolio import onedrive_core

        # Force the auto-heal retry to also produce no refresh_token.
        monkeypatch.setattr(onedrive_core, "_ensure_integration_tokens", lambda: None)

        with pytest.raises(IntegrationStatusError) as ei:
            onedrive_core._get_access_token()

        assert ei.value.code == INTEGRATION_MISSING
        assert ei.value.provider == PROVIDER
        assert ei.value.is_remediable_by_reconnect is True


# ---------------------------------------------------------------------------
# 2. INTEGRATION_EXPIRED — invalid_grant after auto-heal
# ---------------------------------------------------------------------------


class TestExpiredRefreshToken:
    def test_invalid_grant_after_retry_raises_expired(self, monkeypatch):
        """First call → invalid_grant → auto-heal clears env and retries.
        Auto-heal helper restores the token, second call → invalid_grant
        again (token is still revoked) → raise EXPIRED."""
        import os
        from apowerb.tools_store.portfolio import onedrive_core

        monkeypatch.setenv("ONEDRIVE_REFRESH_TOKEN", "stale-token")
        monkeypatch.setenv("ONEDRIVE_CLIENT_ID", "cid")
        monkeypatch.setenv("ONEDRIVE_CLIENT_SECRET", "csec")

        # The auto-heal path pops ONEDRIVE_REFRESH_TOKEN before retrying.
        # Simulate _ensure_integration_tokens re-loading the same stale
        # value from DB — that way the retry reaches the HTTP call again
        # and the second invalid_grant triggers the EXPIRED branch.
        def _restore_stale_token():
            os.environ.setdefault("ONEDRIVE_REFRESH_TOKEN", "stale-token")

        monkeypatch.setattr(
            onedrive_core, "_ensure_integration_tokens", _restore_stale_token
        )

        invalid_grant_resp = _fake_post(
            400, text='{"error":"invalid_grant","error_description":"AADSTS7000215..."}'
        )

        with patch.object(onedrive_core.httpx, "post", return_value=invalid_grant_resp):
            with pytest.raises(IntegrationStatusError) as ei:
                onedrive_core._get_access_token()

        assert ei.value.code == INTEGRATION_EXPIRED
        assert ei.value.provider == PROVIDER
        assert ei.value.is_remediable_by_reconnect is True


# ---------------------------------------------------------------------------
# 3. INTEGRATION_ERROR — generic non-200 from the token endpoint
# ---------------------------------------------------------------------------


class TestGenericTokenEndpointError:
    def test_503_raises_error(self, monkeypatch):
        from apowerb.tools_store.portfolio import onedrive_core

        monkeypatch.setenv("ONEDRIVE_REFRESH_TOKEN", "rt")
        monkeypatch.setenv("ONEDRIVE_CLIENT_ID", "cid")
        monkeypatch.setenv("ONEDRIVE_CLIENT_SECRET", "csec")
        monkeypatch.setattr(onedrive_core, "_ensure_integration_tokens", lambda: None)

        with patch.object(
            onedrive_core.httpx, "post", return_value=_fake_post(503, text="bad gateway")
        ):
            with pytest.raises(IntegrationStatusError) as ei:
                onedrive_core._get_access_token()

        assert ei.value.code == INTEGRATION_ERROR
        # Not remediable by reconnect — reconnecting won't fix Microsoft's
        # outage.
        assert ei.value.is_remediable_by_reconnect is False
        assert "503" in ei.value.message


# ---------------------------------------------------------------------------
# 4. INTEGRATION_ERROR — 200 OK but no access_token
# ---------------------------------------------------------------------------


class TestMalformedTokenResponse:
    def test_200_without_access_token_raises_error(self, monkeypatch):
        from apowerb.tools_store.portfolio import onedrive_core

        monkeypatch.setenv("ONEDRIVE_REFRESH_TOKEN", "rt")
        monkeypatch.setenv("ONEDRIVE_CLIENT_ID", "cid")
        monkeypatch.setenv("ONEDRIVE_CLIENT_SECRET", "csec")
        monkeypatch.setattr(onedrive_core, "_ensure_integration_tokens", lambda: None)

        # Empty body — no access_token field.
        resp = _fake_post(200, json_body={"token_type": "Bearer"})
        with patch.object(onedrive_core.httpx, "post", return_value=resp):
            with pytest.raises(IntegrationStatusError) as ei:
                onedrive_core._get_access_token()

        assert ei.value.code == INTEGRATION_ERROR
        assert ei.value.is_remediable_by_reconnect is False


# ---------------------------------------------------------------------------
# 5. End-to-end — tool returns structured tool_result on auth failure
# ---------------------------------------------------------------------------


class TestToolPropagatesStructuredPayload:
    def test_list_files_returns_integration_status_when_auth_fails(self, monkeypatch):
        """``onedrive_read.tool_list_files`` must propagate the structured
        payload — not the legacy ``status: error`` dict — when the token
        helper raises ``IntegrationStatusError``."""
        from apowerb.tools_store.portfolio import onedrive_read

        def _missing_helper():
            raise IntegrationStatusError(
                code=INTEGRATION_MISSING,
                provider=PROVIDER,
                message="user has not connected onedrive",
            )

        with patch.object(onedrive_read, "_graph_headers", side_effect=_missing_helper):
            out = onedrive_read.tool_list_files()

        assert out["status"] == "integration_status"
        assert out["code"] == INTEGRATION_MISSING
        assert out["provider"] == PROVIDER
        assert out["_integration_status"] is True

    def test_upload_returns_integration_status_when_auth_fails(self, monkeypatch):
        from apowerb.tools_store.portfolio import onedrive_write

        def _expired():
            raise IntegrationStatusError(
                code=INTEGRATION_EXPIRED,
                provider=PROVIDER,
                message="refresh token revoked",
            )

        with patch.object(onedrive_write, "_graph_headers", side_effect=_expired):
            # Pick whichever first tool the module exposes — the auth path is
            # identical across them.
            tools = [
                getattr(onedrive_write, name)
                for name in dir(onedrive_write)
                if name.startswith("tool_")
            ]
            assert tools

            out = None
            for fn in tools:
                try:
                    out = fn("placeholder")
                    break
                except TypeError:
                    continue
            assert out is not None

        assert out["status"] == "integration_status"
        assert out["code"] == INTEGRATION_EXPIRED


# ---------------------------------------------------------------------------
# 6. HTTP status code mapping — used by FastAPI routes
# ---------------------------------------------------------------------------


class TestHttpStatusCodeMapping:
    """``IntegrationStatusError.http_status_code`` is what the FastAPI
    routers in ``onedrive_browser`` / ``dataset_router`` pass to
    ``JSONResponse``. A wrong mapping leaks the wrong HTTP semantics to
    the frontend (401 = "log in again", 403 = "your admin must act",
    502 = "the upstream service failed")."""

    def test_missing_maps_to_401(self):
        err = IntegrationStatusError(
            code=INTEGRATION_MISSING, provider=PROVIDER, message="x"
        )
        assert err.http_status_code == 401

    def test_expired_maps_to_401(self):
        err = IntegrationStatusError(
            code=INTEGRATION_EXPIRED, provider=PROVIDER, message="x"
        )
        assert err.http_status_code == 401

    def test_error_maps_to_502(self):
        """A transient upstream provider error is a bad-gateway, not an
        auth failure — the user reconnecting cannot fix it."""
        err = IntegrationStatusError(
            code=INTEGRATION_ERROR, provider=PROVIDER, message="x"
        )
        assert err.http_status_code == 502

    def test_blocked_by_tenant_string_maps_to_403(self):
        """``INTEGRATION_BLOCKED_BY_TENANT`` is referenced by string in
        the mapping (the constant is added in a separate PR). 403 is
        correct: the user is authenticated, but their org has blocked
        the app at the tenant level."""
        err = IntegrationStatusError(
            code="INTEGRATION_BLOCKED_BY_TENANT", provider=PROVIDER, message="x"
        )
        assert err.http_status_code == 403

    def test_unknown_code_falls_back_to_401(self):
        err = IntegrationStatusError(
            code="WHATEVER_NEW_CODE", provider=PROVIDER, message="x"
        )
        assert err.http_status_code == 401
