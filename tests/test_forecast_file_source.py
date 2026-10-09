"""Prévision depuis une pièce jointe du chat (``file_id``) : 4e source,
exclusive de sql / rows / dataset_id, sur ``tool_thaink2_forecast`` et
``tool_create_forecast_chart``.

L'identifiant d'une pièce jointe est son nom de fichier tel que l'UI l'écrit
dans ``[Uploaded files: nom.xlsx]`` (``result.filename`` de POST /files/upload),
rangé sous ``uploads/agent{id}/``. L'isolation tient à ce dossier : celui de
l'agent racine de l'invocation, dont le propriétaire doit être le propriétaire
courant.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

import apowerb.bi.db_stores as db_stores
import apowerb.tools_store.portfolio.bi_datasets as bi_datasets
import apowerb.tools_store.portfolio.business_intelligence as bi
from apowerb.bi.charts.core import SourceType
from apowerb.bi.charts.service import InMemoryChartStore
from apowerb.tools_store.portfolio import api_call

OWNER = "me@example.com"

_TH2FORECAST_RESPONSE = {
    "status": "success",
    "frequency": "month",
    "warnings": [],
    "series": [{
        "group": None, "model": "prophet", "reliability": "good",
        "beats_baseline": True, "metrics": {},
        "forecast": [{"date": "2024-04-01", "value": 100.0}],
        "warnings": [],
    }],
}

FRAME = pd.DataFrame({
    "date": pd.to_datetime(["2024-01-01", "2024-02-01", "2024-03-01"]),
    "sales": [90, 95.5, 99],
})


def _xlsx_bytes() -> bytes:
    buf = io.BytesIO()
    FRAME.to_excel(buf, index=False)
    return buf.getvalue()


@pytest.fixture()
def uploads(tmp_path, monkeypatch):
    """agent1 (à moi) et agent2 (à un autre), sur disque ; contexte = agent1."""
    root = tmp_path / "uploads"
    (root / "agent1").mkdir(parents=True)
    (root / "agent2").mkdir(parents=True)
    monkeypatch.setattr(bi_datasets, "agent_upload_dir", lambda i: root / f"agent{i}")
    monkeypatch.setattr(bi_datasets, "_is_s3_storage", lambda: False)
    monkeypatch.setenv("ROOT_AGENT_ID", "1")
    monkeypatch.setenv("AGENT_OWNER", OWNER)
    owners = {"1": OWNER, "2": "other@example.com"}
    monkeypatch.setattr(bi_datasets, "_agent_record_owner", lambda agent_id: owners.get(str(agent_id)))
    return root


def _forecast_kwargs(**overrides):
    kwargs = dict(date_var="date", target_var="sales", horizon=3)
    kwargs.update(overrides)
    return kwargs


class TestLoadUploadedFileRows:
    def test_reads_xlsx_attachment_of_the_running_agent(self, uploads):
        (uploads / "agent1" / "ventes.xlsx").write_bytes(_xlsx_bytes())
        loaded = bi_datasets._load_uploaded_file_rows("ventes.xlsx", OWNER, 100_000)
        assert loaded["success"] is True
        assert loaded["columns"] == ["date", "sales"]
        assert loaded["rows"][0] == {"date": "2024-01-01", "sales": 90}
        assert loaded["truncated"] is False
        assert loaded["name"] == "ventes.xlsx"

    def test_reads_csv_and_json_attachments(self, uploads):
        (uploads / "agent1" / "v.csv").write_bytes(b"date;sales\n2024-01-01;1,5\n")
        (uploads / "agent1" / "v.json").write_bytes(json.dumps([{"date": "2024-01-01", "sales": 2}]).encode())
        csv_loaded = bi_datasets._load_uploaded_file_rows("v.csv", OWNER, 100_000)
        json_loaded = bi_datasets._load_uploaded_file_rows("v.json", OWNER, 100_000)
        assert csv_loaded["rows"] == [{"date": "2024-01-01", "sales": 1.5}]
        assert json_loaded["rows"] == [{"date": "2024-01-01", "sales": 2}]

    def test_sheet_is_selectable(self, uploads):
        buf = io.BytesIO()
        with pd.ExcelWriter(buf) as writer:
            pd.DataFrame({"a": [1]}).to_excel(writer, sheet_name="A", index=False)
            FRAME.to_excel(writer, sheet_name="Ventes", index=False)
        (uploads / "agent1" / "m.xlsx").write_bytes(buf.getvalue())
        loaded = bi_datasets._load_uploaded_file_rows("m.xlsx", OWNER, 100_000, sheet="Ventes")
        assert loaded["columns"] == ["date", "sales"]

    def test_other_agents_file_is_not_found_with_the_same_message_as_a_missing_one(self, uploads):
        (uploads / "agent2" / "secret.csv").write_bytes(b"date,sales\n2024-01-01,1\n")
        theirs = bi_datasets._load_uploaded_file_rows("secret.csv", OWNER, 100_000)
        missing = bi_datasets._load_uploaded_file_rows("nope.csv", OWNER, 100_000)
        assert theirs["success"] is False
        assert "introuvable" in theirs["error"].lower()
        assert theirs["error"].replace("secret.csv", "X") == missing["error"].replace("nope.csv", "X")

    @pytest.mark.parametrize("name", ["../agent2/secret.csv", "sub/secret.csv", "/etc/passwd", "..", ""])
    def test_path_like_identifiers_are_refused(self, uploads, name):
        (uploads / "agent2" / "secret.csv").write_bytes(b"date,sales\n2024-01-01,1\n")
        loaded = bi_datasets._load_uploaded_file_rows(name, OWNER, 100_000)
        assert loaded["success"] is False
        assert "rows" not in loaded

    def test_agent_folder_owned_by_someone_else_is_refused(self, uploads, monkeypatch):
        """Sous-agent d'un autre propriétaire dans l'invocation d'un agent racine."""
        (uploads / "agent1" / "v.csv").write_bytes(b"date,sales\n2024-01-01,1\n")
        loaded = bi_datasets._load_uploaded_file_rows("v.csv", "other@example.com", 100_000)
        assert loaded["success"] is False
        assert "introuvable" in loaded["error"].lower()

    def test_no_owner_or_no_root_agent_reads_nothing(self, uploads, monkeypatch):
        (uploads / "agent1" / "v.csv").write_bytes(b"date,sales\n2024-01-01,1\n")
        assert bi_datasets._load_uploaded_file_rows("v.csv", "", 100_000)["success"] is False
        monkeypatch.setenv("ROOT_AGENT_ID", "")
        assert bi_datasets._load_uploaded_file_rows("v.csv", OWNER, 100_000)["success"] is False

    def test_unsupported_type_is_a_french_error(self, uploads):
        (uploads / "agent1" / "doc.pdf").write_bytes(b"%PDF-1.4")
        loaded = bi_datasets._load_uploaded_file_rows("doc.pdf", OWNER, 100_000)
        assert loaded["success"] is False
        assert "non pris en charge" in loaded["error"]

    def test_over_the_row_cap_is_refused_not_truncated(self, uploads):
        body = "date,sales\n" + "\n".join(f"2024-01-01,{i}" for i in range(11))
        (uploads / "agent1" / "big.csv").write_bytes(body.encode())
        loaded = bi_datasets._load_uploaded_file_rows("big.csv", OWNER, 10)
        assert loaded["success"] is False
        assert "10" in loaded["error"]


class TestForecastToolFileSource:
    def test_file_id_with_another_source_is_refused(self):
        base = _forecast_kwargs(file_id="v.xlsx")
        for other in (dict(sql="select 1"), dict(rows=[{"date": "2024-01-01", "sales": 1}]), dict(dataset_id="ds")):
            result = api_call.tool_thaink2_forecast(**base, **other)
            assert result["status"] == "error"
            assert "exactement une source" in result["message"]
            assert "file_id" in result["message"]

    def test_forecasts_the_attachment_end_to_end(self, uploads):
        (uploads / "agent1" / "ventes.xlsx").write_bytes(_xlsx_bytes())
        with patch.object(api_call, "Th2forecastClient") as mock_ctor:
            mock_ctor.return_value.forecast.return_value = _TH2FORECAST_RESPONSE
            result = api_call.tool_thaink2_forecast(**_forecast_kwargs(file_id="ventes.xlsx"))
        assert result["status"] == "success"
        sent = mock_ctor.return_value.forecast.call_args.args[0]
        assert [r["sales"] for r in sent["data"]] == [90, 95.5, 99]
        assert sent["data"][0]["date"] == "2024-01-01"

    def test_unknown_column_lists_the_file_columns(self, uploads):
        (uploads / "agent1" / "ventes.xlsx").write_bytes(_xlsx_bytes())
        result = api_call.tool_thaink2_forecast(**_forecast_kwargs(file_id="ventes.xlsx", target_var="ca"))
        assert result["status"] == "error"
        assert "ca" in result["message"] and "date" in result["message"] and "sales" in result["message"]

    def test_other_agents_file_is_refused(self, uploads):
        (uploads / "agent2" / "secret.xlsx").write_bytes(_xlsx_bytes())
        with patch.object(api_call, "Th2forecastClient") as mock_ctor:
            result = api_call.tool_thaink2_forecast(**_forecast_kwargs(file_id="secret.xlsx"))
        assert result["status"] == "error"
        mock_ctor.assert_not_called()

    def test_no_owner_context_is_an_error(self, uploads, monkeypatch):
        monkeypatch.setenv("AGENT_OWNER", "")
        result = api_call.tool_thaink2_forecast(**_forecast_kwargs(file_id="ventes.xlsx"))
        assert result["status"] == "error"


@pytest.fixture()
def chart_store(monkeypatch):
    store = InMemoryChartStore()
    monkeypatch.setattr(db_stores, "DatabaseChartStore", lambda db, owner=None: store)

    @contextlib.asynccontextmanager
    async def no_db():
        yield None

    monkeypatch.setattr(bi, "_get_session", no_db)
    return store


class TestForecastChartFileSource:
    def test_file_id_with_dataset_id_or_sql_is_refused(self, chart_store, uploads):
        for other in (dict(dataset_id="ds"), dict(sql="select 1")):
            result = bi.tool_create_forecast_chart(
                **_forecast_kwargs(title="t", file_id="ventes.xlsx"), **other,
            )
            assert result["success"] is False
            assert "exactement une source" in result["error"]
        assert asyncio.run(chart_store.list_all()) == []

    def test_materializes_the_file_as_a_dataset_then_charts_it(self, chart_store, uploads):
        (uploads / "agent1" / "ventes.xlsx").write_bytes(_xlsx_bytes())
        calls = []

        async def fake_materialize(file_id, owner, organization_id, project_id, sheet=None):
            calls.append((file_id, owner, sheet))
            return {"success": True, "dataset_id": "ds-new"}

        with patch.object(bi, "_materialize_uploaded_file_dataset", side_effect=fake_materialize), \
             patch.object(api_call, "Th2forecastClient") as mock_ctor:
            mock_ctor.return_value.forecast.return_value = _TH2FORECAST_RESPONSE
            result = bi.tool_create_forecast_chart(
                **_forecast_kwargs(title="Ventes prévues", file_id="ventes.xlsx"),
            )

        assert result["success"] is True, result
        assert calls == [("ventes.xlsx", OWNER, None)]
        chart = asyncio.run(chart_store.get(result["chart_id"]))
        assert chart.source.source_type is SourceType.CSV
        assert chart.source.query == "csv://ds-new"
        assert chart.source.limit == 100_000
        sent = mock_ctor.return_value.forecast.call_args.args[0]
        assert [r["sales"] for r in sent["data"]] == [90, 95.5, 99]

    def test_forecast_failure_creates_neither_chart_nor_dataset(self, chart_store, uploads):
        (uploads / "agent1" / "ventes.xlsx").write_bytes(_xlsx_bytes())
        with patch.object(bi, "_materialize_uploaded_file_dataset") as mock_mat, \
             patch.object(api_call, "Th2forecastClient") as mock_ctor:
            mock_ctor.side_effect = Exception("service indisponible")
            result = bi.tool_create_forecast_chart(
                **_forecast_kwargs(title="t", file_id="ventes.xlsx"),
            )
        assert result["success"] is False
        mock_mat.assert_not_called()
        assert asyncio.run(chart_store.list_all()) == []

    def test_other_agents_file_is_refused(self, chart_store, uploads):
        (uploads / "agent2" / "secret.xlsx").write_bytes(_xlsx_bytes())
        with patch.object(bi, "_materialize_uploaded_file_dataset") as mock_mat:
            result = bi.tool_create_forecast_chart(
                **_forecast_kwargs(title="t", file_id="secret.xlsx"),
            )
        assert result["success"] is False
        mock_mat.assert_not_called()


class _FakeDataStore:
    rows: dict[str, dict] = {}

    def __init__(self, db, owner=None):
        self.owner = owner

    async def get(self, file_id):
        row = self.rows.get(file_id)
        return MagicMock(id=file_id, config=row["metadata"], name=row["name"]) if row else None

    async def save(self, *, file_id, name, organization_id, project_id, permissions=None, metadata=None):
        self.rows[file_id] = {"name": name, "metadata": metadata, "owner": self.owner}


class TestMaterializeUploadedFileDataset:
    @pytest.fixture()
    def env(self, uploads, tmp_path, monkeypatch):
        from apowerb.bi.data import _bi_storage

        _FakeDataStore.rows = {}
        monkeypatch.setattr(_bi_storage, "bi_store_dir", lambda: tmp_path / "bi_store")
        monkeypatch.setattr(_bi_storage, "storage_backend", lambda: "local")
        monkeypatch.setattr(db_stores, "DatabaseDataStore", _FakeDataStore)

        @contextlib.asynccontextmanager
        async def no_db():
            yield None

        monkeypatch.setattr(bi_datasets, "_get_session", no_db)
        (uploads / "agent1" / "ventes.xlsx").write_bytes(_xlsx_bytes())
        return _bi_storage

    def test_stores_a_csv_dataset_owned_by_the_agent_owner(self, env):
        result = asyncio.run(bi_datasets._materialize_uploaded_file_dataset("ventes.xlsx", OWNER, "org", "proj"))
        assert result["success"] is True
        row = _FakeDataStore.rows[result["dataset_id"]]
        assert row["owner"] == OWNER
        assert row["metadata"]["extension"] == ".csv"
        assert row["metadata"]["columns"] == ["date", "sales"]
        assert row["metadata"]["row_count"] == 3
        stored = env.read_file(row["metadata"]["s3_key"])
        assert stored.decode().splitlines()[0] == "date,sales"

    def test_second_run_for_the_same_file_reuses_the_dataset(self, env):
        first = asyncio.run(bi_datasets._materialize_uploaded_file_dataset("ventes.xlsx", OWNER, "org", "proj"))
        second = asyncio.run(bi_datasets._materialize_uploaded_file_dataset("ventes.xlsx", OWNER, "org", "proj"))
        assert first["dataset_id"] == second["dataset_id"]
        assert len(_FakeDataStore.rows) == 1

    def test_changed_content_makes_a_new_dataset(self, env, uploads):
        first = asyncio.run(bi_datasets._materialize_uploaded_file_dataset("ventes.xlsx", OWNER, "org", "proj"))
        buf = io.BytesIO()
        FRAME.head(2).to_excel(buf, index=False)
        (uploads / "agent1" / "ventes.xlsx").write_bytes(buf.getvalue())
        second = asyncio.run(bi_datasets._materialize_uploaded_file_dataset("ventes.xlsx", OWNER, "org", "proj"))
        assert first["dataset_id"] != second["dataset_id"]
        assert len(_FakeDataStore.rows) == 2

    def test_missing_stored_file_is_rewritten_on_reuse(self, env):
        first = asyncio.run(bi_datasets._materialize_uploaded_file_dataset("ventes.xlsx", OWNER, "org", "proj"))
        key = _FakeDataStore.rows[first["dataset_id"]]["metadata"]["s3_key"]
        env.delete_file(key)
        asyncio.run(bi_datasets._materialize_uploaded_file_dataset("ventes.xlsx", OWNER, "org", "proj"))
        assert env.read_file(key) is not None
