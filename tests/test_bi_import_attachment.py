"""Fichier joint du chat -> jeu de donnees BI -> tableau de bord de pilotage.

Defaut observe en production (13/10/2026) : l'agent lisait le CSV joint puis
appelait tool_bi_create_stock_dashboard sans dataset_id ; aucun outil ne
transformait la piece jointe en jeu de donnees.
"""
import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import apowerb.bi.data._bi_storage as storage
import apowerb.bi.db_stores as db_stores
import apowerb.tools_store.portfolio.bi_dashboard_tools as t
import apowerb.tools_store.portfolio.bi_datasets as bi_datasets
from tests.helpers.bi_spec_data import weekly_sales

OWNER = "me@example.com"


def _stock_csv() -> bytes:
    frame = weekly_sales(weeks=60)
    frame["semaine"] = frame["semaine"].dt.strftime("%Y-%m-%d")
    frame["stock"] = 500.0
    frame["delai"] = 2
    return frame.to_csv(index=False).encode()


class _FakeDataStore:
    rows: dict[str, dict] = {}

    def __init__(self, db, owner=None):
        self.owner = owner

    async def get(self, file_id):
        row = self.rows.get(file_id)
        if not row:
            return None
        return MagicMock(id=file_id, config=row["metadata"], name=row["name"])

    async def save(self, *, file_id, name, organization_id, project_id, permissions=None, metadata=None):
        self.rows[file_id] = {"name": name, "metadata": metadata, "owner": self.owner}


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _DatasetSession:
    """Session du lecteur de jeux de donnees : rend la fiche enregistree."""

    async def execute(self, _stmt):
        return _Rows([SimpleNamespace(config=r["metadata"]) for r in _FakeDataStore.rows.values()])


class _DashStore:
    def __init__(self):
        self.saved = {}

    async def save(self, dashboard):
        self.saved[dashboard.id] = dashboard
        return dashboard

    async def get(self, dashboard_id):
        return self.saved.get(dashboard_id)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "uploads"
    (root / "agent1").mkdir(parents=True)
    (root / "agent2").mkdir(parents=True)
    monkeypatch.setattr(bi_datasets, "agent_upload_dir", lambda i: root / f"agent{i}")
    monkeypatch.setattr(bi_datasets, "_is_s3_storage", lambda: False)
    monkeypatch.setenv("ROOT_AGENT_ID", "1")
    monkeypatch.setenv("AGENT_OWNER", OWNER)
    owners = {"1": OWNER, "2": "other@example.com"}
    monkeypatch.setattr(bi_datasets, "_agent_record_owner", lambda agent_id: owners.get(str(agent_id)))
    monkeypatch.setattr(storage, "bi_store_dir", lambda: tmp_path / "bi_store")
    monkeypatch.setattr(storage, "storage_backend", lambda: "local")
    _FakeDataStore.rows = {}
    monkeypatch.setattr(db_stores, "DatabaseDataStore", _FakeDataStore)

    @contextlib.asynccontextmanager
    async def no_db():
        yield _DatasetSession()

    monkeypatch.setattr(bi_datasets, "_get_session", no_db)
    monkeypatch.setattr(t, "_get_session", no_db)
    monkeypatch.setattr(t, "_agent_owner", lambda: OWNER)
    monkeypatch.setattr(t, "_agent_org", lambda: "org")
    monkeypatch.setattr(t, "_agent_project", lambda: "proj")
    store = _DashStore()

    @contextlib.asynccontextmanager
    async def dashboard_store(owner):
        yield store

    monkeypatch.setattr(t, "_dashboard_store", dashboard_store)
    (root / "agent1" / "stocks.csv").write_bytes(_stock_csv())
    (root / "agent2" / "secret.csv").write_bytes(_stock_csv())
    return root


def test_import_attachment_creates_a_dataset_owned_by_the_agent_owner(env):
    out = t.tool_bi_import_attachment(filename="stocks.csv")

    assert out["success"] is True, out
    assert out["dataset_id"] in _FakeDataStore.rows
    assert _FakeDataStore.rows[out["dataset_id"]]["owner"] == OWNER
    assert out["row_count"] == len(_stock_csv().decode().splitlines()) - 1
    assert {"semaine", "famille", "ventes", "stock", "delai"} <= set(out["columns"])


def test_reimporting_the_same_file_reuses_the_dataset(env):
    first = t.tool_bi_import_attachment(filename="stocks.csv")
    second = t.tool_bi_import_attachment(filename="stocks.csv")

    assert first["dataset_id"] == second["dataset_id"]
    assert second["reused"] is True
    assert len(_FakeDataStore.rows) == 1


def test_missing_file_is_a_clear_error_listing_the_available_files(env):
    out = t.tool_bi_import_attachment(filename="absent.csv")

    assert out["success"] is False
    assert "absent.csv" in out["error"] and "stocks.csv" in out["error"]
    assert _FakeDataStore.rows == {}


def test_other_agents_file_is_refused_and_creates_nothing(env, monkeypatch):
    monkeypatch.setenv("ROOT_AGENT_ID", "2")  # dossier d'un autre proprietaire
    out = t.tool_bi_import_attachment(filename="secret.csv")

    assert out["success"] is False
    assert _FakeDataStore.rows == {}


def test_blank_filename_tells_the_agent_what_to_pass(env):
    out = t.tool_bi_import_attachment(filename="  ")

    assert out["success"] is False
    assert "Uploaded files" in out["error"]


def test_stock_dashboard_without_dataset_explains_how_to_import_the_attachment(env):
    out = t.tool_bi_create_stock_dashboard()

    assert out["success"] is False
    assert "tool_bi_import_attachment" in out["error"]
    assert "tool_list_datasets" in out["error"]


def test_import_then_stock_dashboard_with_the_returned_dataset_id(env):
    imported = t.tool_bi_import_attachment(filename="stocks.csv")

    async def agent_turn():  # ADK appelle les outils synchrones depuis une boucle active
        return t.tool_bi_create_stock_dashboard(
            dataset_id=imported["dataset_id"], stock="stock", lead_time_column="delai", horizon=4,
        )

    out = asyncio.run(agent_turn())

    assert out["success"] is True, out
    assert out["errors"] == {}, out["errors"]
    assert len([c for c in out["components"] if c["type"] == "kpi"]) == 4


@pytest.mark.parametrize("template_id", ["forecasting_agent"])
def test_tool_declared_in_agent_templates(template_id):
    from apowerb.core.superagents.templates import SUPERAGENT_TEMPLATES

    tpl = next(t for t in SUPERAGENT_TEMPLATES if t["template_id"] == template_id)
    assert "bi_dashboard_tools.tool_bi_import_attachment" in tpl["recommended_tools"]
