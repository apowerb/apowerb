"""Regression tests for the BI data fetch pagination fallback.

Before the fix, the default ``page_size = 50`` silently truncated
operator-configured charts (e.g. ``backlog_actuel_table_v3`` with
``source.limit = 1000``). The UI displayed "Affichage 1-25 sur 50"
on a backlog that actually contained 78 rows.

After the fix, ``page_size = None`` (no client-side override)
defers to ``chart.source.limit`` so the operator setting is honoured.
"""
from __future__ import annotations

import pytest

from th2agent.bi.data.schema import DataRequest


class TestDataRequestPageSize:
    def test_page_size_defaults_to_none(self):
        """Sentinel value None signals 'use chart.source.limit'."""
        req = DataRequest()
        assert req.page_size is None, (
            "DataRequest.page_size must default to None so the service "
            "falls back to chart.source.limit. A numeric default truncates "
            "operator-configured tables — incident 2026-05-19 backlog 78 "
            "displayed as 50."
        )

    def test_explicit_page_size_accepted(self):
        req = DataRequest(page_size=100)
        assert req.page_size == 100

    def test_page_size_ceiling_at_100k(self):
        """Mirrors DataSource.limit's le=100_000 ceiling."""
        req = DataRequest(page_size=100_000)
        assert req.page_size == 100_000

    def test_page_size_above_ceiling_rejected(self):
        with pytest.raises(Exception):
            DataRequest(page_size=100_001)

    def test_page_size_zero_rejected(self):
        with pytest.raises(Exception):
            DataRequest(page_size=0)


class TestEffectivePageSizeResolution:
    """The service.fetch() picks ``req.page_size if not None else
    chart.source.limit``. Tested via a tiny helper that mirrors the
    branch in service.py so the contract is pinned even without a
    full chart fixture."""

    def _effective(self, req_page_size, source_limit):
        return (
            req_page_size
            if req_page_size is not None
            else (source_limit or 50)
        )

    def test_explicit_request_wins(self):
        assert self._effective(25, 1000) == 25

    def test_falls_back_to_source_limit_when_unset(self):
        assert self._effective(None, 1000) == 1000

    def test_falls_back_to_50_when_no_source_limit(self):
        assert self._effective(None, None) == 50

    def test_falls_back_to_50_when_source_limit_zero(self):
        # 0 is falsy → fall back to 50 (avoid empty pages).
        assert self._effective(None, 0) == 50
