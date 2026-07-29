"""Tests for the ``DB_ALLOWED_OPS`` whitelist on the database tools.

Default behaviour: read-only (SELECT only). That mirrors what every existing
agent expects — single ``DB_ALLOWED_OPS=SELECT`` param means the safety gate
is unchanged for them.

Opt-in: a tool_config can set ``DB_ALLOWED_OPS=SELECT,INSERT,UPDATE`` to
authorise writes. This is the path used by SuiviAR (where the SCEI agent
must persist its diagnostic).

Hard floor: ``DROP / TRUNCATE / ALTER / CREATE / GRANT / REVOKE / EXEC / EXECUTE``
are rejected unconditionally. No config can re-enable them.
"""
from __future__ import annotations

import pytest

from th2agent.tools_store.portfolio.database import (
    _NEVER_ALLOWED_OPS,
    _OPT_IN_OPS,
    _parse_allowed_ops,
    _validate_sql_against_whitelist,
    make_database_tools,
)


# --------------------------------------------------------------------------- #
# _parse_allowed_ops
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, {"SELECT"}),
        ("", {"SELECT"}),
        ("SELECT", {"SELECT"}),
        ("select", {"SELECT"}),
        ("SELECT,INSERT,UPDATE", {"SELECT", "INSERT", "UPDATE"}),
        (" select ,  insert  ", {"SELECT", "INSERT"}),
        # Destructive ops are never opt-in-able — silently dropped.
        ("SELECT,DROP", {"SELECT"}),
        ("DROP,TRUNCATE", {"SELECT"}),  # nothing valid → fall back to SELECT
        # Garbage tokens dropped, valid ones kept.
        ("SELECT,FOOBAR,UPDATE", {"SELECT", "UPDATE"}),
    ],
)
def test_parse_allowed_ops(raw, expected):
    assert _parse_allowed_ops(raw) == expected


def test_opt_in_ops_does_not_include_destructive():
    """Sanity: the opt-in set never includes any never-allowed op."""
    assert not (_OPT_IN_OPS & set(_NEVER_ALLOWED_OPS))


# --------------------------------------------------------------------------- #
# _validate_sql_against_whitelist
# --------------------------------------------------------------------------- #


def test_select_passes_default_whitelist():
    assert (
        _validate_sql_against_whitelist(
            "SELECT * FROM ECOMFOU WHERE ECKTNUMERO='098983'",
            frozenset({"SELECT"}),
        )
        is None
    )


def test_insert_blocked_when_only_select_allowed():
    err = _validate_sql_against_whitelist(
        "INSERT INTO SuiviAR (id) VALUES (1)",
        frozenset({"SELECT"}),
    )
    assert err is not None
    assert "INSERT" in err["error"]
    assert "Allowed: SELECT" in err["error"]


def test_insert_passes_when_authorised():
    err = _validate_sql_against_whitelist(
        "INSERT INTO SuiviAR (id, status) VALUES (1, 'OK')",
        frozenset({"SELECT", "INSERT", "UPDATE"}),
    )
    assert err is None


def test_update_passes_when_authorised():
    err = _validate_sql_against_whitelist(
        "UPDATE SuiviAR SET status='OK' WHERE id=1",
        frozenset({"SELECT", "INSERT", "UPDATE"}),
    )
    assert err is None


@pytest.mark.parametrize(
    "destructive_sql,first_token",
    [
        ("DROP TABLE SuiviAR", "DROP"),
        ("TRUNCATE TABLE SuiviAR", "TRUNCATE"),
        ("ALTER TABLE SuiviAR ADD COLUMN x INT", "ALTER"),
        ("GRANT SELECT ON SuiviAR TO public", "GRANT"),
        ("EXEC sp_drop_users", "EXEC"),
        ("EXECUTE foo", "EXECUTE"),
    ],
)
def test_destructive_first_token_blocked_by_whitelist(destructive_sql, first_token):
    """Defence layer 1: a destructive op as first token is rejected because
    the user can never opt it into the whitelist (parsed out by
    ``_parse_allowed_ops``)."""
    err = _validate_sql_against_whitelist(
        destructive_sql,
        frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    )
    assert err is not None
    assert f"Operation '{first_token}' is not allowed" in err["error"]


@pytest.mark.parametrize(
    "destructive",
    ["DROP", "TRUNCATE", "ALTER", "GRANT", "REVOKE", "EXEC", "EXECUTE"],
)
def test_destructive_smuggled_inside_select_blocked(destructive):
    """Defence layer 2: even if the first token passes (legitimate SELECT),
    a destructive keyword anywhere in the query is rejected by the
    ``_NEVER_ALLOWED_OPS`` regex check."""
    sql = f"SELECT * FROM x WHERE 1=1; {destructive} TABLE PMI"
    err = _validate_sql_against_whitelist(
        sql,
        frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    )
    assert err is not None
    assert "forbidden keyword" in err["error"]
    assert destructive in err["error"]


def test_destructive_in_cte_subquery_blocked():
    """CTE / subquery cannot smuggle in a forbidden keyword."""
    err = _validate_sql_against_whitelist(
        "WITH x AS (SELECT 1) INSERT INTO SuiviAR (id) "
        "SELECT * FROM x; -- DROP TABLE PMI",
        frozenset({"SELECT", "INSERT"}),
    )
    # First-token gate: 'WITH' is not in the whitelist → already blocked.
    assert err is not None


def test_with_first_token_blocked_by_default():
    """``WITH`` (CTE) is not in the default whitelist — caller can opt-in
    by adding it explicitly later if needed; current scope is SELECT only."""
    err = _validate_sql_against_whitelist(
        "WITH cte AS (SELECT 1) SELECT * FROM cte",
        frozenset({"SELECT"}),
    )
    assert err is not None
    assert "WITH" in err["error"]


def test_empty_query_blocked():
    err = _validate_sql_against_whitelist("   ", frozenset({"SELECT"}))
    assert err is not None
    assert "Empty SQL query" in err["error"]


# --------------------------------------------------------------------------- #
# make_database_tools — DB_ALLOWED_OPS plumbing
# --------------------------------------------------------------------------- #


_DB_PMI = {
    "DB_TYPE": "mssql", "DB_HOST": "h", "DB_PORT": "1433",
    "DB_NAME": "PMI", "DB_USER": "u", "DB_PASSWORD": "p",
}
_DB_SUIVIAR_RW = {
    **_DB_PMI,
    "DB_NAME": "SuiviAR",
    "DB_ALLOWED_OPS": "SELECT,INSERT,UPDATE",
}


def test_default_config_is_select_only():
    """A config without DB_ALLOWED_OPS keeps the legacy read-only behaviour."""
    tools = make_database_tools("agent1", db_params=_DB_PMI, name_suffix="pmi")
    run_sql = tools[0]
    res = run_sql("INSERT INTO PMI (id) VALUES (1)")
    assert res["success"] is False
    assert "INSERT" in res["error"]
    assert "Allowed: SELECT" in res["error"]


def test_writable_config_accepts_insert():
    """SuiviAR config opts into INSERT/UPDATE — the gate must let them through.

    We can't actually execute (no DB) — we go far enough to confirm the
    safety gate passes; the conn open will fail later, which is what we
    want to assert on (a different error class, not ``Allowed:``)."""
    tools = make_database_tools(
        "agent1", db_params=_DB_SUIVIAR_RW, name_suffix="suiviar"
    )
    run_sql = tools[0]
    res = run_sql("INSERT INTO SuiviAR (id, status) VALUES (1, 'OK')")
    # Gate passed → the call tried to open a real conn → error mentions
    # the connection failure, NOT the ALLOWED-OPS gate.
    assert res["success"] is False
    assert "Allowed:" not in res.get("error", "")
    assert "Operation 'INSERT'" not in res.get("error", "")


def test_writable_config_still_blocks_destructive():
    """SELECT,INSERT,UPDATE config still cannot DROP/TRUNCATE/etc.

    First-token gate catches the bare ``DROP TABLE`` form because DROP can
    never be in the whitelist. The error message is 'Operation DROP not
    allowed' rather than 'forbidden keyword' — both are blocking, the
    important assertion is that ``success`` is False."""
    tools = make_database_tools(
        "agent1", db_params=_DB_SUIVIAR_RW, name_suffix="suiviar"
    )
    run_sql = tools[0]
    res = run_sql("DROP TABLE SuiviAR")
    assert res["success"] is False
    assert "DROP" in res["error"]


def test_writable_config_blocks_smuggled_destructive():
    """A SELECT-prefixed query that hides a DROP later is still rejected."""
    tools = make_database_tools(
        "agent1", db_params=_DB_SUIVIAR_RW, name_suffix="suiviar"
    )
    run_sql = tools[0]
    res = run_sql("SELECT 1; DROP TABLE PMI")
    assert res["success"] is False
    assert "forbidden keyword" in res["error"]
    assert "DROP" in res["error"]


def test_docstring_for_writable_mentions_ops():
    """The docstring must surface allowed_ops so the LLM picks the right
    syntax when calling the tool. Without that hint the model defaults to
    SELECT and falls back to ``-- noop`` workarounds."""
    tools = make_database_tools(
        "agent1", db_params=_DB_SUIVIAR_RW, name_suffix="suiviar"
    )
    run_sql_doc = tools[0].__doc__ or ""
    assert "INSERT" in run_sql_doc
    assert "UPDATE" in run_sql_doc
    assert "SELECT" in run_sql_doc
