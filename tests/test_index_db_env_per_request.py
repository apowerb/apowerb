"""DB indexing applies the request's connection only while it runs.

``/rag/index-db`` and ``/rag/index-db-nl`` hand the connection to the export
tool through ``DB_*`` env vars. They must be the request's own while the
export runs, and be gone afterwards: the process env is shared by every agent
tool later called in the worker.
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from apowerb.routers.rag import index_db

_KEYS = ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_SCHEMA")


@pytest.fixture
def probe(monkeypatch):
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    seen = {}

    def export(**kwargs):
        seen.update({k: os.environ.get(k) for k in _KEYS})
        return {"status": "ok", "knowledge_id": "k1"}

    monkeypatch.setattr(index_db, "load_from_db_and_index", export)
    monkeypatch.setattr("apowerb.tools_store.tools_helpers.load_tool_config_params",
                        lambda tid, owner_id: ("text_to_sql", {"DB_HOST": "host-a", "DB_USER": "user-a"}))
    monkeypatch.setattr(index_db, "load_tool_config_params",
                        lambda tid, owner_id: ("text_to_sql", {"DB_HOST": "host-a", "DB_USER": "user-a"}),
                        raising=False)
    monkeypatch.setattr("apowerb.core.agent_helpers.set_model_params_as_envvar",
                        lambda *a, **k: None, raising=False)
    return seen


def _env():
    return {k: os.environ.get(k) for k in _KEYS}


def test_index_db_uses_the_config_then_restores_the_env(probe):
    data = SimpleNamespace(tool_config_id="tool_config1", session_id=None, agent_id="agent1",
                           sql_query="SELECT 1", name="n")

    index_db._sync_index_db(data, "owner-a@example.com")

    assert probe["DB_HOST"] == "host-a"
    assert _env() == dict.fromkeys(_KEYS)


def test_index_db_nl_uses_the_request_credentials_then_restores_the_env(probe, monkeypatch):
    monkeypatch.setattr(index_db, "_get_database_schema", lambda: {"tables": ["t"]})
    monkeypatch.setattr(index_db, "_generate_sql_with_llm", lambda q, s: "SELECT 1")
    monkeypatch.setattr(index_db, "_validate_sql_safety", lambda sql: (True, ""))
    creds = SimpleNamespace(host="host-b", port=5432, database="db-b", user="user-b",
                            password="pw-b", schema_name="public")
    data = SimpleNamespace(credentials=creds, tool_config_id=None, session_id=None,
                           agent_id="agent2", nl_description="q", name="n")

    result = index_db._sync_index_db_nl(data, "owner-b@example.com")

    assert result["status"] == "ok"
    assert probe["DB_HOST"] == "host-b"
    assert probe["DB_USER"] == "user-b"
    assert _env() == dict.fromkeys(_KEYS)


def test_index_db_nl_from_a_tool_config_leaves_unset_keys_unset(probe, monkeypatch):
    monkeypatch.setattr(index_db, "_get_database_schema", lambda: {"tables": ["t"]})
    monkeypatch.setattr(index_db, "_generate_sql_with_llm", lambda q, s: "SELECT 1")
    monkeypatch.setattr(index_db, "_validate_sql_safety", lambda sql: (True, ""))
    monkeypatch.setenv("DB_PASSWORD", "left-behind")
    data = SimpleNamespace(credentials=None, tool_config_id="tool_config1", session_id=None,
                           agent_id="agent2", nl_description="q", name="n")

    index_db._sync_index_db_nl(data, "owner-a@example.com")

    assert probe["DB_HOST"] == "host-a"
    assert probe["DB_PASSWORD"] is None
    assert os.environ["DB_PASSWORD"] == "left-behind"
