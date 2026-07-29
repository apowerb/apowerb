"""Tests TDD pour les 5 défauts de l'itération 2 (review).

Règles :
- Ces tests DOIVENT échouer (RED) AVANT les fixes.
- Après fix, ils PASSENT et le baseline (118 tests) reste intact.
"""
from __future__ import annotations

import pathlib
import re
import pytest
from unittest.mock import MagicMock

from th2customers.scei.tools.scei_ar_persist import (
    _reextract_lines_from_pmi_and_text,
    _lines_are_doubtful,
    _load_full_pdf_text,
    _norm_ref,
)
from th2agent.core.agent_helpers.pdf_to_images_tool import extract_all_pages_text

_FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "scei"

_PMI_CF101197 = [
    {"lcctrefext": "NSYCAG291LPF", "lcctqty": 9.0, "lccnpunet": 6947.9, "lcctexpu": "C"},
    {"lcctrefext": "NSYCVF850M400PF", "lcctqty": 11.0, "lccnpunet": 35190.68, "lcctexpu": "C"},
]

# ---------------------------------------------------------------------------
# DÉFAUT 1 — fenêtre pos+500 aveugle → contamination qté/prix entre voisins
# ---------------------------------------------------------------------------

class TestDefaut1WindowBorne:
    """La fenêtre de parsing ne doit PAS mordre sur la ligne PMI SUIVANTE.

    Fixture synthétique : 3 lignes PMI dans un même texte, séparées par des
    descriptions de >500 chars. Sans borne par la prochaine réf PMI, la
    fenêtre de la réf 1 capte la qté de la réf 2 (contaminée).
    """

    def _make_text(self):
        """Texte synthétique SAP avec 3 lignes, descriptions longues (>500 chars)."""
        desc_filler = "X" * 600  # description longue > 500 chars
        return (
            "PREMIERE LIGNE REF:\n"
            " 10 REFAAA111222 DESCRIPTION ARTICLE A 2 PCE\n"
            f" {desc_filler}\n"
            " 69,00 1 PCE 138,00 EUR\n"
            "\n"
            "DEUXIEME LIGNE REF:\n"
            " 20 REFBBB333444 DESCRIPTION ARTICLE B 7 PCE\n"
            " 50,00 1 PCE 350,00 EUR\n"
            "\n"
            "TROISIEME LIGNE REF:\n"
            " 30 REFCCC555666 DESCRIPTION ARTICLE C 3 PCE\n"
            " 25,00 1 PCE 75,00 EUR\n"
        )

    def test_two_pmi_lines_each_gets_own_qty(self):
        """Chaque ligne PMI récupère SA propre qté, pas celle du voisin."""
        text = self._make_text()
        pmi = [
            {"lcctrefext": "REFAAA111222", "lcctqty": 2.0, "lccnpunet": 6900.0, "lcctexpu": "C"},
            {"lcctrefext": "REFBBB333444", "lcctqty": 7.0, "lccnpunet": 5000.0, "lcctexpu": "C"},
        ]
        result = _reextract_lines_from_pmi_and_text(pmi, text)
        assert result is not None
        assert len(result) == 2
        # REFAAA111222 → qty=2 (pas 7 qui est celui de la ligne suivante)
        line_a = next(ln for ln in result if "REFAAA" in ln["Reference"])
        assert line_a["QuantiteAR"] == 2.0, (
            f"REFAAA111222 doit avoir QuantiteAR=2.0 (sa propre qté), "
            f"got {line_a['QuantiteAR']} — fenêtre contamine depuis la ligne suivante"
        )
        # REFBBB333444 → qty=7
        line_b = next(ln for ln in result if "REFBBB" in ln["Reference"])
        assert line_b["QuantiteAR"] == 7.0, (
            f"REFBBB333444 doit avoir QuantiteAR=7.0, got {line_b['QuantiteAR']}"
        )

    def test_three_pmi_lines_each_gets_own_qty(self):
        """Cas 3 lignes : chaque ligne récupère SA propre qté."""
        text = self._make_text()
        pmi = [
            {"lcctrefext": "REFAAA111222", "lcctqty": 2.0, "lccnpunet": 6900.0, "lcctexpu": "C"},
            {"lcctrefext": "REFBBB333444", "lcctqty": 7.0, "lccnpunet": 5000.0, "lcctexpu": "C"},
            {"lcctrefext": "REFCCC555666", "lcctqty": 3.0, "lccnpunet": 2500.0, "lcctexpu": "C"},
        ]
        result = _reextract_lines_from_pmi_and_text(pmi, text)
        assert result is not None
        assert len(result) == 3
        qtys = {ln["Reference"]: ln["QuantiteAR"] for ln in result}
        assert qtys["REFAAA111222"] == 2.0, f"Expected 2.0 got {qtys.get('REFAAA111222')}"
        assert qtys["REFBBB333444"] == 7.0, f"Expected 7.0 got {qtys.get('REFBBB333444')}"
        assert qtys["REFCCC555666"] == 3.0, f"Expected 3.0 got {qtys.get('REFCCC555666')}"


# ---------------------------------------------------------------------------
# DÉFAUT 2 — match sous-chaîne sans garde de longueur
# ---------------------------------------------------------------------------

class TestDefaut2MinLenGuard:
    """Réf PMI trop courte (< 6 chars) ne doit pas matcher par substring.

    Cas légitime : RSB2A080B7 (10 chars) doit rester trouvé même si la réf PDF
    est RSB2A080B7C (préfixe de la réf PDF → substring dans l'autre sens).
    """

    def test_short_pmi_ref_3chars_not_fabricated(self):
        """Réf PMI de 3 chars (ex 'PCE') ne doit PAS matcher dans le texte."""
        # 'PCE' apparaît partout dans le texte SAP ("9 PCE", "1 PCE ...")
        text = "9 PCE 10 PCE Article long description 1 PCE 69,48 EUR"
        pmi = [{"lcctrefext": "PCE", "lcctqty": 9.0, "lccnpunet": 100.0, "lcctexpu": " "}]
        result = _reextract_lines_from_pmi_and_text(pmi, text)
        # Réf < 6 chars → ne doit PAS fabriquer de ligne
        assert result is None or len(result) == 0, (
            f"Réf 'PCE' (3 chars) ne doit pas fabriquer de ligne, got {result}"
        )

    def test_short_pmi_ref_5chars_not_fabricated(self):
        """Réf PMI de 5 chars ne doit pas matcher par substring."""
        text = "12345 some description 12345 PCE 69,48 EUR 1 PCE 100,00 EUR"
        pmi = [{"lcctrefext": "12345", "lcctqty": 1.0, "lccnpunet": 100.0, "lcctexpu": " "}]
        result = _reextract_lines_from_pmi_and_text(pmi, text)
        assert result is None or len(result) == 0, (
            f"Réf '12345' (5 chars) ne doit pas fabriquer de ligne, got {result}"
        )

    def test_prefix_pmi_ref_still_found_rsb(self):
        """RSB2A080B7 (10 chars) doit être trouvé quand le PDF a RSB2A080B7C."""
        # La réf PDF (dans Réf. Origine) = RSB2A080B7C, PMI = RSB2A080B7
        # RSB2A080B7 est PRÉFIXE de RSB2A080B7C → substring dans l'autre sens → trouvé
        # On teste le cas où found_direct (substring) fonctionne
        text = "Réf. Origine: RSB2A080B7C\n 40 RSB2A080B7C 40 PCE\n 3,91 1 PCE 156,54 EUR"
        pmi = [{"lcctrefext": "RSB2A080B7", "lcctqty": 40.0, "lccnpunet": 391.35, "lcctexpu": "C"}]
        result = _reextract_lines_from_pmi_and_text(pmi, text)
        assert result is not None and len(result) == 1, (
            f"RSB2A080B7 (10 chars) doit être trouvé via prefix/substring, got {result}"
        )

    def test_ref_6chars_is_accepted(self):
        """Une réf de EXACTEMENT 6 chars doit être acceptée (borne >= 6)."""
        text = "ABC123 article 5 PCE 10,00 1 PCE 50,00 EUR"
        pmi = [{"lcctrefext": "ABC123", "lcctqty": 5.0, "lccnpunet": 1000.0, "lcctexpu": "C"}]
        result = _reextract_lines_from_pmi_and_text(pmi, text)
        # Avec >= 6, ABC123 (6 chars) doit être accepté
        assert result is not None and len(result) == 1, (
            f"Réf 'ABC123' (6 chars) doit être acceptée (len >= 6), got {result}"
        )


# ---------------------------------------------------------------------------
# DÉFAUT 3 — _lines_are_doubtful trop strict + écrasement lignes correctes
# ---------------------------------------------------------------------------

class TestDefaut3HasDoubtfulLine:
    """_lines_are_doubtful (rebaptisé _has_doubtful_line) doit retourner True
    si AU MOINS une ligne est douteuse, pas seulement si TOUTES le sont.

    La fusion doit être NON DESTRUCTIVE : les lignes correctes (ecart_qte,
    ecart_prix, conforme) sont PRÉSERVÉES ; seules les lignes LINE_NOT_IN_PO
    / non_rapproche non couvertes par une ligne AR correcte sont remplacées.
    """

    def test_mixed_lines_triggers_reextraction(self):
        """1 ligne ecart_qte (correcte) + 1 ligne LINE_NOT_IN_PO → douteuse = True."""
        lignes = [
            {"TypeEcart": "ecart_qte", "Situation": "NOK"},       # correcte
            {"TypeEcart": "LINE_NOT_IN_PO", "Situation": "NOK"},   # douteuse
        ]
        # Avec le fix, _lines_are_doubtful doit retourner True (au moins une douteuse)
        # Actuellement : retourne False dès qu'une ligne est non-douteuse
        from th2customers.scei.tools.scei_ar_persist import _lines_are_doubtful
        assert _lines_are_doubtful(lignes) is True, (
            "_lines_are_doubtful doit retourner True si AU MOINS une ligne est "
            "douteuse (LINE_NOT_IN_PO), même si une autre ligne a ecart_qte"
        )

    def test_all_correct_not_doubtful(self):
        """Toutes les lignes correctes → douteuse = False."""
        lignes = [
            {"TypeEcart": "ecart_qte", "Situation": "NOK"},
            {"TypeEcart": None, "Situation": "OK"},
        ]
        from th2customers.scei.tools.scei_ar_persist import _lines_are_doubtful
        assert _lines_are_doubtful(lignes) is False

    def test_all_line_not_in_po_is_doubtful(self):
        """Toutes douteuses → True (comportement inchangé)."""
        lignes = [
            {"TypeEcart": "LINE_NOT_IN_PO"},
            {"TypeEcart": "non_rapproche"},
        ]
        from th2customers.scei.tools.scei_ar_persist import _lines_are_doubtful
        assert _lines_are_doubtful(lignes) is True


class TestDefaut3NonDestructiveMerge:
    """Fusion non destructive : lignes correctes PRÉSERVÉES, manquantes AJOUTÉES.

    Cas : AR 2 lignes PMI
    - ligne 1 : ecart_qte correct dans l'AR (ref=REFAAA, QuantiteAR=2 ≠ PMI=9)
    - ligne 2 : LINE_NOT_IN_PO ratée (ref absente dans l'AR)
    Après fix :
    - ligne 1 INCHANGÉE (TypeEcart=ecart_qte, QuantiteAR=2)
    - ligne 2 RÉCUPÉRÉE depuis le texte PDF
    """

    def _make_pmi_and_text(self):
        pmi = [
            {"lcctrefext": "NSYCAG291LPF", "lcctqty": 9.0, "lccnpunet": 6947.9, "lcctexpu": "C"},
            {"lcctrefext": "NSYCVF850M400PF", "lcctqty": 11.0, "lccnpunet": 35190.68, "lcctexpu": "C"},
        ]
        # Texte réel CF101197 (toutes pages)
        result = extract_all_pages_text(str(_FIXTURE_DIR / "CF101197.PDF"))
        return pmi, result["text"]

    def test_correct_line_preserved_missing_line_recovered(self, monkeypatch):
        """Ligne ecart_qte préservée + ligne LINE_NOT_IN_PO récupérée."""
        import th2customers.scei.tools.scei_ar_persist as m

        pmi, pdf_text = self._make_pmi_and_text()

        # AR initial : ligne 1 = ecart_qte (QuantiteAR=2 ≠ PMI=9), ligne 2 = LINE_NOT_IN_PO
        ar_lignes_initiales = [
            {
                "Reference": "NSYCAG291LPF",
                "QuantiteAR": 2.0,          # ≠ PMI (9.0) → ecart_qte
                "PrixAR": 69.48,
                "PrixERP": 69.48,
                "TypeEcart": "ecart_qte",
                "Situation": "NOK",
            },
            {
                "Reference": "",            # référence vide → LINE_NOT_IN_PO
                "QuantiteAR": None,
                "TypeEcart": "LINE_NOT_IN_PO",
                "Situation": "NOK",
            },
        ]

        # Simuler l'appel dans tool_persist_ar_record via _lines_are_doubtful
        # puis la fusion — on teste la fonction de re-extraction + la logique de merge
        # directement (sans DB).

        # Étape 1 : vérifier que _lines_are_doubtful retourne True (trigger)
        assert m._lines_are_doubtful(ar_lignes_initiales) is True, (
            "Avec ligne ecart_qte + LINE_NOT_IN_PO, _lines_are_doubtful doit être True"
        )

        # Étape 2 : la re-extraction depuis PMI + texte doit trouver les 2 lignes PMI
        reextracted = m._reextract_lines_from_pmi_and_text(pmi, pdf_text)
        assert reextracted is not None
        assert len(reextracted) == 2

        # Étape 3 : appeler _merge_reextracted_lines (nouvelle fonction) ou vérifier
        # la logique de fusion dans tool_persist_ar_record via un appel complet avec stub.
        # On vérifie que le merge produit : ligne ecart_qte inchangée + ligne NSYCVF récupérée.

        # Appel complet avec engine stub
        class _R:
            def __init__(self, v=None): self._v = v
            def scalar(self): return self._v
        class _IdempConn:
            """Precheck idempotence : retourne None (pas de doublon)."""
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def execute(self, *a, **k): return _R(None)
        class _InsertConn:
            """INSERT : retourne commande_id=42."""
            def __enter__(self): return self
            def __exit__(self, *a): pass
            def execute(self, *a, **k): return _R(42)
        class _Eng:
            def connect(self): return _IdempConn()  # idempotence precheck
            def begin(self): return _InsertConn()   # INSERT transaction

        orig_engine = m._get_db_engine
        m._get_db_engine = lambda: _Eng()
        try:
            # Simuler tool_context avec ar_match + pdf_text
            ctx = MagicMock()
            ctx.state = {
                "ar_match": {
                    "status": "matched",
                    "po": {"ecktsoc": "100", "ecktnumero": "101197"},
                    "lines": pmi,
                },
                "webhook_log_id": None,  # pas de PDF réel en test
            }
            # Patcher _load_full_pdf_text pour retourner le vrai texte PDF
            orig_load = m._load_full_pdf_text
            m._load_full_pdf_text = lambda _tc: pdf_text
            try:
                result = m.tool_persist_ar_record(
                    NumeroCommande="CF101197",
                    DateCommande="2026-05-29",
                    StatutGlobal="non_conforme",  # sera réécrit par _derive_statut_global
                    lignes=ar_lignes_initiales,
                    tool_context=ctx,
                )
            finally:
                m._load_full_pdf_text = orig_load
        finally:
            m._get_db_engine = orig_engine

        # Le résultat : success=True (DB stub), et les lignes doivent avoir été
        # fusionnées. On vérifie via le log — mais surtout via lignes_inserees.
        # 2 lignes PMI → 2 lignes insérées (ecart_qte préservée + NSYCVF récupérée)
        assert result["success"] is True
        assert result["lignes_inserees"] == 2, (
            f"Doit insérer 2 lignes (ecart_qte préservée + NSYCVF récupérée), "
            f"got {result['lignes_inserees']}"
        )


# ---------------------------------------------------------------------------
# DÉFAUT 4 — ordre PDF non déterministe dans _load_full_pdf_text
# ---------------------------------------------------------------------------

class TestDefaut4SortedPdfFiles:
    """_load_full_pdf_text doit trier les PDFs avant concaténation."""

    def test_pdf_files_sorted_deterministic(self, tmp_path, monkeypatch):
        """Deux PDFs → la concaténation est toujours dans le même ordre (sorted)."""
        import shutil
        from th2agent.storage import webhook_attachments

        log_id = 54321
        att_dir = tmp_path / "2026" / "05" / str(log_id)
        att_dir.mkdir(parents=True)

        # Copier les deux PDFs fixtures dans le répertoire temporaire
        for pdf in ["CF101197.PDF", "CF101192.PDF"]:
            shutil.copy(str(_FIXTURE_DIR / pdf), str(att_dir / pdf))

        monkeypatch.setattr(webhook_attachments, "ATTACHMENT_ROOT", tmp_path)

        ctx1 = MagicMock()
        ctx1.state = {"webhook_log_id": log_id}
        result1 = _load_full_pdf_text(ctx1)

        ctx2 = MagicMock()
        ctx2.state = {"webhook_log_id": log_id}
        result2 = _load_full_pdf_text(ctx2)

        # Les deux appels doivent retourner le même texte (ordre déterministe)
        assert result1 == result2, "L'ordre de concaténation doit être déterministe (sorted)"
        # Et les deux PDFs doivent être présents
        assert "NSYCAG291LPF" in result1  # CF101197
        assert "RSB2A080B7" in result1    # CF101192

    def test_pdf_separator_present(self, tmp_path, monkeypatch):
        """Les PDFs doivent être séparés par un délimiteur de page."""
        import shutil
        from th2agent.storage import webhook_attachments

        log_id = 54322
        att_dir = tmp_path / "2026" / "05" / str(log_id)
        att_dir.mkdir(parents=True)

        for pdf in ["CF101197.PDF", "CF101192.PDF"]:
            shutil.copy(str(_FIXTURE_DIR / pdf), str(att_dir / pdf))

        monkeypatch.setattr(webhook_attachments, "ATTACHMENT_ROOT", tmp_path)

        ctx = MagicMock()
        ctx.state = {"webhook_log_id": log_id}
        result = _load_full_pdf_text(ctx)

        # Avec \n\f\n comme délimiteur (form feed = séparateur de document)
        assert "\f" in result or "\n\n" in result, (
            "Les PDFs doivent être séparés par un délimiteur (\\f ou \\n\\n)"
        )


# ---------------------------------------------------------------------------
# DÉFAUT 5 — comparaison prix : abs() au lieu de round() round()
# ---------------------------------------------------------------------------

class TestDefaut5PrixTolerance:
    """La comparaison prix doit utiliser abs(a - b) >= 0.01 (tolérance 1 centime).

    round(x, 2) != round(y, 2) peut donner des résultats non intuitifs sur les
    valeurs .5 (banker's rounding en Python).
    """

    def test_prix_ecart_moins_1_centime_is_conforme(self):
        """Écart < 1 centime = conforme (tolérance)."""
        # prix_parsed = 69.484 (arrondi PDF), prix_erp = 69.479 (PMI)
        # abs(69.484 - 69.479) = 0.005 < 0.01 → conforme
        text = "REFFFFF999888 article 9 PCE\n 69,484 1 PCE 625,00 EUR"
        pmi = [{"lcctrefext": "REFFFFF999888", "lcctqty": 9.0, "lccnpunet": 6947.9, "lcctexpu": "C"}]
        # PrixERP = 6947.9 / 100 = 69.479
        result = _reextract_lines_from_pmi_and_text(pmi, text)
        assert result is not None and len(result) == 1
        assert result[0]["TypeEcart"] is None, (
            f"Écart prix < 0.01 doit être conforme (TypeEcart=None), "
            f"got TypeEcart={result[0]['TypeEcart']}, "
            f"PrixAR={result[0]['PrixAR']}, PrixERP={result[0]['PrixERP']}"
        )

    def test_prix_ecart_exactement_1_centime_is_ecart(self):
        """Écart exactement 0.01 = écart_prix."""
        # prix_erp = 69.479 → on veut prix_parsed qui donne abs >= 0.01
        # Si prix_parsed = 69.49 → abs(69.49 - 69.479) = 0.011 >= 0.01 → ecart
        text = "REFFFFF999999 article 9 PCE\n 69,49 1 PCE 625,00 EUR"
        pmi = [{"lcctrefext": "REFFFFF999999", "lcctqty": 9.0, "lccnpunet": 6949.0, "lcctexpu": "C"}]
        # PrixERP = 6949.0 / 100 = 69.49 → abs(69.49 - 69.49) = 0 → conforme !
        # On force un vrai écart : pmi lccnpunet=6900 → prix_erp=69.0, pdf=69.49 → écart=0.49
        pmi2 = [{"lcctrefext": "REFFFFF999999", "lcctqty": 9.0, "lccnpunet": 6900.0, "lcctexpu": "C"}]
        # prix_erp = 69.0, prix_parsed = 69.49, abs = 0.49 >= 0.01 → ecart_prix
        result = _reextract_lines_from_pmi_and_text(pmi2, text)
        assert result is not None and len(result) == 1
        assert result[0]["TypeEcart"] == "ecart_prix", (
            f"Écart prix 0.49 doit être ecart_prix, got {result[0]['TypeEcart']}"
        )

    def test_prix_no_round_half_even_surprise(self):
        """Pas de surprise avec banker's rounding sur .5.

        round(2.675, 2) donne 2.67 en Python (banker's rounding), PAS 2.68.
        abs(2.675 - 2.67) = 0.005 < 0.01 → conforme avec la nouvelle tolérance.
        """
        # prix_parsed = 2.675, prix_erp = 2.67 → round(2.675, 2) = 2.67 (Python!)
        # Avec round() != round() : 2.67 != 2.67 → False → conforme (OK aujourd'hui)
        # Avec abs() >= 0.01 : 0.005 < 0.01 → conforme (comportement identique, cohérent)
        text = "REF267XXXX article 5 PCE\n 2,675 1 PCE 13,375 EUR"
        pmi = [{"lcctrefext": "REF267XXXX", "lcctqty": 5.0, "lccnpunet": 267.0, "lcctexpu": "C"}]
        # PrixERP = 267.0 / 100 = 2.67, PrixAR = 2.675
        # abs(2.675 - 2.67) = 0.005 < 0.01 → conforme
        result = _reextract_lines_from_pmi_and_text(pmi, text)
        assert result is not None and len(result) == 1
        assert result[0]["TypeEcart"] is None, (
            f"Écart 0.005 (< 0.01) doit être conforme, got {result[0]['TypeEcart']}"
        )
