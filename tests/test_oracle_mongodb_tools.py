"""Tests for the Oracle and MongoDB tools.

The drivers' connection entry points are mocked (no server); BSON conversion
runs on real bson types.
"""

from __future__ import annotations

import datetime
import decimal
from unittest.mock import MagicMock, patch

import pytest
from bson import Decimal128, ObjectId

from apowerb.tools_store.portfolio import mongodb, oracle


# ═══════════════════════════ Oracle ═══════════════════════════
class TestOracle:
    @pytest.fixture(autouse=True)
    def _cfg(self, monkeypatch):
        monkeypatch.setenv("ORACLE_DSN", "db.example.com:1521/ORCLPDB1")
        monkeypatch.setenv("ORACLE_USER", "reader")
        monkeypatch.setenv("ORACLE_PASSWORD", "pw")

    def _conn(self, description, rows):
        conn = MagicMock()
        cursor = conn.cursor.return_value
        cursor.description = description
        cursor.fetchmany.return_value = rows
        return conn, cursor

    def test_read_only_transaction_and_values(self):
        lob = MagicMock()
        lob.read.return_value = "long text"
        conn, cursor = self._conn(
            [("ID",), ("CREATED",), ("AMOUNT",), ("NOTES",)],
            [(1, datetime.datetime(2026, 1, 2, 3, 4), decimal.Decimal("12.5"), lob)],
        )
        with patch("oracledb.connect", return_value=conn) as connect:
            result = oracle.tool_oracle_run_query(
                "SELECT id, created, amount, notes FROM orders;", max_rows=10
            )
        assert result["rows"] == [
            {
                "ID": 1,
                "CREATED": "2026-01-02T03:04:00",
                "AMOUNT": 12.5,
                "NOTES": "long text",
            }
        ]
        assert connect.call_args.kwargs == {
            "user": "reader",
            "password": "pw",
            "dsn": "db.example.com:1521/ORCLPDB1",
        }
        executed = [c.args[0] for c in cursor.execute.call_args_list]
        assert executed == [
            "SET TRANSACTION READ ONLY",
            "SELECT id, created, amount, notes FROM orders",
        ]
        cursor.fetchmany.assert_called_once_with(11)
        conn.rollback.assert_called_once()
        conn.close.assert_called_once()

    def test_extra_row_means_truncated(self):
        conn, _ = self._conn([("N",)], [(1,), (2,), (3,)])
        with patch("oracledb.connect", return_value=conn):
            result = oracle.tool_oracle_run_query("SELECT n FROM t", max_rows=2)
        assert result["row_count"] == 2
        assert result["truncated"] is True

    def test_write_refused_without_connecting(self):
        with patch("oracledb.connect") as connect:
            result = oracle.tool_oracle_run_query("DELETE FROM t")
        assert result["status"] == "error"
        connect.assert_not_called()

    def test_driver_error_mapped_and_connection_closed(self):
        conn, cursor = self._conn([], [])
        cursor.execute.side_effect = [
            None,
            RuntimeError("ORA-00942: table or view does not exist"),
        ]
        with patch("oracledb.connect", return_value=conn):
            result = oracle.tool_oracle_run_query("SELECT * FROM nope")
        assert "ORA-00942" in result["error_message"]
        conn.close.assert_called_once()

    def test_list_tables_binds_owner(self):
        conn, cursor = self._conn(
            [("OWNER",), ("NAME",), ("TYPE",)], [("SALES", "ORDERS", "TABLE")]
        )
        with patch("oracledb.connect", return_value=conn):
            result = oracle.tool_oracle_list_tables(owner="sales")
        assert result["rows"][0]["NAME"] == "ORDERS"
        assert cursor.execute.call_args.args[1] == {"owner": "SALES"}
        with patch("oracledb.connect") as connect:
            bad = oracle.tool_oracle_list_tables(owner="x' OR 1=1 --")
        assert bad["status"] == "error"
        connect.assert_not_called()

    def test_missing_config(self, monkeypatch):
        monkeypatch.delenv("ORACLE_DSN")
        result = oracle.tool_oracle_run_query("SELECT 1 FROM dual")
        assert "ORACLE_DSN" in result["error_message"]


# ═══════════════════════════ MongoDB ═══════════════════════════
class TestMongoDB:
    @pytest.fixture(autouse=True)
    def _cfg(self, monkeypatch):
        monkeypatch.setenv("MONGODB_URI", "mongodb://reader:pw@db.example.com/")
        monkeypatch.setenv("MONGODB_DATABASE", "shop")
        mongodb._clients.clear()

    @pytest.fixture
    def coll(self):
        client = MagicMock()
        collection = client.__getitem__.return_value.__getitem__.return_value
        with patch("pymongo.MongoClient", return_value=client) as ctor:
            yield collection, client, ctor

    def test_find_converts_bson_and_flags_truncation(self, coll):
        collection, client, ctor = coll
        oid = ObjectId("65f1a2b3c4d5e6f708091a2b")
        docs = [
            {
                "_id": oid,
                "amount": Decimal128("12.50"),
                "at": datetime.datetime(2026, 5, 1),
            },
            {"_id": ObjectId(), "amount": Decimal128("1")},
        ]
        cursor = collection.find.return_value
        cursor.max_time_ms.return_value = cursor
        cursor.sort.return_value = cursor
        cursor.limit.return_value = iter(docs)
        result = mongodb.tool_mongodb_find(
            "orders", filter={"status": "paid"}, sort={"at": -1}, limit=1
        )
        assert result["documents"] == [
            {"_id": str(oid), "amount": "12.50", "at": "2026-05-01T00:00:00"}
        ]
        assert result["truncated"] is True
        collection.find.assert_called_once_with({"status": "paid"}, None)
        cursor.sort.assert_called_once_with([("at", -1)])
        cursor.limit.assert_called_once_with(2)
        client.__getitem__.assert_called_with("shop")
        assert ctor.call_args.args[0] == "mongodb://reader:pw@db.example.com/"

    def test_client_is_reused(self, coll):
        _, client, ctor = coll
        client.__getitem__.return_value.list_collection_names.return_value = ["b", "a"]
        first = mongodb.tool_mongodb_list_collections()
        mongodb.tool_mongodb_list_collections()
        assert first["collections"] == ["a", "b"]
        assert ctor.call_count == 1

    def test_aggregate_appends_limit(self, coll):
        collection, _, _ = coll
        collection.aggregate.return_value = iter([{"_id": "FR", "total": 3}])
        pipeline = [{"$group": {"_id": "$country", "total": {"$sum": 1}}}]
        result = mongodb.tool_mongodb_aggregate("orders", pipeline, limit=5)
        assert result["documents"] == [{"_id": "FR", "total": 3}]
        sent = collection.aggregate.call_args.args[0]
        assert sent == [*pipeline, {"$limit": 6}]

    @pytest.mark.parametrize(
        "pipeline",
        [
            [{"$match": {}}, {"$out": "copy"}],
            [{"$merge": {"into": "x"}}],
            [{"$facet": {"a": [{"$out": "copy"}]}}],
            "not a list",
        ],
    )
    def test_write_or_malformed_pipeline_refused(self, coll, pipeline):
        collection, _, _ = coll
        result = mongodb.tool_mongodb_aggregate("orders", pipeline)
        assert result["status"] == "error"
        collection.aggregate.assert_not_called()

    def test_count(self, coll):
        collection, _, _ = coll
        collection.count_documents.return_value = 42
        assert mongodb.tool_mongodb_count("orders", {"x": 1}) == {
            "status": "success",
            "count": 42,
        }

    def test_missing_uri(self, monkeypatch):
        monkeypatch.delenv("MONGODB_URI")
        result = mongodb.tool_mongodb_list_collections()
        assert "MONGODB_URI" in result["error_message"]

    def test_invalid_collection_name(self, coll):
        collection, _, _ = coll
        result = mongodb.tool_mongodb_find("$cmd")
        assert result["status"] == "error"
        collection.find.assert_not_called()


def test_tools_discovered():
    from apowerb.tools_store.tool_manager import ToolsStore

    store = ToolsStore()
    assert "oracle.tool_oracle_run_query" in store.get_tools_in_category("oracle")
    assert "mongodb.tool_mongodb_aggregate" in store.get_tools_in_category("mongodb")
    keys = {
        p["key"] for p in store.get_tool_expected_params("mongodb.tool_mongodb_find")
    }
    assert {"MONGODB_URI", "MONGODB_DATABASE"} <= keys
