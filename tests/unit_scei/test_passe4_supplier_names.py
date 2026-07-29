"""Passe 4 — TDD RED : ar_fournisseur et pmi_fournisseur peuplés dans ARMatchPayload.

PROBLÈME : la gate build_supplier_mismatch_gate_callback lit ar_match.get("ar_fournisseur", "?")
et ar_match.get("pmi_fournisseur", "?"), mais _build_matched_result ne peuple pas ces champs.
L'alerte affiche toujours « AR=? / PMI=? ».

FIX attendu :
1. ARMatchPayload déclare ar_fournisseur: str | None = None et pmi_fournisseur: str | None = None.
2. _build_matched_result peuple ces champs quand supplier_mismatch=True.
3. La gate voit les vraies valeurs (pas « ? »).
"""
from __future__ import annotations

import json
import logging
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers partagés
# ---------------------------------------------------------------------------


def _header(ecctnom: str, ecktnumero: str = "100950") -> dict:
    return {
        "ECKTSOC": "SC",
        "ECKTNUMERO": ecktnumero,
        "ECKTINDICE": "A",
        "ECCTCODE": "SUPP",
        "ECCTNOM": ecctnom,
        "ECCTREFCDE": "REF001",
    }


def _make_sql_with_header(ecctnom: str, ecktnumero: str = "100950"):
    def run_sql(sql: str) -> dict:
        sql_upper = sql.strip().upper()
        if "ECOMFOU" in sql_upper and "LCOMFOU" not in sql_upper:
            return {"success": True, "data": [_header(ecctnom, ecktnumero)]}
        if "LCOMFOU" in sql_upper:
            return {"success": True, "data": []}
        return {"success": True, "data": []}
    return run_sql


def _intake(fournisseur_nom: str | None, num_sql: str = "100950") -> dict:
    extraction: dict = {
        "commande_number_display": f"CF{num_sql}",
        "commande_number_sql": num_sql,
        "lines": [],
    }
    if fournisseur_nom is not None:
        extraction["fournisseur_nom"] = fournisseur_nom
    return {"email_classification": "ar", "extraction_results": extraction}


def _make_gate_callback_context(ar_match_dict: dict) -> MagicMock:
    ctx = MagicMock()
    ctx.state = {"ar_match": json.dumps(ar_match_dict)}
    ctx.agent_name = "scei_ar_notifier"
    return ctx


# ---------------------------------------------------------------------------
# 1. ARMatchPayload : champs ar_fournisseur et pmi_fournisseur
# ---------------------------------------------------------------------------


class TestARMatchPayloadSupplierFields:
    """ARMatchPayload doit accepter ar_fournisseur et pmi_fournisseur."""

    def test_armatchpayload_valid_without_supplier_fields(self):
        """Retro-compatibilité : payload sans ar_fournisseur/pmi_fournisseur reste valide."""
        from th2customers.scei.schemas import ARMatchPayload

        payload = ARMatchPayload(
            status="matched",
            po=None,
            lines=[],
            diagnostic="ok",
            supplier_mismatch=False,
        )
        assert payload.ar_fournisseur is None
        assert payload.pmi_fournisseur is None

    def test_armatchpayload_valid_with_supplier_fields(self):
        """ARMatchPayload accepte ar_fournisseur et pmi_fournisseur non-None."""
        from th2customers.scei.schemas import ARMatchPayload

        payload = ARMatchPayload(
            status="matched",
            po=None,
            lines=[],
            diagnostic="mismatch",
            supplier_mismatch=True,
            ar_fournisseur="TILCO",
            pmi_fournisseur="WILLY LEISSNER",
        )
        assert payload.ar_fournisseur == "TILCO"
        assert payload.pmi_fournisseur == "WILLY LEISSNER"

    def test_armatchpayload_model_dump_includes_supplier_fields(self):
        """model_dump() inclut ar_fournisseur et pmi_fournisseur."""
        from th2customers.scei.schemas import ARMatchPayload

        payload = ARMatchPayload(
            status="matched",
            po=None,
            lines=[],
            diagnostic="mismatch",
            supplier_mismatch=True,
            ar_fournisseur="TILCO",
            pmi_fournisseur="WILLY LEISSNER",
        )
        d = payload.model_dump()
        assert d["ar_fournisseur"] == "TILCO"
        assert d["pmi_fournisseur"] == "WILLY LEISSNER"

    def test_armatchpayload_validate_dict_with_null_fields(self):
        """model_validate() d'un dict avec None reste valide."""
        from th2customers.scei.schemas import ARMatchPayload

        d = {
            "status": "non_rapproche",
            "po": None,
            "lines": [],
            "diagnostic": "not found",
            "supplier_mismatch": False,
            "ar_fournisseur": None,
            "pmi_fournisseur": None,
        }
        payload = ARMatchPayload.model_validate(d)
        assert payload.ar_fournisseur is None
        assert payload.pmi_fournisseur is None


# ---------------------------------------------------------------------------
# 2. match_ar_to_pmi : peuplement sur mismatch
# ---------------------------------------------------------------------------


class TestMatchArToPmiPopulatesSupplierNames:
    """match_ar_to_pmi doit peupler ar_fournisseur et pmi_fournisseur quand mismatch."""

    def test_mismatch_tilco_vs_willy_leissner_populates_names(self):
        """Cas réel CF100950 : ar_fournisseur='TILCO', pmi_fournisseur='WILLY LEISSNER'."""
        from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi

        intake = _intake(fournisseur_nom="TILCO", num_sql="100950")
        run_sql = _make_sql_with_header("WILLY LEISSNER", ecktnumero="100950")
        result = match_ar_to_pmi(intake, run_sql)

        assert result["status"] == "matched"
        assert result.get("supplier_mismatch") is True
        assert result.get("ar_fournisseur") == "TILCO", (
            f"ar_fournisseur attendu 'TILCO', got {result.get('ar_fournisseur')!r}"
        )
        assert result.get("pmi_fournisseur") == "WILLY LEISSNER", (
            f"pmi_fournisseur attendu 'WILLY LEISSNER', got {result.get('pmi_fournisseur')!r}"
        )

    def test_no_mismatch_fields_none_on_match(self):
        """Pas de mismatch : ar_fournisseur et pmi_fournisseur absents ou None."""
        from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi

        intake = _intake(fournisseur_nom="TE CONNECTIVITY", num_sql="101000")
        run_sql = _make_sql_with_header("TE CONNECTIVITY", ecktnumero="101000")
        result = match_ar_to_pmi(intake, run_sql)

        assert result["status"] == "matched"
        assert result.get("supplier_mismatch") is False
        # Les champs peuvent être None ou absents — pas de valeurs textuelles erronées
        assert result.get("ar_fournisseur") in (None, "TE CONNECTIVITY"), (
            "Pas de mismatch : ar_fournisseur peut être None ou la vraie valeur"
        )

    def test_no_mismatch_gate_does_not_see_question_marks(self):
        """Pas de mismatch : la gate ne doit PAS voir '?' si on lisait ar_fournisseur."""
        from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi

        intake = _intake(fournisseur_nom="TE CONNECTIVITY", num_sql="101000")
        run_sql = _make_sql_with_header("TE CONNECTIVITY", ecktnumero="101000")
        result = match_ar_to_pmi(intake, run_sql)

        # La gate lit .get("ar_fournisseur", "?") — si absent, retourne "?".
        # Pour un vrai match, peu importe (pas de log CRITICAL), mais on vérifie
        # qu'on n'a pas de valeur parassite.
        ar_f = result.get("ar_fournisseur")
        assert ar_f != "?", "ar_fournisseur ne doit jamais être '?'"

    def test_mismatch_result_passes_armatchpayload_validation(self):
        """Le dict retourné sur mismatch est valide contre ARMatchPayload."""
        from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi
        from th2customers.scei.schemas import ARMatchPayload

        intake = _intake(fournisseur_nom="TILCO", num_sql="100950")
        run_sql = _make_sql_with_header("WILLY LEISSNER", ecktnumero="100950")
        result = match_ar_to_pmi(intake, run_sql)

        # Doit valider sans erreur
        validated = ARMatchPayload.model_validate(result)
        assert validated.ar_fournisseur == "TILCO"
        assert validated.pmi_fournisseur == "WILLY LEISSNER"

    def test_fournisseur_nom_absent_no_mismatch_fields_none(self):
        """fournisseur_nom absent → pas de mismatch, champs None."""
        from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi

        intake = _intake(fournisseur_nom=None, num_sql="100950")
        run_sql = _make_sql_with_header("WILLY LEISSNER", ecktnumero="100950")
        result = match_ar_to_pmi(intake, run_sql)

        assert result["status"] == "matched"
        assert result.get("supplier_mismatch") is False
        assert result.get("ar_fournisseur") in (None,), (
            "ar_fournisseur absent doit rester None"
        )


# ---------------------------------------------------------------------------
# 3. Gate : log CRITICAL contient les vrais noms (pas « ? »)
# ---------------------------------------------------------------------------


class TestGateLogContainsRealNames:
    """La gate lit les vrais noms de fournisseurs depuis ar_match."""

    def test_gate_log_critical_contains_real_supplier_names(self, caplog):
        """Le log CRITICAL doit contenir TILCO et WILLY LEISSNER, pas '?'."""
        from th2customers.scei.gates import build_supplier_mismatch_gate_callback

        ar_match_dict = {
            "status": "matched",
            "po": {"ecktsoc": "SC", "ecktnumero": "100950"},
            "lines": [],
            "diagnostic": "matched",
            "supplier_mismatch": True,
            "ar_fournisseur": "TILCO",
            "pmi_fournisseur": "WILLY LEISSNER",
        }
        ctx = _make_gate_callback_context(ar_match_dict)

        with caplog.at_level(logging.CRITICAL, logger="th2agent.core.agent_helpers.callbacks"):
            with patch("google.genai.types.Content") as MockContent, \
                 patch("google.genai.types.Part") as MockPart:
                MockContent.return_value = MagicMock()
                MockPart.return_value = MagicMock()
                cb = build_supplier_mismatch_gate_callback(output_key="ar_notify")
                cb(ctx)

        critical_records = [r for r in caplog.records if r.levelno >= logging.CRITICAL]
        assert critical_records, "Doit émettre au moins un log CRITICAL"
        msg = critical_records[0].getMessage()
        assert "TILCO" in msg, f"Le log doit contenir 'TILCO', got: {msg!r}"
        assert "WILLY LEISSNER" in msg, f"Le log doit contenir 'WILLY LEISSNER', got: {msg!r}"
        assert "?" not in msg, f"Le log ne doit PAS contenir '?', got: {msg!r}"

    def test_gate_draft_body_contains_real_supplier_names(self):
        """Le corps du mail opérateur contient les vrais noms, pas '?'."""
        from th2customers.scei.gates import build_supplier_mismatch_gate_callback

        ar_match_dict = {
            "status": "matched",
            "po": {"ecktsoc": "SC", "ecktnumero": "100950"},
            "lines": [],
            "diagnostic": "matched",
            "supplier_mismatch": True,
            "ar_fournisseur": "TILCO",
            "pmi_fournisseur": "WILLY LEISSNER",
        }
        state: dict = {"ar_match": json.dumps(ar_match_dict)}
        ctx = MagicMock()
        ctx.state = state
        ctx.agent_name = "scei_ar_notifier"

        with patch("google.genai.types.Content") as MockContent, \
             patch("google.genai.types.Part") as MockPart:
            MockContent.return_value = MagicMock()
            MockPart.return_value = MagicMock()
            cb = build_supplier_mismatch_gate_callback(output_key="ar_notify")
            cb(ctx)

        written = json.loads(state["ar_notify"])
        body = written.get("draft_body", "")
        assert "TILCO" in body, f"draft_body doit contenir 'TILCO', got: {body!r}"
        assert "WILLY LEISSNER" in body, f"draft_body doit contenir 'WILLY LEISSNER', got: {body!r}"
        assert "?" not in body, f"draft_body ne doit PAS contenir '?', got: {body!r}"

    def test_end_to_end_match_then_gate_sees_real_names(self):
        """Test E2E : match_ar_to_pmi produit un dict, la gate lit les vrais noms."""
        from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi
        from th2customers.scei.gates import build_supplier_mismatch_gate_callback

        # 1. Étape match
        intake = _intake(fournisseur_nom="TILCO", num_sql="100950")
        run_sql = _make_sql_with_header("WILLY LEISSNER", ecktnumero="100950")
        result = match_ar_to_pmi(intake, run_sql)

        assert result.get("supplier_mismatch") is True
        assert result.get("ar_fournisseur") == "TILCO"
        assert result.get("pmi_fournisseur") == "WILLY LEISSNER"

        # 2. Étape gate (simule ce que le state writer dépose)
        state: dict = {"ar_match": json.dumps(result)}
        ctx = MagicMock()
        ctx.state = state
        ctx.agent_name = "scei_ar_notifier"

        with patch("google.genai.types.Content") as MockContent, \
             patch("google.genai.types.Part") as MockPart:
            MockContent.return_value = MagicMock()
            MockPart.return_value = MagicMock()
            cb = build_supplier_mismatch_gate_callback(output_key="ar_notify")
            cb(ctx)

        written = json.loads(state["ar_notify"])
        body = written.get("draft_body", "")
        assert "TILCO" in body
        assert "WILLY LEISSNER" in body
        assert "?" not in body
