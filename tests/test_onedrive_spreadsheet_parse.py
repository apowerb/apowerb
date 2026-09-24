"""Unit tests for ``download_and_parse_spreadsheet``.

This helper extends ``shared_download_and_parse`` to support every common
tabular file format we want the BI pipeline to ingest:

- ``.xlsx`` / ``.xls`` / ``.xlsm`` → pandas ``read_excel`` with openpyxl/xlrd
- ``.ods``                        → pandas ``read_excel`` with the odf engine
- ``.csv``                        → pandas ``read_csv`` with auto-delimiter
- ``.tsv``                        → pandas ``read_csv`` with ``\\t``
- Any other extension             → ``(None, error_msg)`` with an explicit 415-like message

Every Graph HTTP call is monkeypatched; no network traffic.
"""

from __future__ import annotations

import io
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest

from apowerb.tools_store.portfolio import onedrive_core


# ---------------------------------------------------------------------------
# Helpers — build a fake httpx.get that returns bytes we control
# ---------------------------------------------------------------------------


def _fake_response(content: bytes, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = content
    resp.text = content.decode("utf-8", errors="replace") if status_code != 200 else ""
    return resp


def _patch_download(monkeypatch: pytest.MonkeyPatch, content: bytes, status_code: int = 200) -> None:
    """Make every httpx.get invoked inside onedrive_core return our bytes."""
    def fake_get(url: str, headers: dict[str, Any], timeout: int = 60, **kw: Any) -> MagicMock:
        return _fake_response(content, status_code=status_code)

    monkeypatch.setattr(onedrive_core.httpx, "get", fake_get, raising=True)


# ---------------------------------------------------------------------------
# Tests — XLSX branch
# ---------------------------------------------------------------------------


class TestDownloadAndParseSpreadsheetXlsx:
    def test_parses_xlsx_first_sheet_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        df_in = pd.DataFrame([{"A": 1, "B": "x"}, {"A": 2, "B": "y"}])
        buf = io.BytesIO()
        df_in.to_excel(buf, index=False, engine="openpyxl")
        _patch_download(monkeypatch, buf.getvalue())

        df, err = onedrive_core.download_and_parse_spreadsheet(
            "Reports/data.xlsx",
            {"Authorization": "Bearer fake"},
        )

        assert err is None
        assert df is not None
        assert list(df.columns) == ["A", "B"]
        assert df.iloc[0]["A"] == 1
        assert df.iloc[1]["B"] == "y"

    def test_xlsm_routed_through_excel_branch(self, monkeypatch: pytest.MonkeyPatch) -> None:
        df_in = pd.DataFrame([{"col": 42}])
        buf = io.BytesIO()
        df_in.to_excel(buf, index=False, engine="openpyxl")
        _patch_download(monkeypatch, buf.getvalue())

        df, err = onedrive_core.download_and_parse_spreadsheet(
            "Reports/macro.xlsm",
            {"Authorization": "Bearer fake"},
        )
        assert err is None
        assert df is not None
        assert df.iloc[0]["col"] == 42


# ---------------------------------------------------------------------------
# Tests — CSV / TSV branches
# ---------------------------------------------------------------------------


class TestDownloadAndParseSpreadsheetCsv:
    def test_parses_csv_with_comma_delimiter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        csv_bytes = b"name,age\nAlice,30\nBob,25\n"
        _patch_download(monkeypatch, csv_bytes)

        df, err = onedrive_core.download_and_parse_spreadsheet(
            "Reports/people.csv",
            {"Authorization": "Bearer fake"},
        )
        assert err is None
        assert df is not None
        assert list(df.columns) == ["name", "age"]
        assert df.iloc[0]["name"] == "Alice"
        assert int(df.iloc[1]["age"]) == 25

    def test_parses_csv_with_semicolon_autodetected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        csv_bytes = b"name;age\nAlice;30\nBob;25\n"
        _patch_download(monkeypatch, csv_bytes)

        df, err = onedrive_core.download_and_parse_spreadsheet(
            "Reports/people.csv",
            {"Authorization": "Bearer fake"},
        )
        assert err is None
        assert df is not None
        # sep=None auto-detects the semicolon.
        assert list(df.columns) == ["name", "age"]

    def test_parses_tsv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tsv_bytes = b"name\tage\nAlice\t30\nBob\t25\n"
        _patch_download(monkeypatch, tsv_bytes)

        df, err = onedrive_core.download_and_parse_spreadsheet(
            "Reports/people.tsv",
            {"Authorization": "Bearer fake"},
        )
        assert err is None
        assert df is not None
        assert list(df.columns) == ["name", "age"]


# ---------------------------------------------------------------------------
# Tests — unsupported extension & download errors
# ---------------------------------------------------------------------------


class TestDownloadAndParseSpreadsheetErrors:
    def test_unsupported_extension_returns_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_download(monkeypatch, b"not a spreadsheet")

        df, err = onedrive_core.download_and_parse_spreadsheet(
            "Reports/weird.parquet",
            {"Authorization": "Bearer fake"},
        )
        assert df is None
        assert err is not None
        # Message must mention the offending extension so the frontend can act.
        assert "parquet" in err.lower() or "unsupported" in err.lower()

    def test_no_extension_returns_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_download(monkeypatch, b"random bytes")

        df, err = onedrive_core.download_and_parse_spreadsheet(
            "Reports/noextension",
            {"Authorization": "Bearer fake"},
        )
        assert df is None
        assert err is not None

    def test_download_http_error_is_reported(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_download(monkeypatch, b"not found", status_code=404)

        df, err = onedrive_core.download_and_parse_spreadsheet(
            "Reports/missing.csv",
            {"Authorization": "Bearer fake"},
        )
        assert df is None
        assert err is not None
        assert "404" in err


# ---------------------------------------------------------------------------
# Tests — sheet_name forwarding
# ---------------------------------------------------------------------------


class TestDownloadAndParseSpreadsheetSheetName:
    def test_sheet_name_is_forwarded_to_excel_engine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Build a workbook with two sheets; ask for the second one by name.
        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine="openpyxl") as writer:
            pd.DataFrame([{"col": 1}]).to_excel(writer, index=False, sheet_name="First")
            pd.DataFrame([{"col": 2}]).to_excel(writer, index=False, sheet_name="Second")
        _patch_download(monkeypatch, buf.getvalue())

        df, err = onedrive_core.download_and_parse_spreadsheet(
            "Reports/multi.xlsx",
            {"Authorization": "Bearer fake"},
            sheet_name="Second",
        )
        assert err is None
        assert df is not None
        assert df.iloc[0]["col"] == 2

    def test_sheet_name_ignored_for_csv(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Passing sheet_name for a CSV must not crash — it is silently ignored."""
        _patch_download(monkeypatch, b"a,b\n1,2\n")

        df, err = onedrive_core.download_and_parse_spreadsheet(
            "Reports/data.csv",
            {"Authorization": "Bearer fake"},
            sheet_name="Something",
        )
        assert err is None
        assert df is not None
        assert list(df.columns) == ["a", "b"]


# ---------------------------------------------------------------------------
# Tests — xlsx/xlsm engine (calamine)
# ---------------------------------------------------------------------------


def _mixed_workbook() -> bytes:
    """A sheet mixing the cell types a business workbook carries, plus a
    second sheet, written by openpyxl like the files users upload."""
    from datetime import date, datetime

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame(
            {
                "date": [date(2025, 1, 31), date(2025, 2, 28), None],
                "horodatage": [datetime(2025, 3, 1, 14, 30), None, datetime(2025, 3, 2, 8, 0)],
                "client": ["Société Générale", "", "Łódź & Co"],
                "montant": [1234.56, -0.01, None],
                "quantite": [3, 0, 12],
                "actif": [True, False, True],
            }
        ).to_excel(writer, index=False, sheet_name="Ventes")
        pd.DataFrame([{"k": "v"}]).to_excel(writer, index=False, sheet_name="Notes")
    return buf.getvalue()


class TestXlsxEngine:
    """xlsx/xlsm are read with calamine (Rust): ~4x faster than openpyxl on
    20k-100k row files, same DataFrame. These tests pin the engine and the
    parity so a revert or an engine swap is caught."""

    @pytest.mark.parametrize("ext", [".xlsx", ".xlsm"])
    @pytest.mark.parametrize(
        "parse", ["download_and_parse_spreadsheet", "shared_download_and_parse"]
    )
    def test_excel_branch_reads_with_calamine(
        self, monkeypatch: pytest.MonkeyPatch, parse: str, ext: str
    ) -> None:
        _patch_download(monkeypatch, _mixed_workbook())
        seen: dict[str, Any] = {}
        real_read_excel = pd.read_excel

        def spy(*args: Any, **kwargs: Any) -> pd.DataFrame:
            seen.update(kwargs)
            return real_read_excel(*args, **kwargs)

        monkeypatch.setattr(pd, "read_excel", spy)

        df, err = getattr(onedrive_core, parse)(
            f"Reports/ventes{ext}", {"Authorization": "Bearer fake"}
        )

        assert err is None
        assert df is not None
        assert seen.get("engine") == "calamine"

    @pytest.mark.parametrize("sheet_name", [None, "Ventes", "Notes", 1])
    def test_same_dataframe_as_openpyxl(
        self, monkeypatch: pytest.MonkeyPatch, sheet_name: str | int | None
    ) -> None:
        content = _mixed_workbook()
        _patch_download(monkeypatch, content)
        kwargs = {} if sheet_name is None else {"sheet_name": sheet_name}

        df, err = onedrive_core.download_and_parse_spreadsheet(
            "Reports/ventes.xlsx", {"Authorization": "Bearer fake"}, **kwargs
        )

        assert err is None
        expected = pd.read_excel(io.BytesIO(content), engine="openpyxl", **kwargs)
        pd.testing.assert_frame_equal(df, expected)
