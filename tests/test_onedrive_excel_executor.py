"""Unit tests for OnedriveExcelQueryExecutor.

These tests cover the BI data source `ONEDRIVE_EXCEL`. They verify:

1. Missing source_options.item_path returns an error dict.
2. Missing owner_id returns an error (tenant isolation).
3. Missing Integration row (no OneDrive connected) returns a 401-like error.
4. Happy path returns list[dict] from the parsed DataFrame.
5. `limit` from DataSource is respected.
6. `sheet_name` option is forwarded to the Excel parser when provided.

All Graph API calls are mocked — no network traffic is performed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pandas as pd
import pytest

from apowerb.bi.charts.core import DataSource, SourceType
from apowerb.bi.data.onedrive_excel_executor import OnedriveExcelQueryExecutor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _db_with_integration(refresh_token: str | None) -> AsyncMock:
    """Build an AsyncSession mock whose execute() returns a result whose
    scalar_one_or_none() yields an Integration-like object (or None)."""
    db = AsyncMock()
    result = MagicMock()
    if refresh_token is None:
        result.scalar_one_or_none.return_value = None
    else:
        integration = MagicMock()
        integration.refresh_token = refresh_token
        result.scalar_one_or_none.return_value = integration
    db.execute = AsyncMock(return_value=result)
    return db


def _make_source(
    *,
    item_path: str | None = "reports/data.xlsx",
    sheet_name: str | None = None,
    item_id: str | None = None,
    limit: int | None = 1000,
) -> DataSource:
    opts: dict = {}
    if item_path is not None:
        opts["item_path"] = item_path
    if sheet_name is not None:
        opts["sheet_name"] = sheet_name
    if item_id is not None:
        opts["item_id"] = item_id
    return DataSource(
        source_type=SourceType.ONEDRIVE_EXCEL,
        query="onedrive_excel",
        source_options=opts,
        limit=limit,
    )


# ---------------------------------------------------------------------------
# Tests — guard clauses
# ---------------------------------------------------------------------------


class TestOnedriveExcelExecutorGuards:
    @pytest.mark.asyncio
    async def test_missing_item_path_returns_error(self):
        db = _db_with_integration("tok")
        executor = OnedriveExcelQueryExecutor(owner_id="1", db_session=db)

        source = DataSource(
            source_type=SourceType.ONEDRIVE_EXCEL,
            query="onedrive_excel",
            source_options={},  # no item_path, no item_id
            limit=100,
        )

        rows = await executor.run(source)

        assert isinstance(rows, list)
        assert len(rows) == 1
        assert "error" in rows[0]
        assert "item_path" in rows[0]["error"]

    @pytest.mark.asyncio
    async def test_missing_user_id_returns_error(self):
        db = _db_with_integration("tok")
        executor = OnedriveExcelQueryExecutor(owner_id=None, db_session=db)

        source = _make_source()

        rows = await executor.run(source)

        assert isinstance(rows, list)
        assert len(rows) == 1
        assert "error" in rows[0]
        assert "owner_id" in rows[0]["error"].lower() or "user" in rows[0]["error"].lower()

    @pytest.mark.asyncio
    async def test_missing_integration_returns_error(self):
        db = _db_with_integration(None)
        executor = OnedriveExcelQueryExecutor(owner_id="42", db_session=db)

        source = _make_source()

        rows = await executor.run(source)

        assert isinstance(rows, list)
        assert len(rows) == 1
        assert "error" in rows[0]
        msg = rows[0]["error"].lower()
        # Must mention that OneDrive isn't connected, and never raise.
        assert "onedrive" in msg or "connect" in msg or "integration" in msg


# ---------------------------------------------------------------------------
# Tests — happy path
# ---------------------------------------------------------------------------


class TestOnedriveExcelExecutorHappyPath:
    @pytest.mark.asyncio
    async def test_happy_path_returns_rows(self):
        db = _db_with_integration("tok")
        executor = OnedriveExcelQueryExecutor(owner_id="7", db_session=db)

        df = pd.DataFrame(
            [
                {"Email": "a@b.c", "Status": "sent"},
                {"Email": "d@e.f", "Status": "bounced"},
                {"Email": "g@h.i", "Status": ""},
            ]
        )

        with patch(
            "apowerb.bi.data.onedrive_excel_executor.download_and_parse_spreadsheet",
            return_value=(df, None),
        ), patch(
            "apowerb.bi.data.onedrive_excel_executor._graph_headers",
            return_value={"Authorization": "Bearer fake"},
        ), patch(
            "apowerb.bi.data.onedrive_excel_executor.decrypt_value",
            side_effect=lambda v: v,
        ):
            rows = await executor.run(_make_source())

        assert rows == [
            {"Email": "a@b.c", "Status": "sent"},
            {"Email": "d@e.f", "Status": "bounced"},
            {"Email": "g@h.i", "Status": ""},
        ]

    @pytest.mark.asyncio
    async def test_respects_limit(self):
        db = _db_with_integration("tok")
        executor = OnedriveExcelQueryExecutor(owner_id="7", db_session=db)

        df = pd.DataFrame(
            [
                {"Email": "a@b.c"},
                {"Email": "d@e.f"},
                {"Email": "g@h.i"},
                {"Email": "j@k.l"},
            ]
        )

        with patch(
            "apowerb.bi.data.onedrive_excel_executor.download_and_parse_spreadsheet",
            return_value=(df, None),
        ), patch(
            "apowerb.bi.data.onedrive_excel_executor._graph_headers",
            return_value={"Authorization": "Bearer fake"},
        ), patch(
            "apowerb.bi.data.onedrive_excel_executor.decrypt_value",
            side_effect=lambda v: v,
        ):
            rows = await executor.run(_make_source(limit=2))

        assert len(rows) == 2
        assert rows[0]["Email"] == "a@b.c"
        assert rows[1]["Email"] == "d@e.f"

    @pytest.mark.asyncio
    async def test_sheet_name_option_is_forwarded(self):
        """If source_options.sheet_name is present, it must reach the Excel
        parser helper so we actually read the right worksheet."""
        db = _db_with_integration("tok")
        executor = OnedriveExcelQueryExecutor(owner_id="7", db_session=db)

        df = pd.DataFrame([{"A": 1}])

        with patch(
            "apowerb.bi.data.onedrive_excel_executor.download_and_parse_spreadsheet",
            return_value=(df, None),
        ) as mock_parse, patch(
            "apowerb.bi.data.onedrive_excel_executor._graph_headers",
            return_value={"Authorization": "Bearer fake"},
        ), patch(
            "apowerb.bi.data.onedrive_excel_executor.decrypt_value",
            side_effect=lambda v: v,
        ):
            rows = await executor.run(
                _make_source(
                    item_path="Reports/data.xlsx",
                    sheet_name="Sheet2",
                )
            )

        assert rows == [{"A": 1}]
        # First positional arg must be the item_path.
        args, kwargs = mock_parse.call_args
        assert args[0] == "Reports/data.xlsx"
        # sheet_name must be passed — either as a kwarg or through call args.
        assert kwargs.get("sheet_name") == "Sheet2" or "Sheet2" in args

    @pytest.mark.asyncio
    async def test_csv_source_is_parsed_via_multiformat_helper(self):
        """The executor must route ALL tabular files (csv/tsv/ods/xlsx) through
        ``download_and_parse_spreadsheet`` — not the legacy Excel-only helper.
        """
        db = _db_with_integration("tok")
        executor = OnedriveExcelQueryExecutor(owner_id="7", db_session=db)

        df = pd.DataFrame([{"name": "Alice", "age": 30}])

        with patch(
            "apowerb.bi.data.onedrive_excel_executor.download_and_parse_spreadsheet",
            return_value=(df, None),
        ) as mock_parse, patch(
            "apowerb.bi.data.onedrive_excel_executor._graph_headers",
            return_value={"Authorization": "Bearer fake"},
        ), patch(
            "apowerb.bi.data.onedrive_excel_executor.decrypt_value",
            side_effect=lambda v: v,
        ):
            rows = await executor.run(_make_source(item_path="Reports/people.csv"))

        assert rows == [{"name": "Alice", "age": 30}]
        args, _ = mock_parse.call_args
        assert args[0] == "Reports/people.csv"

    @pytest.mark.asyncio
    async def test_parse_error_returns_error_dict(self):
        """A parse failure from download_and_parse_spreadsheet is reported as an
        error row, never raised."""
        db = _db_with_integration("tok")
        executor = OnedriveExcelQueryExecutor(owner_id="7", db_session=db)

        with patch(
            "apowerb.bi.data.onedrive_excel_executor.download_and_parse_spreadsheet",
            return_value=(None, "Download failed (HTTP 404)"),
        ), patch(
            "apowerb.bi.data.onedrive_excel_executor._graph_headers",
            return_value={"Authorization": "Bearer fake"},
        ), patch(
            "apowerb.bi.data.onedrive_excel_executor.decrypt_value",
            side_effect=lambda v: v,
        ):
            rows = await executor.run(_make_source())

        assert isinstance(rows, list)
        assert len(rows) == 1
        assert "error" in rows[0]
        assert "404" in rows[0]["error"] or "Download" in rows[0]["error"]
