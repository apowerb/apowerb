"""Where a DB export lands must not depend on an earlier request.

The index-db routes wrote ``AGENT_FOLDER`` to ``os.environ`` and never cleared
it; ``tool_load_from_db_and_index`` read it to choose the CSV folder. The same
function is an agent tool, so an agent calling it later in the worker wrote
its export, and returned its download path, under the folder of the last
index-db request.
"""

from __future__ import annotations

import contextlib
import contextvars
from pathlib import Path
from types import SimpleNamespace

import pytest

from apowerb.core import invocation_context as ic
from apowerb.routers.rag import index_db
from apowerb.tools_store.portfolio import db_to_rag


class _Cursor:
    description = [("id",)]

    def execute(self, sql, params=None):
        pass

    def fetchall(self):
        return [{"id": 1}]

    def close(self):
        pass


class _Conn:
    def cursor(self, cursor_factory=None):
        return _Cursor()

    def close(self):
        pass


@pytest.fixture(autouse=True)
def _db(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_FOLDER", raising=False)
    monkeypatch.setattr(db_to_rag, "uploads_dir", lambda: tmp_path)
    monkeypatch.setattr(db_to_rag, "_get_connection", lambda: _Conn())
    monkeypatch.setattr(db_to_rag, "read_only_transaction", lambda *a: contextlib.nullcontext())
    monkeypatch.setattr(
        db_to_rag, "tool_create_knowledge", lambda **k: {"status": "success", "knowledge_id": "k"}
    )
    monkeypatch.setattr(index_db, "load_tool_config_params", lambda *a, **k: (None, {}))
    yield


def test_index_db_request_exports_to_its_own_session_folder():
    data = SimpleNamespace(
        tool_config_id="tool_config9", session_id="session-b", agent_id="agent2",
        sql_query="SELECT id FROM orders", name="kb-b",
    )
    result = index_db._sync_index_db(data, owner_id="b@b.test")
    assert Path(result["csv_path"]).parent.name == "session-b"
    assert result["download_path"].startswith("/api/files/session-b/")


def test_agent_tool_does_not_export_into_the_previous_request_folder():
    # An index-db request for another agent ran earlier in this worker.
    data = SimpleNamespace(
        tool_config_id="tool_config9", session_id="session-b", agent_id="agent2",
        sql_query="SELECT id FROM orders", name="kb-b",
    )
    index_db._sync_index_db(data, owner_id="b@b.test")

    # Now agent 1 calls the same function as a tool in its own run. The run
    # gets its own context, as an ADK invocation does, so the identity bound
    # here does not stay set for the tests that follow.
    def agent_run():
        ic._root_invocation_var.set(None)  # a fresh invocation records its root
        ic.bind_agent_identity(
            owner="a@a.test", organization_id=None, project_id=None, agent_id=1, invocation_id="inv-a"
        )
        return db_to_rag.tool_load_from_db_and_index("SELECT id FROM orders", "kb-a")

    result = contextvars.copy_context().run(agent_run)

    assert Path(result["csv_path"]).parent.name == "agent1"
    assert result["download_path"].startswith("/api/files/agent1/")
