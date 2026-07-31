"""Unit tests for select_write_config — deps injected, no DB, no real imports."""

from apowerb.core.db.write_config import select_write_config

_PARSE = lambda v: set((v or "").split(","))


def _cfg(tcid, db_type="mssql", ops="SELECT,INSERT", owner="u", cat="database"):
    return {
        "tool_config_id": tcid,
        "tool_category": cat,
        "owner_id": owner,
        "tool_config_params": {
            "DB_TYPE": db_type,
            "DB_ALLOWED_OPS": ops,
            "DB_NAME": f"db{tcid}",
        },
    }


def _select(configs):
    return select_write_config("u", _list_configs=lambda o: configs, _parse_ops=_PARSE)


def test_selects_lowest_numeric_id_mssql_insert():
    assert _select([_cfg("tool_config15"), _cfg("tool_config9"), _cfg("tool_config20")])[
        "DB_NAME"
    ] == "dbtool_config9"


def test_skips_non_mssql_and_non_insert():
    assert _select([
        _cfg("tool_config1", db_type="postgres"),
        _cfg("tool_config2", ops="SELECT"),
    ]) is None


def test_skips_system_owner():
    assert _select([_cfg("tool_config1", owner="system")]) is None


def test_sqlserver_alias_accepted():
    assert _select([_cfg("tool_config3", db_type="sqlserver")])["DB_NAME"] == "dbtool_config3"


def test_empty_returns_none():
    assert _select([]) is None
