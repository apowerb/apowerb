"""Tests pour tools_store.portfolio.bi_datasets — accès aux jeux de données
importés (BI), limité au propriétaire de l'agent.
"""
import contextlib
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import apowerb.bi.db_stores as db_stores
import apowerb.tools_store.portfolio.bi_datasets as bi_datasets

OWNER = "owner@example.com"
OTHER = "other@example.com"

_CSV_MONTHLY = (
    "date,region,revenue\n"
    "2025-01-01,north,100\n"
    "2025-02-01,north,110\n"
    "2025-03-01,south,90\n"
    "2025-04-01,south,95\n"
)


class FakeRow(SimpleNamespace):
    pass


class FakeDataStore:
    """Double minimal de DatabaseDataStore : owner-scope + type=data."""

    def __init__(self, db, owner=None):
        self._owner = owner

    async def get(self, file_id):
        row = _ROWS.get(file_id)
        if row is None:
            return None
        if self._owner and row.owner.lower() != self._owner.lower():
            return None
        return row

    async def list(self, page=1, page_size=20, organization_id=None, project_id=None):
        rows = [r for r in _ROWS.values() if (not self._owner) or r.owner.lower() == self._owner.lower()]
        rows.sort(key=lambda r: r.created_at, reverse=True)
        return rows[:page_size], len(rows)


_ROWS: dict[str, FakeRow] = {}


def _register_dataset(file_id, owner, name, columns, row_count, s3_key, created_at=None):
    _ROWS[file_id] = FakeRow(
        id=file_id,
        owner=owner,
        name=name,
        type="data",
        created_at=created_at or datetime.now(timezone.utc),
        config={
            "s3_key": s3_key,
            "columns": columns,
            "row_count": row_count,
        },
    )


@pytest.fixture(autouse=True)
def _patch_session(monkeypatch):
    _ROWS.clear()

    @contextlib.asynccontextmanager
    async def no_db():
        yield None

    monkeypatch.setattr(bi_datasets, "_get_session", no_db)
    monkeypatch.setattr(db_stores, "DatabaseDataStore", FakeDataStore)
    yield


def _patch_read_file(monkeypatch, content: bytes | None):
    monkeypatch.setattr(
        "apowerb.bi.data._bi_storage.read_file", lambda key: content
    )


class TestToolListDatasets:
    def test_lists_only_owner_datasets_most_recent_first(self, monkeypatch):
        _register_dataset(
            "ds-old", OWNER, "old.csv", ["a"], 10, "bi/data/x/y/data/ds-old.csv",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        _register_dataset(
            "ds-new", OWNER, "new.csv", ["a"], 20, "bi/data/x/y/data/ds-new.csv",
            created_at=datetime(2026, 2, 1, tzinfo=timezone.utc),
        )
        _register_dataset(
            "ds-other", OTHER, "other.csv", ["a"], 5, "bi/data/x/y/data/ds-other.csv",
        )
        monkeypatch.setenv("AGENT_OWNER", OWNER)

        result = bi_datasets.tool_list_datasets()

        assert result["success"] is True
        ids = [d["dataset_id"] for d in result["datasets"]]
        assert ids == ["ds-new", "ds-old"]

    def test_no_owner_context_is_an_error(self, monkeypatch):
        monkeypatch.delenv("AGENT_OWNER", raising=False)
        result = bi_datasets.tool_list_datasets()
        assert result["success"] is False


class TestToolDescribeDataset:
    def test_owner_mismatch_is_not_found(self, monkeypatch):
        _register_dataset("ds1", OTHER, "f.csv", ["a"], 4, "bi/data/x/y/data/ds1.csv")
        monkeypatch.setenv("AGENT_OWNER", OWNER)

        result = bi_datasets.tool_describe_dataset(dataset_id="ds1")

        assert result["success"] is False
        assert "introuvable" in result["error"].lower()

    def test_missing_dataset_gives_the_same_message_as_foreign_owner(self, monkeypatch):
        monkeypatch.setenv("AGENT_OWNER", OWNER)

        missing = bi_datasets.tool_describe_dataset(dataset_id="does-not-exist")

        _register_dataset("ds1", OTHER, "f.csv", ["a"], 4, "bi/data/x/y/data/ds1.csv")
        foreign = bi_datasets.tool_describe_dataset(dataset_id="ds1")

        assert missing["error"] == foreign["error"]

    def test_valid_dataset_infers_types_and_monthly_frequency(self, monkeypatch):
        _register_dataset(
            "ds1", OWNER, "sales.csv", ["date", "region", "revenue"], 4,
            "bi/data/x/y/data/ds1.csv",
        )
        monkeypatch.setenv("AGENT_OWNER", OWNER)
        _patch_read_file(monkeypatch, _CSV_MONTHLY.encode("utf-8"))

        result = bi_datasets.tool_describe_dataset(dataset_id="ds1")

        assert result["success"] is True
        cols = {c["name"]: c for c in result["columns"]}
        assert cols["date"]["type"] == "date"
        assert cols["date"]["suggested_frequency"] == "month"
        assert cols["revenue"]["type"] == "number"
        assert cols["revenue"]["min"] == 90
        assert cols["revenue"]["max"] == 110
        assert cols["region"]["type"] == "text"
        assert cols["region"]["distinct_count"] == 2
        assert len(result["sample_rows"]) == 4

    def test_reads_by_verified_s3_key_not_raw_id(self, monkeypatch):
        """La lecture se fait par la clé S3 de la ligne vérifiée, jamais par
        l'identifiant brut fourni par l'appelant."""
        _register_dataset(
            "ds1", OWNER, "sales.csv", ["date", "region", "revenue"], 4,
            "bi/data/real/verified/key.csv",
        )
        monkeypatch.setenv("AGENT_OWNER", OWNER)

        captured_keys = []

        def fake_read_file(key):
            captured_keys.append(key)
            return _CSV_MONTHLY.encode("utf-8")

        monkeypatch.setattr("apowerb.bi.data._bi_storage.read_file", fake_read_file)

        bi_datasets.tool_describe_dataset(dataset_id="ds1")

        assert captured_keys == ["bi/data/real/verified/key.csv"]


class TestLoadOwnedDatasetRows:
    @pytest.mark.asyncio
    async def test_owner_mismatch_is_not_found(self, monkeypatch):
        _register_dataset("ds1", OTHER, "f.csv", ["a"], 4, "bi/data/x/y/data/ds1.csv")

        result = await bi_datasets._load_owned_dataset_rows("ds1", OWNER, 100000)

        assert result["success"] is False

    @pytest.mark.asyncio
    async def test_beyond_cap_is_reported_truncated_never_silent(self, monkeypatch):
        _register_dataset("ds1", OWNER, "f.csv", ["date", "region", "revenue"], 4, "bi/data/x/y/data/ds1.csv")
        _patch_read_file(monkeypatch, _CSV_MONTHLY.encode("utf-8"))

        result = await bi_datasets._load_owned_dataset_rows("ds1", OWNER, 2)

        assert result["success"] is True
        assert result["truncated"] is True
        assert len(result["rows"]) == 2

    @pytest.mark.asyncio
    async def test_under_cap_is_not_truncated(self, monkeypatch):
        _register_dataset("ds1", OWNER, "f.csv", ["date", "region", "revenue"], 4, "bi/data/x/y/data/ds1.csv")
        _patch_read_file(monkeypatch, _CSV_MONTHLY.encode("utf-8"))

        result = await bi_datasets._load_owned_dataset_rows("ds1", OWNER, 100000)

        assert result["success"] is True
        assert result["truncated"] is False
        assert len(result["rows"]) == 4


class TestEmptyOwnerReadsNothing:
    """Un store sans propriétaire ne filtre plus rien : le helper doit
    refuser un propriétaire vide au lieu de lire le jeu de n'importe qui."""

    @pytest.mark.asyncio
    async def test_empty_owner_is_not_found_even_for_an_existing_dataset(self, monkeypatch):
        _register_dataset("ds-a", OWNER, "a.csv", ["date", "revenue"], 4, "bi/data/x/y/data/ds-a.csv")
        _patch_read_file(monkeypatch, _CSV_MONTHLY.encode())
        loaded = await bi_datasets._load_owned_dataset_rows("ds-a", "", 100_000)
        assert loaded == {"success": False, "error": "Jeu de données introuvable."}


class TestDescribeReportsTruncation:
    def test_describe_flags_a_dataset_beyond_the_cap(self, monkeypatch):
        _register_dataset("ds-big", OWNER, "big.csv", ["date", "revenue"], 3, "bi/data/x/y/data/ds-big.csv")
        _patch_read_file(monkeypatch, _CSV_MONTHLY.encode())
        monkeypatch.setenv("AGENT_OWNER", OWNER)
        monkeypatch.setattr(bi_datasets, "_DESCRIBE_ROW_CAP", 2)
        result = bi_datasets.tool_describe_dataset("ds-big")
        assert result["success"] is True
        assert result["truncated"] is True
