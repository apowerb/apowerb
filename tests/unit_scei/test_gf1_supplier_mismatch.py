"""GARDE-FOU #1 — TDD supplier mismatch : comparaison fournisseur tolérante.

Cas réel CF100950 : ECKTNUMERO=100950 existe en PMI sous 'WILLY LEISSNER'
mais l'AR vient de TILCO → faux matched sans ce garde-fou.

Règles :
- fournisseur_nom absent (None) → comportement inchangé (matched normal).
- Correspondance tolérante (casse, accents, espaces, ponctuation, suffixes) → matched, supplier_mismatch=False.
- Divergence forte (tokens sans chevauchement) → matched + supplier_mismatch=True + diagnostic ⚠.
- NE PAS basculer en non_rapproche : le statut reste matched dans tous les cas.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock


# ---------------------------------------------------------------------------
# Fixtures helpers
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
            return {"success": True, "sql": sql, "row_count": 1, "data": [_header(ecctnom, ecktnumero)]}
        if "LCOMFOU" in sql_upper:
            return {"success": True, "sql": sql, "row_count": 0, "data": []}
        return {"success": True, "sql": sql, "row_count": 0, "data": []}
    return run_sql


def _intake(fournisseur_nom: str | None, num_sql: str = "100950") -> dict:
    extraction = {
        "commande_number_display": f"CF{num_sql}",
        "commande_number_sql": num_sql,
        "lines": [],
    }
    if fournisseur_nom is not None:
        extraction["fournisseur_nom"] = fournisseur_nom
    return {"email_classification": "ar", "extraction_results": extraction}


# ---------------------------------------------------------------------------
# Tests RED : ces tests échouent AVANT l'implémentation de la comparaison
# ---------------------------------------------------------------------------


class TestSupplierMismatchLogic:
    """Comparaison fournisseur dans match_ar_to_pmi."""

    def test_fournisseur_absent_comportement_inchange(self):
        """Cas 1 : fournisseur_nom absent → matched sans supplier_mismatch."""
        from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi

        intake = _intake(fournisseur_nom=None)
        run_sql = _make_sql_with_header("TILCO FRANCE SAS")
        result = match_ar_to_pmi(intake, run_sql)

        assert result["status"] == "matched"
        assert result.get("supplier_mismatch", False) is False

    def test_te_connectivity_casse_tolerance(self):
        """Cas 2 : 'TE Connectivity' vs 'TE CONNECTIVITY' → matched, supplier_mismatch=False."""
        from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi

        intake = _intake(fournisseur_nom="TE Connectivity")
        run_sql = _make_sql_with_header("TE CONNECTIVITY", ecktnumero="101000")
        result = match_ar_to_pmi(intake, run_sql)

        assert result["status"] == "matched"
        assert result.get("supplier_mismatch", False) is False

    def test_socomec_vs_socomec_sa_tolerance_suffixe(self):
        """Cas 3 : 'SOCOMEC' vs 'SOCOMEC SA' → matched, supplier_mismatch=False."""
        from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi

        intake = _intake(fournisseur_nom="SOCOMEC")
        run_sql = _make_sql_with_header("SOCOMEC SA", ecktnumero="101001")
        result = match_ar_to_pmi(intake, run_sql)

        assert result["status"] == "matched"
        assert result.get("supplier_mismatch", False) is False

    def test_ecomal_vs_ecomal_france_tolerance(self):
        """Cas 4 : 'ECOMAL' vs 'ECOMAL France' → matched, supplier_mismatch=False."""
        from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi

        intake = _intake(fournisseur_nom="ECOMAL")
        run_sql = _make_sql_with_header("ECOMAL France", ecktnumero="101002")
        result = match_ar_to_pmi(intake, run_sql)

        assert result["status"] == "matched"
        assert result.get("supplier_mismatch", False) is False

    def test_tilco_vs_willy_leissner_divergence_forte(self):
        """Cas 5 (CF100950) : 'TILCO' vs 'WILLY LEISSNER' → matched + supplier_mismatch=True."""
        from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi

        intake = _intake(fournisseur_nom="TILCO")
        run_sql = _make_sql_with_header("WILLY LEISSNER", ecktnumero="100950")
        result = match_ar_to_pmi(intake, run_sql)

        assert result["status"] == "matched", "Doit rester matched (pas non_rapproche)"
        assert result.get("supplier_mismatch") is True
        diag = result.get("diagnostic", "")
        assert "⚠" in diag or "fournisseur divergent" in diag.lower()
        assert "TILCO" in diag
        assert "WILLY LEISSNER" in diag

    def test_tilco_france_sas_vs_tilco_tolerance(self):
        """'TILCO' (AR court) vs 'TILCO FRANCE SAS' (PMI long) → matched, supplier_mismatch=False."""
        from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi

        intake = _intake(fournisseur_nom="TILCO")
        run_sql = _make_sql_with_header("TILCO FRANCE SAS", ecktnumero="101090")
        result = match_ar_to_pmi(intake, run_sql)

        assert result["status"] == "matched"
        assert result.get("supplier_mismatch", False) is False

    def test_statut_reste_matched_meme_avec_mismatch(self):
        """supplier_mismatch=True ne change pas le status 'matched'."""
        from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi

        intake = _intake(fournisseur_nom="FOURNISSEUR A")
        run_sql = _make_sql_with_header("FOURNISSEUR B COMPLETEMENT DIFFERENT XYZ", ecktnumero="101003")
        result = match_ar_to_pmi(intake, run_sql)

        # Le statut NE doit PAS basculer en non_rapproche
        assert result["status"] == "matched"
        assert result.get("supplier_mismatch") is True

    def test_non_rapproche_pas_de_supplier_mismatch(self):
        """Si non_rapproche (PO pas trouvé), supplier_mismatch n'est pas défini/False."""
        from th2customers.scei.superagents.scei_pmi_match import match_ar_to_pmi

        intake = _intake(fournisseur_nom="TILCO", num_sql="999999")
        # run_sql ne retourne rien
        def run_sql(sql: str) -> dict:
            return {"success": True, "sql": sql, "row_count": 0, "data": []}

        result = match_ar_to_pmi(intake, run_sql)
        assert result["status"] == "non_rapproche"
        assert result.get("supplier_mismatch", False) is False
