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
        outlook, "_reader_agent_folders", lambda aid, **k: ([f"agent{aid}"], True),
        raising=False,
    )
    src = tmp_path / "s.pdf"
    src.write_bytes(b"%PDF-1.4")
    outlook._stage_stored_attachments([{"path": str(src), "filename": "x.pdf"}], 7)
    assert {p.parent.name for p in uploads.rglob("x.pdf")} == {"agent7"}


def test_an_unreadable_agent_store_does_not_break_the_replay(tmp_path, monkeypatch):
    """A store error must degrade, never raise -- and must not announce.

    Falling back to the triggered agent alone means we no longer know where the
    reader lives, so claiming the PDF is staged would be a guess. The agent is
    told instead that no attachment is available, which surfaces as a readable
    failure rather than a confident wrong verdict.
    """
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
    assert names == []


def test_non_pdf_attachments_are_ignored(tmp_path, monkeypatch):
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(outlook, "uploads_dir", lambda: uploads)
    monkeypatch.setattr(
        outlook, "_reader_agent_folders", lambda aid, **k: ([f"agent{aid}"], True),
        raising=False,
    )
    src = tmp_path / "s.docx"
    src.write_bytes(b"not a pdf")
    assert outlook._stage_stored_attachments(
        [{"path": str(src), "filename": "note.docx"}], 3
    ) == []

# ---------------------------------------------------------------------------
# Coverage added after an external review: the first bench only exercised one
# flat parent with three direct children, and claimed a fallback contract it
# never tested at depth.
# ---------------------------------------------------------------------------


def _store_from(tree, fail_at_depth=None):
    """Fake agent store over {parent_id: [child names]}.

    fail_at_depth: raise on the Nth query (0-based) to exercise a failure that
    happens AFTER the walk already collected descendants.
    """
    state = {"calls": 0}

    class _Row:
        def __init__(self, sub_agents):
            self.sub_agents = sub_agents

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, query):
            if fail_at_depth is not None and state["calls"] - 1 == fail_at_depth:
                raise RuntimeError("store went away mid-walk")
            return [
                _Row(json.dumps(tree[i]) if i in tree else None)
                for i in getattr(query, "_asked", [])
            ]

    class _Col:
        def in_(self, ids):
            state["calls"] += 1
            q = type("Q", (), {})()
            q._asked = list(ids)
            return q

    class _Table:
        c = type("C", (), {"agent_id": _Col()})()

        def select(self):
            return self

        def where(self, cond):
            return cond

    return type(
        "S",
        (),
        {
            "agent_table": _Table(),
            "engine": type("E", (), {"begin": staticmethod(lambda: _Conn())})(),
        },
    )()


@pytest.fixture
def folders_for(monkeypatch):
    def _go(tree, agent_id=1, fail_at_depth=None, **kw):
        import apowerb.core.agent_main as agent_main

        monkeypatch.setattr(
            agent_main, "agent_store", _store_from(tree, fail_at_depth)
        )
        return outlook._reader_agent_folders(agent_id, **kw)

    return _go


def test_a_grandchild_is_reached(folders_for):
    """The reader can sit two levels down, not just one."""
    assert folders_for({1: ["agent2"], 2: ["agent3"]}) == (
        ["agent1", "agent2", "agent3"],
        True,
    )


def test_a_cycle_does_not_loop_forever(folders_for):
    got, complete = folders_for({1: ["agent2"], 2: ["agent1", "agent3"], 3: ["agent2"]})
    assert sorted(got) == ["agent1", "agent2", "agent3"]
    assert complete is True


def test_a_shared_child_is_listed_once(folders_for):
    """Two parents pointing at the same sub-agent must not duplicate it."""
    got, _ = folders_for({1: ["agent2", "agent3"], 2: ["agent4"], 3: ["agent4"]})
    assert got.count("agent4") == 1


def test_the_depth_cap_reports_an_incomplete_list(folders_for):
    """Truncation must be declared, not silently returned as a full list: the
    caller refuses to announce a file when coverage is not complete."""
    deep = {1: ["agent2"], 2: ["agent3"], 3: ["agent4"], 4: ["agent5"]}
    assert folders_for(deep, _max_depth=2) == (
        ["agent1", "agent2", "agent3"],
        False,
    )


def test_a_failure_midway_falls_back_to_the_parent_alone(folders_for):
    """The regression an external reviewer caught: the accumulator was mutated
    during the walk, so a late failure returned a half-explored tree that read
    like a complete one."""
    assert folders_for({1: ["agent2"], 2: ["agent3"]}, fail_at_depth=1) == (
        ["agent1"],
        False,
    )


def test_a_partial_copy_is_not_announced_as_staged(tmp_path, monkeypatch):
    """Announcing a file the reader cannot open is the original defect."""
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(outlook, "uploads_dir", lambda: uploads)
    monkeypatch.setattr(
        outlook, "_reader_agent_folders", lambda aid, **k: (["agentA", "agentB"], True),
        raising=False,
    )
    real_copy = outlook.shutil.copyfile

    def _copy(src, dst):
        if "agentB" in str(dst):
            raise PermissionError("read-only")
        return real_copy(src, dst)

    monkeypatch.setattr(outlook.shutil, "copyfile", _copy)
    src = tmp_path / "s.pdf"
    src.write_bytes(b"%PDF-1.4")
    assert outlook._stage_stored_attachments(
        [{"path": str(src), "filename": "z.pdf"}], 1
    ) == []


def test_an_undeletable_uploads_dir_does_not_raise(tmp_path, monkeypatch):
    """makedirs used to sit outside every guard: a permission error there broke
    the whole replay, despite the documented best-effort contract."""
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(outlook, "uploads_dir", lambda: uploads)
    monkeypatch.setattr(
        outlook, "_reader_agent_folders", lambda aid, **k: (["agentA"], True),
        raising=False,
    )
    monkeypatch.setattr(
        outlook.os, "makedirs", lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
    )
    src = tmp_path / "s.pdf"
    src.write_bytes(b"%PDF-1.4")
    assert outlook._stage_stored_attachments(
        [{"path": str(src), "filename": "z.pdf"}], 1
    ) == []


def test_the_replay_note_never_promises_a_file_that_is_not_there():
    """An agent told the PDF is present, finding nothing, still answers -- and a
    confident answer with no document read is the failure mode to close."""
    empty = outlook._replay_instruction([])
    assert "NO attachment could be staged" in empty
    assert "ALREADY in your uploads directory" not in empty
    full = outlook._replay_instruction(["ar.pdf"])
    assert "ar.pdf" in full and "ALREADY in your uploads directory" in full


# ---------------------------------------------------------------------------
# Second review pass: an unreachable folder was dropped from the copy list but
# the file was still announced, and a truncated walk still counted as complete.
# ---------------------------------------------------------------------------


def _one_pdf(tmp_path):
    src = tmp_path / "s.pdf"
    src.write_bytes(b"%PDF-1.4")
    return [{"path": str(src), "filename": "z.pdf"}]


def test_an_uncreatable_folder_blocks_the_announcement(tmp_path, monkeypatch):
    """Dropping a folder we could not create used to leave the copy list
    self-consistent, so the file was announced while that reader had nothing --
    the original defect, reintroduced through the error path."""
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(outlook, "uploads_dir", lambda: uploads)
    monkeypatch.setattr(
        outlook, "_reader_agent_folders", lambda aid, **k: (["agentA", "agentB"], True),
        raising=False,
    )
    real = outlook.os.makedirs

    def _mk(path, **kw):
        if "agentB" in str(path):
            raise OSError("read-only")
        return real(path, **kw)

    monkeypatch.setattr(outlook.os, "makedirs", _mk)
    assert outlook._stage_stored_attachments(_one_pdf(tmp_path), 1) == []


def test_an_incomplete_reader_list_blocks_the_announcement(tmp_path, monkeypatch):
    """Every copy succeeds, but the walk never saw the whole tree."""
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(outlook, "uploads_dir", lambda: uploads)
    monkeypatch.setattr(
        outlook, "_reader_agent_folders", lambda aid, **k: (["agentA"], False),
        raising=False,
    )
    assert outlook._stage_stored_attachments(_one_pdf(tmp_path), 1) == []


def test_a_refused_staging_leaves_no_half_copy_behind(tmp_path, monkeypatch):
    """A leftover from a partial attempt is indistinguishable from a good one."""
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(outlook, "uploads_dir", lambda: uploads)
    monkeypatch.setattr(
        outlook, "_reader_agent_folders", lambda aid, **k: (["agentA", "agentB"], True),
        raising=False,
    )
    real_copy = outlook.shutil.copyfile

    def _copy(src, dst):
        if "agentB" in str(dst):
            raise PermissionError("read-only")
        return real_copy(src, dst)

    monkeypatch.setattr(outlook.shutil, "copyfile", _copy)
    assert outlook._stage_stored_attachments(_one_pdf(tmp_path), 1) == []
    assert list(uploads.rglob("z.pdf")) == []


# ---------------------------------------------------------------------------
# Third review pass: the cleanup could delete a pre-existing good copy, and a
# missing agent row was read as a leaf.
# ---------------------------------------------------------------------------


def test_a_failed_staging_leaves_a_preexisting_copy_intact(tmp_path, monkeypatch):
    """The previous cleanup removed the destination on failure -- including a
    good copy staged by an earlier attempt. Staging is now two-phase: nothing
    already on disk is touched unless every destination landed."""
    uploads = tmp_path / "uploads"
    monkeypatch.setattr(outlook, "uploads_dir", lambda: uploads)
    monkeypatch.setattr(
        outlook, "_reader_agent_folders", lambda aid, **k: (["agentA", "agentB"], True),
        raising=False,
    )
    good = uploads / "agentA" / "z.pdf"
    good.parent.mkdir(parents=True)
    good.write_bytes(b"%PDF-1.4 GOOD COPY FROM AN EARLIER ATTEMPT")

    real_copy = outlook.shutil.copyfile

    def _copy(src, dst):
        if "agentB" in str(dst):
            raise PermissionError("read-only")
        return real_copy(src, dst)

    monkeypatch.setattr(outlook.shutil, "copyfile", _copy)
    assert outlook._stage_stored_attachments(_one_pdf(tmp_path), 1) == []
    assert good.exists(), "the earlier good copy was destroyed"
    assert good.read_bytes() == b"%PDF-1.4 GOOD COPY FROM AN EARLIER ATTEMPT"
    assert list(uploads.rglob("*.part")) == [], "temp files left behind"


def test_a_missing_agent_row_is_not_read_as_a_leaf(monkeypatch):
    """No row for an agent means we do not know whether it has children.
    Treating silence as "no children" is how an unlisted reader ends up with a
    file announced that it never received."""
    import apowerb.core.agent_main as agent_main

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, query):
            return []  # the agent row is gone

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
            return cond

    monkeypatch.setattr(
        agent_main,
        "agent_store",
        type(
            "S",
            (),
            {
                "agent_table": _Table(),
                "engine": type("E", (), {"begin": staticmethod(lambda: _Conn())})(),
            },
        )(),
    )
    assert outlook._reader_agent_folders(1) == (["agent1"], False)
