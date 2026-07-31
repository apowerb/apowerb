"""Unit tests for DatabaseQueryExecutor MSSQL branch.

Covers:
1. _detect_db_type recognises MSSQL from db_type / DB_TYPE / port 1433 / category.
2. _run_mssql builds the correct ODBC connection string from DB_* params.
3. Query wrapping uses SELECT TOP <limit>.
4. Rows are returned as list[dict] mirroring pyodbc cursor.description columns.
5. pyodbc.Error is caught and re-raised as RuntimeError with config id.

pyodbc.connect is mocked end-to-end — no real ODBC driver required.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pyodbc
import pytest

from apowerb.bi.data.db_executor import DatabaseQueryExecutor


@pytest.fixture
def exec_inst():
    return DatabaseQueryExecutor(tool_config_id="15", owner_id="com@scei88.fr")


@pytest.fixture
def suiviar_config():
    return {
        "tool_config_params": {
            "DB_HOST": "192.168.1.205",
            "DB_PORT": "1433",
            "DB_NAME": "SuiviAR",
            "DB_USER": "th2agent",
            "DB_PASSWORD": "super-secret",
            "DB_ODBC_DRIVER": "ODBC Driver 18 for SQL Server",
            "DB_ENCRYPT": "no",
            "DB_TRUST_SERVER_CERTIFICATE": "yes",
        }
    }


# ---------------------------------------------------------------------------
# _detect_db_type
# ---------------------------------------------------------------------------


class TestDetectDbType:
    def test_db_type_mssql_lowercase(self, exec_inst):
        assert exec_inst._detect_db_type({"tool_config_params": {"db_type": "mssql"}}) == "mssql"

    def test_db_type_sqlserver_lowercase(self, exec_inst):
        assert exec_inst._detect_db_type({"tool_config_params": {"db_type": "sqlserver"}}) == "mssql"

    def test_db_type_sql_server_lowercase(self, exec_inst):
        assert exec_inst._detect_db_type({"tool_config_params": {"db_type": "sql_server"}}) == "mssql"

    def test_db_type_uppercase_key(self, exec_inst):
        assert exec_inst._detect_db_type({"tool_config_params": {"DB_TYPE": "mssql"}}) == "mssql"

    def test_port_1433_infers_mssql(self, exec_inst):
        assert exec_inst._detect_db_type({"tool_config_params": {"port": "1433"}}) == "mssql"

    def test_db_port_1433_uppercase_infers_mssql(self, exec_inst):
        assert exec_inst._detect_db_type({"tool_config_params": {"DB_PORT": "1433"}}) == "mssql"

    def test_category_mssql_infers_mssql(self, exec_inst):
        cfg = {"tool_config_params": {}, "tool_category": "mssql_server"}
        assert exec_inst._detect_db_type(cfg) == "mssql"

    def test_category_sqlserver_infers_mssql(self, exec_inst):
        cfg = {"tool_config_params": {}, "tool_category": "sqlserver"}
        assert exec_inst._detect_db_type(cfg) == "mssql"

    def test_tool_name_mssql_infers_mssql(self, exec_inst):
        cfg = {"tool_config_params": {}, "tool_name": "mssql.tool_run_sql"}
        assert exec_inst._detect_db_type(cfg) == "mssql"

    def test_default_postgresql_when_no_hint(self, exec_inst):
        assert exec_inst._detect_db_type({"tool_config_params": {}}) == "postgresql"

    def test_mysql_still_detected(self, exec_inst):
        assert exec_inst._detect_db_type({"tool_config_params": {"db_type": "mysql"}}) == "mysql"

    def test_mysql_port_3306_still_detected(self, exec_inst):
        assert exec_inst._detect_db_type({"tool_config_params": {"port": "3306"}}) == "mysql"


# ---------------------------------------------------------------------------
# _run_mssql happy path
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_pyodbc_connect():
    """Yield a patched pyodbc.connect that returns a mock connection with
    a cursor that reports 2 columns and 1 row."""
    mock_cursor = MagicMock()
    mock_cursor.description = [("jour",), ("nb_ars",)]
    mock_cursor.fetchall.return_value = [("2026-05-12", 22)]
    mock_conn = MagicMock()
    mock_conn.cursor.return_value = mock_cursor
    with patch("pyodbc.connect", return_value=mock_conn) as p:
        yield p, mock_cursor


async def test_run_mssql_builds_conn_str(exec_inst, suiviar_config, mock_pyodbc_connect):
    mock_connect, _ = mock_pyodbc_connect
    await exec_inst._run_mssql("SELECT * FROM v_suiviar_daily", 100, suiviar_config)
    conn_str = mock_connect.call_args[0][0]
    assert "DRIVER={ODBC Driver 18 for SQL Server}" in conn_str
    assert "SERVER=192.168.1.205,1433" in conn_str
    assert "DATABASE=SuiviAR" in conn_str
    assert "UID=th2agent" in conn_str
    assert "PWD=super-secret" in conn_str
    assert "Encrypt=no" in conn_str
    assert "TrustServerCertificate=yes" in conn_str


async def test_run_mssql_injects_top_into_plain_select(exec_inst, suiviar_config, mock_pyodbc_connect):
    _, mock_cursor = mock_pyodbc_connect
    await exec_inst._run_mssql("SELECT * FROM v_suiviar_daily", 250, suiviar_config)
    executed = mock_cursor.execute.call_args[0][0]
    # TOP injected after the first SELECT, query NOT wrapped in a subquery
    assert executed.startswith("SELECT TOP 250 *")
    assert "FROM v_suiviar_daily" in executed
    assert "AS _q" not in executed


async def test_run_mssql_strips_trailing_semicolon(exec_inst, suiviar_config, mock_pyodbc_connect):
    _, mock_cursor = mock_pyodbc_connect
    await exec_inst._run_mssql("SELECT TypeEcart, COUNT(*) FROM dbo.LignesCommande GROUP BY TypeEcart;", 50, suiviar_config)
    executed = mock_cursor.execute.call_args[0][0]
    # The semicolon inside the wrapped subquery must be stripped to keep T-SQL syntax valid.
    assert ";" not in executed.split("AS _q")[0]


async def test_run_mssql_returns_rows_as_dicts(exec_inst, suiviar_config, mock_pyodbc_connect):
    rows = await exec_inst._run_mssql("SELECT * FROM v_suiviar_daily", 100, suiviar_config)
    assert rows == [{"jour": "2026-05-12", "nb_ars": 22}]


async def test_run_mssql_uses_default_driver_when_missing(exec_inst, mock_pyodbc_connect):
    mock_connect, _ = mock_pyodbc_connect
    cfg = {
        "tool_config_params": {
            "DB_HOST": "h", "DB_NAME": "d", "DB_USER": "u", "DB_PASSWORD": "p"
        }
    }
    await exec_inst._run_mssql("SELECT 1", 10, cfg)
    conn_str = mock_connect.call_args[0][0]
    assert "DRIVER={ODBC Driver 18 for SQL Server}" in conn_str
    assert "SERVER=h,1433" in conn_str  # default port


# ---------------------------------------------------------------------------
# _run_mssql error handling
# ---------------------------------------------------------------------------


async def test_run_mssql_pyodbc_error_reraised_as_runtime(exec_inst, suiviar_config):
    with patch("pyodbc.connect", side_effect=pyodbc.Error("connection refused")):
        with pytest.raises(RuntimeError, match=r"MSSQL query error \(config=15\)"):
            await exec_inst._run_mssql("SELECT 1", 10, suiviar_config)


# ---------------------------------------------------------------------------
# Run dispatch routes MSSQL to _run_mssql
# ---------------------------------------------------------------------------


async def test_run_dispatch_routes_mssql(exec_inst, suiviar_config, mock_pyodbc_connect):
    from apowerb.bi.charts.core import DataSource, SourceType
    source = DataSource(kind=SourceType.DATABASE, query="SELECT 1 AS x", limit=5)
    with patch.object(exec_inst, "_get_config", return_value={
        **suiviar_config,
        "tool_category": "database",
        "tool_name": "database.tool_run_sql",
    }):
        rows = await exec_inst.run(source)
    # rows comes from mock fixture
    assert isinstance(rows, list)


# ---------------------------------------------------------------------------
# Query wrapping branches (fix for ORDER BY in subquery error 1033)
# ---------------------------------------------------------------------------


async def test_run_mssql_plain_select_with_order_by_injects_top(exec_inst, suiviar_config, mock_pyodbc_connect):
    """Plain SELECT with ORDER BY: inject TOP after first SELECT, no wrap."""
    _, mock_cursor = mock_pyodbc_connect
    q = "SELECT jour, nb_ars FROM dbo.v_suiviar_daily WHERE jour >= '2026-01-01' ORDER BY jour"
    await exec_inst._run_mssql(q, 100, suiviar_config)
    executed = mock_cursor.execute.call_args[0][0]
    # Must NOT be wrapped (no "SELECT TOP 100 * FROM (")
    assert not executed.startswith("SELECT TOP 100 * FROM ("), f"Should not wrap, got: {executed[:80]}"
    # TOP must be injected after the first SELECT
    assert executed.lower().startswith("select top 100 ")
    # ORDER BY must be preserved at top level
    assert executed.lower().rstrip().endswith("order by jour")


async def test_run_mssql_preserves_existing_top(exec_inst, suiviar_config, mock_pyodbc_connect):
    """Query already starting with SELECT TOP <n>: left untouched (user controls cap)."""
    _, mock_cursor = mock_pyodbc_connect
    q = "SELECT TOP 10 FournisseurNom, nb_anomalies FROM dbo.v_suiviar_top_fournisseurs ORDER BY nb_anomalies DESC"
    await exec_inst._run_mssql(q, 10000, suiviar_config)
    executed = mock_cursor.execute.call_args[0][0]
    # No double TOP
    assert "TOP 10 " in executed and "TOP 10000" not in executed
    assert "ORDER BY nb_anomalies DESC" in executed


async def test_run_mssql_cte_with_order_by_appends_fetch_next(exec_inst, suiviar_config, mock_pyodbc_connect):
    """CTE (WITH ...) ending with ORDER BY: append OFFSET 0 ROWS FETCH NEXT N ROWS ONLY."""
    _, mock_cursor = mock_pyodbc_connect
    q = "WITH x AS (SELECT * FROM dbo.Commandes) SELECT * FROM x ORDER BY DateReceptionAR DESC"
    await exec_inst._run_mssql(q, 50, suiviar_config)
    executed = mock_cursor.execute.call_args[0][0]
    assert "OFFSET 0 ROWS FETCH NEXT 50 ROWS ONLY" in executed
    # Original ORDER BY untouched, not double-wrapped
    assert executed.startswith("WITH x AS")


async def test_run_mssql_cte_no_order_by_uses_outer_wrap(exec_inst, suiviar_config, mock_pyodbc_connect):
    """CTE without ORDER BY: outer wrap is safe."""
    _, mock_cursor = mock_pyodbc_connect
    q = "WITH x AS (SELECT * FROM dbo.Commandes) SELECT count(*) AS n FROM x"
    await exec_inst._run_mssql(q, 50, suiviar_config)
    executed = mock_cursor.execute.call_args[0][0]
    assert executed.startswith("SELECT TOP 50 * FROM (")
    assert "WITH x AS" in executed
