"""Tests for the data warehouse tools (Snowflake, Databricks, BigQuery,
Redshift) and their shared read-only guard.

Request shapes are pinned against the contracts verified 2026-10-06 (see each
module docstring). httpx / boto3 / google-auth are mocked throughout — no
network, no real warehouse.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from apowerb.tools_store.portfolio import (
    bigquery,
    databricks,
    redshift,
    snowflake,
    warehouse_core,
)


def _resp(payload: dict | None = None, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload or {}
    resp.raise_for_status.return_value = None
    resp.text = ""
    return resp


def _http_error(status_code: int, text: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test")
    response = httpx.Response(status_code, text=text, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    for module in (snowflake, databricks, bigquery, redshift):
        monkeypatch.setattr(module.time, "sleep", lambda _s: None)


# ═══════════════════════════ read-only guard ═══════════════════════════
class TestReadOnlyGuard:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM t",
            "  select a from t;  ",
            "WITH x AS (SELECT 1) SELECT * FROM x",
            "SHOW TABLES",
            "DESCRIBE t",
            "SELECT REPLACE(name, 'a', 'b'), updated_at FROM t",
            "SELECT * FROM t WHERE note = 'please delete; drop it'",
            "SELECT 1 -- delete later",
        ],
    )
    def test_allowed(self, sql):
        assert warehouse_core.validate_read_only_sql(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "",
            "DELETE FROM t",
            "INSERT INTO t VALUES (1)",
            "WITH x AS (SELECT 1) DELETE FROM t",
            "SELECT 1; DROP TABLE t",
            "SELECT * FROM t; SELECT 2",
            "MERGE INTO t USING s ON true WHEN MATCHED THEN DELETE",
            "COPY INTO @stage FROM t",
            "CALL proc()",
            "EXECUTE IMMEDIATE 'DROP TABLE t'",
        ],
    )
    def test_refused(self, sql):
        with pytest.raises(ValueError):
            warehouse_core.validate_read_only_sql(sql)

    def test_rows_result_caps_and_flags(self):
        result = warehouse_core.rows_result(["a"], [[1], [2], [3]], 2, 3)
        assert result["rows"] == [{"a": 1}, {"a": 2}]
        assert result["truncated"] is True
        assert warehouse_core.rows_result(["a"], [[1]], 5, 1)["truncated"] is False


# ═══════════════════════════ Snowflake ═══════════════════════════
class TestSnowflake:
    @pytest.fixture(autouse=True)
    def _cfg(self, monkeypatch):
        monkeypatch.setenv(
            "SNOWFLAKE_ACCOUNT", "https://myorg-acct.snowflakecomputing.com"
        )
        monkeypatch.setenv("SNOWFLAKE_TOKEN", "pat")
        monkeypatch.setenv("SNOWFLAKE_WAREHOUSE", "WH")
        monkeypatch.delenv("SNOWFLAKE_ROLE", raising=False)

    def test_sync_request_shape(self):
        payload = {
            "resultSetMetaData": {
                "numRows": 2,
                "rowType": [{"name": "ID"}, {"name": "N"}],
            },
            "data": [["1", "a"], ["2", None]],
        }
        with patch("httpx.request", return_value=_resp(payload)) as req:
            result = snowflake.tool_snowflake_run_query("SELECT id, n FROM t")
        assert result["status"] == "success"
        assert result["rows"] == [{"ID": "1", "N": "a"}, {"ID": "2", "N": None}]
        assert req.call_args.args == (
            "POST",
            "https://myorg-acct.snowflakecomputing.com/api/v2/statements",
        )
        headers = req.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer pat"
        assert (
            headers["X-Snowflake-Authorization-Token-Type"]
            == "PROGRAMMATIC_ACCESS_TOKEN"
        )
        body = req.call_args.kwargs["json"]
        assert body["statement"] == "SELECT id, n FROM t"
        assert body["warehouse"] == "WH"
        assert "role" not in body

    def test_async_202_is_polled(self):
        pending = _resp({"statementHandle": "h1"}, status_code=202)
        done = _resp(
            {
                "resultSetMetaData": {"numRows": 1, "rowType": [{"name": "X"}]},
                "data": [["1"]],
            }
        )
        with patch("httpx.request", side_effect=[pending, done]) as req:
            result = snowflake.tool_snowflake_run_query("SELECT 1 AS x")
        assert result["rows"] == [{"X": "1"}]
        assert req.call_args_list[1].args == (
            "GET",
            "https://myorg-acct.snowflakecomputing.com/api/v2/statements/h1",
        )

    def test_write_refused_without_request(self):
        with patch("httpx.request") as req:
            result = snowflake.tool_snowflake_run_query("DELETE FROM t")
        assert result["status"] == "error"
        req.assert_not_called()

    def test_account_stays_on_snowflake_domain(self, monkeypatch):
        monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "evil.com/x?")
        payload = {"resultSetMetaData": {"numRows": 0, "rowType": []}, "data": []}
        with patch("httpx.request", return_value=_resp(payload)) as req:
            snowflake.tool_snowflake_run_query("SELECT 1")
        assert req.call_args.args[1].startswith(
            "https://evil.com.snowflakecomputing.com/"
        )
        monkeypatch.setenv("SNOWFLAKE_ACCOUNT", "bad account!")
        with patch("httpx.request") as req:
            result = snowflake.tool_snowflake_run_query("SELECT 1")
        assert result["status"] == "error"
        req.assert_not_called()

    def test_422_mapped(self):
        with patch(
            "httpx.request", side_effect=_http_error(422, "SQL compilation error")
        ):
            result = snowflake.tool_snowflake_run_query("SELECT nope")
        assert result["http_status"] == 422
        assert "SQL compilation error" in result["error_message"]

    def test_list_tables_rejects_injection(self):
        with patch("httpx.request") as req:
            result = snowflake.tool_snowflake_list_tables(schema="x' OR '1'='1")
        assert result["status"] == "error"
        req.assert_not_called()


# ═══════════════════════════ Databricks ═══════════════════════════
class TestDatabricks:
    @pytest.fixture(autouse=True)
    def _cfg(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_HOST", "https://adb-1.azuredatabricks.net/")
        monkeypatch.setenv("DATABRICKS_TOKEN", "dapi")
        monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "wh1")
        monkeypatch.delenv("DATABRICKS_CATALOG", raising=False)
        monkeypatch.delenv("DATABRICKS_SCHEMA", raising=False)

    def _done(self, rows, total=None, truncated=False):
        return {
            "statement_id": "s1",
            "status": {"state": "SUCCEEDED"},
            "manifest": {
                "schema": {"columns": [{"name": "a"}]},
                "total_row_count": len(rows) if total is None else total,
                "truncated": truncated,
            },
            "result": {"data_array": rows},
        }

    def test_request_shape_and_poll(self):
        pending = {"statement_id": "s1", "status": {"state": "PENDING"}}
        with patch(
            "httpx.request", side_effect=[_resp(pending), _resp(self._done([["1"]]))]
        ) as req:
            result = databricks.tool_databricks_run_query(
                "SELECT a FROM t", max_rows=10
            )
        assert result["rows"] == [{"a": "1"}]
        first, second = req.call_args_list
        assert first.args == (
            "POST",
            "https://adb-1.azuredatabricks.net/api/2.0/sql/statements",
        )
        assert first.kwargs["headers"]["Authorization"] == "Bearer dapi"
        body = first.kwargs["json"]
        assert body["warehouse_id"] == "wh1"
        assert body["row_limit"] == 11
        assert body["disposition"] == "INLINE" and body["format"] == "JSON_ARRAY"
        assert second.args == (
            "GET",
            "https://adb-1.azuredatabricks.net/api/2.0/sql/statements/s1",
        )

    def test_extra_row_means_truncated(self):
        data = self._done([["1"], ["2"], ["3"]], truncated=True)
        with patch("httpx.request", return_value=_resp(data)):
            result = databricks.tool_databricks_run_query("SELECT a FROM t", max_rows=2)
        assert result["row_count"] == 2
        assert result["truncated"] is True
        assert result["total_rows"] is None

    def test_failed_state_is_error(self):
        failed = {
            "statement_id": "s1",
            "status": {"state": "FAILED", "error": {"message": "boom"}},
        }
        with patch("httpx.request", return_value=_resp(failed)):
            result = databricks.tool_databricks_run_query("SELECT 1")
        assert result["status"] == "error"
        assert "boom" in result["error_message"]

    def test_host_must_be_a_host(self, monkeypatch):
        monkeypatch.setenv("DATABRICKS_HOST", "https://user@evil.com:8443/x")
        with patch("httpx.request", return_value=_resp(self._done([]))) as req:
            databricks.tool_databricks_run_query("SELECT 1")
        assert req.call_args.args[1].startswith("https://evil.com/api/")
        monkeypatch.setenv("DATABRICKS_HOST", "https://bad_host!/")
        with patch("httpx.request") as req:
            result = databricks.tool_databricks_run_query("SELECT 1")
        assert result["status"] == "error"
        req.assert_not_called()

    def test_list_tables_uses_catalog(self):
        with patch("httpx.request", return_value=_resp(self._done([]))) as req:
            databricks.tool_databricks_list_tables(catalog="main", schema="sales")
        statement = req.call_args.kwargs["json"]["statement"]
        assert "`main`.information_schema.tables" in statement
        assert "table_schema = 'sales'" in statement


# ═══════════════════════════ BigQuery ═══════════════════════════
class TestBigQuery:
    @pytest.fixture(autouse=True)
    def _cfg(self, monkeypatch):
        monkeypatch.setenv("BIGQUERY_PROJECT_ID", "my-proj")
        monkeypatch.setenv(
            "BIGQUERY_SERVICE_ACCOUNT_JSON", '{"type": "service_account"}'
        )
        monkeypatch.setenv("BIGQUERY_MAX_BYTES_BILLED", "1000000")
        monkeypatch.delenv("BIGQUERY_LOCATION", raising=False)
        monkeypatch.setattr(bigquery, "_access_token", lambda: "ya29")

    def test_request_shape_poll_and_cells(self):
        pending = {
            "jobComplete": False,
            "jobReference": {"jobId": "j1", "location": "EU"},
        }
        done = {
            "jobComplete": True,
            "schema": {"fields": [{"name": "n"}, {"name": "tags"}]},
            "rows": [{"f": [{"v": "1"}, {"v": [{"v": "a"}, {"v": "b"}]}]}],
            "totalRows": "1",
        }
        with patch("httpx.request", side_effect=[_resp(pending), _resp(done)]) as req:
            result = bigquery.tool_bigquery_run_query("SELECT n, tags FROM ds.t")
        assert result["rows"] == [{"n": "1", "tags": ["a", "b"]}]
        assert result["total_rows"] == 1
        first, second = req.call_args_list
        assert first.args == (
            "POST",
            "https://bigquery.googleapis.com/bigquery/v2/projects/my-proj/queries",
        )
        assert first.kwargs["headers"]["Authorization"] == "Bearer ya29"
        body = first.kwargs["json"]
        assert body["useLegacySql"] is False
        assert body["maximumBytesBilled"] == "1000000"
        assert second.args == (
            "GET",
            "https://bigquery.googleapis.com/bigquery/v2/projects/my-proj/queries/j1",
        )
        assert second.kwargs["params"]["location"] == "EU"

    def test_total_rows_flags_truncation(self):
        done = {
            "jobComplete": True,
            "schema": {"fields": [{"name": "n"}]},
            "rows": [{"f": [{"v": "1"}]}],
            "totalRows": "50",
        }
        with patch("httpx.request", return_value=_resp(done)):
            result = bigquery.tool_bigquery_run_query("SELECT n FROM ds.t", max_rows=1)
        assert result["truncated"] is True

    def test_script_with_write_refused(self):
        with patch("httpx.request") as req:
            result = bigquery.tool_bigquery_run_query(
                "SELECT 1; DELETE FROM ds.t WHERE true"
            )
        assert result["status"] == "error"
        req.assert_not_called()

    def test_list_tables(self):
        datasets = {"datasets": [{"datasetReference": {"datasetId": "sales"}}]}
        tables = {
            "tables": [{"tableReference": {"tableId": "orders"}, "type": "TABLE"}]
        }
        with patch(
            "httpx.request", side_effect=[_resp(datasets), _resp(tables)]
        ) as req:
            result = bigquery.tool_bigquery_list_tables()
        assert result["rows"] == [
            {"dataset": "sales", "table": "orders", "type": "TABLE"}
        ]
        assert req.call_args.args[1].endswith("/projects/my-proj/datasets/sales/tables")

    def test_missing_key(self, monkeypatch):
        monkeypatch.undo()
        monkeypatch.setenv("BIGQUERY_PROJECT_ID", "my-proj")
        monkeypatch.delenv("BIGQUERY_SERVICE_ACCOUNT_JSON", raising=False)
        with patch("httpx.request") as req:
            result = bigquery.tool_bigquery_run_query("SELECT 1")
        assert "BIGQUERY_SERVICE_ACCOUNT_JSON" in result["error_message"]
        req.assert_not_called()


# ═══════════════════════════ Redshift ═══════════════════════════
class TestRedshift:
    @pytest.fixture(autouse=True)
    def _cfg(self, monkeypatch):
        monkeypatch.setenv("REDSHIFT_REGION", "eu-west-3")
        monkeypatch.setenv("REDSHIFT_DATABASE", "dev")
        monkeypatch.setenv("REDSHIFT_WORKGROUP", "wg")
        for name in (
            "REDSHIFT_CLUSTER_ID",
            "REDSHIFT_SECRET_ARN",
            "REDSHIFT_DB_USER",
            "REDSHIFT_ACCESS_KEY_ID",
        ):
            monkeypatch.delenv(name, raising=False)

    def _client(self, statuses, result=None, has_result=True):
        client = MagicMock()
        client.execute_statement.return_value = {"Id": "q1"}
        client.describe_statement.side_effect = [
            {"Status": s, "HasResultSet": has_result, "Error": "bad sql"}
            for s in statuses
        ]
        client.get_statement_result.return_value = result or {}
        return client

    def test_serverless_poll_and_cells(self):
        result = {
            "ColumnMetadata": [{"name": "id"}, {"name": "name"}, {"name": "x"}],
            "Records": [[{"longValue": 1}, {"stringValue": "a"}, {"isNull": True}]],
            "TotalNumRows": 1,
        }
        client = self._client(["SUBMITTED", "STARTED", "FINISHED"], result)
        with patch.object(redshift, "_client", return_value=client):
            out = redshift.tool_redshift_run_query("SELECT id, name, x FROM t")
        assert out["rows"] == [{"id": 1, "name": "a", "x": None}]
        client.execute_statement.assert_called_once_with(
            Sql="SELECT id, name, x FROM t", Database="dev", WorkgroupName="wg"
        )
        assert client.describe_statement.call_count == 3

    def test_failed_statement(self):
        client = self._client(["FAILED"])
        with patch.object(redshift, "_client", return_value=client):
            out = redshift.tool_redshift_run_query("SELECT nope")
        assert out["status"] == "error"
        assert "bad sql" in out["error_message"]
        client.get_statement_result.assert_not_called()

    def test_cluster_needs_login(self, monkeypatch):
        monkeypatch.delenv("REDSHIFT_WORKGROUP")
        monkeypatch.setenv("REDSHIFT_CLUSTER_ID", "c1")
        client = self._client([])
        with patch.object(redshift, "_client", return_value=client):
            out = redshift.tool_redshift_run_query("SELECT 1")
        assert "REDSHIFT_SECRET_ARN" in out["error_message"]
        client.execute_statement.assert_not_called()

    def test_write_refused(self):
        client = self._client([])
        with patch.object(redshift, "_client", return_value=client):
            out = redshift.tool_redshift_run_query("UPDATE t SET a = 1")
        assert out["status"] == "error"
        client.execute_statement.assert_not_called()


def test_tools_discovered():
    from apowerb.tools_store.tool_manager import ToolsStore

    store = ToolsStore()
    for category in ("snowflake", "databricks", "bigquery", "redshift"):
        tools = store.get_tools_in_category(category)
        assert f"{category}.tool_{category}_run_query" in tools
        assert f"{category}.tool_{category}_list_tables" in tools
    assert store.get_tools_in_category("warehouse_core") == []
    keys = {
        p["key"]
        for p in store.get_tool_expected_params("snowflake.tool_snowflake_run_query")
    }
    assert {"SNOWFLAKE_ACCOUNT", "SNOWFLAKE_TOKEN"} <= keys
