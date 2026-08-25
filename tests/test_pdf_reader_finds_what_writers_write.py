"""pdf_first_page must find what the writers wrote.

Root cause of a production incident: the reader closure is bound to the folder
of the agent that DECLARES the tool -- a sub-agent of a sequential pipeline --
while tool_download_attachment and every other factory tool write to
uploads/agent{ROOT_AGENT_ID}, the agent the run was triggered on. The intake
downloaded the AR, then reported an empty uploads dir for a file sitting one
directory away, and the pipeline published a verdict on an unread document.
"""

from __future__ import annotations

import os

import pytest

from apowerb.core.agent_helpers.pdf_to_images_tool import _make_pdf_first_page

MINIMAL_PDF = (
    b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n"
)


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    import apowerb.core.agent_helpers.pdf_to_images_tool as mod

    monkeypatch.setattr(mod, "uploads_dir", lambda: root)
    return root


def _write(uploads, folder, name):
    d = uploads / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(MINIMAL_PDF)


def test_it_reads_from_its_own_folder(uploads, monkeypatch):
    monkeypatch.setenv("ROOT_AGENT_ID", "12")
    _write(uploads, "agent8", "ar.pdf")
    assert _make_pdf_first_page("agent8")("ar.pdf")["status"] == "success"


def test_it_also_reads_where_the_writers_write(uploads, monkeypatch):
    """The regression: the file is in the triggered agent's folder, which is
    where tool_download_attachment puts it, and the reader is bound to the
    declaring sub-agent."""
    monkeypatch.setenv("ROOT_AGENT_ID", "12")
    _write(uploads, "agent12", "ar.pdf")
    assert _make_pdf_first_page("agent8")("ar.pdf")["status"] == "success"


def test_its_own_folder_wins_when_both_hold_the_name(uploads, monkeypatch):
    monkeypatch.setenv("ROOT_AGENT_ID", "12")
    _write(uploads, "agent8", "ar.pdf")
    _write(uploads, "agent12", "ar.pdf")
    assert _make_pdf_first_page("agent8")("ar.pdf")["status"] == "success"


def test_a_genuinely_absent_file_names_both_folders(uploads, monkeypatch):
    """Negative control: the error must not go quiet, and must say where it
    looked -- the previous message named a single folder, which is what made
    the incident so hard to read."""
    monkeypatch.setenv("ROOT_AGENT_ID", "12")
    _write(uploads, "agent12", "autre.pdf")
    out = _make_pdf_first_page("agent8")("ar.pdf")
    assert out["status"] == "error"
    assert "agent8" in out["message"] and "agent12" in out["message"]
    assert "agent12/autre.pdf" in out["available_files"]


def test_without_a_root_agent_id_nothing_changes(uploads, monkeypatch):
    monkeypatch.delenv("ROOT_AGENT_ID", raising=False)
    _write(uploads, "agent8", "ar.pdf")
    assert _make_pdf_first_page("agent8")("ar.pdf")["status"] == "success"
    out = _make_pdf_first_page("agent8")("absent.pdf")
    assert out["status"] == "error" and "agent12" not in out["message"]


def test_a_crafted_filename_cannot_escape_the_uploads_dir(uploads, monkeypatch):
    monkeypatch.setenv("ROOT_AGENT_ID", "12")
    secret = uploads.parent / "passwd"
    secret.write_bytes(MINIMAL_PDF)
    out = _make_pdf_first_page("agent8")("../../passwd")
    assert out["status"] == "error"
    assert not os.path.isabs(out.get("message", ""))
