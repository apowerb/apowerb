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
from apowerb.bi.spec.errors import SpecError
from apowerb.bi.spec.models import AggregateStep, DashboardDataRequest, MeasureSpec, VisualSpec
from apowerb.bi.spec.validate import validate_spec
from tests.helpers.bi_spec_data import build_dashboard, dataset_source
from tests.helpers.bi_template_env import dashboard_from_spec, sales_frame

DATASET_ID = "11111111-2222-3333-4444-555555555555"
FRENCH = re.compile(r"[éèàçêûô«»]|\b(unités|semaines?|Toutes?|Tous|soit|sem\.)\b")


@pytest.fixture(autouse=True)
def _fresh_caches():
    from apowerb.bi.spec import engine

    engine.clear_caches()
    yield


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


@pytest.mark.parametrize(
    ("count", "fr", "en"),
    [
        (0, "0 famille", "0 product families"),
        (1, "1 famille", "1 product family"),
        (2, "2 familles", "2 product families"),
        (1234, "1\u202f234 familles", "1,234 product families"),
    ],
)
def test_plural_by_language(count, fr, en):
    assert formatting.format_plural(count, "famille", "familles") == fr
    assert formatting.format_plural(count, "product family", "product families", lang="en") == en


def test_plural_zero_is_singular_in_french_only():
    assert formatting.format_plural(0, "semaine", "semaines") == "0 semaine"
    assert formatting.format_plural(1.5, "semaine", "semaines") == "1,5 semaine"
    assert formatting.format_plural(0, "week", "weeks", lang="en") == "0 weeks"
    assert formatting.format_plural(1.5, "week", "weeks", lang="en") == "1.5 weeks"


def test_plural_of_a_missing_value_is_the_missing_mark():
    assert formatting.format_plural(None, "famille", "familles") == formatting.MISSING


def _plural_dashboard(lang: str | None):
    source = dataset_source(measures=[MeasureSpec(name="nb", label="Nb", expr="COUNT(DISTINCT famille)")])
    text = VisualSpec(type="text", source="ventes", text="{{nb|plural:famille concernée:familles concernées}}")
    dash = build_dashboard({"t": text}, source=source, filters=[])
    return dash.model_copy(update={"locale": lang}) if lang else dash


@pytest.mark.parametrize(
    ("families", "expected"),
    [(0, "0 famille concernée"), (1, "1 famille concernée"), (2, "2 familles concernées")],
)
def test_plural_token_renders_in_french(monkeypatch, families, expected):
    import apowerb.bi.spec.sources as sources
    from apowerb.bi.spec.engine import compute_dashboard

    frame = sales_frame()
    frame = frame[frame["famille"].isin(sorted(frame["famille"].unique())[:families])]

    async def fake_load(dataset_id, owner, sheet=None):
        return frame

    monkeypatch.setattr(sources, "load_dataset", fake_load)
    for lang in (None, "fr"):
        resp = asyncio.run(compute_dashboard(_plural_dashboard(lang), DashboardDataRequest(), owner="o@example.com"))
        assert resp.components["t"].text == expected


def test_plural_token_in_english_dashboard(monkeypatch):
    import apowerb.bi.spec.sources as sources
    from apowerb.bi.spec.engine import compute_dashboard

    async def fake_load(dataset_id, owner, sheet=None):
        return sales_frame()

    monkeypatch.setattr(sources, "load_dataset", fake_load)
    dash = _plural_dashboard("en")
    dash.components[0].visual.text = "{{nb|plural:product family:product families}}"
    resp = asyncio.run(compute_dashboard(dash, DashboardDataRequest(), owner="o@example.com"))
    assert resp.components["t"].text == "5 product families"


def test_validation_accepts_plural_with_two_words_and_rejects_one():
    def problems(text: str) -> list[str]:
        source = dataset_source(measures=[MeasureSpec(name="nb", label="Nb", expr="COUNT(*)")])
        visual = VisualSpec(type="text", source="ventes", text=text)
        return validate_spec(build_dashboard({"t": visual}, source=source, filters=[]))

    assert problems("{{nb|plural:famille:familles}}") == []
    assert any("plural" in p for p in problems("{{nb|plural}}"))
    assert any("plural" in p for p in problems("{{nb|plural:famille}}"))


@pytest.mark.parametrize(
    ("safety", "lead", "lang", "expected"),
    [
        (1.0, 1, "fr", "Seuil de sécurité : 1 semaine de prévision. Délai fournisseur : 1 semaine,"),
        (2.0, 2, "fr", "Seuil de sécurité : 2 semaines de prévision. Délai fournisseur : 2 semaines,"),
        (1.0, 1, "en", "Safety stock: 1 week of forecast. Supplier lead time: 1 week,"),
        (2.0, 2, "en", "Safety stock: 2 weeks of forecast. Supplier lead time: 2 weeks,"),
    ],
)
def test_build_time_week_counts_agree_with_their_number(safety, lead, lang, expected):
    from apowerb.bi.spec.templates import stock_pilotage

    spec = stock_pilotage(DATASET_ID, stock="stock", safety_factor=safety, lead_time=lead, lang=lang)
    texts = [c["visual"].get("text") or "" for c in spec["components"]]
    assert any(expected in t for t in texts)


def _compute_visual(monkeypatch, visual: VisualSpec, lang: str | None, source=None):
    import apowerb.bi.spec.sources as sources
    from apowerb.bi.spec.engine import compute_dashboard

    async def fake_load(dataset_id, owner, sheet=None):
        return sales_frame()

    monkeypatch.setattr(sources, "load_dataset", fake_load)
    dash = build_dashboard({"t": visual}, source=source or dataset_source(), filters=[])
    if lang:
        dash = dash.model_copy(update={"locale": lang})
    resp = asyncio.run(compute_dashboard(dash, DashboardDataRequest(), owner="o@example.com"))
    return resp


@pytest.mark.parametrize(
    ("lang", "expected"),
    [
        (None, "Mesure inconnue « inconnue » dans la source « ventes »."),
        ("fr", "Mesure inconnue « inconnue » dans la source « ventes »."),
        ("en", "Unknown measure “inconnue” in source “ventes”."),
    ],
)
def test_component_error_follows_the_dashboard_language(monkeypatch, lang, expected):
    resp = _compute_visual(monkeypatch, VisualSpec(type="text", source="ventes", text="{{inconnue}}"), lang)
    assert resp.components["t"].error == expected


@pytest.mark.parametrize(
    ("lang", "expected"),
    [(None, "Source inconnue « nope »."), ("en", "Unknown source “nope”.")],
)
def test_unknown_source_error_follows_the_dashboard_language(monkeypatch, lang, expected):
    resp = _compute_visual(monkeypatch, VisualSpec(type="text", source="nope", text="x"), lang)
    assert resp.components["t"].error == expected


@pytest.mark.parametrize(
    ("lang", "prefix", "needle", "meta"),
    [
        (None, "Source « ventes » indisponible : ", "colonne introuvable zzz", "Source indisponible : "),
        ("en", "Source “ventes” unavailable: ", "column not found: zzz", "Source unavailable: "),
    ],
)
def test_source_error_reaches_the_tile_and_the_source_meta_in_the_dashboard_language(
    monkeypatch, lang, prefix, needle, meta
):
    source = dataset_source(transforms=[AggregateStep(group_by=["zzz"], aggregates={})])
    resp = _compute_visual(monkeypatch, VisualSpec(type="text", source="ventes", text="x"), lang, source)
    error = resp.components["t"].error
    assert error.startswith(prefix) and needle in error
    assert resp.sources["ventes"].warnings[0].startswith(meta) and needle in resp.sources["ventes"].warnings[0]


def test_spec_error_text_falls_back_to_french():
    assert SpecError("bonjour", en="hello").text("en") == "hello"
    assert SpecError("bonjour", en="hello").text("fr") == "bonjour"
    assert SpecError("bonjour", en="hello").text(None) == "bonjour"
    assert SpecError("bonjour").text("en") == "bonjour"


@pytest.mark.parametrize(
    ("lang", "unavailable", "forecast_missing", "plan", "kpi_warning"),
    [
        (
            None,
            "Prévision indisponible : moteur en panne.",
            "Prévision moteur absente pour ",
            "plan calculé avec la moyenne des 4 dernières semaines de ventes.",
            "Prévision indisponible : plan calculé avec la moyenne des 4 dernières semaines de ventes.",
        ),
        (
            "en",
            "Forecast unavailable: engine down.",
            "Engine forecast missing for ",
            "plan computed with the average of the last 4 weeks of sales.",
            "Forecast unavailable: plan computed with the average of the last 4 weeks of sales.",
        ),
    ],
)
def test_forecast_engine_fallback_warnings_follow_the_dashboard_language(
    monkeypatch, lang, unavailable, forecast_missing, plan, kpi_warning
):
    import apowerb.bi.spec.sources as sources
    import apowerb.bi.spec.transforms as transforms
    from apowerb.bi.spec.engine import compute_dashboard
    from apowerb.bi.spec.templates import stock_pilotage
    from tests.helpers.bi_template_env import stock_frame

    frame = stock_frame()

    async def fake_load(dataset_id, owner, sheet=None):
        return frame

    def down(body):
        raise transforms.ForecastUnavailable("moteur en panne", en="engine down")

    monkeypatch.setattr(sources, "load_dataset", fake_load)
    monkeypatch.setattr(transforms, "_call_forecast_engine", down)
    spec = stock_pilotage(
        DATASET_ID, stock="stock", safety_column="seuil_securite", lead_time_column="delai_fournisseur_sem",
        lang=lang or "fr",
    )
    dash = dashboard_from_spec(spec)
    if lang:
        dash = dash.model_copy(update={"locale": lang})
    resp = asyncio.run(compute_dashboard(dash, DashboardDataRequest(), owner="alice@example.com"))
    warnings = resp.sources["plan"].warnings
    assert len(warnings) == 2 and warnings[0] == unavailable
    assert warnings[1].startswith(forecast_missing) and warnings[1].endswith(plan)
    tile_warnings = [w for r in resp.components.values() if r.meta for w in r.meta.warnings]
    assert kpi_warning in tile_warnings
