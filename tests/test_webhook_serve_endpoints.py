"""Integration tests for the two new serve endpoints.

We exercise the security envelope explicitly: cross-user 404, hostile
filename, missing file on disk, MIME from DB, Content-Disposition
heuristic, body-as-html happy path.
"""
from __future__ import annotations

import os
import pathlib
from datetime import datetime, timezone
from unittest.mock import MagicMock

from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient


USER_A_ID = 1
USER_A_EMAIL = "alice@example.com"


def _fake_user(user_id: int, email: str):
    u = MagicMock()
    u.email = email
    u.user_id = user_id
    u.role = "USER"
    return u


def _make_log(
    log_id: int,
    *,
    user_id: int = USER_A_ID,
    body_html: str = "<p>hello</p>",
    attachments: list | None = None,
):
    log = MagicMock()
    log.id = log_id
    log.user_id = user_id
    log.subscription_id = 1
    log.agent_id = 1
    log.email_body_html = body_html
    log.attachments = attachments or []
    log.email_subject = "test"
    log.email_sender = "s@x"
    log.agent_message = "msg"
    log.agent_response = "resp"
    log.trigger_event = "created"
    log.status = "success"
    log.error_message = None
    log.duration_ms = 12
    log.created_at = datetime.now(timezone.utc)
    return log


class _FakeSession:
    def __init__(self, *, scalar_one_or_none=None):
        self._scalar_one_or_none = scalar_one_or_none

    async def execute(self, stmt):
        res = MagicMock()
        res.scalar_one_or_none = MagicMock(return_value=self._scalar_one_or_none)
        return res


def _build_app(session: _FakeSession, *, user_id: int | None = USER_A_ID, email: str | None = USER_A_EMAIL):
    from th2agent.auth.dependencies import get_current_user
    from th2agent.helpers.database import get_db
    from th2agent.routers.webhooks import router

    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def _user_override():
        if user_id is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="x")
        return _fake_user(user_id, email or USER_A_EMAIL)

    async def _db_override():
        yield session

    app.dependency_overrides[get_current_user] = _user_override
    app.dependency_overrides[get_db] = _db_override
    return app


# ---------------------------------------------------------------------------
# GET /api/webhooks/logs/{log_id}/body
# ---------------------------------------------------------------------------


class TestBodyEndpoint:
    def test_returns_html_for_owner(self):
        log = _make_log(42, body_html="<p>Hi <b>there</b></p>")
        client = TestClient(_build_app(_FakeSession(scalar_one_or_none=log)))
        resp = client.get("/api/webhooks/logs/42/body")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "<p>Hi <b>there</b></p>" in resp.text

    def test_404_when_owner_mismatch(self):
        # Simulate the SQL filter eliminating the log
        client = TestClient(_build_app(_FakeSession(scalar_one_or_none=None)))
        resp = client.get("/api/webhooks/logs/42/body")
        assert resp.status_code == 404

    def test_returns_empty_body_as_empty_html(self):
        log = _make_log(42, body_html=None)
        client = TestClient(_build_app(_FakeSession(scalar_one_or_none=log)))
        resp = client.get("/api/webhooks/logs/42/body")
        assert resp.status_code == 200
        assert resp.text == ""

    def test_requires_authentication(self):
        client = TestClient(
            _build_app(_FakeSession(scalar_one_or_none=None), user_id=None, email=None),
            raise_server_exceptions=False,
        )
        resp = client.get("/api/webhooks/logs/42/body")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/webhooks/logs/{log_id}/attachments/{filename}
# ---------------------------------------------------------------------------


class TestAttachmentEndpoint:
    def _prepare_log_with_file(self, tmp_path, log_id: int, filename: str, content: bytes, content_type: str):
        # Mirror the production layout under ATTACHMENT_ROOT and patch it
        # for the duration of this test.
        from th2agent.storage import webhook_attachments as wa
        wa.ATTACHMENT_ROOT = tmp_path  # type: ignore[attr-defined]

        target = tmp_path / "2026" / "05" / str(log_id) / filename
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

        return _make_log(
            log_id,
            attachments=[{
                "filename": filename,
                "path": str(target),
                "content_type": content_type,
                "size": len(content),
            }],
        )

    def test_inline_pdf_disposition_and_db_mime(self, tmp_path):
        log = self._prepare_log_with_file(
            tmp_path, 100, "Facture.pdf", b"%PDF-1.7\n...", "application/pdf",
        )
        client = TestClient(_build_app(_FakeSession(scalar_one_or_none=log)))
        resp = client.get("/api/webhooks/logs/100/attachments/Facture.pdf")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        # PDF must be inline (so iframe preview works).
        assert "inline" in resp.headers.get("content-disposition", "")
        assert resp.content.startswith(b"%PDF")

    def test_uppercase_extension_uses_db_mime_not_guess(self, tmp_path):
        # mimetypes.guess_type("AR.PDF") returns None on Linux. The DB
        # has "application/pdf", that's what we must serve.
        log = self._prepare_log_with_file(
            tmp_path, 101, "AR.PDF", b"%PDF-1.7\n", "application/pdf",
        )
        client = TestClient(_build_app(_FakeSession(scalar_one_or_none=log)))
        resp = client.get("/api/webhooks/logs/101/attachments/AR.PDF")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"

    def test_non_pdf_disposition_is_attachment(self, tmp_path):
        log = self._prepare_log_with_file(
            tmp_path, 102, "data.xlsx", b"PK\x03\x04...",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        client = TestClient(_build_app(_FakeSession(scalar_one_or_none=log)))
        resp = client.get("/api/webhooks/logs/102/attachments/data.xlsx")
        assert resp.status_code == 200
        # Non-PDF / non-image must download.
        assert "attachment" in resp.headers.get("content-disposition", "")

    def test_image_disposition_inline(self, tmp_path):
        log = self._prepare_log_with_file(
            tmp_path, 103, "photo.png", b"\x89PNG\r\n", "image/png",
        )
        client = TestClient(_build_app(_FakeSession(scalar_one_or_none=log)))
        resp = client.get("/api/webhooks/logs/103/attachments/photo.png")
        assert resp.status_code == 200
        assert "inline" in resp.headers.get("content-disposition", "")

    def test_404_when_filename_not_in_db(self, tmp_path):
        # File exists on disk but isn't declared in the row's attachments JSON.
        log = self._prepare_log_with_file(
            tmp_path, 104, "Facture.pdf", b"x", "application/pdf",
        )
        # Override declared attachments to something else
        log.attachments = [{"filename": "other.pdf", "path": "/tmp/other.pdf",
                            "content_type": "application/pdf", "size": 0}]
        client = TestClient(_build_app(_FakeSession(scalar_one_or_none=log)),
                            raise_server_exceptions=False)
        resp = client.get("/api/webhooks/logs/104/attachments/Facture.pdf")
        assert resp.status_code == 404

    def test_404_when_owner_mismatch(self, tmp_path):
        # log not returned because the SQL filter eliminated it
        client = TestClient(_build_app(_FakeSession(scalar_one_or_none=None)),
                            raise_server_exceptions=False)
        resp = client.get("/api/webhooks/logs/105/attachments/any.pdf")
        assert resp.status_code == 404

    def test_404_when_file_missing_on_disk(self, tmp_path):
        # Declared in DB but file removed (or PR #188 wasn't deployed yet
        # when the row was created).
        from th2agent.storage import webhook_attachments as wa
        wa.ATTACHMENT_ROOT = tmp_path
        log = _make_log(106, attachments=[{
            "filename": "lost.pdf", "path": "/nope", "content_type": "application/pdf", "size": 0
        }])
        client = TestClient(_build_app(_FakeSession(scalar_one_or_none=log)),
                            raise_server_exceptions=False)
        resp = client.get("/api/webhooks/logs/106/attachments/lost.pdf")
        assert resp.status_code == 404

    def test_path_traversal_attempt_is_404(self, tmp_path):
        # The URL has a hostile-looking filename. After sanitize_filename
        # it becomes "passwd" (no path components). The row has no
        # attachment named "passwd", so 404.
        log = self._prepare_log_with_file(
            tmp_path, 107, "Facture.pdf", b"%PDF-1.7\n", "application/pdf",
        )
        client = TestClient(_build_app(_FakeSession(scalar_one_or_none=log)),
                            raise_server_exceptions=False)
        resp = client.get("/api/webhooks/logs/107/attachments/..%2F..%2Fetc%2Fpasswd")
        assert resp.status_code in (404, 400)
        # Either way, must NOT have served any content.
        assert b"root:" not in resp.content

    def test_requires_authentication(self):
        client = TestClient(
            _build_app(_FakeSession(scalar_one_or_none=None), user_id=None, email=None),
            raise_server_exceptions=False,
        )
        resp = client.get("/api/webhooks/logs/108/attachments/x.pdf")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Org-shared read access (2026-06-01): a viewer in the SAME organization as
# the log owner (same email domain) may READ detail/body/attachments. A
# viewer from another org still gets 404 (no enumeration oracle).
# ---------------------------------------------------------------------------


class _SeqSession:
    """Returns successive scalar_one_or_none values, one per execute()."""

    def __init__(self, results):
        self._results = list(results)

    async def execute(self, stmt):
        res = MagicMock()
        val = self._results.pop(0) if self._results else None
        res.scalar_one_or_none = MagicMock(return_value=val)
        return res


def _fake_owner(user_id: int, email: str):
    o = MagicMock()
    o.user_id = user_id
    o.email = email
    return o


class TestOrgSharedReadAccess:
    def test_same_org_non_owner_can_read_detail(self):
        log = _make_log(50, user_id=1)               # owner = user 1
        owner = _fake_owner(1, "alice@scei88.fr")
        session = _SeqSession([log, owner])          # 1) log  2) owner lookup
        app = _build_app(session, user_id=2, email="bob@scei88.fr")  # same domain
        resp = TestClient(app).get("/api/webhooks/logs/50")
        assert resp.status_code == 200

    def test_other_org_non_owner_gets_404(self):
        log = _make_log(50, user_id=1)
        owner = _fake_owner(1, "alice@scei88.fr")
        session = _SeqSession([log, owner])
        app = _build_app(session, user_id=2, email="carol@rival.com")  # other domain
        resp = TestClient(app).get("/api/webhooks/logs/50")
        assert resp.status_code == 404

    def test_owner_reads_without_owner_lookup(self):
        log = _make_log(50, user_id=7)
        session = _SeqSession([log])                 # only the log; no 2nd query
        app = _build_app(session, user_id=7, email="dave@scei88.fr")
        resp = TestClient(app).get("/api/webhooks/logs/50")
        assert resp.status_code == 200

    def test_missing_log_is_404(self):
        session = _SeqSession([None])
        app = _build_app(session, user_id=2, email="bob@scei88.fr")
        resp = TestClient(app).get("/api/webhooks/logs/999")
        assert resp.status_code == 404

    def test_body_stays_owner_strict_for_same_org(self):
        # /body is NOT org-shared (only detail + attachments are). A same-org
        # non-owner must not read the raw email HTML. The owner-strict SQL
        # filter eliminates the row, so the mocked session returns None -> 404.
        session = _FakeSession(scalar_one_or_none=None)
        app = _build_app(session, user_id=2, email="bob@scei88.fr")
        resp = TestClient(app).get("/api/webhooks/logs/50/body")
        assert resp.status_code == 404
