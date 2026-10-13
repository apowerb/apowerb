"""The dashboard language drives the texts the engine writes at compute time.

Numbers and dates in rendered texts and KPIs, the « Toutes » table row, the
« Toutes familles » filter label and the period labels follow ``Dashboard.locale``;
a dashboard without a locale stays French.
"""

from __future__ import annotations

import asyncio
import re

import pytest

from apowerb.bi.spec import catalog, formatting
from apowerb.bi.spec.filters import all_label, period_label
from apowerb.bi.spec.models import DashboardDataRequest
from tests.helpers.bi_template_env import dashboard_from_spec, sales_frame

DATASET_ID = "11111111-2222-3333-4444-555555555555"
FRENCH = re.compile(r"[éèàçêûô«»]|\b(unités|semaines?|Toutes?|Tous|soit|sem\.)\b")


def _compute(monkeypatch, lang: str | None):
    import apowerb.bi.spec.sources as sources
    from apowerb.bi.spec.engine import compute_dashboard

    df = sales_frame()

    async def fake_load(dataset_id, owner, sheet=None):
        return df

    monkeypatch.setattr(sources, "load_dataset", fake_load)
    template = catalog.get_template("stock_pilotage", lang=lang or "fr")
    columns, _ = template.clean_columns({"date": "semaine", "family": "famille", "sales": "ventes"})
    params, _ = template.clean_params({})
    spec = template.build(DATASET_ID, columns, params)
    dash = dashboard_from_spec(spec)
    if lang is not None:
        dash = dash.model_copy(update={"locale": lang})
    resp = asyncio.run(compute_dashboard(dash, DashboardDataRequest(), owner="alice@example.com"))
    return dash, resp


def test_english_dashboard_renders_english_numbers_labels_and_rows(monkeypatch):
    dash, resp = _compute(monkeypatch, "en")
    assert resp.filters["famille"].label == "All product families"
    assert resp.filters["periode"].label == "Last 26 weeks"
    texts = [r.text for r in resp.components.values() if r.type == "text" and r.text]
    kpis = [r.value for r in resp.components.values() if r.type == "kpi" and r.value]
    assert texts and kpis
    for text in texts:
        assert not FRENCH.search(text), text
    for kpi in kpis:
        assert not FRENCH.search(" ".join(filter(None, [kpi.formatted, kpi.secondary, kpi.target_label]))), kpi
        assert "," not in kpi.formatted or re.search(r"\d,\d{3}", kpi.formatted), kpi.formatted
    table = next(r for r in resp.components.values() if r.type == "table")
    assert table.rows[-1]["famille"] == "All"


def test_dashboard_without_locale_stays_french(monkeypatch):
    dash, resp = _compute(monkeypatch, None)
    assert resp.filters["famille"].label == "Toutes familles"
    assert resp.filters["periode"].label == "26 dernières semaines"
    table = next(r for r in resp.components.values() if r.type == "table")
    assert table.rows[-1]["famille"] == "Toutes"


@pytest.mark.parametrize(
    ("value", "fmt", "unit", "fr", "en"),
    [
        (15300, "compact", "unités", "15,3 k unités", "15.3k unités"),
        (1234567, "integer", None, "1\u202f234\u202f567", "1,234,567"),
        (0.969, "percent", None, "96,9 %", "96.9%"),
        (1.04, "weeks", None, "1,0 sem.", "1.0 wk"),
        (2.5, "decimal1", None, "2,5", "2.5"),
        (2_400_000_000, "compact", None, "2,4 Md", "2.4B"),
    ],
)
def test_format_value_by_language(value, fmt, unit, fr, en):
    assert formatting.format_value(value, fmt, unit) == fr
    assert formatting.format_value(value, fmt, unit, lang="en") == en


def test_format_delta_and_date_by_language():
    assert formatting.format_delta(1.1, 1.0, lang="en")[1] == "+10.0%"
    assert formatting.format_delta(0.5, 0.4, "percent", lang="en")[1] == "+10.0 pt"
    assert formatting.format_date("2025-12-29", "en") == "Dec 29, 2025"
    assert formatting.format_date("2025-12-29") == "29 déc. 2025"


def test_filter_labels_by_language():
    assert all_label("Famille") == "Toutes familles"
    assert all_label("Product family", "en") == "All product families"
    assert all_label("Customer", "en") == "All customers"
    assert period_label("26w", "en") == "Last 26 weeks"
    assert period_label("1m", "en") == "Last month"
    assert period_label("all", "en") == "Whole period"
    assert period_label("ytd", "en") == "Year to date"
