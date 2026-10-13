"""Dashboard filters that reshape visuals without dropping rows: time grain
(bucket the date axis) and aggregation (function applied to a raw column),
plus the day / year / year-to-date / month-to-date period presets."""

from __future__ import annotations

from unittest.mock import patch

import pandas as pd
import pytest

from apowerb.bi.spec import engine, sources
from apowerb.bi.spec.errors import SpecError
from apowerb.bi.spec.models import DashboardDataRequest, FilterSpec, MeasureSpec, VisualSpec
from apowerb.bi.spec.validate import validate_spec
from tests.helpers.bi_spec_data import OWNER, build_dashboard, combo, dataset_source, kpi, weekly_sales

MEASURES = [
    MeasureSpec(name="ventes_tot", label="Ventes", expr="SUM(ventes)"),
    MeasureSpec(name="ventes_moy_ligne", label="Moyenne", expr="SUM(ventes) / COUNT(*)"),
    MeasureSpec(name="nb_semaines", label="Semaines", expr="COUNT(DISTINCT semaine)"),
    MeasureSpec(name="n", label="Lignes", expr="COUNT(*)"),
]
GRAIN = FilterSpec(id="grain", label="Grain", field="semaine", type="time_grain")
AGG = FilterSpec(id="agg", label="Agrégation", field="ventes", type="aggregation")
PERIOD = FilterSpec(id="periode", label="Période", field="semaine", type="relative_period", default="all")


@pytest.fixture(autouse=True)
def _fresh_caches():
    engine.clear_caches()
    yield
    engine.clear_caches()


def dash(visuals, filters):
    return build_dashboard(visuals, source=dataset_source(measures=MEASURES), filters=filters)


async def compute(dashboard, df=None, **filters):
    frame = weekly_sales() if df is None else df

    async def fake_load(dataset_id, owner, sheet=None):
        return frame

    with patch.object(sources, "load_dataset", fake_load):
        return await engine.compute_dashboard(dashboard, DashboardDataRequest(filters=filters), owner=OWNER)


def rows(res, cid):
    comp = res.components[cid]
    assert comp.error is None, comp.error
    return comp.rows


def value(res, cid):
    comp = res.components[cid]
    assert comp.error is None, comp.error
    return comp.value.value


class TestTimeGrain:
    async def test_month_grain_buckets_the_date_axis(self):
        df = weekly_sales()
        res = await compute(dash({"c": combo("ventes")}, [PERIOD, GRAIN]), grain="month")
        out = rows(res, "c")
        months = df.groupby(df.semaine.dt.to_period("M")).ventes.sum()
        assert len(out) == len(months)
        assert out[0]["x"] == "2023-01-01"
        assert out[0]["ventes"] == pytest.approx(months.iloc[0])
        assert res.filters["grain"].label == "Par mois"

    async def test_year_grain_and_default(self):
        grain = GRAIN.model_copy(update={"default": "year"})
        res = await compute(dash({"c": combo("ventes")}, [PERIOD, grain]))
        assert [r["x"] for r in rows(res, "c")] == ["2023-01-01", "2024-01-01", "2025-01-01"]

    async def test_no_grain_keeps_raw_dates(self):
        res = await compute(dash({"c": combo("ventes")}, [PERIOD, GRAIN]))
        assert len(rows(res, "c")) == 130
        assert res.filters["grain"].label == "Détail"

    async def test_grain_drops_no_rows_and_ignores_other_axes(self):
        res = await compute(
            dash({"k": kpi("n"), "f": combo("ventes", x="famille")}, [PERIOD, GRAIN]), grain="quarter"
        )
        assert value(res, "k") == 130 * 5
        assert len(rows(res, "f")) == 5
        assert "grain" not in res.components["k"].meta.filters_applied

    async def test_ignore_filters_keeps_raw_dates(self):
        res = await compute(dash({"c": combo("ventes", ignore_filters=["grain"])}, [PERIOD, GRAIN]), grain="year")
        assert len(rows(res, "c")) == 130

    async def test_unknown_grain_is_a_spec_error(self):
        with pytest.raises(SpecError):
            await compute(dash({"c": combo("ventes")}, [PERIOD, GRAIN]), grain="decade")


class TestAggregation:
    async def test_avg_on_raw_series(self):
        df = weekly_sales()
        res = await compute(dash({"c": combo("ventes", x="famille")}, [PERIOD, AGG]), agg="avg")
        got = {r["x"]: r["ventes"] for r in rows(res, "c")}
        for fam, mean in df.groupby("famille").ventes.mean().items():
            assert got[fam] == pytest.approx(mean)
        assert res.filters["agg"].label == "Moyenne"

    async def test_plain_sum_measure_follows_the_function(self):
        df = weekly_sales()
        res = await compute(dash({"k": kpi("ventes_tot"), "m": kpi("ventes_moy_ligne")}, [PERIOD, AGG]), agg="max")
        assert value(res, "k") == pytest.approx(df.ventes.max())
        assert value(res, "m") == pytest.approx(df.ventes.sum() / len(df))

    async def test_count_and_default_sum(self):
        df = weekly_sales()
        res = await compute(dash({"k": kpi("ventes_tot")}, [PERIOD, AGG]), agg="count")
        assert value(res, "k") == len(df)
        res = await compute(dash({"k": kpi("ventes_tot")}, [PERIOD, AGG]))
        assert value(res, "k") == pytest.approx(df.ventes.sum())

    async def test_table_raw_column_follows_the_function(self):
        df = weekly_sales()
        table = VisualSpec(type="table", source="ventes", x="famille", series=[], columns=[{"key": "famille"}, {"key": "ventes"}])
        res = await compute(dash({"t": table}, [PERIOD, AGG]), agg="min")
        got = {r["famille"]: r["ventes"] for r in rows(res, "t")}
        assert got["Alpha"] == pytest.approx(df[df.famille == "Alpha"].ventes.min())

    async def test_unknown_function_is_a_spec_error(self):
        with pytest.raises(SpecError):
            await compute(dash({"k": kpi("ventes_tot")}, [PERIOD, AGG]), agg="median")


class TestPeriodPresets:
    @pytest.mark.parametrize(
        "preset,offset,label",
        [
            ("7d", pd.Timedelta(days=7), "7 derniers jours"),
            ("90d", pd.Timedelta(days=90), "90 derniers jours"),
            ("1y", pd.DateOffset(years=1), "1 dernière année"),
            ("2y", pd.DateOffset(years=2), "2 dernières années"),
            ("45d", pd.Timedelta(days=45), "45 derniers jours"),
        ],
    )
    async def test_day_and_year_presets(self, preset, offset, label):
        df = weekly_sales()
        last = df.semaine.max()
        expected = df[df.semaine > last - offset].semaine.nunique()
        res = await compute(dash({"k": kpi("nb_semaines")}, [PERIOD]), periode=preset)
        assert value(res, "k") == expected
        assert res.filters["periode"].label == label

    @pytest.mark.parametrize(
        "preset,start,label",
        [
            ("ytd", lambda last: pd.Timestamp(last.year, 1, 1), "Depuis le début de l'année"),
            ("mtd", lambda last: pd.Timestamp(last.year, last.month, 1), "Depuis le début du mois"),
        ],
    )
    async def test_to_date_presets(self, preset, start, label):
        df = weekly_sales()
        last = df.semaine.max()
        expected = df[df.semaine >= start(last)].semaine.nunique()
        res = await compute(dash({"k": kpi("nb_semaines")}, [PERIOD]), periode=preset)
        assert value(res, "k") == expected
        assert res.filters["periode"].label == label


class TestValidation:
    def errors(self, filters):
        return validate_spec(dash({"c": combo("ventes")}, [PERIOD, *filters]))

    def test_valid_defaults(self):
        assert self.errors([GRAIN.model_copy(update={"default": "week"}), AGG.model_copy(update={"default": "avg"})]) == []
        period = PERIOD.model_copy(update={"id": "p2", "default": "ytd"})
        assert self.errors([period]) == []

    def test_bad_defaults(self):
        errs = self.errors([GRAIN.model_copy(update={"default": "decade"}), AGG.model_copy(update={"default": "median"})])
        assert any("grain" in e and "decade" in e for e in errs)
        assert any("agg" in e and "median" in e for e in errs)
