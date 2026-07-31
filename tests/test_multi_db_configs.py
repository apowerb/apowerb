"""Tests for multi-DB-config support in ``rebind_database``.

Previously an agent could only attach a single ``database`` tool_config —
``rebind_database`` looped through ``tools_ids`` and broke at the first one
with ``DB_NAME``. The SCEI agent needs **two** DBs (PMI for SELECTs on
``ECOMFOU/LCOMFOU``, SuiviAR for INSERT/UPDATE on the diagnostic table),
so the binder now emits one pair of tools per DB with a slugified suffix
when N>=2 configs are present.

Single-config agents still get the canonical ``tool_run_sql`` /
``tool_db`` names so existing prompts keep working.
"""
from __future__ import annotations

import pytest

from apowerb.tools_store.portfolio.database import (
    _slugify_db_name,
    make_database_tools,
)


# --------------------------------------------------------------------------- #
# _slugify_db_name
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("PMI", "pmi"),
        ("SuiviAR", "suiviar"),
        ("PMI DB", "pmi_db"),
        ("PMI-DB", "pmi_db"),
        ("PMI/DB", "pmidb"),
        ("  PMI   ", "pmi"),
        ("", "db"),
        ("---", "db"),
        ("ÉâPMI", "pmi"),  # non-ascii stripped
        ("a" * 50, "a" * 32),  # max 32 chars
    ],
)
def test_slugify(raw, expected):
    assert _slugify_db_name(raw) == expected


# --------------------------------------------------------------------------- #
# make_database_tools — naming
# --------------------------------------------------------------------------- #


_DB_PMI = {
    "DB_TYPE": "mssql", "DB_HOST": "host1", "DB_PORT": "1433",
    "DB_NAME": "PMI", "DB_USER": "u", "DB_PASSWORD": "p",
}
_DB_SUIVIAR = {
    "DB_TYPE": "mssql", "DB_HOST": "host2", "DB_PORT": "1433",
    "DB_NAME": "SuiviAR", "DB_USER": "u", "DB_PASSWORD": "p",
}


def test_canonical_names_when_no_suffix():
    tools = make_database_tools("agent1", db_params=_DB_PMI)
    names = [t.__name__ for t in tools]
    assert names == ["tool_run_sql", "tool_db"]


def test_suffixed_names_when_suffix_provided():
    tools = make_database_tools("agent1", db_params=_DB_PMI, name_suffix="pmi")
    names = [t.__name__ for t in tools]
    assert names == ["tool_run_sql_pmi", "tool_db_pmi"]


def test_docstring_mentions_db_name_for_suffixed():
    tools = make_database_tools("agent1", db_params=_DB_PMI, name_suffix="pmi")
    assert "PMI" in tools[0].__doc__
    assert "PMI" in tools[1].__doc__


def test_no_suffix_keeps_default_docstring():
    tools = make_database_tools("agent1", db_params=_DB_PMI)
    # Original docstring talks about generic "database", not the specific name
    assert "PMI" not in (tools[0].__doc__ or "").split("\n")[0]


# --------------------------------------------------------------------------- #
# rebind_database — multi-DB end-to-end
# --------------------------------------------------------------------------- #


def _placeholder_run_sql():
    """Empty placeholder named ``tool_run_sql`` — what tool_configs initially load."""
    pass


_placeholder_run_sql.__name__ = "tool_run_sql"


def _placeholder_tool_db():
    pass


_placeholder_tool_db.__name__ = "tool_db"


def test_rebind_with_two_dbs_emits_suffixed_tools(monkeypatch):
    from apowerb.core.agent_helpers import tools_binder
    from apowerb.tools_store import tools_helpers

    def fake_load(tid, owner_id):
        if tid == "tool_config_pmi":
            return ("database.tool_run_sql", _DB_PMI)
        if tid == "tool_config_suiviar":
            return ("database.tool_run_sql", _DB_SUIVIAR)
        return ("?", {})

    monkeypatch.setattr(tools_helpers, "load_tool_config_params", fake_load)

    tools_funcs = [_placeholder_run_sql, _placeholder_tool_db]
    out = tools_binder.rebind_database(
        agent_name="agentX",
        tools_ids=["tool_config_pmi", "tool_config_suiviar"],
        tools_funcs=tools_funcs,
        owner_id="user@x",
    )
    names = sorted(t.__name__ for t in out)
    assert names == [
        "tool_db_pmi",
        "tool_db_suiviar",
        "tool_run_sql_pmi",
        "tool_run_sql_suiviar",
    ]
    # No duplicate __name__ — the dedup #110 + Vertex AI both pass.
    assert len(set(names)) == len(names)


def test_rebind_with_single_db_keeps_canonical_names(monkeypatch):
    from apowerb.core.agent_helpers import tools_binder
    from apowerb.tools_store import tools_helpers

    def fake_load(tid, owner_id):
        return ("database.tool_run_sql", _DB_PMI)

    monkeypatch.setattr(tools_helpers, "load_tool_config_params", fake_load)

    out = tools_binder.rebind_database(
        agent_name="agentY",
        tools_ids=["tool_config_pmi"],
        tools_funcs=[_placeholder_run_sql, _placeholder_tool_db],
        owner_id="user@y",
    )
    names = sorted(t.__name__ for t in out)
    assert names == ["tool_db", "tool_run_sql"]


def test_rebind_with_no_db_falls_back_to_env(monkeypatch):
    """No DB config attached → tools still bound, fall back to env vars."""
    from apowerb.core.agent_helpers import tools_binder
    from apowerb.tools_store import tools_helpers

    monkeypatch.setattr(
        tools_helpers,
        "load_tool_config_params",
        lambda tid, owner_id: ("?", {}),
    )

    out = tools_binder.rebind_database(
        agent_name="agentZ",
        tools_ids=["tool_config_x"],
        tools_funcs=[_placeholder_run_sql, _placeholder_tool_db],
        owner_id="user@z",
    )
    names = sorted(t.__name__ for t in out)
    assert names == ["tool_db", "tool_run_sql"]


def test_rebind_collision_resolution(monkeypatch):
    """Two DBs with the same DB_NAME (rare but possible) — slugs must stay unique."""
    from apowerb.core.agent_helpers import tools_binder
    from apowerb.tools_store import tools_helpers

    db_a = dict(_DB_PMI)
    db_b = dict(_DB_PMI)  # same DB_NAME='PMI'

    def fake_load(tid, owner_id):
        if tid == "a":
            return ("database.tool_run_sql", db_a)
        return ("database.tool_run_sql", db_b)

    monkeypatch.setattr(tools_helpers, "load_tool_config_params", fake_load)

    out = tools_binder.rebind_database(
        agent_name="agentC",
        tools_ids=["a", "b"],
        tools_funcs=[_placeholder_run_sql, _placeholder_tool_db],
        owner_id="x",
    )
    names = sorted(t.__name__ for t in out)
    assert names == ["tool_db_pmi", "tool_db_pmi_2", "tool_run_sql_pmi", "tool_run_sql_pmi_2"]
    assert len(set(names)) == len(names)


def test_rebind_no_op_when_no_db_placeholder():
    """If the tool list has no tool_run_sql/tool_db, rebind_database is a no-op."""
    from apowerb.core.agent_helpers import tools_binder

    def other():
        pass

    other.__name__ = "tool_other"

    out = tools_binder.rebind_database(
        agent_name="agentN",
        tools_ids=["tool_config_pmi"],
        tools_funcs=[other],
        owner_id="x",
    )
    assert out == [other]
