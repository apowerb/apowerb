"""Tests de apowerb.bi.forecast_tracking — fonctions pures du suivi en boucle
fermée (contrat étape 5 §3). Aucune I/O, aucune route ici : voir
tests/test_forecast_loop_router.py pour l'orchestration HTTP.

Les actuels viennent de la RÉPONSE du moteur (`series[].history`), jamais de
`data` brut : th2forecast y régularise les dates (ISO) et stringifie
`group` — un test dédié couvre le cas où `data` porte un format différent
pour vérifier qu'il n'entre pour rien dans le calcul.
"""
from __future__ import annotations

from apowerb.bi.forecast_tracking import compute_config_hash, compute_tracking, prunable_snapshot_payload

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


def _series(group, forecast, *, level=None, history=None):
    return {"group": group, "level": level, "model": "prophet", "history": history or [], "forecast": forecast}


def _hist(*pairs):
    """pairs : (date, value) -> [{"date":..., "value":...}, ...]"""
    return [{"date": d, "value": v} for d, v in pairs]


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


class TestPrunableSnapshotPayload:
    def test_keeps_only_group_level_model_forecast(self):
        result = {
            "status": "success",
            "warnings": ["bruit"],
            "series": [
                {
                    "group": "A",
                    "level": "bottom",
                    "model": "prophet",
                    "metrics": {"mase": 0.5},
                    "history": _hist(("2024-01-01", 10)),
                    "forecast": [{"date": "2024-02-01", "value": 10, "lower_80": 8, "upper_80": 12}],
                    "warnings": [],
                }
            ],
        }
        pruned = prunable_snapshot_payload(result)
        assert pruned == {
            "series": [
                {
                    "group": "A",
                    "level": "bottom",
                    "model": "prophet",
                    "forecast": [{"date": "2024-02-01", "value": 10, "lower_80": 8, "upper_80": 12}],
                }
            ]
        }

    def test_level_defaults_to_none_when_absent(self):
        result = {"series": [{"group": "A", "model": "prophet", "forecast": []}]}
        pruned = prunable_snapshot_payload(result)
        assert pruned["series"][0]["level"] is None


class TestComputeTrackingEmptyCases:
    def test_no_snapshots_gives_zero_points(self):
        series = [_series("A", [], history=_hist(("2024-01-01", 10)))]
        out = compute_tracking(series=series, snapshots=[], config_hash="h1")
        assert out == {"points": 0, "since": None, "coverage": {}, "mase": None, "breaches": [], "latest_breach": False}

    def test_snapshot_with_different_config_hash_is_ignored(self):
        snap = _snapshot("OTHER", "2024-01-01", [_series("A", [{"date": "2024-02-01", "value": 10, "lower_80": 5, "upper_80": 15}])])
        series = [_series("A", [], history=_hist(("2024-02-01", 10)))]
        out = compute_tracking(series=series, snapshots=[snap], config_hash="h1")
        assert out["points"] == 0

    def test_snapshot_with_no_prior_date_is_ignored(self):
        """history_end >= date : rien à comparer (le point est dans l'historique d'entraînement de ce snapshot, pas un réel futur)."""
        snap = _snapshot("h1", "2024-03-01", [_series("A", [{"date": "2024-02-01", "value": 10, "lower_80": 5, "upper_80": 15}])])
        series = [_series("A", [], history=_hist(("2024-02-01", 10)))]
        out = compute_tracking(series=series, snapshots=[snap], config_hash="h1")
        assert out["points"] == 0


class TestActualsComeFromEngineHistoryNotRawData:
    def test_raw_request_data_is_never_consulted(self):
        """`compute_tracking` ne prend même pas `data` en paramètre : un format de
        date non-ISO ou un groupe numérique dans la requête brute ne peut pas
        casser le rapprochement, puisque seule `series[].history` (régularisée
        par le moteur) est utilisée."""
        snap = _snapshot("h1", "2024-01-01", [_series("A", [{"date": "2024-02-01", "value": 100, "lower_80": 90, "upper_80": 110}])])
        # L'historique moteur est bien ISO et le groupe une chaîne, même si la
        # requête d'origine avait pu envoyer tout autre chose (non représenté
        # ici : cette fonction ne voit jamais `data`).
        series = [_series("A", [], history=_hist(("2024-02-01", 95)))]
        out = compute_tracking(series=series, snapshots=[snap], config_hash="h1")
        assert out["points"] == 1


class TestComparisonSelection:
    def test_picks_the_most_recent_snapshot_before_the_date(self):
        """Deux instantanés valides pour la même date réelle : le plus récent (history_end le plus grand) gagne."""
        old = _snapshot("h1", "2024-01-01", [_series("A", [{"date": "2024-03-01", "value": 100, "lower_80": 90, "upper_80": 110}])])
        recent = _snapshot("h1", "2024-02-01", [_series("A", [{"date": "2024-03-01", "value": 50, "lower_80": 40, "upper_80": 60}])])
        series = [_series("A", [], history=_hist(("2024-03-01", 55)))]
        out = compute_tracking(series=series, snapshots=[old, recent], config_hash="h1")
        assert out["points"] == 1
        # 55 est dans [40, 60] (le snapshot récent) mais hors [90, 110] (l'ancien) :
        # si l'ancien avait été choisi par erreur, on aurait une rupture.
        assert out["breaches"] == []

    def test_no_forecast_point_for_that_series_key_is_skipped(self):
        snap = _snapshot("h1", "2024-01-01", [_series("A", [{"date": "2024-02-01", "value": 10, "lower_80": 5, "upper_80": 15}])])
        series = [_series("B", [], history=_hist(("2024-02-01", 10)))]  # groupe B, seul A prévu
        out = compute_tracking(series=series, snapshots=[snap], config_hash="h1")
        assert out["points"] == 0

    def test_same_group_different_level_does_not_mix(self):
        """Un agrégat "Total" et une série du bas peuvent partager le même group
        sous des level différents (hiérarchie) : ils ne doivent pas se
        rapprocher l'un de l'autre."""
        snap = _snapshot(
            "h1", "2024-01-01",
            [
                _series("Nord", [{"date": "2024-02-01", "value": 1000, "lower_80": 900, "upper_80": 1100}], level="region"),
                _series("Nord", [{"date": "2024-02-01", "value": 10, "lower_80": 5, "upper_80": 15}], level="bottom"),
            ],
        )
        series = [_series("Nord", [], level="bottom", history=_hist(("2024-02-01", 12)))]
        out = compute_tracking(series=series, snapshots=[snap], config_hash="h1")
        assert out["points"] == 1
        assert out["breaches"] == []  # 12 est dans [5, 15] (bottom), pas comparé à [900, 1100] (region)


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
        history = _hist(
            ("2023-12-01", 100),  # avant history_end du snapshot : pas comparé
            ("2024-02-01", 95),   # dans les deux bandes
            ("2024-03-01", 130),  # hors des deux bandes
        )
        series = [_series("A", [], history=history)]
        out = compute_tracking(series=series, snapshots=[snap], config_hash="h1")
        assert out["points"] == 2
        assert out["since"] == "2024-02-01"
        assert out["coverage"] == {"80": 0.5, "95": 0.5}

    def test_breach_reports_widest_band_when_outside_both(self):
        snap = self._snapshot_two_points()
        series = [_series("A", [], history=_hist(("2024-02-01", 130)))]
        out = compute_tracking(series=series, snapshots=[snap], config_hash="h1")
        assert len(out["breaches"]) == 1
        breach = out["breaches"][0]
        assert breach["level"] == "95" and breach["direction"] == "above"
        assert breach["group"] == "A" and breach["actual"] == 130

    def test_breach_reports_narrowest_band_when_only_that_one_is_missed(self):
        snap = self._snapshot_two_points()
        series = [_series("A", [], history=_hist(("2024-02-01", 115)))]  # hors 80 (90-110), dans 95 (80-120)
        out = compute_tracking(series=series, snapshots=[snap], config_hash="h1")
        assert len(out["breaches"]) == 1
        assert out["breaches"][0]["level"] == "80"

    def test_no_breach_when_inside_every_band(self):
        snap = self._snapshot_two_points()
        series = [_series("A", [], history=_hist(("2024-02-01", 100)))]
        out = compute_tracking(series=series, snapshots=[snap], config_hash="h1")
        assert out["breaches"] == []
        assert out["latest_breach"] is False

    def test_latest_breach_true_only_when_the_most_recent_actual_date_breaches(self):
        snap = self._snapshot_two_points()
        history = _hist(
            ("2024-02-01", 999),  # rupture mais pas la plus récente
            ("2024-03-01", 100),  # la plus récente, pas de rupture
        )
        series = [_series("A", [], history=history)]
        out = compute_tracking(series=series, snapshots=[snap], config_hash="h1")
        assert out["latest_breach"] is False
        assert len(out["breaches"]) == 1

    def test_breaches_most_recent_first_and_capped_at_20(self):
        forecast = []
        base_year, base_month = 2024, 2
        for i in range(25):
            month = base_month + i
            year = base_year + (month - 1) // 12
            month = (month - 1) % 12 + 1
            forecast.append({"date": f"{year}-{month:02d}-01", "value": 0, "lower_80": -1, "upper_80": 1})
        snap = _snapshot("h1", "2024-01-01", [_series("A", forecast)])
        history = _hist(*[(p["date"], 100) for p in forecast])  # tous hors bande [-1,1]
        series = [_series("A", [], history=history)]
        out = compute_tracking(series=series, snapshots=[snap], config_hash="h1")
        assert out["points"] == 25
        assert len(out["breaches"]) == 20
        dates = [b["date"] for b in out["breaches"]]
        assert dates == sorted(dates, reverse=True)

    def test_mase_is_error_over_mean_absolute_diff_of_history(self):
        snap = self._snapshot_two_points()
        history = _hist(
            ("2024-01-01", 100),
            ("2024-01-15", 110),  # |diff| = 10
            ("2024-02-01", 120),  # prévu 100 -> erreur 20 ; |diff| = 10
        )
        series = [_series("A", [], history=history)]
        out = compute_tracking(series=series, snapshots=[snap], config_hash="h1")
        # erreur (20) / diff moyen de l'historique (10) = 2.0
        assert out["mase"] == 2.0

    def test_mase_is_none_when_history_has_no_variation(self):
        """Historique constant (diffs tous nuls) : dénominateur nul, MASE non calculable."""
        snap = self._snapshot_two_points()
        history = _hist(
            ("2024-01-01", 100),
            ("2024-01-15", 100),
            ("2024-02-01", 100),  # comparé au snapshot (value=100) : erreur nulle aussi
        )
        series = [_series("A", [], history=history)]
        out = compute_tracking(series=series, snapshots=[snap], config_hash="h1")
        assert out["mase"] is None


class TestConfigHashExcludesScenarios:
    def test_adding_a_scenario_does_not_change_the_hash(self):
        """Contrat étape 7 §2d : les scénarios ne modifient pas la prévision
        de base (tâches séparées côté moteur), donc `scenarios` sort de
        `config_hash` — ajouter un scénario ne remet pas le suivi à zéro."""
        with_scenario = {**BASE_PAYLOAD, "scenarios": [{"name": "Promo +15%"}]}
        assert compute_config_hash(with_scenario) == compute_config_hash(BASE_PAYLOAD)


class TestPrunableSnapshotPayloadKeepsScenarios:
    def test_scenarios_are_kept_when_present(self):
        """Contrat etape 7 SS2d : l'instantane elague garde aussi les
        scenarios (name + forecast date/value) pour permettre le calcul de
        tracking.adjustments plus tard."""
        result = {
            "status": "success",
            "series": [
                {
                    "group": "A", "level": None, "model": "prophet",
                    "history": _hist(("2024-01-01", 10)),
                    "forecast": [{"date": "2024-02-01", "value": 10}],
                    "scenarios": [
                        {"name": "Promo +15%", "forecast": [
                            {"date": "2024-02-01", "value": 11.5, "lower_80": 10, "upper_80": 13},
                        ]},
                    ],
                }
            ],
        }
        pruned = prunable_snapshot_payload(result)
        assert pruned["series"][0]["scenarios"] == [
            {"name": "Promo +15%", "forecast": [{"date": "2024-02-01", "value": 11.5}]}
        ]

    def test_scenarios_key_absent_when_engine_did_not_send_any(self):
        result = {"status": "success", "series": [
            {"group": "A", "level": None, "model": "prophet", "history": [], "forecast": []},
        ]}
        pruned = prunable_snapshot_payload(result)
        assert "scenarios" not in pruned["series"][0]


class TestBreachExplanation:
    """Contrat etape 7 SS2b : chaque rupture (tracking.breaches[i]) gagne une
    explanation {kind, magnitude, consecutive, event}. Priorite : event >
    common_shock > level_shift (consecutive >= 2) > spike."""

    def _snap(self, points_by_group):
        series = [_series(g, pts) for g, pts in points_by_group.items()]
        return _snapshot("h1", "2024-01-01", series)

    def test_spike_is_the_default_for_an_isolated_breach(self):
        snap = self._snap({"A": [
            {"date": "2024-02-01", "value": 100, "lower_80": 90, "upper_80": 110},
        ]})
        series = [_series("A", [], history=_hist(("2024-02-01", 130)))]
        out = compute_tracking(series=series, snapshots=[snap], config_hash="h1")
        exp = out["breaches"][0]["explanation"]
        assert exp["kind"] == "spike"
        assert exp["consecutive"] == 1
        assert exp["event"] is None

    def test_magnitude_is_overshoot_over_half_band_width(self):
        # upper=110, value=100 -> demi-largeur = 10 ; actual=130 -> depassement 20 -> 2.0
        snap = self._snap({"A": [
            {"date": "2024-02-01", "value": 100, "lower_80": 90, "upper_80": 110},
        ]})
        series = [_series("A", [], history=_hist(("2024-02-01", 130)))]
        out = compute_tracking(series=series, snapshots=[snap], config_hash="h1")
        assert out["breaches"][0]["explanation"]["magnitude"] == 2.0

    def test_level_shift_when_two_consecutive_breaches_same_direction(self):
        snap = self._snap({"A": [
            {"date": "2024-02-01", "value": 100, "lower_80": 90, "upper_80": 110},
            {"date": "2024-03-01", "value": 100, "lower_80": 90, "upper_80": 110},
        ]})
        history = _hist(("2024-02-01", 130), ("2024-03-01", 130))
        series = [_series("A", [], history=history)]
        out = compute_tracking(series=series, snapshots=[snap], config_hash="h1")
        latest = next(b for b in out["breaches"] if b["date"] == "2024-03-01")
        assert latest["explanation"]["kind"] == "level_shift"
        assert latest["explanation"]["consecutive"] == 2

    def test_common_shock_when_two_other_series_breach_same_date_and_direction(self):
        snap = self._snap({
            "A": [{"date": "2024-02-01", "value": 100, "lower_80": 90, "upper_80": 110}],
            "B": [{"date": "2024-02-01", "value": 100, "lower_80": 90, "upper_80": 110}],
            "C": [{"date": "2024-02-01", "value": 100, "lower_80": 90, "upper_80": 110}],
        })
        series = [
            _series("A", [], history=_hist(("2024-02-01", 130))),
            _series("B", [], history=_hist(("2024-02-01", 130))),
            _series("C", [], history=_hist(("2024-02-01", 130))),
        ]
        out = compute_tracking(series=series, snapshots=[snap], config_hash="h1")
        for b in out["breaches"]:
            assert b["explanation"]["kind"] == "common_shock"

    def test_event_wins_over_everything_when_it_covers_the_date_and_group(self):
        snap = self._snap({
            "A": [{"date": "2024-02-01", "value": 100, "lower_80": 90, "upper_80": 110}],
            "B": [{"date": "2024-02-01", "value": 100, "lower_80": 90, "upper_80": 110}],
            "C": [{"date": "2024-02-01", "value": 100, "lower_80": 90, "upper_80": 110}],
        })
        series = [
            _series("A", [], history=_hist(("2024-02-01", 130))),
            _series("B", [], history=_hist(("2024-02-01", 130))),
            _series("C", [], history=_hist(("2024-02-01", 130))),
        ]
        events = [{"name": "Promo de decembre", "ranges": [{"start": "2024-01-15", "end": "2024-02-15"}], "groups": ["A"]}]
        out = compute_tracking(series=series, snapshots=[snap], config_hash="h1", events=events)
        by_group = {b["group"]: b for b in out["breaches"]}
        assert by_group["A"]["explanation"]["kind"] == "event"
        assert by_group["A"]["explanation"]["event"] == "Promo de decembre"
        assert by_group["B"]["explanation"]["kind"] == "common_shock"


class TestTrackingAdjustments:
    """Contrat etape 7 SS2d : tracking.adjustments compare, par nom de
    scenario, l'erreur de base a l'erreur avec le scenario, sur les memes
    dates comparables que le suivi."""

    def _snap_with_scenario(self):
        return _snapshot("h1", "2024-01-01", [
            {
                "group": "A", "level": None, "model": "prophet", "history": [],
                "forecast": [
                    {"date": "2024-02-01", "value": 100, "lower_80": 90, "upper_80": 110},
                    {"date": "2024-03-01", "value": 100, "lower_80": 90, "upper_80": 110},
                ],
                "scenarios": [
                    {"name": "Promo +15%", "forecast": [
                        {"date": "2024-02-01", "value": 115},
                        {"date": "2024-03-01", "value": 115},
                    ]},
                ],
            },
        ])

    def test_adjustments_reports_mae_base_and_mae_scenario(self):
        snap = self._snap_with_scenario()
        history = _hist(("2024-02-01", 118), ("2024-03-01", 112))
        series = [_series("A", [], history=history)]
        out = compute_tracking(series=series, snapshots=[snap], config_hash="h1")
        assert out["adjustments"] == [
            {"name": "Promo +15%", "points": 2, "mae_base": 15.0, "mae_scenario": 3.0},
        ]

    def test_adjustments_absent_when_no_scenario_in_any_snapshot(self):
        snap = _snapshot("h1", "2024-01-01", [_series("A", [
            {"date": "2024-02-01", "value": 100, "lower_80": 90, "upper_80": 110},
        ])])
        series = [_series("A", [], history=_hist(("2024-02-01", 100)))]
        out = compute_tracking(series=series, snapshots=[snap], config_hash="h1")
        assert "adjustments" not in out

    def test_adding_a_scenario_does_not_reset_points_or_breaches(self):
        """Regression contrat etape 7 SS2d : le suivi de base ne doit pas
        etre affecte par la presence de scenarios dans l'instantane."""
        snap = self._snap_with_scenario()
        history = _hist(("2024-02-01", 130))
        series = [_series("A", [], history=history)]
        out = compute_tracking(series=series, snapshots=[snap], config_hash="h1")
        assert out["points"] == 1
        assert len(out["breaches"]) == 1
