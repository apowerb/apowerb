"""Import d'un FEC (fichier des écritures comptables) comme jeu de données BI.

Contrat de sortie : 18 colonnes minuscules dans l'ordre légal ; ``debit``,
``credit``, ``montantdevise`` numériques ; dates en dates ; codes en chaînes.
"""

from __future__ import annotations

import csv
import io
from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.bi.data import _bi_storage
from apowerb.bi.data.fec_import import (
    FEC_COLUMNS,
    FecImportError,
    is_fec,
    parse_fec,
)

LEGAL = [
    "JournalCode", "JournalLib", "EcritureNum", "EcritureDate", "CompteNum",
    "CompteLib", "CompAuxNum", "CompAuxLib", "PieceRef", "PieceDate",
    "EcritureLib", "Debit", "Credit", "EcritureLet", "DateLet", "ValidDate",
    "Montantdevise", "Idevise",
]
ROWS = [
    ["VE", "Ventes", "1", "20240105", "411000", "Clients", "C001", "Dupont",
     "F001", "20240105", "Facture Dupont", "1200,50", "0,00", "", "", "20240106", "", ""],
    ["VE", "Ventes", "1", "20240105", "707000", "Ventes", "", "",
     "F001", "20240105", "Facture Dupont", "0,00", "1200,50", "AA", "20240201", "20240106", "1300,00", "USD"],
    ["BQ", "Banque", "2", "20240131", "512000", "Banque", "", "",
     "", "", "Café du coin", "", "89,9", "", "", "", "", ""],
]


def _fec(header=LEGAL, rows=ROWS, sep="\t", nl="\r\n") -> str:
    return nl.join(sep.join(r) for r in [header, *rows]) + nl


def test_columns_contract():
    assert FEC_COLUMNS == [h.lower() for h in LEGAL]


class TestParse:
    def test_tab_separated(self):
        df = parse_fec(_fec().encode("utf-8"))
        assert list(df.columns) == FEC_COLUMNS
        assert len(df) == 3
        assert df["debit"].tolist() == [1200.5, 0.0, 0.0]
        assert df["credit"].tolist() == [0.0, 1200.5, 89.9]
        assert df["comptenum"].tolist() == ["411000", "707000", "512000"]
        assert df["compauxnum"].tolist()[0] == "C001"
        assert pd.api.types.is_datetime64_any_dtype(df["ecrituredate"])
        assert df["ecrituredate"][0] == pd.Timestamp("2024-01-05")
        assert df["datelet"][1] == pd.Timestamp("2024-02-01")
        assert pd.isna(df["datelet"][0]) and pd.isna(df["piecedate"][2])
        assert df["montantdevise"][1] == 1300.0
        assert pd.isna(df["montantdevise"][0])

    def test_pipe_separated(self):
        df = parse_fec(_fec(sep="|", nl="\n").encode("ascii", "replace"))
        assert list(df.columns) == FEC_COLUMNS and len(df) == 3

    def test_iso_8859_15_accents(self):
        df = parse_fec(_fec().encode("iso-8859-15"))
        assert df["ecriturelib"][2] == "Café du coin"

    def test_euro_sign_8859_15(self):
        rows = [r[:] for r in ROWS]
        rows[0][10] = "Remise 5€"
        df = parse_fec(_fec(rows=rows).encode("iso-8859-15"))
        assert df["ecriturelib"][0] == "Remise 5€"

    @pytest.mark.parametrize("bom", [b"", b"\xef\xbb\xbf"])
    def test_utf8_with_and_without_bom(self, bom):
        df = parse_fec(bom + _fec().encode("utf-8"))
        assert df["ecriturelib"][2] == "Café du coin"
        assert list(df.columns) == FEC_COLUMNS

    def test_header_case_and_accents_variants(self):
        header = [h.upper() for h in LEGAL]
        header[0] = "Journalcode"
        header[16] = "MontantDevise"
        header[3] = "Écriturédate"
        df = parse_fec(_fec(header=header).encode("utf-8"))
        assert list(df.columns) == FEC_COLUMNS
        assert df["montantdevise"][1] == 1300.0

    def test_leading_zeros_kept(self):
        rows = [r[:] for r in ROWS]
        rows[0][0] = "0A"
        rows[0][4] = "0411000"
        rows[0][6] = "007"
        df = parse_fec(_fec(rows=rows).encode("utf-8"))
        assert df["journalcode"][0] == "0A"
        assert df["comptenum"][0] == "0411000"
        assert df["compauxnum"][0] == "007"

    @pytest.mark.parametrize("raw,expected", [
        ("20240105", "2024-01-05"), ("2024-01-05", "2024-01-05"), ("05/01/2024", "2024-01-05"),
    ])
    def test_date_formats(self, raw, expected):
        rows = [r[:] for r in ROWS]
        rows[0][3] = raw
        df = parse_fec(_fec(rows=rows).encode("utf-8"))
        assert df["ecrituredate"][0] == pd.Timestamp(expected)

    def test_empty_debit_credit_are_zero(self):
        df = parse_fec(_fec().encode("utf-8"))
        assert df["debit"][2] == 0.0
        assert df["credit"].notna().all() and df["debit"].notna().all()

    def test_montant_sens_variant(self):
        header = [h for h in LEGAL if h not in ("Debit", "Credit")]
        i = header.index("EcritureLib") + 1
        header[i:i] = ["Montant", "Sens"]
        rows = []
        for r, (m, s) in zip(ROWS, [("1200,50", "D"), ("1200,50", "C"), ("89,9", "c")]):
            r = r[:11] + r[13:]
            r[i:i] = [m, s]
            rows.append(r)
        df = parse_fec(_fec(header=header, rows=rows).encode("utf-8"))
        assert list(df.columns) == FEC_COLUMNS
        assert df["debit"].tolist() == [1200.5, 0.0, 0.0]
        assert df["credit"].tolist() == [0.0, 1200.5, 89.9]

    def test_montant_sens_invalid_sens_reports_line(self):
        header = [h for h in LEGAL if h not in ("Debit", "Credit")] + ["Montant", "Sens"]
        rows = [r[:11] + r[13:] + ["10,00", "X"] for r in ROWS]
        with pytest.raises(FecImportError, match=r"(?i)ligne 2"):
            parse_fec(_fec(header=header, rows=rows).encode("utf-8"))

    def test_missing_column_is_an_error(self):
        header = [h for h in LEGAL if h != "CompteNum"]
        rows = [r[:4] + r[5:] for r in ROWS]
        with pytest.raises(FecImportError, match="comptenum"):
            parse_fec(_fec(header=header, rows=rows).encode("utf-8"))

    def test_missing_amounts_is_an_error(self):
        header = [h for h in LEGAL if h not in ("Debit", "Credit")]
        rows = [r[:11] + r[13:] for r in ROWS]
        with pytest.raises(FecImportError, match="(?i)debit"):
            parse_fec(_fec(header=header, rows=rows).encode("utf-8"))

    def test_malformed_row_reports_line_number(self):
        rows = [r[:] for r in ROWS]
        rows[1] = rows[1][:10]
        with pytest.raises(FecImportError, match=r"(?i)ligne 3"):
            parse_fec(_fec(rows=rows).encode("utf-8"))

    def test_bad_amount_reports_line_number(self):
        rows = [r[:] for r in ROWS]
        rows[2][12] = "abc"
        with pytest.raises(FecImportError, match=r"(?i)ligne 4"):
            parse_fec(_fec(rows=rows).encode("utf-8"))

    def test_bad_date_reports_line_number(self):
        rows = [r[:] for r in ROWS]
        rows[0][3] = "31/31/2024"
        with pytest.raises(FecImportError, match=r"(?i)ligne 2"):
            parse_fec(_fec(rows=rows).encode("utf-8"))

    def test_no_data_rows(self):
        with pytest.raises(FecImportError):
            parse_fec(_fec(rows=[]).encode("utf-8"))


class TestDetection:
    def test_fec_header_detected(self):
        assert is_fec(_fec().encode("iso-8859-15"))
        assert is_fec(_fec(sep="|").encode("utf-8"))

    def test_partial_fec_header_still_detected(self):
        header = [h for h in LEGAL if h != "CompteNum"]
        assert is_fec(_fec(header=header, rows=[]).encode("utf-8"))

    @pytest.mark.parametrize("body", [
        b"date,sales\n2024-01-01,90\n",
        b"journal,montant\nA,1\n",
        b"\x00\x01\x02binary",
        b"",
    ])
    def test_non_fec_not_detected(self, body):
        assert not is_fec(body)


# --------------------------------------------------------------- upload


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
        u.email = "dev@example.com"
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
        c.tmp = tmp_path
        yield c


def _post(client, name, body):
    return client.post(
        "/api/v1/bi/upload-csv",
        data={"organization_id": "thaink2.com", "project_id": "thaink2"},
        files={"file": (name, body, "application/octet-stream")},
    )


class TestUpload:
    @pytest.mark.parametrize("name", ["123456789FEC20241231.txt", "fec.csv"])
    def test_upload_returns_the_18_columns(self, client, name):
        resp = _post(client, name, _fec().encode("iso-8859-15"))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["columns"] == FEC_COLUMNS
        assert body["row_count"] == 3
        assert body["content_type"] == "text/csv"
        meta = client.save.await_args.kwargs["metadata"]
        assert meta["columns"] == FEC_COLUMNS
        assert meta["separator"] == ","

    def test_stored_csv_is_normalised(self, client):
        resp = _post(client, "123456789FEC20241231.txt", _fec().encode("iso-8859-15"))
        meta = client.save.await_args.kwargs["metadata"]
        stored = next((client.tmp / "bi_store").rglob(f"{resp.json()['file_id']}.csv"))
        rows = list(csv.DictReader(io.StringIO(stored.read_text("utf-8"))))
        assert list(rows[0].keys()) == FEC_COLUMNS
        assert rows[0]["ecrituredate"] == "2024-01-05"
        assert rows[0]["debit"] == "1200.5"
        assert rows[0]["comptenum"] == "411000"
        assert rows[0]["datelet"] == ""
        assert rows[2]["ecriturelib"] == "Café du coin"
        assert meta["s3_key"]

    def test_malformed_fec_is_a_400_with_line(self, client):
        rows = [r[:] for r in ROWS]
        rows[1] = rows[1][:10]
        resp = _post(client, "fec.txt", _fec(rows=rows).encode("utf-8"))
        assert resp.status_code == 400
        assert "ligne 3" in resp.json()["detail"].lower()
        client.save.assert_not_awaited()

    def test_non_fec_csv_unchanged(self, client):
        raw = b"date,sales\n2024-01-01,90\n2024-02-01,95.5\n"
        resp = _post(client, "ventes.csv", raw)
        assert resp.status_code == 200
        assert resp.json()["columns"] == ["date", "sales"]
        stored = next((client.tmp / "bi_store").rglob(f"{resp.json()['file_id']}*"))
        assert stored.read_bytes() == raw

    def test_non_fec_txt_unchanged(self, client):
        resp = _post(client, "ventes.txt", b"date\tsales\n2024-01-01\t90\n")
        assert resp.status_code == 200
        assert resp.json()["columns"] == ["date", "sales"]
