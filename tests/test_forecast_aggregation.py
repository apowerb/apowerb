"""Prévision sur des lignes au grain transaction (plusieurs lignes par date).

th2forecast refuse une série où une date revient plusieurs fois (« Doublons
de dates ») : le CSV de commandes de la démo BI (une ligne par commande) ne
pouvait donc pas être prévu. Le cœur agrège la mesure par date — ou par
période quand ``frequency`` est donnée — avant l'appel, en somme, et le dit
dans ``warnings``.

Le moteur simulé ici rejette les doublons comme le vrai : sans agrégation,
ces tests échouent.
"""
from __future__ import annotations

import importlib.util
from collections import Counter
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from apowerb.integrations.th2forecast_client import Th2forecastAPIError
from apowerb.schema import forecast_schema
from apowerb.tools_store.portfolio import api_call, bi_datasets

OWNER = "me@example.com"
_REPLAY = Path(__file__).resolve().parents[1] / "bench" / "bi_tokens" / "replay.py"


def _write_orders_csv(path: Path) -> None:
    """Le CSV du banc BI (13 554 commandes, graine 1421), généré par le banc lui-même."""
    spec = importlib.util.spec_from_file_location("bi_tokens_replay", _REPLAY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.write_orders_csv(path)


def _engine(body: dict) -> dict:
    """th2forecast simulé : refuse une (groupe, date) répétée, sinon prévoit."""
    group_var = body.get("group_var")
    keys = Counter((row.get(group_var) if group_var else None, row[body["date_var"]]) for row in body["data"])
    if any(n > 1 for n in keys.values()):
        raise Th2forecastAPIError(400, {"status": "error", "errors": [
            {"field": "data", "message": "Doublons de dates dans la série."},
        ]})
    groups = sorted({g for g, _ in keys}, key=str)
    return {
        "status": "success",
        "frequency": body.get("frequency") or "day",
        "warnings": [],
        "series": [{
            "group": g, "model": "prophet", "reliability": "good", "beats_baseline": True,
            "metrics": {}, "history": [], "warnings": [],
            "forecast": [{"date": "2026-10-01", "value": 1.0, "lower_80": 0.5, "upper_80": 1.5}],
        } for g in groups],
    }


@pytest.fixture()
def engine():
    with patch.object(api_call, "Th2forecastClient") as ctor:
        ctor.return_value.forecast.side_effect = _engine
        yield ctor.return_value.forecast


@pytest.fixture(scope="module")
def orders_root(tmp_path_factory):
    root = tmp_path_factory.mktemp("uploads")
    _write_orders_csv(root / "agent1" / "online_store_orders.csv")
    return root


@pytest.fixture()
def orders_upload(orders_root, monkeypatch):
    root = orders_root
    monkeypatch.setattr(bi_datasets, "agent_upload_dir", lambda i: root / f"agent{i}")
    monkeypatch.setattr(bi_datasets, "_is_s3_storage", lambda: False)
    monkeypatch.setenv("ROOT_AGENT_ID", "1")
    monkeypatch.setenv("AGENT_OWNER", OWNER)
    monkeypatch.setattr(bi_datasets, "_agent_record_owner", lambda agent_id: OWNER)
    return "online_store_orders.csv"


class TestOrderGrainForecast:
    def test_monthly_revenue_forecast_on_order_rows_succeeds(self, engine, orders_upload):
        result = api_call.tool_thaink2_forecast(
            date_var="order_date", target_var="order_amount", horizon=3,
            frequency="month", file_id=orders_upload,
        )
        assert result["status"] == "success", result
        sent = engine.call_args.args[0]["data"]
        # 13 554 commandes du 01/10/2025 au 30/09/2026 -> 12 mois, au 1er du mois.
        assert [r["order_date"] for r in sent] == [
            f"{y}-{m:02d}-01" for y, m in [(2025, 10), (2025, 11), (2025, 12)] + [(2026, k) for k in range(1, 10)]
        ]

    def test_the_monthly_values_are_the_sums_of_the_orders(self, engine, orders_upload):
        loaded = bi_datasets._load_uploaded_file_rows(orders_upload, OWNER, 100_000)
        expected: Counter = Counter()
        for row in loaded["rows"]:
            expected[str(row["order_date"])[:7]] += float(row["order_amount"])
        api_call.tool_thaink2_forecast(
            date_var="order_date", target_var="order_amount", horizon=3,
            frequency="month", file_id=orders_upload,
        )
        sent = engine.call_args.args[0]["data"]
        assert {r["order_date"][:7]: r["order_amount"] for r in sent} == pytest.approx(dict(expected))
        assert sum(r["order_amount"] for r in sent) == pytest.approx(sum(expected.values()))

    def test_the_aggregation_is_stated_in_the_warnings(self, engine, orders_upload):
        result = api_call.tool_thaink2_forecast(
            date_var="order_date", target_var="order_amount", horizon=3,
            frequency="month", file_id=orders_upload,
        )
        note = " ".join(result["warnings"])
        assert "order_amount" in note and "somme" in note and "mois" in note
        assert "13554" in note and "12" in note


class TestAggregateForecastRows:
    def test_unique_dates_are_left_untouched(self):
        rows = [{"d": "2024-01-01", "v": 1}, {"d": "2024-02-01", "v": 2}]
        assert forecast_schema.aggregate_forecast_rows(rows, "d", "v", None, "month") == (rows, None)

    def test_without_frequency_duplicates_are_summed_per_exact_date(self):
        rows = [{"d": "2024-01-01", "v": 1}, {"d": "2024-01-01", "v": 2}, {"d": "2024-01-02", "v": 4}]
        out, note = forecast_schema.aggregate_forecast_rows(rows, "d", "v", None, None)
        assert out == [{"d": "2024-01-01", "v": 3.0}, {"d": "2024-01-02", "v": 4.0}]
        assert note and "date" in note

    def test_groups_are_aggregated_separately(self):
        rows = [
            {"d": "2024-01-03", "v": 1, "g": "A"}, {"d": "2024-01-03", "v": 2, "g": "A"},
            {"d": "2024-01-20", "v": 4, "g": "A"}, {"d": "2024-01-05", "v": 10, "g": "B"},
        ]
        out, _ = forecast_schema.aggregate_forecast_rows(rows, "d", "v", "g", "month")
        assert out == [{"d": "2024-01-01", "v": 7.0, "g": "A"}, {"d": "2024-01-01", "v": 10.0, "g": "B"}]

    def test_unreadable_rows_are_left_out_and_counted(self):
        rows = [{"d": "2024-01-01", "v": 1}, {"d": "2024-01-01", "v": ""}, {"d": "2024-01-01", "v": 2}]
        out, note = forecast_schema.aggregate_forecast_rows(rows, "d", "v", None, None)
        assert out == [{"d": "2024-01-01", "v": 3.0}]
        assert "1 ligne(s)" in note

    def test_mixed_utc_offsets_keep_their_local_day(self):
        rows = [
            {"d": "2024-03-30T00:30:00+01:00", "v": 1}, {"d": "2024-03-30T12:00:00+01:00", "v": 2},
            {"d": "2024-04-02T00:30:00+02:00", "v": 4},
        ]
        out, _ = forecast_schema.aggregate_forecast_rows(rows, "d", "v", None, None)
        assert out == [{"d": "2024-03-30", "v": 3.0}, {"d": "2024-04-02", "v": 4.0}]

    def test_missing_group_stays_null(self):
        rows = [{"d": "2024-01-01", "v": 1, "g": None}, {"d": "2024-01-01", "v": 2, "g": None}]
        out, _ = forecast_schema.aggregate_forecast_rows(rows, "d", "v", "g", None)
        assert out == [{"d": "2024-01-01", "v": 3.0, "g": None}]

    def test_week_buckets_start_on_monday(self):
        rows = [{"d": "2024-01-03", "v": 1}, {"d": "2024-01-03", "v": 1}, {"d": "2024-01-07", "v": 2}]  # mer., dim.
        out, _ = forecast_schema.aggregate_forecast_rows(rows, "d", "v", None, "week")
        assert out == [{"d": "2024-01-01", "v": 4.0}]

    def test_hierarchy_columns_are_kept_as_keys(self):
        rows = [{"d": "2024-01-03", "v": 1, "g": "A", "r": "N"}, {"d": "2024-01-03", "v": 2, "g": "A", "r": "N"}]
        out, _ = forecast_schema.aggregate_forecast_rows(rows, "d", "v", "g", "month", ["r"])
        assert out == [{"d": "2024-01-01", "v": 3.0, "g": "A", "r": "N"}]


class TestWidgetRedisplay:
    """Le widget réaffiché poste les lignes brutes de sa source à
    POST /api/v1/forecast : la même agrégation doit s'y appliquer."""

    def test_router_aggregates_order_rows(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        import apowerb.routers.forecast as forecast_router
        from apowerb.auth.dependencies import get_current_user

        app = FastAPI()
        app.include_router(forecast_router.router, prefix="/api/v1")
        app.dependency_overrides[get_current_user] = lambda: MagicMock(email=OWNER, user_id=1)
        body = {
            "data": [
                {"order_date": "2024-01-03", "order_amount": 10},
                {"order_date": "2024-01-03", "order_amount": 5},
                {"order_date": "2024-02-11", "order_amount": 7},
            ],
            "date_var": "order_date", "target_var": "order_amount",
            "horizon": 1, "frequency": "month",
        }
        with patch.object(forecast_router, "Th2forecastClient") as ctor:
            ctor.return_value.forecast.side_effect = _engine
            resp = TestClient(app).post("/api/v1/forecast", json=body)
        assert resp.status_code == 200, resp.text
        sent = ctor.return_value.forecast.call_args.args[0]["data"]
        assert sent == [
            {"order_date": "2024-01-01", "order_amount": 15.0},
            {"order_date": "2024-02-01", "order_amount": 7.0},
        ]
        assert any("order_amount" in w for w in resp.json()["warnings"])
