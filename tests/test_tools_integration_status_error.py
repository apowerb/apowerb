"""Each tool that depends on Google or Microsoft auth must surface the
structured ``IntegrationStatusError`` payload to the LLM instead of a
free-form string. This test sweeps the tools that were missing the catch
in PR #87 — drive / calendar / docs / sheets / teams.

When a tool catches only ``RuntimeError`` (or ``Exception``), the
``IntegrationStatusError`` is technically caught (it inherits from
RuntimeError) but ``str(e)`` strips the structured ``code``, so the
LLM cannot decide what to do. With the explicit ``except
IntegrationStatusError`` first, the tool returns the dict produced by
``IntegrationStatusError.as_tool_result()``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from apowerb.tools_store.portfolio.integration_status import (
    INTEGRATION_MISSING,
    IntegrationStatusError,
)


def _missing(provider: str) -> IntegrationStatusError:
    return IntegrationStatusError(
        code=INTEGRATION_MISSING,
        provider=provider,
        message=f"User has not connected {provider}",
    )


def _assert_structured(result: dict, expected_provider: str) -> None:
    assert result["status"] == "integration_status"
    assert result["code"] == INTEGRATION_MISSING
    assert result["provider"] == expected_provider
    assert result["_integration_status"] is True
    assert result["remediable_by_reconnect"] is True
    assert result["retry"] is False


# ---------------------------------------------------------------------------
# Google Drive
# ---------------------------------------------------------------------------


class TestGoogleDriveStructuredAuthError:
    def test_get_account_info(self):
        from apowerb.tools_store.portfolio import google_drive

        with patch.object(
            google_drive, "google_auth_headers", side_effect=_missing("google")
        ):
            out = google_drive.tool_get_account_info()

        _assert_structured(out, "google")


# ---------------------------------------------------------------------------
# Google Calendar
# ---------------------------------------------------------------------------


class TestGoogleCalendarStructuredAuthError:
    def test_list_events(self):
        from apowerb.tools_store.portfolio import google_calendar

        with patch.object(
            google_calendar, "google_auth_headers", side_effect=_missing("google")
        ):
            out = google_calendar.tool_list_events()

        _assert_structured(out, "google")


# ---------------------------------------------------------------------------
# Google Docs
# ---------------------------------------------------------------------------


class TestGoogleDocsStructuredAuthError:
    def test_read_document(self):
        from apowerb.tools_store.portfolio import google_docs

        # Pick whichever public tool exists; tool_read_document is the most
        # universal naming. Fall back on inspection if absent.
        tools = [
            getattr(google_docs, name)
            for name in dir(google_docs)
            if name.startswith("tool_")
        ]
        assert tools, "google_docs exposes no tool_* function"

        with patch.object(
            google_docs, "google_auth_headers", side_effect=_missing("google")
        ):
            # Call the first tool with placeholder arguments. We don't care
            # which one — the auth path is identical. A signature mismatch
            # is a test bug, not a production bug.
            out = None
            for fn in tools:
                try:
                    out = fn("placeholder-id")
                    break
                except TypeError:
                    continue
            assert out is not None, "no tool_* accepted a 1-arg call"

        _assert_structured(out, "google")


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------


class TestGoogleSheetsStructuredAuthError:
    def test_first_tool(self):
        from apowerb.tools_store.portfolio import google_sheets

        tools = [
            getattr(google_sheets, name)
            for name in dir(google_sheets)
            if name.startswith("tool_")
        ]
        assert tools

        with patch.object(
            google_sheets, "google_auth_headers", side_effect=_missing("google")
        ):
            out = None
            for fn in tools:
                try:
                    out = fn("placeholder-id")
                    break
                except TypeError:
                    continue
            assert out is not None

        _assert_structured(out, "google")

    def test_write_cells_specifically(self):
        """``tool_write_cells`` is the only Google Sheets tool whose try
        block does NOT have ``except IntegrationStatusError`` immediately
        after the auth call — the JSONDecodeError clause sits between
        them. Make sure the structured payload still wins on auth failure
        (the JSON in ``values`` parses fine, so we reach the auth call
        and the IntegrationStatusError is raised as expected)."""
        from apowerb.tools_store.portfolio import google_sheets

        with patch.object(
            google_sheets, "google_auth_headers", side_effect=_missing("google")
        ):
            out = google_sheets.tool_write_cells(
                spreadsheet_id="abc",
                range="Sheet1!A1:B2",
                values='[["Name","Age"],["Alice","30"]]',
            )

        _assert_structured(out, "google")

    def test_write_cells_invalid_json_takes_precedence(self):
        """If the JSON cannot be parsed, the JSONDecodeError branch must
        still win — no auth call attempted, no structured payload."""
        from apowerb.tools_store.portfolio import google_sheets

        # Even with a stub that would raise IntegrationStatusError, the
        # JSON parsing fails first so the helper is never called.
        with patch.object(
            google_sheets, "google_auth_headers", side_effect=_missing("google")
        ):
            out = google_sheets.tool_write_cells(
                spreadsheet_id="abc",
                range="Sheet1!A1:B2",
                values="not-valid-json",
            )

        assert out["status"] == "error"
        assert "JSON" in out["message"] or "json" in out["message"].lower()
        # NOT a structured integration_status payload.
        assert "code" not in out


# ---------------------------------------------------------------------------
# Teams
# ---------------------------------------------------------------------------


class TestTeamsStructuredAuthError:
    def test_list_chats(self):
        from apowerb.tools_store.portfolio import teams

        # _graph_headers calls microsoft_auth_headers — patch the upstream.
        with patch.object(
            teams, "microsoft_auth_headers", side_effect=_missing("microsoft")
        ):
            out = teams.tool_list_chats()

        _assert_structured(out, "microsoft")
