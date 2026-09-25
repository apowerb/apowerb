"""Tests de apowerb.bi.forecast_tracking — fonctions pures du suivi en boucle
fermée (contrat étape 5 §3). Aucune I/O, aucune route ici : voir
tests/test_forecast_loop_router.py pour l'orchestration HTTP."""
from __future__ import annotations

from apowerb.bi.forecast_tracking import compute_config_hash, compute_tracking

BASE_PAYLOAD = {
    "date_var": "date",
    "target_var": "sales",
    "group_var": "store",
    "frequency": "month",
    "horizon": 3,
    "models": ["prophet"],
    "confidence_levels": [0.8, 0.95],
    "hierarchy": None,
    "reconciliation": None,
    "events": None,
    "scenarios": None,
}


def _snapshot(config_hash, history_end, series):
    return {"config_hash": config_hash, "history_end": history_end, "payload": {"series": series}}


def _series(group, forecast):
    return {"group": group, "model": "prophet", "forecast": forecast}


class TestConfigHash:
    def test_same_config_same_hash_regardless_of_key_order(self):
        a = compute_config_hash(BASE_PAYLOAD)
        b = compute_config_hash({k: BASE_PAYLOAD[k] for k in reversed(list(BASE_PAYLOAD))})
        assert a == b

    def test_data_does_not_affect_the_hash(self):
        with_data = {**BASE_PAYLOAD, "data": [{"date": "2024-01-01", "sales": 1}]}
        assert compute_config_hash(with_data) == compute_config_hash(BASE_PAYLOAD)

    def test_different_models_change_the_hash(self):
        other = {**BASE_PAYLOAD, "models": ["ets"]}
        assert compute_config_hash(other) != compute_config_hash(BASE_PAYLOAD)

    def test_different_hierarchy_changes_the_hash(self):
        other = {**BASE_PAYLOAD, "hierarchy": ["region"]}
        assert compute_config_hash(other) != compute_config_hash(BASE_PAYLOAD)


class TestComputeTrackingEmptyCases:
    def test_no_snapshots_gives_zero_points(self):
        out = compute_tracking(
            data=[{"date": "2024-01-01", "sales": 10, "store": "A"}],
            date_var="date", target_var="sales", group_var="store",
            snapshots=[], config_hash="h1",
        )
        assert out == {"points": 0, "since": None, "coverage": {}, "mase": None, "breaches": [], "latest_breach": False}

    def test_snapshot_with_different_config_hash_is_ignored(self):
        snap = _snapshot("OTHER", "2024-01-01", [_series("A", [{"date": "2024-02-01", "value": 10, "lower_80": 5, "upper_80": 15}])])
        out = compute_tracking(
            data=[{"date": "2024-02-01", "sales": 10, "store": "A"}],
            date_var="date", target_var="sales", group_var="store",
            snapshots=[snap], config_hash="h1",
        )
        assert out["points"] == 0

    def test_snapshot_with_no_prior_date_is_ignored(self):
        """history_end >= date : rien à comparer (le point est dans l'historique d'entraînement de ce snapshot, pas un réel futur)."""
        snap = _snapshot("h1", "2024-03-01", [_series("A", [{"date": "2024-02-01", "value": 10, "lower_80": 5, "upper_80": 15}])])
        out = compute_tracking(
            data=[{"date": "2024-02-01", "sales": 10, "store": "A"}],
            date_var="date", target_var="sales", group_var="store",
            snapshots=[snap], config_hash="h1",
        )
        assert out["points"] == 0


class TestComparisonSelection:
    def test_picks_the_most_recent_snapshot_before_the_date(self):
        """Deux instantanés valides pour la même date réelle : le plus récent (history_end le plus grand) gagne."""
        old = _snapshot("h1", "2024-01-01", [_series("A", [{"date": "2024-03-01", "value": 100, "lower_80": 90, "upper_80": 110}])])
        recent = _snapshot("h1", "2024-02-01", [_series("A", [{"date": "2024-03-01", "value": 50, "lower_80": 40, "upper_80": 60}])])
        out = compute_tracking(
            data=[{"date": "2024-03-01", "sales": 55, "store": "A"}],
            date_var="date", target_var="sales", group_var="store",
            snapshots=[old, recent], config_hash="h1",
        )
        assert out["points"] == 1
        # 55 est dans [40, 60] (le snapshot récent) mais hors [90, 110] (l'ancien) :
        # si l'ancien avait été choisi par erreur, on aurait une rupture.
        assert out["breaches"] == []

    def test_no_forecast_point_for_that_group_or_date_is_skipped(self):
        snap = _snapshot("h1", "2024-01-01", [_series("A", [{"date": "2024-02-01", "value": 10, "lower_80": 5, "upper_80": 15}])])
        out = compute_tracking(
            data=[{"date": "2024-02-01", "sales": 10, "store": "B"}],  # groupe B, seul A prévu
            date_var="date", target_var="sales", group_var="store",
            snapshots=[snap], config_hash="h1",
        )
        assert out["points"] == 0


class TestCoverageMaseBreaches:
    def _snapshot_two_points(self):
        return _snapshot(
            "h1", "2024-01-01",
            [_series("A", [
                {"date": "2024-02-01", "value": 100, "lower_80": 90, "upper_80": 110, "lower_95": 80, "upper_95": 120},
                {"date": "2024-03-01", "value": 100, "lower_80": 90, "upper_80": 110, "lower_95": 80, "upper_95": 120},
            ])],
        )

    def test_points_and_coverage_for_two_comparable_points(self):
        snap = self._snapshot_two_points()
        data = [
            {"date": "2023-12-01", "sales": 100, "store": "A"},  # historique (pas de comparaison, avant history_end)
            {"date": "2024-02-01", "sales": 95, "store": "A"},   # dans les deux bandes
            {"date": "2024-03-01", "sales": 130, "store": "A"},  # hors des deux bandes
        ]
        out = compute_tracking(
            data=data, date_var="date", target_var="sales", group_var="store",
            snapshots=[snap], config_hash="h1",
        )
        assert out["points"] == 2
        assert out["since"] == "2024-02-01"
        assert out["coverage"] == {"80": 0.5, "95": 0.5}

    def test_breach_reports_widest_band_when_outside_both(self):
        snap = self._snapshot_two_points()
        data = [{"date": "2024-02-01", "sales": 130, "store": "A"}]
        out = compute_tracking(
            data=data, date_var="date", target_var="sales", group_var="store",
            snapshots=[snap], config_hash="h1",
        )
        assert len(out["breaches"]) == 1
        breach = out["breaches"][0]
        assert breach["level"] == "95" and breach["direction"] == "above"
        assert breach["group"] == "A" and breach["actual"] == 130

    def test_breach_reports_narrowest_band_when_only_that_one_is_missed(self):
        snap = self._snapshot_two_points()
        data = [{"date": "2024-02-01", "sales": 115, "store": "A"}]  # hors 80 (90-110), dans 95 (80-120)
        out = compute_tracking(
            data=data, date_var="date", target_var="sales", group_var="store",
            snapshots=[snap], config_hash="h1",
        )
        assert len(out["breaches"]) == 1
        assert out["breaches"][0]["level"] == "80"

    def test_no_breach_when_inside_every_band(self):
        snap = self._snapshot_two_points()
        data = [{"date": "2024-02-01", "sales": 100, "store": "A"}]
        out = compute_tracking(
            data=data, date_var="date", target_var="sales", group_var="store",
            snapshots=[snap], config_hash="h1",
        )
        assert out["breaches"] == []
        assert out["latest_breach"] is False

    def test_latest_breach_true_only_when_the_most_recent_actual_date_breaches(self):
        snap = self._snapshot_two_points()
        data = [
            {"date": "2024-02-01", "sales": 999, "store": "A"},   # rupture mais pas la plus récente
            {"date": "2024-03-01", "sales": 100, "store": "A"},   # la plus récente, pas de rupture
        ]
        out = compute_tracking(
            data=data, date_var="date", target_var="sales", group_var="store",
            snapshots=[snap], config_hash="h1",
        )
        assert out["latest_breach"] is False
        assert len(out["breaches"]) == 1

    def test_breaches_most_recent_first_and_capped_at_20(self):
        forecast = [
            {"date": f"2024-{m:02d}-01", "value": 0, "lower_80": -1, "upper_80": 1}
            for m in range(2, 2 + 25)
        ]
        # dates au-delà de décembre : on reste sur des mois valides (2 à 12, puis 2025).
        forecast = []
        base_year, base_month = 2024, 2
        for i in range(25):
            month = base_month + i
            year = base_year + (month - 1) // 12
            month = (month - 1) % 12 + 1
            forecast.append({"date": f"{year}-{month:02d}-01", "value": 0, "lower_80": -1, "upper_80": 1})
        snap = _snapshot("h1", "2024-01-01", [_series("A", forecast)])
        data = [{"date": p["date"], "sales": 100, "store": "A"} for p in forecast]  # tous hors bande [-1,1]
        out = compute_tracking(
            data=data, date_var="date", target_var="sales", group_var="store",
            snapshots=[snap], config_hash="h1",
        )
        assert out["points"] == 25
        assert len(out["breaches"]) == 20
        dates = [b["date"] for b in out["breaches"]]
        assert dates == sorted(dates, reverse=True)

    def test_mase_is_error_over_mean_absolute_diff_of_history(self):
        snap = self._snapshot_two_points()
        data = [
            {"date": "2024-01-01", "sales": 100, "store": "A"},
            {"date": "2024-01-15", "sales": 110, "store": "A"},  # |diff| = 10
            {"date": "2024-02-01", "sales": 120, "store": "A"},  # prévu 100 -> erreur 20 ; |diff| = 10
        ]
        out = compute_tracking(
            data=data, date_var="date", target_var="sales", group_var="store",
            snapshots=[snap], config_hash="h1",
        )
        # erreur (20) / diff moyen de l'historique (10) = 2.0
        assert out["mase"] == 2.0

    def test_mase_is_none_when_history_has_no_variation(self):
        """Historique constant (diffs tous nuls) : dénominateur nul, MASE non calculable."""
        snap = self._snapshot_two_points()
        data = [
            {"date": "2024-01-01", "sales": 100, "store": "A"},
            {"date": "2024-01-15", "sales": 100, "store": "A"},
            {"date": "2024-02-01", "sales": 100, "store": "A"},  # comparé au snapshot (value=100) : erreur nulle aussi
        ]
        out = compute_tracking(
            data=data, date_var="date", target_var="sales", group_var="store",
            snapshots=[snap], config_hash="h1",
        )
        assert out["mase"] is None
