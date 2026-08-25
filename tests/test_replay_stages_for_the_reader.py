"""Replay staging must put the PDF where the agent that READS it looks.

Bug: a webhook triggers one agent, and the replay path staged stored
attachments into that agent's uploads/ folder only. But tool_pdf_first_page is
bound by bind_pdf_first_page to the folder of the agent that DECLARES the tool
-- in a sequential pipeline, a sub-agent. The reader therefore saw an empty
available_files list and the run continued without ever opening the
attachment: extraction empty, nothing to match, and a confident wrong verdict
persisted and mailed out. No exception, no failed status.

The bench drives the real _stage_stored_attachments against a fake agent
table, and checks the filesystem -- the staging bug was invisible to anything
that only looked at return values, since the function returned the filename it
had "staged" all along.
"""

from __future__ import annotations

import json

import pytest

from apowerb.routers.webhook_handlers import outlook


PARENT = 12
CHILDREN = ["agent8", "agent9", "agent10"]


class _Row:
    def __init__(self, sub_agents):
        self.sub_agents = sub_agents


@pytest.fixture
def staged_into(tmp_path, monkeypatch):
    """Stage one PDF and return the set of folders that received it."""
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(outlook, "uploads_dir", lambda: uploads)

    src = tmp_path / "source.pdf"
    src.write_bytes(b"%PDF-1.4 payload")

    tree = {PARENT: json.dumps(CHILDREN)}

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, query):
            return [_Row(tree.get(i)) for i in getattr(query, "_asked", [PARENT])]

    class _Col:
        def in_(self, ids):
            q = type("Q", (), {})()
            q._asked = list(ids)
            return q

    class _Table:
        c = type("C", (), {"agent_id": _Col()})()

        def select(self):
            return self

        def where(self, cond):
            cond._asked = getattr(cond, "_asked", [PARENT])
            return cond

    class _Store:
        agent_table = _Table()
        engine = type("E", (), {"begin": staticmethod(lambda: _Conn())})()

    import apowerb.core.agent_main as agent_main

    monkeypatch.setattr(agent_main, "agent_store", _Store())

    def _run():
        names = outlook._stage_stored_attachments(
            [{"path": str(src), "filename": "ar.pdf"}], PARENT
        )
        assert names == ["ar.pdf"]
        return {
            p.parent.name
            for p in uploads.rglob("ar.pdf")
        }

    return _run


def test_the_reading_sub_agent_receives_the_pdf(staged_into):
    """The regression: agent8 declares tool_pdf_first_page and found nothing."""
    assert "agent8" in staged_into()


def test_every_sub_agent_and_the_parent_receive_the_pdf(staged_into):
    assert staged_into() == {"agent12", "agent8", "agent9", "agent10"}


def test_a_flat_agent_still_gets_its_own_folder(tmp_path, monkeypatch):
    """Negative control: no sub-agents means the old behaviour, unchanged."""
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(outlook, "uploads_dir", lambda: uploads)
    monkeypatch.setattr(
        outlook, "_reader_agent_folders", lambda aid, **k: [f"agent{aid}"], raising=False
    )
    src = tmp_path / "s.pdf"
    src.write_bytes(b"%PDF-1.4")
    outlook._stage_stored_attachments([{"path": str(src), "filename": "x.pdf"}], 7)
    assert {p.parent.name for p in uploads.rglob("x.pdf")} == {"agent7"}


def test_an_unreadable_agent_store_does_not_break_the_replay(tmp_path, monkeypatch):
    """Staging is best effort: a store error must degrade, never raise."""
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(outlook, "uploads_dir", lambda: uploads)

    class _Boom:
        @property
        def agent_table(self):
            raise RuntimeError("store unavailable")

    import apowerb.core.agent_main as agent_main

    monkeypatch.setattr(agent_main, "agent_store", _Boom())
    src = tmp_path / "s.pdf"
    src.write_bytes(b"%PDF-1.4")
    names = outlook._stage_stored_attachments(
        [{"path": str(src), "filename": "y.pdf"}], 5
    )
    assert names == ["y.pdf"]
    assert {p.parent.name for p in uploads.rglob("y.pdf")} == {"agent5"}


def test_non_pdf_attachments_are_ignored(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(outlook, "uploads_dir", lambda: uploads)
    monkeypatch.setattr(
        outlook, "_reader_agent_folders", lambda aid, **k: [f"agent{aid}"], raising=False
    )
    src = tmp_path / "s.docx"
    src.write_bytes(b"not a pdf")
    assert outlook._stage_stored_attachments(
        [{"path": str(src), "filename": "note.docx"}], 3
    ) == []
