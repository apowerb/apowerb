"""Chargeur tabulaire partagé (pièce jointe du chat, import BI) : un octet
flux + un nom de fichier -> colonnes + lignes JSON-sûres, ou une erreur
française explicite."""
from __future__ import annotations

import datetime as dt
import io
import json
import zipfile

import pandas as pd
import pytest

from apowerb.helpers.tabular_loader import (
    TabularLoadError,
    load_tabular,
    to_csv_bytes,
)

ROWS = [
    {"date": "2024-01-01", "sales": 90, "region": "N"},
    {"date": "2024-02-01", "sales": 95.5, "region": "S"},
]


def _xlsx(sheets: dict[str, pd.DataFrame]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=name, index=False)
    return buf.getvalue()


def _ods(rows: list[list[str]]) -> bytes:
    """ODS minimal écrit à la main : odfpy n'est pas une dépendance."""
    body = "".join(
        "<table:table-row>"
        + "".join(
            f'<table:table-cell office:value-type="string"><text:p>{c}</text:p></table:table-cell>'
            if not c.replace(".", "").isdigit()
            else f'<table:table-cell office:value-type="float" office:value="{c}"><text:p>{c}</text:p></table:table-cell>'
            for c in row
        )
        + "</table:table-row>"
        for row in rows
    )
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
        'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0" '
        'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" office:version="1.2">'
        f'<office:body><office:spreadsheet><table:table table:name="Feuille1">{body}'
        "</table:table></office:spreadsheet></office:body></office:document-content>"
    )
    manifest = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2">'
        '<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.spreadsheet"/>'
        '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/>'
        "</manifest:manifest>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet", compress_type=zipfile.ZIP_STORED)
        z.writestr("content.xml", content)
        z.writestr("META-INF/manifest.xml", manifest)
    return buf.getvalue()


class TestDelimited:
    def test_csv_comma(self):
        data = load_tabular(b"date,sales,region\n2024-01-01,90,N\n2024-02-01,95.5,S\n", "a.csv")
        assert data.columns == ["date", "sales", "region"]
        assert data.rows == ROWS

    def test_csv_semicolon_sniffed_with_french_decimals(self):
        data = load_tabular("date;sales\n2024-01-01;90,5\n2024-02-01;95\n".encode(), "a.csv")
        assert data.columns == ["date", "sales"]
        assert data.rows[0]["sales"] == 90.5
        assert data.rows[1]["sales"] == 95

    def test_tsv(self):
        data = load_tabular(b"date\tsales\n2024-01-01\t90\n", "a.tsv")
        assert data.columns == ["date", "sales"]
        assert data.rows == [{"date": "2024-01-01", "sales": 90}]

    def test_txt_delimiter_sniffed(self):
        data = load_tabular(b"date|sales\n2024-01-01|90\n2024-02-01|91\n", "a.txt")
        assert data.columns == ["date", "sales"]
        assert len(data.rows) == 2

    def test_utf8_bom_is_stripped(self):
        data = load_tabular("﻿date,sales\n2024-01-01,1\n".encode("utf-8"), "a.csv")
        assert data.columns == ["date", "sales"]

    def test_latin1_fallback(self):
        data = load_tabular("date,région\n2024-01-01,é\n".encode("latin-1"), "a.csv")
        assert data.columns == ["date", "région"]
        assert data.rows[0]["région"] == "é"

    def test_empty_cells_become_none(self):
        data = load_tabular(b"date,sales\n2024-01-01,\n", "a.csv")
        assert data.rows == [{"date": "2024-01-01", "sales": None}]

    def test_blank_and_duplicate_headers_are_made_unique(self):
        data = load_tabular(b"date,sales,sales,\n2024-01-01,1,2,3\n", "a.csv")
        assert data.columns == ["date", "sales", "sales_2", "colonne_4"]

    def test_numeric_header_row_means_no_header(self):
        data = load_tabular(b"1,2\n3,4\n", "a.csv")
        assert data.columns == ["colonne_1", "colonne_2"]
        assert len(data.rows) == 2


class TestSpreadsheets:
    def test_xlsx_first_sheet_by_default_dates_as_iso(self):
        frame = pd.DataFrame(
            {"date": [dt.datetime(2024, 1, 1), dt.datetime(2024, 2, 1)], "sales": [90, 95.5]}
        )
        other = pd.DataFrame({"x": [1]})
        data = load_tabular(_xlsx({"Ventes": frame, "Autre": other}), "a.xlsx")
        assert data.sheet == "Ventes"
        assert data.columns == ["date", "sales"]
        assert data.rows == [
            {"date": "2024-01-01", "sales": 90},
            {"date": "2024-02-01", "sales": 95.5},
        ]

    def test_xlsx_named_sheet(self):
        data = load_tabular(
            _xlsx({"A": pd.DataFrame({"x": [1]}), "B": pd.DataFrame({"y": [2]})}),
            "a.xlsx",
            sheet="B",
        )
        assert data.sheet == "B"
        assert data.rows == [{"y": 2}]

    def test_unknown_sheet_lists_available_sheets(self):
        with pytest.raises(TabularLoadError) as err:
            load_tabular(_xlsx({"A": pd.DataFrame({"x": [1]})}), "a.xlsx", sheet="Z")
        assert "Z" in str(err.value) and "A" in str(err.value)

    def test_xlsm_extension_is_read_like_xlsx(self):
        data = load_tabular(_xlsx({"A": pd.DataFrame({"x": [1]})}), "macro.xlsm")
        assert data.rows == [{"x": 1}]

    def test_leading_blank_rows_before_header_are_skipped(self):
        frame = pd.DataFrame([[None, None], ["date", "sales"], ["2024-01-01", 5]])
        buf = io.BytesIO()
        frame.to_excel(buf, index=False, header=False)
        data = load_tabular(buf.getvalue(), "a.xlsx")
        assert data.columns == ["date", "sales"]
        assert data.rows == [{"date": "2024-01-01", "sales": 5}]

    def test_ods(self):
        data = load_tabular(_ods([["date", "sales"], ["2024-01-01", "90"]]), "a.ods")
        assert data.columns == ["date", "sales"]
        assert data.rows == [{"date": "2024-01-01", "sales": 90}]

    def test_corrupt_xlsx_is_a_french_error_not_a_traceback(self):
        with pytest.raises(TabularLoadError) as err:
            load_tabular(b"not a zip", "a.xlsx")
        assert "xlsx" in str(err.value).lower()


class TestJsonAndParquet:
    def test_json_list_of_records(self):
        data = load_tabular(json.dumps(ROWS).encode(), "a.json")
        assert data.columns == ["date", "sales", "region"]
        assert data.rows == ROWS

    def test_json_object_wrapping_a_single_list(self):
        data = load_tabular(json.dumps({"data": ROWS}).encode(), "a.json")
        assert data.rows == ROWS

    def test_json_union_of_keys_and_nested_values_stringified(self):
        raw = json.dumps([{"a": 1}, {"a": 2, "b": {"k": 1}}]).encode()
        data = load_tabular(raw, "a.json")
        assert data.columns == ["a", "b"]
        assert data.rows[0]["b"] is None
        assert data.rows[1]["b"] == '{"k": 1}'

    def test_json_scalar_is_refused(self):
        with pytest.raises(TabularLoadError):
            load_tabular(b"42", "a.json")

    def test_parquet(self):
        buf = io.BytesIO()
        pd.DataFrame(
            {"date": [pd.Timestamp("2024-01-01")], "sales": [90]}
        ).to_parquet(buf)
        data = load_tabular(buf.getvalue(), "a.parquet")
        assert data.columns == ["date", "sales"]
        assert data.rows == [{"date": "2024-01-01", "sales": 90}]


class TestRefusals:
    def test_unsupported_type_names_the_supported_ones(self):
        with pytest.raises(TabularLoadError) as err:
            load_tabular(b"%PDF-1.4", "a.pdf")
        message = str(err.value)
        assert ".pdf" in message and ".xlsx" in message and ".csv" in message

    def test_empty_file(self):
        with pytest.raises(TabularLoadError) as err:
            load_tabular(b"", "a.csv")
        assert "vide" in str(err.value).lower()

    def test_header_only_is_empty(self):
        with pytest.raises(TabularLoadError) as err:
            load_tabular(b"date,sales\n", "a.csv")
        assert "aucune ligne" in str(err.value).lower()

    def test_binary_content_in_a_csv_is_refused(self):
        with pytest.raises(TabularLoadError):
            load_tabular(b"\x00\x01\x02\xff\xfe" * 50, "a.csv")

    def test_too_many_rows_is_an_explicit_error_not_a_truncation(self):
        body = "d,v\n" + "\n".join(f"2024-01-01,{i}" for i in range(11))
        with pytest.raises(TabularLoadError) as err:
            load_tabular(body.encode(), "a.csv", max_rows=10)
        assert "10" in str(err.value) and "lignes" in str(err.value)

    def test_default_cap_is_the_forecast_cap(self):
        from apowerb.schema.forecast_schema import MAX_DATA_ROWS
        import inspect

        assert inspect.signature(load_tabular).parameters["max_rows"].default == MAX_DATA_ROWS


class TestToCsv:
    def test_roundtrip_through_csv_keeps_values(self):
        data = load_tabular(json.dumps(ROWS).encode(), "a.json")
        again = load_tabular(to_csv_bytes(data), "a.csv")
        assert again.columns == data.columns
        assert again.rows == data.rows


class TestReviewFindings:
    def test_codes_keep_leading_zeros_and_underscores(self):
        data = load_tabular(b"code,n\n007,1_000\n", "a.csv")
        assert data.rows == [{"code": "007", "n": "1_000"}]

    def test_thousands_comma_untouched_outside_semicolon_files(self):
        data = load_tabular(b"a\tb\n1,234\t5\n", "a.tsv")
        assert data.rows[0]["a"] == "1,234"

    def test_single_record_jsonl(self):
        data = load_tabular(b'{"a": 1, "b": 2}\n', "a.jsonl")
        assert data.rows == [{"a": 1, "b": 2}]

    def test_oversized_input_is_refused_before_parsing(self):
        with pytest.raises(TabularLoadError) as err:
            load_tabular(b"a\n1\n", "a.csv", max_bytes=2)
        assert "Mo" in str(err.value)
