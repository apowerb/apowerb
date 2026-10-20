"""Database tools connect only with the credentials of a tool_config.

The worker's own ``DB_*`` variables describe the platform database. A
database tool reached without a tool_config (bare ``database.tool_run_sql``
ref in a workflow or an agent, SuperAgent template without a saved config)
must report that no database is configured, never connect with them.
"""
from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import psycopg2
import pytest

from apowerb.tools_store import tool_env as tool_env_mod
from apowerb.tools_store.portfolio import database, db_to_rag, text_to_sql

_PROCESS_DB = {
    "DB_HOST": "process-host.invalid",
    "DB_PORT": "6543",
    "DB_NAME": "process_db",
    "DB_USER": "process_user",
    "DB_PASSWORD": "process-password",
}


@pytest.fixture
def connect(monkeypatch):
    for key, value in _PROCESS_DB.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("DB_TYPE", raising=False)
    mock = MagicMock(side_effect=ConnectionError("no database in tests"))
    monkeypatch.setattr(psycopg2, "connect", mock)
    return mock


def _process_values_used(mock: MagicMock) -> list:
    """Calls that carry any of the process DB_* values."""
    sentinels = set(_PROCESS_DB.values())
    return [
        call
        for call in mock.call_args_list
        if sentinels & {str(v) for v in call.kwargs.values()}
        or sentinels & {str(v) for v in call.args}
    ]


def test_run_sql_without_config_does_not_connect(connect):
    result = database.tool_run_sql("SELECT 1")

    assert result["success"] is False
    assert _process_values_used(connect) == []


def test_unbound_database_tools_do_not_connect(connect):
    run_sql = next(
        fn for fn in database.make_database_tools("agent1", db_params=None)
        if fn.__name__ == "tool_run_sql"
    )

    result = run_sql("SELECT 1")

    assert result["success"] is False
    assert _process_values_used(connect) == []


def test_bare_workflow_tool_ref_does_not_connect(connect):
    from apowerb.core import workflow_runtime

    _run_agent, run_tool, _run_rag, _notify = workflow_runtime.bindings_for(
        "owner-a@example.com", None
    )

    result = asyncio.run(run_tool("database.tool_run_sql", {"sql": "SELECT 1"}))

    assert result["success"] is False
    assert _process_values_used(connect) == []


def test_db_to_rag_without_config_does_not_connect(connect):
    result = db_to_rag.load_from_db_and_index(
        sql_query="SELECT 1", knowledge_name="k", folder="default",
    )

    assert result["status"] == "error"
    assert _process_values_used(connect) == []


def test_text_to_sql_env_connection_without_config_refused(connect):
    with pytest.raises(ValueError):
        text_to_sql._get_db_connection()

    assert _process_values_used(connect) == []


def test_tool_config_without_db_keys_does_not_use_process_values(connect):
    # A config of another kind is active: its params say nothing about DB_*.
    with tool_env_mod.tool_env({"SOME_API_KEY": "x"}):
        result = database.tool_run_sql("SELECT 1")

    assert result["success"] is False
    assert _process_values_used(connect) == []


def test_tool_config_credentials_are_used(connect):
    params = {
        "DB_HOST": "config-host",
        "DB_NAME": "config_db",
        "DB_USER": "config_user",
        "DB_PASSWORD": "config-password",
    }
    wrapped = tool_env_mod.with_tool_config(
        database.tool_run_sql, params, "database.tool_run_sql"
    )

    wrapped("SELECT 1")

    connect.assert_called_once()
    kwargs = connect.call_args.kwargs
    assert kwargs["host"] == "config-host"
    assert kwargs["database"] == "config_db"
    assert kwargs["user"] == "config_user"
    assert kwargs["password"] == "config-password"
    assert kwargs["port"] == 5432
    assert _process_values_used(connect) == []


def test_partial_config_does_not_borrow_process_values(connect):
    # The config names a database but no host/port: defaults, not the worker's.
    params = {"DB_NAME": "config_db", "DB_USER": "u", "DB_PASSWORD": "p"}
    with tool_env_mod.tool_env(params):
        database.tool_run_sql("SELECT 1")

    connect.assert_called_once()
    assert connect.call_args.kwargs["host"] == "localhost"
    assert _process_values_used(connect) == []


def test_nested_scope_keeps_outer_config(connect):
    params = {"DB_NAME": "config_db", "DB_USER": "u", "DB_PASSWORD": "p"}
    with tool_env_mod.tool_env(params):
        with tool_env_mod.tool_env({"SOME_API_KEY": "x"}):
            database.tool_run_sql("SELECT 1")
        assert tool_env_mod.config_env("DB_NAME") == "config_db"
    assert tool_env_mod.config_env("DB_NAME") is None

    assert connect.call_args.kwargs["database"] == "config_db"


def test_database_tools_still_report_their_config_params():
    from apowerb.tools_store.tool_manager import get_tools_store

    keys = {
        p["key"]
        for p in get_tools_store().get_tool_expected_params("database.tool_run_sql")
    }

    assert {"DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD"} <= keys
