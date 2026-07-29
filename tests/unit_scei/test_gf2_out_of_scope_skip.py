"""GARDE-FOU #2 — TDD : is_upstream_skip doit reconnaître status=='out_of_scope'.

RED phase: ce test échoue AVANT le fix (out_of_scope n'est pas encore reconnu).
GREEN phase: après ajout de la condition dans is_upstream_skip.

Non-régression: skip / not_ar / __skipped_upstream__ / process / matched /
non_rapproche doivent garder leur comportement actuel.
"""
from __future__ import annotations

import json


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state(payload: dict) -> dict:
    return {"ar_match": json.dumps(payload)}


# ---------------------------------------------------------------------------
# RED : out_of_scope doit retourner True — va échouer avant le fix
# ---------------------------------------------------------------------------


class TestOutOfScopeIsSkip:
    """is_upstream_skip doit reconnaitre status='out_of_scope'."""

    def test_out_of_scope_returns_true(self):
        """CF principal: gate PMI écrit out_of_scope -> downstream doit skipper."""
        from th2agent.core.agent_helpers.callbacks import is_upstream_skip

        state = _state({"status": "out_of_scope", "diagnostic": "numéro invalide"})
        assert is_upstream_skip(state, "ar_match") is True

    def test_out_of_scope_with_extra_fields_returns_true(self):
        """out_of_scope avec po=null et lines=[] (format ARMatchPayload réel)."""
        from th2agent.core.agent_helpers.callbacks import is_upstream_skip

        state = _state({
            "status": "out_of_scope",
            "po": None,
            "lines": [],
            "diagnostic": "commande_number invalide: 'CF999'",
        })
        assert is_upstream_skip(state, "ar_match") is True


# ---------------------------------------------------------------------------
# Non-régression : comportements existants inchangés
# ---------------------------------------------------------------------------


class TestExistingBehaviourUnchanged:
    """Vérifie que le fix ne casse pas les cas existants."""

    def test_skip_status_still_returns_true(self):
        from th2agent.core.agent_helpers.callbacks import is_upstream_skip

        state = {"ar_intake": json.dumps({"status": "skip", "raison": "non-AR"})}
        assert is_upstream_skip(state, "ar_intake") is True

    def test_not_ar_classification_still_returns_true(self):
        from th2agent.core.agent_helpers.callbacks import is_upstream_skip

        state = {"ar_intake": json.dumps({
            "email_classification": "not_ar",
            "raison": "facture",
            "extraction_results": {},
        })}
        assert is_upstream_skip(state, "ar_intake") is True

    def test_skipped_upstream_still_returns_true(self):
        from th2agent.core.agent_helpers.callbacks import is_upstream_skip

        state = {"ar_match": json.dumps({"__skipped_upstream__": True, "reason": "..."})}
        assert is_upstream_skip(state, "ar_match") is True

    def test_process_status_still_returns_false(self):
        from th2agent.core.agent_helpers.callbacks import is_upstream_skip

        state = {"ar_intake": json.dumps({"status": "process"})}
        assert is_upstream_skip(state, "ar_intake") is False

    def test_matched_status_returns_false(self):
        from th2agent.core.agent_helpers.callbacks import is_upstream_skip

        state = _state({
            "status": "matched",
            "po": {"ecktsoc": "SC", "ecktnumero": "101090"},
            "lines": [],
            "diagnostic": "matched via ECKTNUMERO",
        })
        assert is_upstream_skip(state, "ar_match") is False

    def test_non_rapproche_status_returns_false(self):
        from th2agent.core.agent_helpers.callbacks import is_upstream_skip

        state = _state({
            "status": "non_rapproche",
            "po": None,
            "lines": [],
            "diagnostic": "PO non trouvé",
        })
        assert is_upstream_skip(state, "ar_match") is False

    def test_missing_key_returns_false(self):
        from th2agent.core.agent_helpers.callbacks import is_upstream_skip

        assert is_upstream_skip({}, "ar_intake") is False

    def test_error_sentinel_returns_false(self):
        from th2agent.core.agent_helpers.callbacks import is_upstream_skip

        state = {"ar_intake": json.dumps({"__error__": "extract_failed"})}
        assert is_upstream_skip(state, "ar_intake") is False
