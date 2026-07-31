"""Tests for _resolve_chart_ref: accept UUID directly or resolve a chart name.

Covers the anti-récidive fix from incident 2026-05-12 where the BI agent
passed the chart name instead of the UUID to tool_add_chart_to_dashboard,
causing HTTP 404 on chart data fetch.
"""

from __future__ import annotations

import uuid as uuid_mod

import pytest

from apowerb.bi.charts.core import Chart, ChartType, DataSource
from apowerb.bi.charts.service import ChartService, InMemoryChartStore
from apowerb.tools_store.portfolio.business_intelligence import _resolve_chart_ref


@pytest.fixture
async def svc_with_charts():
    """ChartService preloaded with 3 charts (2 distinct names + 1 duplicate)."""
    store = InMemoryChartStore()
    svc = store  # helper expects a store, not a service
    c1 = Chart(
        id=str(uuid_mod.uuid4()),
        name='daily_volume',
        title='Daily Volume',
        chart_type=ChartType.BAR,
        source=DataSource(query='SELECT 1'),
        owner='owner@x.com',
        organization_id='org_x',
    )
    c2 = Chart(
        id=str(uuid_mod.uuid4()),
        name='top_fournisseurs',
        title='Top Fournisseurs',
        chart_type=ChartType.BAR,
        source=DataSource(query='SELECT 2'),
        owner='owner@x.com',
        organization_id='org_x',
    )
    c3 = Chart(
        id=str(uuid_mod.uuid4()),
        name='daily_volume',  # intentional duplicate name
        title='Daily Volume v2',
        chart_type=ChartType.BAR,
        source=DataSource(query='SELECT 3'),
        owner='owner@x.com',
        organization_id='org_x',
    )
    await store.save(c1)
    await store.save(c2)
    await store.save(c3)
    return store, c1, c2, c3


async def test_resolve_passes_uuid_through(svc_with_charts):
    store, c1, _, _ = svc_with_charts
    resolved, err = await _resolve_chart_ref(c1.id, store)
    assert resolved == c1.id
    assert err is None


async def test_resolve_passes_uppercase_uuid(svc_with_charts):
    store, c1, _, _ = svc_with_charts
    resolved, err = await _resolve_chart_ref(c1.id.upper(), store)
    assert resolved == c1.id.upper()  # passes through as-is, no error
    assert err is None


async def test_resolve_by_unique_name(svc_with_charts):
    store, _, c2, _ = svc_with_charts
    resolved, err = await _resolve_chart_ref('top_fournisseurs', store)
    assert resolved == c2.id
    assert err is None


async def test_resolve_name_not_found(svc_with_charts):
    store, _, _, _ = svc_with_charts
    resolved, err = await _resolve_chart_ref('nonexistent_chart', store)
    assert resolved is None
    assert 'No chart named' in err
    assert 'nonexistent_chart' in err
    assert 'tool_create_chart' in err  # hint to agent


async def test_resolve_name_ambiguous(svc_with_charts):
    store, c1, _, c3 = svc_with_charts
    # both c1 and c3 have name='daily_volume'
    resolved, err = await _resolve_chart_ref('daily_volume', store)
    assert resolved is None
    assert 'Ambiguous' in err
    assert c1.id in err and c3.id in err


async def test_resolve_garbled_uuid_falls_back_to_name_lookup(svc_with_charts):
    """A string that looks like a UUID but isn't valid hex should not match the UUID
    pattern → falls back to name lookup → No chart named."""
    store, _, _, _ = svc_with_charts
    bad_uuid = '11111111-2222-3333-4444-zzzzzzzzzzzz'  # 'z' chars
    resolved, err = await _resolve_chart_ref(bad_uuid, store)
    assert resolved is None
    assert 'No chart named' in err
