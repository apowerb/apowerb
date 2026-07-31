"""CORRECTION A — TDD RED : is_upstream_skip doit reconnaître status_final=='SKIPPED'.

Le recorder (agent10) peut émettre {"status_final":"SKIPPED",...} quand l'AR
est déjà enregistré ou hors-scope côté enregistrement.
is_upstream_skip ne reconnaissait pas ce cas → le notifier (agent11) s'exécutait
et pouvait envoyer un faux mail.

RED: ces tests ÉCHOUENT avant le fix (status_final pas encore reconnu).
"""
from __future__ import annotations

import json


def _ar_record_state(payload: dict) -> dict:
    return {"ar_record": json.dumps(payload)}


class TestArRecordSkippedRecognised:
    """is_upstream_skip doit reconnaitre status_final='SKIPPED' dans ar_record."""

    def test_status_final_skipped_returns_true(self):
        """Cas principal : recorder skipped → notifier ne doit pas s'exécuter."""
        from apowerb.core.agent_helpers.callbacks import is_upstream_skip

        state = _ar_record_state({
            "status_final": "SKIPPED",
            "commande_id": None,
            "lignes_inserees": 0,
            "erreurs": [],
        })
        assert is_upstream_skip(state, "ar_record") is True

    def test_status_final_skipped_minimal_returns_true(self):
        """Payload minimal valide avec status_final=SKIPPED."""
        from apowerb.core.agent_helpers.callbacks import is_upstream_skip

        state = _ar_record_state({"status_final": "SKIPPED"})
        assert is_upstream_skip(state, "ar_record") is True

    def test_status_final_ok_returns_false(self):
        """status_final=OK → notifier doit s'exécuter normalement."""
        from apowerb.core.agent_helpers.callbacks import is_upstream_skip

        state = _ar_record_state({
            "status_final": "OK",
            "commande_id": "CMD-001",
            "lignes_inserees": 3,
            "erreurs": [],
        })
        assert is_upstream_skip(state, "ar_record") is False

    def test_status_final_nok_returns_false(self):
        """status_final=NOK → notifier doit s'exécuter (signaler l'erreur)."""
        from apowerb.core.agent_helpers.callbacks import is_upstream_skip

        state = _ar_record_state({
            "status_final": "NOK",
            "commande_id": "CMD-001",
            "lignes_inserees": 0,
            "erreurs": ["INSERT failed"],
        })
        assert is_upstream_skip(state, "ar_record") is False


class TestNonRegressionAfterCorrA:
    """Non-régression : tous les cas existants inchangés après CORRECTION A."""

    def test_status_skip_still_true(self):
        from apowerb.core.agent_helpers.callbacks import is_upstream_skip
        state = {"ar_intake": json.dumps({"status": "skip", "raison": "non-AR"})}
        assert is_upstream_skip(state, "ar_intake") is True

    def test_out_of_scope_still_true(self):
        from apowerb.core.agent_helpers.callbacks import is_upstream_skip
        state = {"ar_match": json.dumps({"status": "out_of_scope", "diagnostic": "invalide"})}
        assert is_upstream_skip(state, "ar_match") is True

    def test_not_ar_still_true(self):
        from apowerb.core.agent_helpers.callbacks import is_upstream_skip
        state = {"ar_intake": json.dumps({"email_classification": "not_ar", "raison": "fac"})}
        assert is_upstream_skip(state, "ar_intake") is True

    def test_skipped_upstream_still_true(self):
        from apowerb.core.agent_helpers.callbacks import is_upstream_skip
        state = {"ar_match": json.dumps({"__skipped_upstream__": True})}
        assert is_upstream_skip(state, "ar_match") is True

    def test_matched_still_false(self):
        from apowerb.core.agent_helpers.callbacks import is_upstream_skip
        state = {"ar_match": json.dumps({"status": "matched", "po": {}, "lines": []})}
        assert is_upstream_skip(state, "ar_match") is False

    def test_non_rapproche_still_false(self):
        from apowerb.core.agent_helpers.callbacks import is_upstream_skip
        state = {"ar_match": json.dumps({"status": "non_rapproche", "po": None, "lines": []})}
        assert is_upstream_skip(state, "ar_match") is False

    def test_missing_key_still_false(self):
        from apowerb.core.agent_helpers.callbacks import is_upstream_skip
        assert is_upstream_skip({}, "ar_record") is False

    def test_error_sentinel_still_false(self):
        from apowerb.core.agent_helpers.callbacks import is_upstream_skip
        state = {"ar_intake": json.dumps({"__error__": "extract_failed"})}
        assert is_upstream_skip(state, "ar_intake") is False
