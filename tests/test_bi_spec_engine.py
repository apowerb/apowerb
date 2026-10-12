"""Dashboard spec v2 engine: transforms, measures, filters, visuals, sandbox.

All data is synthetic (five generic families, weekly); the dataset read is
mocked at ``apowerb.bi.spec.sources.load_dataset``.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, patch

import numpy as np
import pandas as pd
import pytest

from apowerb.bi.spec.formatting import format_date_fr

from apowerb.bi.spec import duck, engine, sources, sql_guard, transforms
from apowerb.bi.spec.errors import SpecError
from apowerb.bi.spec.formatting import format_delta, format_value
from apowerb.bi.spec.models import (
    ColorRule,
    ColumnSpec,
    DashboardDataRequest,
    DeriveStep,
    FilterSpec,
    InventorySimStep,
    KpiSpec,
    MeasureSpec,
    NaiveForecastStep,
    ReferenceLine,
    SeriesSpec,
    ThresholdSpec,
    VisualSpec,
    WaterfallStep,
)
from apowerb.bi.spec.transforms import (
    ForecastUnavailable,
    TransformState,
    apply_transforms,
    naive_forecast_series,
    simulate_inventory,
)
from apowerb.bi.spec.visuals import status_of
from tests.helpers.bi_spec_data import (
    FAMILIES,
    OWNER,
    build_dashboard,
    combo,
    dataset_source,
    kpi,
    pipeline_source,
    weekly_sales,
)


@pytest.fixture(autouse=True)
def _fresh_caches():
    engine.clear_caches()
    yield
    engine.clear_caches()


async def compute(dashboard, df=None, *, components=None, **filters):
    frame = weekly_sales() if df is None else df

    async def fake_load(dataset_id, owner, sheet=None):
        return frame

    with patch.object(sources, "load_dataset", fake_load):
        return await engine.compute_dashboard(
            dashboard, DashboardDataRequest(filters=filters, components=components), owner=OWNER
        )


def value_of(result, cid):
    comp = result.components[cid]
    assert comp.error is None, comp.error
    return comp.value.value


# ---------------------------------------------------------------------------
# naive_forecast
# ---------------------------------------------------------------------------


class TestNaiveForecast:
    SERIES = np.array([10, 20, 30, 40, 50, 60, 70, 80], dtype=float)

    def test_hand_computed_values_with_clamped_trend(self):
        out = naive_forecast_series(self.SERIES, window=2, season_length=4, trend_bounds=(0.8, 1.25))
        # t0: no history. t1..t3: mean of the previous (up to 2) periods.
        # t4, t5: same period last year, last year's window not available -> trend 1.
        # t6: 30 * clamp(mean(50,60)/mean(10,20)=3.67 -> 1.25) = 37.5 ; t7: 40 * 1.25 = 50
        expected = [np.nan, 10, 15, 25, 10, 20, 37.5, 50]
        assert np.allclose(out, expected, equal_nan=True)

    def test_unclamped_trend(self):
        out = naive_forecast_series(self.SERIES, window=2, season_length=4, trend_bounds=(0.0, 100.0))
        assert out[6] == pytest.approx(30 * 55 / 15)
        assert out[7] == pytest.approx(40 * 65 / 25)

    def test_missing_values_are_ignored_in_means(self):
        values = np.array([10, np.nan, 30, 40], dtype=float)
        out = naive_forecast_series(values, window=2, season_length=52, trend_bounds=(0.8, 1.25))
        assert out[2] == pytest.approx(10.0)  # mean of [10, nan]
        assert out[3] == pytest.approx(30.0)  # mean of [nan, 30]

    async def test_step_is_per_group_and_marks_forecast_provenance(self):
        rows = []
        for fam, mult in (("a", 1), ("b", 100)):
            for i, v in enumerate(self.SERIES):
                rows.append({"semaine": pd.Timestamp("2024-01-01") + pd.Timedelta(weeks=i), "famille": fam, "ventes": v * mult})
        step = NaiveForecastStep(date="semaine", value="ventes", by=["famille"], window=2, season_length=4)
        state = await apply_transforms(TransformState(pd.DataFrame(rows), {}), [step], ctx=None)
        a = state.df[state.df.famille == "a"]["prevision"].to_numpy()
        b = state.df[state.df.famille == "b"]["prevision"].to_numpy()
        assert np.allclose(a, [np.nan, 10, 15, 25, 10, 20, 37.5, 50], equal_nan=True)
        assert np.allclose(b, a * 100, equal_nan=True)
        assert state.prov["prevision"].value == "forecast"


# ---------------------------------------------------------------------------
# inventory_sim
# ---------------------------------------------------------------------------


def _sim_frame(weeks=60, spike_at=None):
    rng = np.random.default_rng(3)
    demand = np.round(100 + rng.normal(0, 10, weeks))
    if spike_at is not None:
        demand[spike_at] = 5000
    dates = pd.date_range("2024-01-01", periods=weeks, freq="7D")
    frames = []
    for fam in ("a", "b"):
        frames.append(pd.DataFrame({"semaine": dates, "famille": fam, "ventes": demand * (1 if fam == "a" else 2)}))
    return pd.concat(frames, ignore_index=True)


async def _simulate(df, **kw):
    steps = [
        NaiveForecastStep(date="semaine", value="ventes", by=["famille"], window=4, season_length=52),
        InventorySimStep(date="semaine", by=["famille"], demand="ventes", forecast="prevision", **kw),
    ]
    return await apply_transforms(TransformState(df, {}), steps, ctx=None)


class TestInventorySim:
    async def test_stock_balance_holds_on_every_row(self):
        state = await _simulate(_sim_frame(), default_lot=10, partial_rate=0.5)
        df = state.df
        assert np.allclose(df.stock_debut + df.appro - df.ventes_servies, df.stock_fin)
        assert np.allclose(df.ventes + 0 * df.stock_fin - df.ventes_servies, df.demande_non_servie)
        assert (df.ventes_servies <= df.ventes + 1e-9).all()
        assert (df.stock_fin >= -1e-9).all()
        # the opening stock is the previous closing stock, per group
        for _, grp in df.groupby("famille"):
            assert np.allclose(grp.stock_debut.to_numpy()[1:], grp.stock_fin.to_numpy()[:-1])

    async def test_lead_time_and_lot_rounding(self):
        state = await _simulate(_sim_frame(), default_lot=25, partial_rate=0.0, lead_time=2)
        grp = state.df[state.df.famille == "a"].reset_index(drop=True)
        assert (grp.commande % 25 == 0).all()
        assert np.allclose(grp.appro.to_numpy()[2:], grp.commande.to_numpy()[:-2])
        assert grp.appro.iloc[0] == 0 and grp.appro.iloc[1] == 0

    async def test_deterministic_with_seed(self):
        df = _sim_frame()
        a = (await _simulate(df, partial_rate=0.5, seed=1)).df
        b = (await _simulate(df, partial_rate=0.5, seed=1)).df
        c = (await _simulate(df, partial_rate=0.5, seed=2)).df
        pd.testing.assert_frame_equal(a, b)
        assert not np.allclose(a.appro, c.appro)

    async def test_stockout_creates_unserved_demand(self):
        state = await _simulate(_sim_frame(spike_at=30), default_lot=10)
        row = state.df[(state.df.famille == "a")].reset_index(drop=True).iloc[30]
        assert row.demande_non_servie > 0
        assert row.ventes_servies == pytest.approx(row.stock_debut + row.appro)
        assert row.stock_fin == pytest.approx(0.0)

    async def test_outputs_are_simulated(self):
        state = await _simulate(_sim_frame())
        for col in transforms.INVENTORY_COLUMNS:
            assert state.prov[col].value == "simulated"

    def test_lot_sizes_by_group_key(self):
        step = InventorySimStep(date="semaine", by=["famille"], lot_sizes={"a": 50}, default_lot=7)
        demand = np.full(10, 100.0)
        out_a = simulate_inventory(demand, demand, step, key="a", lot_key=None)
        out_b = simulate_inventory(demand, demand, step, key="b", lot_key=None)
        assert (out_a["commande"] % 50 == 0).all()
        assert (out_b["commande"] % 7 == 0).all()


# ---------------------------------------------------------------------------
# Measures, thresholds, formats
# ---------------------------------------------------------------------------


class TestThresholds:
    def test_higher_is_better_bounds(self):
        th = ThresholdSpec(direction="higher_is_better", good=0.99, warn=0.95)
        assert [status_of(v, th) for v in (1.0, 0.99, 0.9899, 0.95, 0.9499, 0.0)] == [
            "good", "good", "warn", "warn", "bad", "bad",
        ]

    def test_lower_is_better_bounds(self):
        th = ThresholdSpec(direction="lower_is_better", good=2.0, warn=4.0)
        assert [status_of(v, th) for v in (1.0, 2.0, 2.01, 4.0, 4.01)] == ["good", "good", "warn", "warn", "bad"]

    def test_none_without_threshold_or_value(self):
        assert status_of(None, ThresholdSpec(good=1, warn=0)) is None
        assert status_of(1.0, None) is None

    async def test_kpi_status_end_to_end(self):
        df = pd.DataFrame(
            {"semaine": pd.date_range("2024-01-01", periods=5, freq="7D"), "taux": [1.0, 0.97, 0.5, 0.99, 0.95]}
        )
        source = dataset_source(
            measures=[
                MeasureSpec(
                    name="taux", label="Taux", expr="AVG(taux)", format="percent",
                    thresholds=ThresholdSpec(direction="higher_is_better", good=0.99, warn=0.95),
                )
            ]
        )
        visual = VisualSpec(
            type="table", source="ventes", x="semaine",
            columns=[ColumnSpec(key="semaine"), ColumnSpec(key="taux", use_thresholds=True)],
        )
        chart = VisualSpec(
            type="bar", source="ventes", x="semaine", series=[SeriesSpec(key="taux", use_thresholds=True)]
        )
        res = await compute(build_dashboard({"t": visual, "c": chart}, source=source, filters=[]), df)
        statuses = [r["_status_taux"] for r in res.components["t"].rows]
        assert statuses == ["good", "warn", "bad", "good", "warn"]
        colors = [r["_color_taux"] for r in res.components["c"].rows]
        assert colors == statuses


class TestFormatting:
    def test_fr_formats(self):
        thin = " "
        assert format_value(1234567, "number") == f"1{thin}234{thin}567"
        assert format_value(1234.5, "number") == f"1{thin}234,5"
        assert format_value(1234.567, "integer") == f"1{thin}235"
        assert format_value(15300, "compact") == "15,3 k"
        assert format_value(2_100_000, "compact") == "2,1 M"
        assert format_value(950, "compact") == "950"
        assert format_value(0.969, "percent") == "96,9 %"
        assert format_value(3.14159, "decimal1") == "3,1"
        assert format_value(3.14159, "decimal2") == "3,14"
        assert format_value(1.0, "weeks") == "1,0 sem."
        assert format_value(None, "number") == "—"
        assert format_value(float("nan"), "percent") == "—"
        assert format_value(12, "number", "kg") == "12 kg"

    def test_deltas(self):
        assert format_delta(110, 100, "number") == (pytest.approx(0.1), "+10,0 %")
        assert format_delta(90, 100, "number")[1] == "-10,0 %"
        assert format_delta(0.97, 0.95, "percent")[1] == "+2,0 pt"
        assert format_delta(1, 0, "number") == (None, None)
        assert format_delta(None, 1, "number") == (None, None)


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


class TestFilters:
    async def test_relative_period_26w_is_exactly_26_weeks(self):
        dash = build_dashboard({"k": kpi("nb_semaines"), "n": kpi("n")})
        res = await compute(dash)
        assert value_of(res, "k") == 26
        assert value_of(res, "n") == 26 * len(FAMILIES)
        f = res.filters["periode"]
        assert f.label == "26 dernières semaines"
        df = weekly_sales()
        last = df.semaine.max()
        assert f.date_to == last.strftime("%Y-%m-%d")
        assert f.date_from == (last - pd.Timedelta(weeks=25)).strftime("%Y-%m-%d")

    @pytest.mark.parametrize("preset,weeks", [("13w", 13), ("52w", 52), ("all", 130)])
    async def test_other_presets(self, preset, weeks):
        res = await compute(build_dashboard({"k": kpi("nb_semaines")}), periode=preset)
        assert value_of(res, "k") == weeks

    async def test_months_preset(self):
        df = weekly_sales()
        last = df.semaine.max()
        expected = df[(df.semaine > last - pd.DateOffset(months=3))].semaine.nunique()
        res = await compute(build_dashboard({"k": kpi("nb_semaines")}), periode="3m")
        assert value_of(res, "k") == expected
        assert res.filters["periode"].label == "3 derniers mois"

    async def test_unknown_period_is_a_spec_error(self):
        with pytest.raises(SpecError):
            await compute(build_dashboard({"k": kpi("n")}), periode="7y")

    async def test_period_is_relative_to_last_real_value_not_forecast_rows(self):
        df = weekly_sales(weeks=60)
        last_real = df.semaine.max()
        future = df[df.semaine == last_real].copy()
        future["semaine"] = last_real + pd.Timedelta(weeks=8)
        future["ventes"] = np.nan
        future["prix"] = np.nan  # appended forecast rows carry nothing but date, keys and forecasts
        data = pd.concat([df, future], ignore_index=True)
        res = await compute(build_dashboard({"k": kpi("nb_semaines")}), data, periode="13w")
        assert res.filters["periode"].date_to == last_real.strftime("%Y-%m-%d")
        assert res.sources["ventes"].last_date == last_real.strftime("%Y-%m-%d")
        # 13 real weeks + the forecast week after the last real date, which is kept
        assert value_of(res, "k") == 13 + 1
        real = await compute(
            build_dashboard({"r": VisualSpec(type="kpi", source="ventes", kpi=KpiSpec(measure="reel"))},
                            source=dataset_source(measures=[MeasureSpec(name="reel", label="R", expr="COUNT(DISTINCT semaine) FILTER (WHERE ventes IS NOT NULL)")])),
            data, periode="13w",
        )
        assert value_of(real, "r") == 13

    async def test_relative_period_only_cuts_the_lower_bound(self):
        df = weekly_sales(weeks=40)
        last_real = df.semaine.max()
        future = pd.DataFrame({"semaine": [last_real + pd.Timedelta(weeks=i) for i in (1, 2, 3)], "famille": "Alpha"})
        data = pd.concat([df, future], ignore_index=True)
        dash = build_dashboard({"c": VisualSpec(type="bar", source="ventes", x="semaine", series=[SeriesSpec(key="n")])})
        res = await compute(dash.model_copy(update={"sources": [dataset_source(measures=[MeasureSpec(name="n", label="N", expr="COUNT(*)")])]}), data, periode="13w", famille="Alpha")
        xs = [r["x"] for r in res.components["c"].rows]
        assert len(xs) == 13 + 3
        assert xs[-1] == (last_real + pd.Timedelta(weeks=3)).strftime("%Y-%m-%d")

    async def test_select_on_numeric_column_normalises_1_and_1_0(self):
        df = pd.DataFrame({"semaine": pd.date_range("2024-01-01", periods=4, freq="7D"), "annee": [2023.0, 2024.0, 2024.0, np.nan], "ventes": [1.0, 2, 3, 4]})
        filters = [FilterSpec(id="an", label="Année", field="annee", type="select")]
        dash = build_dashboard({"k": kpi("n")}, source=dataset_source(measures=[MeasureSpec(name="n", label="N", expr="COUNT(*)")]), filters=filters)
        for value in (2024, "2024", 2024.0):
            engine.clear_caches()
            assert value_of(await compute(dash, df, an=value), "k") == 2

    async def test_select_family_and_options(self):
        dash = build_dashboard({"n": kpi("n")})
        res = await compute(dash, famille="Charlie")
        assert value_of(res, "n") == 26
        assert res.filters["famille"].label == "Charlie"
        assert res.filter_options["famille"] == sorted(FAMILIES)
        assert res.components["n"].meta.filters_applied == {"periode": "26w", "famille": "Charlie"}
        all_fam = await compute(dash)
        assert all_fam.filters["famille"].value is None
        assert all_fam.filters["famille"].label == "Toutes familles"
        assert "famille" not in all_fam.components["n"].meta.filters_applied

    async def test_multi_filter(self):
        filters = [FilterSpec(id="familles", label="Famille", field="famille", type="multi")]
        dash = build_dashboard({"n": kpi("n")}, filters=filters)
        res = await compute(dash, familles=["Alpha", "Echo"])
        assert value_of(res, "n") == 2 * 130
        assert res.filters["familles"].label == "Alpha, Echo"

    async def test_date_range_filter(self):
        filters = [FilterSpec(id="plage", label="Plage", field="semaine", type="date_range")]
        dash = build_dashboard({"k": kpi("nb_semaines")}, filters=filters)
        res = await compute(dash, plage={"from": "2023-03-06", "to": "2023-03-27"})
        assert value_of(res, "k") == 4
        assert res.filters["plage"].label == "du 6 mars 2023 au 27 mars 2023"

    async def test_filter_not_applied_to_source_without_field(self):
        filters = [FilterSpec(id="zone", label="Zone", field="zone", type="select")]
        res = await compute(build_dashboard({"n": kpi("n")}, filters=filters), zone="Nord")
        assert value_of(res, "n") == 650  # no source has the field: nothing filtered

    async def test_ignore_filters_and_highlight(self):
        table = VisualSpec(
            type="table", source="ventes", x="famille",
            columns=[ColumnSpec(key="famille"), ColumnSpec(key="ventes_tot")],
            ignore_filters=["famille"], highlight_filter="famille",
        )
        res = await compute(build_dashboard({"t": table, "k": kpi("ventes_tot")}), famille="Bravo")
        rows = res.components["t"].rows
        assert [r["famille"] for r in rows] == sorted(FAMILIES)
        assert {r["famille"]: r["_highlight"] for r in rows} == {f: f == "Bravo" for f in FAMILIES}
        assert res.components["t"].meta.ignored_filters == ["famille"]
        assert "famille" not in res.components["t"].meta.filters_applied
        # the KPI next to it does honour the filter
        df = weekly_sales()
        bravo = df[df.famille == "Bravo"]
        expected = bravo[bravo.semaine > df.semaine.max() - pd.Timedelta(weeks=26)].ventes.sum()
        assert value_of(res, "k") == expected


# ---------------------------------------------------------------------------
# Visuals
# ---------------------------------------------------------------------------


class TestVisuals:
    async def test_kpi_delta_previous_period(self):
        df = weekly_sales()
        alpha = df[df.famille == "Alpha"]
        last = alpha.semaine.max()
        cur = alpha[alpha.semaine > last - pd.Timedelta(weeks=26)].ventes.sum()
        prev = alpha[(alpha.semaine > last - pd.Timedelta(weeks=52)) & (alpha.semaine <= last - pd.Timedelta(weeks=26))].ventes.sum()
        res = await compute(build_dashboard({"k": kpi("ventes_tot", compare="previous_period")}), famille="Alpha")
        v = res.components["k"].value
        assert v.value == cur
        assert v.delta == pytest.approx((cur - prev) / prev)
        assert v.delta_formatted.startswith("+") or v.delta_formatted.startswith("-")

    async def test_kpi_delta_previous_point(self):
        df = weekly_sales()
        alpha = df[df.famille == "Alpha"].sort_values("semaine")
        v_last, v_prev = alpha.ventes.iloc[-1], alpha.ventes.iloc[-2]
        res = await compute(build_dashboard({"k": kpi("ventes_tot", compare="previous_point")}), famille="Alpha")
        assert res.components["k"].value.delta == pytest.approx((v_last - v_prev) / v_prev)

    async def test_kpi_without_compare_has_no_delta_and_secondary_renders(self):
        k = kpi("nsl", secondary="sur {{nb_semaines}} semaines, {{filters.periode}}", target_label="cible ≥ 99 %")
        res = await compute(build_dashboard({"k": k}))
        v = res.components["k"].value
        assert v.delta is None and v.delta_formatted is None
        assert v.secondary == "sur 26 semaines, 26 dernières semaines"
        assert v.target_label == "cible ≥ 99 %"
        assert v.formatted.endswith(" %")

    async def test_combo_rows_one_per_x_with_series_and_colors(self):
        chart = VisualSpec(
            type="combo", source="ventes", x="semaine",
            series=[
                SeriesSpec(key="ventes_tot", color_rules=[ColorRule(op=">=", value=500, color="good"), ColorRule(op="<", value=300, color="#ff0000")]),
                SeriesSpec(key="prevu_tot", mark="line"),
                SeriesSpec(key="ventes", label="Ventes (champ)"),
            ],
            reference_lines=[ReferenceLine(value=400, label="Cible"), ReferenceLine(measure="ventes_tot", label="Total")],
        )
        res = await compute(build_dashboard({"c": chart}))
        comp = res.components["c"]
        assert len(comp.rows) == 26
        xs = [r["x"] for r in comp.rows]
        assert xs == sorted(xs)
        df = weekly_sales()
        first = df[df.semaine == pd.Timestamp(xs[0])]
        assert comp.rows[0]["ventes_tot"] == first.ventes.sum()
        assert comp.rows[0]["ventes"] == first.ventes.sum()  # field series = SUM(field)
        for r in comp.rows:
            expected = "good" if r["ventes_tot"] >= 500 else ("#ff0000" if r["ventes_tot"] < 300 else None)
            assert r.get("_color_ventes_tot") == expected
            assert "_color_prevu_tot" not in r
        assert comp.reference_lines[0]["value"] == 400 and comp.reference_lines[0]["label"] == "Cible"
        assert comp.reference_lines[1]["value"] == sum(r["ventes_tot"] for r in comp.rows)

    async def test_sort_and_top_n(self):
        bar = VisualSpec(
            type="hbar", source="ventes", x="famille", series=[SeriesSpec(key="ventes_tot")],
            sort="value_desc", top_n=3,
        )
        pie = VisualSpec(type="pie", source="ventes", x="famille", series=[SeriesSpec(key="ventes_tot")])
        res = await compute(build_dashboard({"b": bar, "p": pie}))
        rows = res.components["b"].rows
        assert len(rows) == 3
        assert [r["x"] for r in rows] == ["Echo", "Delta", "Charlie"]
        assert [r["ventes_tot"] for r in rows] == sorted((r["ventes_tot"] for r in rows), reverse=True)
        pie_rows = res.components["p"].rows
        assert set(pie_rows[0]) == {"x", "value"}
        assert [r["x"] for r in pie_rows][0] == "Echo"

    async def test_waterfall_is_consistent(self):
        df = pd.DataFrame({"semaine": [pd.Timestamp("2024-01-01")], "ini": [100.0], "entrees": [30.0], "sorties": [20.0]})
        source = dataset_source(
            measures=[
                MeasureSpec(name="ini", label="Stock initial", expr="SUM(ini)"),
                MeasureSpec(name="entrees", label="Entrées", expr="SUM(entrees)"),
                MeasureSpec(name="sorties", label="Sorties", expr="SUM(sorties)"),
                MeasureSpec(name="fin", label="Stock final", expr="SUM(ini) + SUM(entrees) - SUM(sorties)"),
                MeasureSpec(name="faux", label="Faux total", expr="SUM(ini)"),
            ]
        )

        def wf(end):
            return VisualSpec(
                type="waterfall", source="ventes",
                steps=[
                    WaterfallStep(label="Initial", measure="ini", kind="start"),
                    WaterfallStep(label="Entrées", measure="entrees", kind="increase"),
                    WaterfallStep(label="Sorties", measure="sorties", kind="decrease"),
                    WaterfallStep(label="Final", measure=end, kind="end", color="#123456"),
                ],
            )

        res = await compute(build_dashboard({"w": wf("fin"), "bad": wf("faux")}, source=source, filters=[]), df)
        rows = res.components["w"].rows
        assert [(r["kind"], r["value"], r["base"]) for r in rows] == [
            ("start", 100.0, 0.0), ("increase", 30.0, 100.0), ("decrease", 20.0, 110.0), ("end", 110.0, 0.0),
        ]
        assert rows[3]["color"] == "#123456" and rows[1]["color"] == "good" and rows[2]["color"] == "bad"
        assert res.components["w"].meta.warnings == []
        assert res.components["bad"].meta.warnings  # end total differs from the cumulated steps

    async def test_text_template(self):
        text = VisualSpec(
            type="text", source="ventes",
            text="Ventes {{ventes_tot}} ({{ventes_tot|integer}}), NSL {{nsl}} / {{nsl|decimal2}} — {{filters.famille}} — "
                 "du {{period.from}} au {{period.to}} (dernier réel {{period.last}})",
        )
        df = weekly_sales()
        last = df.semaine.max()
        res = await compute(build_dashboard({"t": text}), famille="Alpha")
        out = res.components["t"].text
        alpha = df[(df.famille == "Alpha") & (df.semaine > last - pd.Timedelta(weeks=26))]
        total = alpha.ventes.sum()
        assert f"Ventes {format_value(total, 'compact')} ({format_value(total, 'integer')})" in out
        assert "— Alpha —" in out
        first = format_date_fr(alpha.semaine.min())
        assert f"du {first} au {format_date_fr(last)} (dernier réel {format_date_fr(last)})" in out
        assert "{{" not in out

    async def test_unknown_template_measure_fails_only_that_component(self):
        bad = VisualSpec(type="text", source="ventes", text="{{inconnue}}")
        res = await compute(build_dashboard({"bad": bad, "ok": kpi("n")}))
        assert "inconnue" in res.components["bad"].error
        assert res.components["ok"].error is None

    async def test_note_badges_from_source_fields(self):
        note = VisualSpec(type="note", source="ventes", text="**Méthode** : prévision naïve")
        explicit = VisualSpec(type="note", text="x", badges=["forecast"])
        res = await compute(build_dashboard({"n": note, "e": explicit}))
        assert res.components["n"].text == "**Méthode** : prévision naïve"
        assert [b.value for b in res.components["n"].badges] == ["real", "simulated", "forecast"]
        assert [b.value for b in res.components["e"].badges] == ["forecast"]

    async def test_table_total_row_and_status(self):
        table = VisualSpec(
            type="table", source="ventes", x="famille",
            columns=[ColumnSpec(key="famille"), ColumnSpec(key="ventes_tot"), ColumnSpec(key="nsl", use_thresholds=True)],
            total_row=True, sort="value_desc",
        )
        res = await compute(build_dashboard({"t": table}))
        rows = res.components["t"].rows
        assert rows[-1]["famille"] == "Toutes" and rows[-1]["_total"] is True
        assert rows[-1]["ventes_tot"] == pytest.approx(sum(r["ventes_tot"] for r in rows[:-1]))
        assert rows[0]["ventes_tot"] >= rows[1]["ventes_tot"]

    async def test_component_subset_and_unknown_source_error(self):
        dash = build_dashboard({"a": kpi("n"), "b": VisualSpec(type="kpi", source="nope", kpi=KpiSpec(measure="n"))})
        res = await compute(dash)
        assert res.components["a"].error is None
        assert "nope" in res.components["b"].error
        only = await compute(dash, components=["a"])
        assert list(only.components) == ["a"]

    async def test_meta_traces_measure_provenance(self):
        res = await compute(build_dashboard({"k": kpi("nsl"), "v": kpi("ventes_tot")}))
        nsl = res.components["k"].meta
        assert [p.value for p in nsl.provenance] == ["real", "simulated"]
        assert nsl.measures[0].name == "nsl" and "demande_non_servie" in nsl.measures[0].expr
        assert res.components["v"].meta.provenance[0].value == "real"
        assert res.components["v"].meta.row_count == 26 * len(FAMILIES)


# ---------------------------------------------------------------------------
# Sources: derive, join, aggregate, sql, provenance
# ---------------------------------------------------------------------------


class TestSources:
    async def test_derive_inherits_and_overrides_provenance(self):
        source = dataset_source(
            transforms=[
                NaiveForecastStep(date="semaine", value="ventes", by=["famille"]),
                InventorySimStep(date="semaine", by=["famille"]),
                DeriveStep(column="ecart", expr="stock_fin - ventes"),  # simulated input
                DeriveStep(column="double", expr="ventes * 2"),  # real input
                DeriveStep(column="manuel", expr="ventes * 2", provenance="forecast"),  # explicit
                DeriveStep(column="plan", expr="prevision * 2"),  # forecast input
            ]
        )
        res = await compute(build_dashboard({"k": kpi("n")}, source=source))
        prov = {f.name: f.provenance.value for f in res.sources["ventes"].fields}
        assert prov["ecart"] == "simulated"
        assert prov["double"] == "real"
        assert prov["manuel"] == "forecast"
        assert prov["plan"] == "forecast"
        assert prov["prevision"] == "forecast" and prov["stock_fin"] == "simulated" and prov["ventes"] == "real"

    async def test_declared_fields_override_inferred(self):
        from apowerb.bi.spec.models import FieldSpec

        source = dataset_source().model_copy(
            update={"fields": [FieldSpec(name="ventes", label="Ventes hebdo", format="integer", provenance="simulated")]}
        )
        res = await compute(build_dashboard({"k": kpi("n")}, source=source))
        f = {f.name: f for f in res.sources["ventes"].fields}["ventes"]
        assert (f.label, f.format, f.provenance.value) == ("Ventes hebdo", "integer", "simulated")

    async def test_inferred_types_and_roles(self):
        res = await compute(build_dashboard({"k": kpi("n")}, source=dataset_source()))
        fields = {f.name: f for f in res.sources["ventes"].fields}
        assert (fields["semaine"].type, fields["semaine"].role) == ("date", "date")
        assert (fields["famille"].type, fields["famille"].role) == ("string", "dimension")
        assert (fields["ventes"].type, fields["ventes"].role) == ("number", "measure")

    async def test_filter_aggregate_sql_and_child_source(self):
        from apowerb.bi.spec.models import AggregateStep, FilterStep, SourceSpec, SqlStep

        parent = dataset_source("brut", transforms=[FilterStep(where="famille IN ('Alpha', 'Bravo')")])
        child = SourceSpec(
            name="par_famille", kind="source", parent="brut",
            transforms=[
                AggregateStep(group_by=["famille"], aggregates={"total": "SUM(ventes)"}),
                SqlStep(query="SELECT famille, total, total * 2 AS double_total FROM input ORDER BY famille"),
            ],
            measures=[MeasureSpec(name="t", label="T", expr="SUM(double_total)")],
        )
        dash = build_dashboard({"k": VisualSpec(type="kpi", source="par_famille", kpi=KpiSpec(measure="t"))}, filters=[])
        dash = dash.model_copy(update={"sources": [parent, child]})
        df = weekly_sales()
        res = await compute(dash, df)
        expected = df[df.famille.isin(["Alpha", "Bravo"])].ventes.sum() * 2
        assert value_of(res, "k") == expected
        assert res.sources["par_famille"].row_count == 2

    async def test_join_brings_columns_of_another_source(self):
        from apowerb.bi.spec.models import AggregateStep, JoinStep

        cost = dataset_source("cout", transforms=[AggregateStep(group_by=["famille"], aggregates={"prix_moyen": "AVG(prix)"})])
        main = dataset_source(
            "ventes",
            transforms=[JoinStep(source="cout", on=["famille"], columns=["prix_moyen"])],
            measures=[MeasureSpec(name="ca", label="CA", expr="SUM(ventes * prix_moyen)")],
        )
        dash = build_dashboard({"k": kpi("ca")}, filters=[]).model_copy(update={"sources": [main, cost]})
        df = weekly_sales()
        res = await compute(dash, df)
        assert value_of(res, "k") == pytest.approx((df.ventes * df.prix).sum())

    async def test_source_cycle_reports_errors_without_crashing(self):
        from apowerb.bi.spec.models import SourceSpec

        a = SourceSpec(name="a", kind="source", parent="b")
        b = SourceSpec(name="b", kind="source", parent="a")
        dash = build_dashboard({"k": VisualSpec(type="kpi", source="a", kpi=KpiSpec(measure="m"))}, filters=[])
        dash = dash.model_copy(update={"sources": [a, b]})
        res = await compute(dash)
        assert "indisponible" in res.components["k"].error
        assert "Cycle" in res.sources["a"].warnings[0]

    async def test_dataset_read_is_scoped_to_the_dashboard_owner(self):
        frame = weekly_sales(weeks=4)
        resolver = AsyncMock(return_value="bi/data/org/proj/data/abc.csv")
        csv_bytes = frame.to_csv(index=False).encode()
        with patch("apowerb.bi.data.csv_executor._resolve_s3_key", resolver), patch(
            "apowerb.bi.data._bi_storage.read_file", return_value=csv_bytes
        ):
            res = await engine.compute_dashboard(
                build_dashboard({"n": kpi("n")}, filters=[]), DashboardDataRequest(), owner="boss@example.com"
            )
        resolver.assert_awaited_once()
        assert resolver.await_args.args[1] == "boss@example.com"
        assert value_of(res, "n") == 4 * len(FAMILIES)

    async def test_unknown_dataset_fails_the_source_only(self):
        with patch("apowerb.bi.data.csv_executor._resolve_s3_key", AsyncMock(return_value=None)):
            res = await engine.compute_dashboard(
                build_dashboard({"n": kpi("n"), "t": VisualSpec(type="note", text="ok")}, filters=[]),
                DashboardDataRequest(), owner=OWNER,
            )
        assert "introuvable" in res.components["n"].error
        assert res.components["t"].error is None

    def test_parse_dataset_bytes_normalises_headers_dates_and_decimals(self):
        from apowerb.bi.spec.frames import coerce_frame

        raw = "Date semaine;Famille produit;Quantité vendue;Prix\n05/01/2024;A;10;1,5\n12/01/2024;B;20;2,5\n".encode()
        df = coerce_frame(sources.parse_dataset_bytes(raw, key="x.csv"))
        assert list(df.columns) == ["date_semaine", "famille_produit", "quantite_vendue", "prix"]
        assert str(df.date_semaine.dtype).startswith("datetime64")
        assert df.date_semaine.iloc[0] == pd.Timestamp("2024-01-05")
        assert df.prix.tolist() == [1.5, 2.5]


# ---------------------------------------------------------------------------
# SQL guard and sandbox
# ---------------------------------------------------------------------------

FORBIDDEN = [
    "(SELECT COUNT(*) FROM read_csv('/etc/passwd'))",
    "SUM(ventes); DROP TABLE frame",
    "SUM(ventes) ; SELECT 1",
    "ATTACH 'x.db'",
    "(SELECT 1 FROM 'secret.csv')",
    "(SELECT 1 FROM \"secret.parquet\")",
    "COPY frame TO 'x.csv'",
    "INSTALL httpfs",
    "LOAD httpfs",
    "PRAGMA database_list",
    "SET enable_external_access = true",
    "CALL pragma_version()",
    "EXPORT DATABASE 'x'",
    "IMPORT DATABASE 'x'",
    "CREATE TABLE t AS SELECT 1",
    "DROP TABLE frame",
    "INSERT INTO frame VALUES (1)",
    "UPDATE frame SET ventes = 0",
    "DELETE FROM frame",
    "(SELECT * FROM glob('/*'))",
    "(SELECT * FROM read_parquet('x.parquet'))",
    "(SELECT * FROM read_json('x.json'))",
    "(SELECT 1 FROM frame, '/etc/hosts')",
    "(SELECT 1 FROM frame f, \"/etc/hosts.csv\")",
    "(SELECT COUNT(*) FROM duckdb_settings())",
    "(SELECT COUNT(*) FROM duckdb_extensions())",
    "(SELECT COUNT(*) FROM duckdb_secrets())",
    "(SELECT COUNT(*) FROM duckdb_tables)",
    "SUM(ventes",
    "SUM(ventes))",
    "",
]


class TestSqlGuard:
    @pytest.mark.parametrize("expr", FORBIDDEN)
    def test_forbidden_expressions_are_refused(self, expr):
        with pytest.raises(SpecError):
            sql_guard.check_expression(expr, "Test")

    @pytest.mark.parametrize(
        "expr",
        [
            "SUM(ventes)",
            "SUM(CASE WHEN famille = 'drop' THEN ventes END)",
            "SUM(CASE WHEN note = 'set; load' THEN 1 END)",
            'SUM("set")',
            "(SELECT MAX(semaine) FROM frame)",
            "(SELECT MAX(semaine) FROM frame WHERE famille IN ('a', 'b') AND COALESCE(note, 'x') = 'x')",
            "COALESCE(MAX(note), 'n/a')",
            "1 - SUM(demande_non_servie) / NULLIF(SUM(ventes), 0)",
        ],
    )
    def test_legitimate_expressions_pass(self, expr):
        assert sql_guard.check_expression(expr, "Test")

    def test_comments_are_stripped(self):
        assert sql_guard.check_expression("SUM(ventes) -- ; DROP TABLE x", "T") == "SUM(ventes)"
        cleaned = sql_guard.check_expression("SUM(/* ; DROP */ ventes)", "T")
        assert ";" not in cleaned and "DROP" not in cleaned and "ventes" in cleaned

    def test_query_must_be_a_single_select(self):
        assert sql_guard.check_query("SELECT * FROM input;", "Q") == "SELECT * FROM input"
        for bad in ("DROP TABLE input", "SELECT 1; SELECT 2", "SELECT * FROM read_csv('/etc/passwd')", "ATTACH 'x'"):
            with pytest.raises(SpecError):
                sql_guard.check_query(bad, "Q")

    @pytest.mark.parametrize("expr", FORBIDDEN[:12])
    async def test_forbidden_measure_only_breaks_its_component(self, expr):
        source = dataset_source(measures=[MeasureSpec(name="piege", label="P", expr=expr or "1"), MeasureSpec(name="ok", label="OK", expr="COUNT(*)")])
        dash = build_dashboard({"bad": kpi("piege"), "good": kpi("ok")}, source=source, filters=[])
        res = await compute(dash)
        assert res.components["bad"].error
        assert res.components["good"].error is None

    async def test_forbidden_transform_expression_makes_the_source_unavailable(self):
        source = dataset_source(transforms=[DeriveStep(column="x", expr="(SELECT COUNT(*) FROM read_csv('/etc/passwd'))")])
        res = await compute(build_dashboard({"k": kpi("n")}, source=source, filters=[]))
        assert "indisponible" in res.components["k"].error
        assert "read_csv" in res.sources["ventes"].warnings[0].lower()

    def test_sandbox_blocks_file_access_even_without_the_guard(self):
        with duck.SqlRunner() as runner:
            for query in (
                "SELECT * FROM read_csv('/etc/passwd')",
                "SELECT * FROM '/etc/passwd'",
                "SELECT * FROM glob('/*')",
                "SELECT * FROM read_text('/etc/hosts')",
            ):
                with pytest.raises(SpecError):
                    runner.run(query)
            with pytest.raises(SpecError):
                runner.run("ATTACH '/tmp/evil.db' AS evil")
            with pytest.raises(SpecError):
                runner.run("COPY (SELECT 1) TO '/tmp/evil.csv'")
            with pytest.raises(SpecError):
                runner.run("SET enable_external_access = true")

    async def test_file_read_through_an_expression_is_impossible_with_the_guard_bypassed(self, monkeypatch):
        monkeypatch.setattr(sql_guard, "check_expression", lambda expr, what="": expr)
        source = dataset_source(measures=[MeasureSpec(name="piege", label="P", expr="(SELECT COUNT(*) FROM read_csv('/etc/passwd'))")])
        res = await compute(build_dashboard({"k": kpi("piege")}, source=source, filters=[]))
        assert res.components["k"].error
        assert res.components["k"].value is None

    async def test_query_timeout(self, monkeypatch):
        monkeypatch.setattr(duck, "QUERY_TIMEOUT_S", 0.3)
        slow = "(SELECT SUM(a.ventes * b.ventes * c.ventes) FROM frame a, frame b, frame c, frame d)"
        source = dataset_source(measures=[MeasureSpec(name="lent", label="L", expr=slow), MeasureSpec(name="ok", label="OK", expr="COUNT(*)")])
        started = time.monotonic()
        res = await compute(build_dashboard({"slow": kpi("lent"), "fast": kpi("ok")}, source=source, filters=[]))
        assert time.monotonic() - started < 5
        assert "Délai" in res.components["slow"].error
        assert res.components["fast"].error is None

    async def test_global_budget_exhausted_fails_remaining_components_but_responds(self, monkeypatch):
        monkeypatch.setattr(engine, "COMPUTE_BUDGET_S", 0.4)
        slow = "(SELECT SUM(a.ventes * b.ventes * c.ventes) FROM frame a, frame b, frame c, frame d)"
        source = dataset_source(measures=[MeasureSpec(name="lent", label="L", expr=slow), MeasureSpec(name="ok", label="OK", expr="COUNT(*)")])
        visuals = {"s1": kpi("lent"), "s2": kpi("lent"), "s3": kpi("ok"), "s4": kpi("ok")}
        started = time.monotonic()
        res = await compute(build_dashboard(visuals, source=source, filters=[]))
        assert time.monotonic() - started < 5
        assert set(res.components) == set(visuals)
        assert "Délai" in res.components["s1"].error
        assert all("Délai" in res.components[c].error for c in ("s2", "s3", "s4"))

    @pytest.mark.parametrize(
        "expr",
        [
            "LENGTH(repeat('x', 2000000000))",
            "LENGTH(lpad('x', 2000000000, 'y'))",
            "(SELECT len(range(2000000000)))",
            "(SELECT COUNT(*) FROM generate_series(1, 2000000000))",
            "(WITH RECURSIVE t(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM t) SELECT MAX(n) FROM t)",
        ],
    )
    async def test_memory_amplifiers_are_refused_before_running(self, expr):
        with pytest.raises(SpecError):
            sql_guard.check_expression(expr, "Test")
        source = dataset_source(measures=[MeasureSpec(name="bombe", label="B", expr=expr), MeasureSpec(name="ok", label="OK", expr="COUNT(*)")])
        started = time.monotonic()
        res = await compute(build_dashboard({"b": kpi("bombe"), "g": kpi("ok")}, source=source, filters=[]))
        assert time.monotonic() - started < 3
        assert res.components["b"].error and res.components["g"].error is None

    async def test_row_cap(self, monkeypatch):
        monkeypatch.setattr(duck, "MAX_RESULT_ROWS", 100)
        from apowerb.bi.spec.models import SqlStep

        source = dataset_source(transforms=[SqlStep(query="SELECT * FROM input")])
        res = await compute(build_dashboard({"k": kpi("n")}, source=source, filters=[]))
        assert "trop volumineux" in res.sources["ventes"].warnings[0]


# ---------------------------------------------------------------------------
# th2forecast
# ---------------------------------------------------------------------------


def _engine_response(df, horizon=3):
    series = []
    for fam, grp in df.groupby("famille"):
        last = grp.semaine.max()
        series.append(
            {
                "group": fam,
                "model": "prophet",
                "forecast": [
                    {"date": (last + pd.Timedelta(weeks=i + 1)).strftime("%Y-%m-%d"), "value": 1000.0 + i}
                    for i in range(horizon)
                ],
            }
        )
    return {"status": "success", "series": series}


def forecast_source():
    from apowerb.bi.spec.models import ForecastStep

    return dataset_source(
        transforms=[ForecastStep(date="semaine", value="ventes", by=["famille"], horizon=3, output="prevision_moteur")],
        measures=[
            MeasureSpec(name="ventes_tot", label="Ventes", expr="SUM(ventes)"),
            MeasureSpec(name="moteur", label="Moteur", expr="SUM(prevision_moteur)"),
            MeasureSpec(name="n", label="Lignes", expr="COUNT(*)"),
            MeasureSpec(name="nb_semaines", label="Semaines", expr="COUNT(DISTINCT semaine)"),
        ],
    )


class TestForecastStep:
    async def test_future_rows_are_appended_with_forecast_provenance(self):
        df = weekly_sales(weeks=60)
        calls = []

        def fake_engine(body):
            calls.append(body)
            return _engine_response(df)

        with patch.object(transforms, "_call_forecast_engine", fake_engine):
            dash = build_dashboard(
                {"k": kpi("moteur"), "n": kpi("n"), "w": kpi("nb_semaines"), "v": kpi("ventes_tot")},
                source=forecast_source(), filters=default_filters_no_period(),
            )
            res = await compute(dash, df)
        assert value_of(res, "n") == len(df) + 3 * len(FAMILIES)
        assert value_of(res, "k") == sum(1000.0 + i for i in range(3)) * len(FAMILIES)
        assert value_of(res, "v") == df.ventes.sum()  # future rows carry no real value
        prov = {f.name: f.provenance.value for f in res.sources["ventes"].fields}
        assert prov["prevision_moteur"] == "forecast"
        assert res.sources["ventes"].last_date == df.semaine.max().strftime("%Y-%m-%d")
        assert len(calls) == 1 and calls[0]["group_var"] == "famille" and calls[0]["horizon"] == 3
        assert res.components["k"].meta.provenance[0].value == "forecast"

    async def test_engine_output_is_cached_by_data_and_parameters(self):
        df = weekly_sales(weeks=30)
        calls = []

        def fake_engine(body):
            calls.append(1)
            return _engine_response(df)

        from apowerb.bi.spec.models import ForecastStep

        step = ForecastStep(date="semaine", value="ventes", by=["famille"], horizon=3)
        with patch.object(transforms, "_call_forecast_engine", fake_engine):
            transforms._forecast_frame(df, step)
            transforms._forecast_frame(df, step)
            assert len(calls) == 1
            transforms._forecast_frame(df, step.model_copy(update={"horizon": 3, "frequency": "week"}))
            transforms._forecast_frame(df.assign(ventes=df.ventes + 1), step)
            assert len(calls) == 3

    @pytest.mark.parametrize("failure", ["engine_down", "not_configured"])
    async def test_failure_keeps_the_source_and_other_components(self, failure):
        df = weekly_sales(weeks=40)
        dash = build_dashboard(
            {"fc": kpi("moteur"), "ok": kpi("ventes_tot"), "n": kpi("n")},
            source=forecast_source(), filters=default_filters_no_period(),
        )
        if failure == "engine_down":
            ctx = patch.object(transforms, "_call_forecast_engine", side_effect=ForecastUnavailable("service coupé"))
        else:
            from apowerb.integrations.th2forecast_client import Th2forecastNotConfigured

            ctx = patch("apowerb.integrations.th2forecast_client.Th2forecastClient", side_effect=Th2forecastNotConfigured())
        with ctx:
            res = await compute(dash, df)
        assert value_of(res, "ok") == df.ventes.sum()
        assert value_of(res, "n") == len(df)  # no future rows, source still usable
        assert res.components["fc"].value.value is None
        assert res.components["fc"].meta.warnings and "Prévision indisponible" in res.components["fc"].meta.warnings[0]
        assert res.components["ok"].meta.warnings == []
        assert "Prévision indisponible" in res.sources["ventes"].warnings[0]


def default_filters_no_period():
    return [FilterSpec(id="famille", label="Famille", field="famille", type="select")]


# ---------------------------------------------------------------------------
# Cache and performance
# ---------------------------------------------------------------------------


class TestCacheAndPerformance:
    async def test_source_is_materialised_once_across_calls_and_components(self):
        df = weekly_sales(weeks=20)
        loader = AsyncMock(return_value=df)
        dash = build_dashboard({f"k{i}": kpi("n") for i in range(5)})
        with patch.object(sources, "load_dataset", loader):
            await engine.compute_dashboard(dash, DashboardDataRequest(), owner=OWNER)
            await engine.compute_dashboard(dash, DashboardDataRequest(filters={"famille": "Alpha"}), owner=OWNER)
        assert loader.await_count == 1

    async def test_changed_definition_invalidates_the_cached_source(self):
        df = weekly_sales(weeks=20)
        loader = AsyncMock(return_value=df)
        with patch.object(sources, "load_dataset", loader):
            await engine.compute_dashboard(build_dashboard({"k": kpi("n")}), DashboardDataRequest(), owner=OWNER)
            changed = build_dashboard({"k": kpi("n")}, source=pipeline_source().model_copy(update={"label": "autre"}))
            await engine.compute_dashboard(changed, DashboardDataRequest(), owner=OWNER)
        assert loader.await_count == 2

    async def test_compute_time_on_3000_rows_and_15_components(self, capsys):
        df = weekly_sales(weeks=300, families=[f"F{i}" for i in range(10)])
        assert len(df) == 3000
        visuals = {
            "k1": kpi("ventes_tot", compare="previous_period"),
            "k2": kpi("nsl", compare="previous_point"),
            "k3": kpi("stock_tot"),
            "k4": kpi("n"),
            "k5": kpi("prevu_tot"),
            "c1": combo("ventes_tot", "prevu_tot"),
            "c2": combo("stock_tot", "ventes_tot"),
            "c3": VisualSpec(type="bar", source="ventes", x="famille", series=[SeriesSpec(key="ventes_tot")], sort="value_desc", top_n=5),
            "c4": VisualSpec(type="pie", source="ventes", x="famille", series=[SeriesSpec(key="ventes_tot")]),
            "c5": VisualSpec(type="line", source="ventes", x="semaine", series=[SeriesSpec(key="nsl", use_thresholds=True)]),
            "t1": VisualSpec(type="table", source="ventes", x="famille", columns=[ColumnSpec(key="famille"), ColumnSpec(key="ventes_tot"), ColumnSpec(key="nsl", use_thresholds=True)], total_row=True),
            "w1": VisualSpec(type="waterfall", source="ventes", steps=[WaterfallStep(label="a", measure="ventes_tot", kind="start"), WaterfallStep(label="b", measure="ventes_tot", kind="end")]),
            "x1": VisualSpec(type="text", source="ventes", text="{{ventes_tot}} {{nsl}} {{period.last}}"),
            "x2": VisualSpec(type="text", source="ventes", text="{{n}} lignes, {{filters.famille}}"),
            "n1": VisualSpec(type="note", source="ventes", text="note"),
        }
        assert len(visuals) == 15
        dash = build_dashboard(visuals)
        loader = AsyncMock(return_value=df)
        timings = []
        with patch.object(sources, "load_dataset", loader):
            started = time.perf_counter()
            await engine.compute_dashboard(dash, DashboardDataRequest(), owner=OWNER)
            cold = time.perf_counter() - started
            for i in range(5):
                # a different filter value each time: the response cache cannot help
                started = time.perf_counter()
                res = await engine.compute_dashboard(
                    dash, DashboardDataRequest(filters={"famille": f"F{i}"}), owner=OWNER
                )
                timings.append(time.perf_counter() - started)
        assert all(c.error is None for c in res.components.values())
        warm = sorted(timings)[len(timings) // 2]
        with capsys.disabled():
            print(f"\n[perf] 3000 rows, 15 components: cold={cold * 1000:.0f} ms, warm median={warm * 1000:.0f} ms")
        assert warm < 0.5  # target 0.3 s; slack for loaded CI machines
