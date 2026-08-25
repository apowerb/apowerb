"""Tests for the Graph-404 replay fallback helpers (outlook handler).
Completes the replay design: when an email left the mailbox, the agent reads
the stored PDFs from disk instead of re-fetching from Graph."""

from __future__ import annotations

import os
import shutil

import pytest


@pytest.fixture
def stored_pdf():
    """A stored attachment dict pointing to a real PDF on disk + cleanup."""
    src_dir = os.path.join("uploads", "_stored_src_test")
    os.makedirs(src_dir, exist_ok=True)
    path = os.path.join(src_dir, "AR_CF101085.pdf")
    with open(path, "wb") as f:
        f.write(b"%PDF-1.4 fake ar")
    yield {"filename": "AR_CF101085.pdf", "content_type": "application/pdf", "path": path}
    shutil.rmtree(src_dir, ignore_errors=True)
    shutil.rmtree(os.path.join("uploads", "agent999"), ignore_errors=True)


class TestStageStoredAttachments:
    def test_stages_pdf_into_agent_uploads(self, stored_pdf):
        from apowerb.routers.webhook_handlers.outlook import (
            _stage_stored_attachments,
        )

        staged = _stage_stored_attachments([stored_pdf], 999)
        assert staged == ["AR_CF101085.pdf"]
        dst = os.path.join("uploads", "agent999", "AR_CF101085.pdf")
        assert os.path.exists(dst)
        with open(dst, "rb") as f:
            assert f.read()[:5] == b"%PDF-"

    def test_skips_non_pdf_and_missing(self):
        from apowerb.routers.webhook_handlers.outlook import (
            _stage_stored_attachments,
        )

        staged = _stage_stored_attachments(
            [
                {"filename": "logo.png", "content_type": "image/png", "path": "/tmp/x.png"},
                {"filename": "gone.pdf", "content_type": "application/pdf", "path": "/no/such.pdf"},
                {"filename": None, "path": None},
            ],
            999,
        )
        assert staged == []

    def test_empty_list(self):
        from apowerb.routers.webhook_handlers.outlook import (
            _stage_stored_attachments,
        )

        assert _stage_stored_attachments([], 999) == []
        assert _stage_stored_attachments(None, 999) == []


class TestReplayInstruction:
    def test_mentions_files_and_bans_graph_tools(self):
        from apowerb.routers.webhook_handlers.outlook import _replay_instruction

        note = _replay_instruction(["AR_CF101085.pdf"])
        assert "AR_CF101085.pdf" in note
        assert "REPLAY MODE" in note
        assert "tool_read_email" in note and "tool_download_attachment" in note
        assert "tool_pdf_first_page" in note

    def test_handles_no_files(self):
        """With nothing staged, the note must not describe an attachment.

        It used to interpolate "(none)" into a sentence that still asserted the
        PDFs were "ALREADY in your uploads directory". An agent reading that
        looks for a file, finds none, and answers anyway -- a confident verdict
        with no document read. The note now says so plainly instead.
        """
        from apowerb.routers.webhook_handlers.outlook import _replay_instruction

        note = _replay_instruction([])
        assert "ALREADY in your uploads directory" not in note
        assert "NO attachment could be staged" in note
        assert "do NOT infer its contents" in note
        assert "REPLAY MODE" in note


class TestReplayPathIntegration:
    """Integration: when fetch_email raises a 404, process_webhook_log_row
    must reconstruct the input from the stored copy (attachments as a Python
    list, like SQLAlchemy JSON returns) and run the agent from staged PDFs."""

    async def test_404_replays_from_stored(self, stored_pdf, monkeypatch):
        import contextlib
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        import apowerb.routers.webhook_handlers.outlook as ol

        # Fake log row: attachments is a LIST (ORM JSON column), agent_message set
        log_row = SimpleNamespace(
            id=999,
            subscription_id=1,
            user_id=3,
            agent_id=999,
            resource_id="res",
            agent_message="Subject: CC n CF101085\n\nbody",
            email_subject="CC n 11077978 V/R:CF101085",
            email_sender="x@dupont-est.fr",
            attachments=[stored_pdf],  # already a list, NOT a JSON string
        )

        class _DB:
            async def get(self, model, _id):
                return log_row

            async def commit(self):
                return None

        @contextlib.asynccontextmanager
        async def _session():
            yield _DB()

        monkeypatch.setattr(ol.sessionmanager, "session", _session)
        monkeypatch.setattr(
            ol.OutlookWebhookService, "get_access_token_for_user",
            AsyncMock(return_value="tok"),
        )
        monkeypatch.setattr(
            ol.OutlookWebhookService, "fetch_email",
            AsyncMock(side_effect=RuntimeError(
                "Failed to fetch email from Graph API (HTTP 404): ErrorItemNotFound")),
        )
        captured = {}

        async def _run(**kw):
            captured.update(kw)
            return "done"

        monkeypatch.setattr(ol, "run_agent_for_webhook", _run)
        monkeypatch.setattr(ol, "create_webhook_notification", AsyncMock(), raising=False)

        # Worker now passes the log_id scalar; the processor loads its own row.
        await ol.process_webhook_log_row(999)

        # The agent was run from the stored copy, not Graph
        assert "REPLAY MODE" in captured["message_text"]
        assert "AR_CF101085.pdf" in captured["message_text"]
        atts = captured["initial_state"]["attachments"]
        assert atts and atts[0]["filename"] == "AR_CF101085.pdf"
        # staged on disk where tool_pdf_first_page reads
        assert os.path.exists(os.path.join("uploads", "agent999", "AR_CF101085.pdf"))

    async def test_401_does_not_replay_propagates(self, monkeypatch):
        import contextlib
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        import pytest as _pytest
        import apowerb.routers.webhook_handlers.outlook as ol

        @contextlib.asynccontextmanager
        async def _session():
            yield SimpleNamespace(get=AsyncMock())

        monkeypatch.setattr(ol.sessionmanager, "session", _session)
        monkeypatch.setattr(
            ol.OutlookWebhookService, "get_access_token_for_user",
            AsyncMock(return_value="tok"),
        )
        monkeypatch.setattr(
            ol.OutlookWebhookService, "fetch_email",
            AsyncMock(side_effect=RuntimeError(
                "Failed to fetch email from Graph API (HTTP 401): token expired")),
        )
        # 401 must NOT trigger replay — it propagates
        with _pytest.raises(RuntimeError, match="401"):
            await ol.process_webhook_log_row(999)
