"""POST /bi/upload-csv accepte les mêmes formats tabulaires que la prévision
depuis une pièce jointe (xlsx, ods, json, parquet…), via le chargeur partagé.
Un CSV garde exactement son comportement : octets d'origine, séparateur détecté.
Tout autre format est converti en CSV canonique avant stockage, car
``csv_executor`` ne lit que du CSV."""

from __future__ import annotations

import io
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.bi.data import _bi_storage

USER = "dev@example.com"
ORG = "thaink2.com"
FRAME = pd.DataFrame({
    "date": pd.to_datetime(["2024-01-01", "2024-02-01"]),
    "sales": [90, 95.5],
})


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(_bi_storage, "bi_store_dir", lambda: tmp_path / "bi_store")
    monkeypatch.setattr(_bi_storage, "storage_backend", lambda: "local")

    from apowerb.auth.dependencies import get_current_user
    from apowerb.bi.data.upload_router import router
    from apowerb.helpers.database import get_db

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async def _user():
        u = MagicMock()
        u.email = USER
        return u

    async def _db():
        yield MagicMock()

    app.dependency_overrides[get_current_user] = _user
    app.dependency_overrides[get_db] = _db

    with patch("apowerb.bi.data.upload_router.DatabaseDataStore") as store_cls, \
         patch("apowerb.bi.data.upload_router.mirror_as_input_artifact", new=AsyncMock()) as mirror:
        store_cls.return_value.save = AsyncMock(return_value=None)
        c = TestClient(app)
        c.save = store_cls.return_value.save
        c.mirror = mirror
        yield c


def _post(client, name, body, **form):
    return client.post(
        "/api/v1/bi/upload-csv",
        data={"organization_id": ORG, "project_id": "thaink2", **form},
        files={"file": (name, body, "application/octet-stream")},
    )


def _xlsx() -> bytes:
    buf = io.BytesIO()
    FRAME.to_excel(buf, index=False)
    return buf.getvalue()


def _parquet() -> bytes:
    buf = io.BytesIO()
    FRAME.to_parquet(buf)
    return buf.getvalue()


class TestNonCsvFormats:
    @pytest.mark.parametrize("name,body", [
        ("ventes.xlsx", _xlsx()),
        ("ventes.xlsm", _xlsx()),
        ("ventes.json", json.dumps([{"date": "2024-01-01", "sales": 90}, {"date": "2024-02-01", "sales": 95.5}]).encode()),
        ("ventes.parquet", _parquet()),
        ("ventes.tsv", b"date\tsales\n2024-01-01\t90\n2024-02-01\t95.5\n"),
    ], ids=["xlsx", "xlsm", "json", "parquet", "tsv"])
    def test_imports_and_describes_the_table(self, client, name, body):
        resp = _post(client, name, body)
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload["filename"] == name
        assert payload["columns"] == ["date", "sales"]
        assert payload["row_count"] == 2
        assert payload["sample_rows"][0] == {"date": "2024-01-01", "sales": 90}
        assert payload["separator"] == ","

    def test_stored_file_is_canonical_csv_that_csv_executor_reads(self, client):
        resp = _post(client, "ventes.xlsx", _xlsx())
        stored = _bi_storage.read_file(resp.json()["key"])
        assert resp.json()["key"].endswith(".csv")
        assert stored.decode().splitlines() == ["date,sales", "2024-01-01,90", "2024-02-01,95.5"]

    def test_metadata_describes_a_csv_dataset(self, client):
        _post(client, "ventes.xlsx", _xlsx())
        kwargs = client.save.call_args.kwargs
        assert kwargs["name"] == "ventes.xlsx"
        meta = kwargs["metadata"]
        assert meta["extension"] == ".csv" and meta["content_type"] == "text/csv"
        assert meta["columns"] == ["date", "sales"] and meta["row_count"] == 2
        assert meta["uploaded_by"] == USER

    def test_sheet_form_field_selects_the_sheet(self, client):
        buf = io.BytesIO()
        with pd.ExcelWriter(buf) as writer:
            pd.DataFrame({"a": [1]}).to_excel(writer, sheet_name="A", index=False)
            FRAME.to_excel(writer, sheet_name="Ventes", index=False)
        resp = _post(client, "m.xlsx", buf.getvalue(), sheet="Ventes")
        assert resp.json()["columns"] == ["date", "sales"]


class TestArtifactMirror:
    def test_original_file_is_mirrored_like_a_csv_upload(self, client):
        body = _xlsx()
        _post(client, "ventes.xlsx", body)
        kwargs = client.mirror.call_args.kwargs
        assert kwargs["filename"] == "ventes.xlsx" and kwargs["data"] == body
        assert kwargs["app_name"] == f"bi-{ORG}" and kwargs["source"] == "bi"

    def test_mirror_failure_does_not_fail_the_import(self, client):
        client.mirror.side_effect = RuntimeError("boom")
        assert _post(client, "ventes.xlsx", _xlsx()).status_code == 200


class TestRefusals:
    def test_unsupported_extension_is_400_and_lists_formats(self, client):
        resp = _post(client, "doc.pdf", b"%PDF-1.4")
        assert resp.status_code == 400
        assert ".xlsx" in resp.json()["detail"] and ".csv" in resp.json()["detail"]
        client.save.assert_not_called()

    def test_corrupt_xlsx_is_400_not_500(self, client):
        resp = _post(client, "bad.xlsx", b"not a zip")
        assert resp.status_code == 400
        client.save.assert_not_called()

    def test_too_many_rows_is_400(self, client):
        with patch("apowerb.bi.data.upload_router.MAX_DATA_ROWS", 1):
            resp = _post(client, "ventes.xlsx", _xlsx())
        assert resp.status_code == 400
        assert "lignes" in resp.json()["detail"]


class TestCsvUnchanged:
    def test_csv_keeps_original_bytes_and_detected_separator(self, client):
        raw = "date;sales\n2024-01-01;90\n".encode()
        resp = _post(client, "ventes.csv", raw)
        assert resp.status_code == 200
        assert _bi_storage.read_file(resp.json()["key"]) == raw
        assert resp.json()["separator"] == ";"
