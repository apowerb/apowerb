"""HTTP 423 (resourceLocked) on Microsoft Graph must surface as
``INTEGRATION_BLOCKED_BY_TENANT`` so the LLM does not nag the user to
reconnect — only the Microsoft 365 admin can resolve it.

Background
----------
On 2026-05-07 the SCEI tenant blocked the app via SharePoint App Access
Policy. ``/me`` kept returning 200 (identity OK) but every ``/me/drive/*``
call returned ``HTTP 423 resourceLocked``. The previous fallback returned
a generic ``"Graph API error"`` dict, which made the LLM ask the user to
reconnect (no effect).

This test covers the two ``_handle_graph_error`` helpers (OneDrive + Teams)
where a 423 reaching them is necessarily tenant-level: the transient
file-sync 423 is retried *before* these helpers are called.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from th2agent.tools_store.portfolio.integration_status import (
    INTEGRATION_BLOCKED_BY_TENANT,
)


def _fake_resp(status_code: int, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    return resp


# ---------------------------------------------------------------------------
# OneDrive
# ---------------------------------------------------------------------------


class TestOneDriveHandleGraphError:
    def test_423_maps_to_blocked_by_tenant(self):
        from th2agent.tools_store.portfolio.onedrive_core import _handle_graph_error

        resp = _fake_resp(
            423,
            text='{"error":{"code":"resourceLocked","message":"Access blocked"}}',
        )

        out = _handle_graph_error(resp, "list files")

        assert out is not None
        assert out["code"] == INTEGRATION_BLOCKED_BY_TENANT
        assert out["provider"] == "microsoft"
        assert out["status"] == "integration_status"
        assert out["remediable_by_reconnect"] is False
        assert out["retry"] is False
        # Message must hint at the admin remediation path so the LLM
        # surfaces it to the user verbatim.
        assert "admin" in out["message"].lower()
        assert "423" in out["message"]

    def test_2xx_returns_none(self):
        from th2agent.tools_store.portfolio.onedrive_core import _handle_graph_error

        assert _handle_graph_error(_fake_resp(200), "x") is None

    def test_401_keeps_legacy_format(self):
        """We do NOT touch existing 401/403/404/4xx mappings. A 401 still
        returns the legacy ``status=error`` dict so callers built around it
        keep working."""
        from th2agent.tools_store.portfolio.onedrive_core import _handle_graph_error

        out = _handle_graph_error(_fake_resp(401, text="unauth"), "x")

        assert out["status"] == "error"
        assert "code" not in out  # not a structured integration_status payload

    def test_500_does_not_map_to_blocked_by_tenant(self):
        """A non-423 5xx is a generic Graph error, not a tenant block."""
        from th2agent.tools_store.portfolio.onedrive_core import _handle_graph_error

        out = _handle_graph_error(_fake_resp(500, text="boom"), "x")

        assert out["status"] == "error"
        assert "code" not in out


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------


class TestTeamsHandleGraphError:
    def test_423_maps_to_blocked_by_tenant(self):
        from th2agent.tools_store.portfolio.teams import _handle_graph_error

        resp = _fake_resp(
            423,
            text='{"error":{"code":"resourceLocked","message":"Access blocked"}}',
        )

        out = _handle_graph_error(resp, "list chats")

        assert out is not None
        assert out["code"] == INTEGRATION_BLOCKED_BY_TENANT
        assert out["provider"] == "microsoft"
        assert out["remediable_by_reconnect"] is False

    def test_403_keeps_legacy_format(self):
        from th2agent.tools_store.portfolio.teams import _handle_graph_error

        out = _handle_graph_error(_fake_resp(403, text="forbidden"), "x")

        assert out["status"] == "error"
        assert "code" not in out


# ---------------------------------------------------------------------------
# Sanity — INTEGRATION_BLOCKED_BY_TENANT is non-remediable by reconnect
# ---------------------------------------------------------------------------


class TestRemediabilityFlag:
    def test_blocked_by_tenant_is_not_remediable(self):
        from th2agent.tools_store.portfolio.integration_status import (
            INTEGRATION_BLOCKED_BY_TENANT as code,
            IntegrationStatusError,
        )

        err = IntegrationStatusError(code=code, provider="microsoft", message="x")

        assert err.is_remediable_by_reconnect is False
        assert err.as_tool_result()["remediable_by_reconnect"] is False


# ---------------------------------------------------------------------------
# shared_upload_df — 423 surviving the full retry budget routes through
# _handle_graph_error so callers receive a structured payload instead of
# the legacy "Upload failed (HTTP 423)" prose.
# ---------------------------------------------------------------------------


class TestSharedUploadDf423:
    def test_residual_423_returns_structured_payload(self, monkeypatch):
        """If retries are exhausted on a tenant-level 423, callers must
        receive a dict carrying ``code=INTEGRATION_BLOCKED_BY_TENANT``,
        not a free-form string."""
        from th2agent.tools_store.portfolio import onedrive_core

        # The retry loop imports ``time as _time`` inside the function and
        # calls ``_time.sleep(...)``; patching the module-level
        # ``time.sleep`` no-ops the cumulative ~63 s budget.
        import time as _time_mod
        monkeypatch.setattr(_time_mod, "sleep", lambda _seconds: None)

        # Every PUT — initial attempt + 5 retries — replies 423.
        always_locked = _fake_resp(
            423, text='{"error":{"code":"resourceLocked","message":"tenant policy"}}'
        )
        monkeypatch.setattr(
            onedrive_core.httpx, "put", lambda *a, **kw: always_locked
        )

        # Minimal DataFrame so .to_excel does not blow up — pandas is
        # already a project dependency.
        import pandas as pd
        df = pd.DataFrame({"col": [1, 2, 3]})

        out = onedrive_core.shared_upload_df(
            "tracker.xlsx", df, headers={"Authorization": "Bearer x"}
        )

        assert isinstance(out, dict), (
            f"residual 423 must surface as structured dict, got {type(out).__name__}"
        )
        assert out.get("code") == INTEGRATION_BLOCKED_BY_TENANT
        assert out.get("provider") == "microsoft"
        assert out.get("remediable_by_reconnect") is False

    def test_other_4xx_keeps_legacy_string(self, monkeypatch):
        """A non-423 4xx still returns the plain-string payload so the
        existing ``isinstance(upload_err, dict)`` branch on callers is
        not pointlessly entered."""
        from th2agent.tools_store.portfolio import onedrive_core

        # No retries triggered (only 423 retries), so a 500 returns immediately.
        monkeypatch.setattr(
            onedrive_core.httpx, "put",
            lambda *a, **kw: _fake_resp(500, text="boom"),
        )

        import pandas as pd
        df = pd.DataFrame({"col": [1]})

        out = onedrive_core.shared_upload_df(
            "x.xlsx", df, headers={"Authorization": "Bearer x"}
        )

        assert isinstance(out, str)
        assert "500" in out
