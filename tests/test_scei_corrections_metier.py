"""Tests TDD lot 2 — corrections métier déterministes SCEI.

Règles :
- Ces tests DOIVENT échouer (RED) AVANT les fixes.
- Après fix, ils PASSENT et le baseline iter1/2/3 reste intact.

Fixes ciblés :
  A — clé prix `lcctpunet` (PAS `lccnpunet`) dans _reextract_lines_from_pmi_and_text.
      + recherche réf via `lcctrefext` ET `lcctcodart`.
  B — _derive_fournisseur : matched -> po.ecctnom, sinon llm_value.
  C — PrixERP unitaire dans _reconcile_lines : recalcul lcctpunet/facteur.
  D — ligne ref-matchée tout-None -> conforme (jamais ecart inventé).
"""
from __future__ import annotations


from th2customers.scei.tools.scei_ar_persist import (
    _reextract_lines_from_pmi_and_text,
    _reconcile_lines,
    _derive_fournisseur,
)


# ---------------------------------------------------------------------------
# Fixtures réelles ar_match.lines (clés prouvées live — NE PAS MODIFIER)
# ---------------------------------------------------------------------------


_PMI_CF101232 = [
    {
        "lcctcode": "100065",
        "lcctcodart": "SPEC02622",
        "lcctrefext": "758-1650",
        "lcctqty": 60.0,
        "lcctpunet": 309.0,
        "lcctexpu": "C",
    },
    {
        "lcctcode": "100065",
        "lcctcodart": "RS0213",
        "lcctrefext": "457-4757",
        "lcctqty": 10.0,
        "lcctpunet": 309.0,
        "lcctexpu": "C",
    },
]

# PMI pour CF101249 : pERP total stocké = 700.40 mais lcctpunet=70.04 expu=' '


# ===========================================================================
# FIX A — clé lcctpunet dans _reextract_lines_from_pmi_and_text
# ===========================================================================

class TestFixAKeyLcctpunet:
    """_reextract_lines_from_pmi_and_text doit lire lcctpunet (pas lccnpunet)."""


    def test_recherche_ref_via_lcctcodart_si_lcctrefext_absent(self):
        """Quand lcctrefext est vide, la recherche doit se faire via lcctcodart."""
        pmi = [
            {
                "lcctcode": "100065",
                "lcctcodart": "SPEC02622",
                "lcctrefext": "",  # vide
                "lcctqty": 60.0,
                "lcctpunet": 309.0,
                "lcctexpu": "C",
            }
        ]
        # PDF avec SPEC02622 (le lcctcodart) et N PCE
        pdf = (
            "Poste 10 SPEC02622 Description article\n"
            " 60 PCE\n"
            " 3,09 1 PCE 185,40 EUR\n"
        )
        result = _reextract_lines_from_pmi_and_text(pmi, pdf)
        assert result is not None and len(result) == 1
        line = result[0]
        # Ligne trouvée via lcctcodart
        assert line["QuantiteAR"] == 60.0


# ===========================================================================
# FIX B — _derive_fournisseur
# ===========================================================================

class TestFixBDeriveFournisseur:
    """_derive_fournisseur doit retourner po.ecctnom sur AR matched."""

    def _make_ar_match(self, status: str, ecctnom: str | None = None) -> dict:
        po = {"ecctnom": ecctnom, "ecktnumero": "101087", "ecktsoc": "100"}
        return {"status": status, "po": po, "lines": []}

    def test_matched_retourne_ecctnom(self):
        """AR matched → FournisseurNom = po.ecctnom (TILCO), pas la valeur LLM."""
        ar_match = self._make_ar_match("matched", ecctnom="TILCO")
        result = _derive_fournisseur(ar_match, "SCEI CHARMES")
        assert result == "TILCO"

    def test_matched_ecctnom_strip(self):
        """Espaces autour de ecctnom sont retirés."""
        ar_match = self._make_ar_match("matched", ecctnom="  AMS  ")
        result = _derive_fournisseur(ar_match, "SCEI CHARMES")
        assert result == "AMS"

    def test_matched_ecctnom_clamp_100(self):
        """ecctnom > 100 chars est tronqué à 100."""
        long_name = "A" * 150
        ar_match = self._make_ar_match("matched", ecctnom=long_name)
        result = _derive_fournisseur(ar_match, "SCEI CHARMES")
        assert len(result) == 100

    def test_non_matched_retourne_llm_value(self):
        """AR non matché → FournisseurNom = valeur LLM conservée."""
        ar_match = self._make_ar_match("not_matched", ecctnom="TILCO")
        result = _derive_fournisseur(ar_match, "SCEI CHARMES")
        assert result == "SCEI CHARMES"

    def test_matched_ecctnom_none_retourne_llm_value(self):
        """matched mais ecctnom=None → fallback LLM."""
        ar_match = self._make_ar_match("matched", ecctnom=None)
        result = _derive_fournisseur(ar_match, "RADIOSPARE")
        assert result == "RADIOSPARE"

    def test_ar_match_non_dict_retourne_llm_value(self):
        """ar_match non-dict → valeur LLM conservée."""
        result = _derive_fournisseur(None, "TILCO")
        assert result == "TILCO"

    def test_matched_ecctnom_vide_retourne_llm_value(self):
        """ecctnom chaîne vide → fallback LLM."""
        ar_match = self._make_ar_match("matched", ecctnom="")
        result = _derive_fournisseur(ar_match, "TILCO LLM")
        assert result == "TILCO LLM"


# ===========================================================================
# FIX C — PrixERP unitaire dans _reconcile_lines
# ===========================================================================

class TestFixCPrixErpUnitaire:
    """_reconcile_lines doit recalculer PrixERP=lcctpunet/facteur et comparer vs PrixAR."""


    def test_cf101232_l1_expu_c_recalcule_conforme(self):
        """CF101232 L1 : lcctpunet=309 expu='C' → 3.09 = PrixAR → conforme."""
        lignes = [
            {
                "Reference": "758-1650",
                "QuantiteAR": 60.0,
                "QuantiteERP": 60.0,
                "PrixAR": 3.09,
                "PrixERP": 309.0,  # non converti
                "TypeEcart": "ecart_prix",
                "Situation": "NOK",
            }
        ]
        pmi_lines = [
            {
                "lcctcode": "100065",
                "lcctcodart": "SPEC02622",
                "lcctrefext": "758-1650",
                "lcctqty": 60.0,
                "lcctpunet": 309.0,
                "lcctexpu": "C",
            }
        ]
        result = _reconcile_lines(lignes, pmi_lines)
        assert len(result) == 1
        line = result[0]
        assert line["TypeEcart"] is None, f"CF101232 L1 attendu conforme, got {line['TypeEcart']!r}"
        assert line["Situation"] == "OK"

    def test_cf101232_l2_ecart_qte_preserve(self):
        """CF101232 L2 : qAR=20 vs lcctqty=60 → ecart_qte préservé (vrai écart)."""
        lignes = [
            {
                "Reference": "457-4757",
                "QuantiteAR": 20.0,
                "QuantiteERP": 10.0,
                "PrixAR": 3.09,
                "PrixERP": 309.0,
                "TypeEcart": "ecart_prix",
                "Situation": "NOK",
            }
        ]
        pmi_lines = [
            {
                "lcctcode": "100065",
                "lcctcodart": "RS0213",
                "lcctrefext": "457-4757",
                "lcctqty": 60.0,
                "lcctpunet": 309.0,
                "lcctexpu": "C",
            }
        ]
        result = _reconcile_lines(lignes, pmi_lines)
        assert len(result) == 1
        line = result[0]
        assert line["TypeEcart"] == "ecart_qte"
        assert line["Situation"] == "NOK"

    def test_lcctpunet_none_pas_de_rederivation(self):
        """Si lcctpunet est None, pas de recalcul → comportement inchangé (PrixERP du recorder)."""
        lignes = [
            {
                "Reference": "REFTEST123",
                "QuantiteAR": 5.0,
                "QuantiteERP": 5.0,
                "PrixAR": 10.0,
                "PrixERP": 10.0,
                "TypeEcart": None,
                "Situation": "OK",
            }
        ]
        pmi_lines = [
            {
                "lcctcode": "100065",
                "lcctcodart": "REFTEST123",
                "lcctrefext": "REFTEST123",
                "lcctqty": 5.0,
                "lcctpunet": None,
                "lcctexpu": "C",
            }
        ]
        result = _reconcile_lines(lignes, pmi_lines)
        assert len(result) == 1
        # Pas d'ecart avec les valeurs originales
        line = result[0]
        assert line["TypeEcart"] is None

    def test_expu_m_facteur_1000(self):
        """lcctpunet=5000 expu='M' → prix_erp_unit=5.0."""
        lignes = [
            {
                "Reference": "ARTM001234567",
                "QuantiteAR": 1000.0,
                "PrixAR": 5.0,
                "PrixERP": 5000.0,  # non converti
                "TypeEcart": "ecart_prix",
                "Situation": "NOK",
            }
        ]
        pmi_lines = [
            {
                "lcctcode": "100065",
                "lcctcodart": "ARTM001234567",
                "lcctrefext": "ARTM001234567",
                "lcctqty": 1000.0,
                "lcctpunet": 5000.0,
                "lcctexpu": "M",
            }
        ]
        result = _reconcile_lines(lignes, pmi_lines)
        assert len(result) == 1
        line = result[0]
        assert line["TypeEcart"] is None, f"EXPU M attendu conforme, got {line['TypeEcart']!r}"
        assert abs(line["PrixERP"] - 5.0) < 0.01


# ===========================================================================
# FIX D — ligne ref-matchée tout-None → conforme
# ===========================================================================

class TestFixDNoneConforme:
    """Ligne ref-matchée avec qty_ar=None ET prix_ar=None → conforme (pas ecart)."""

    def test_qty_ar_none_prix_ar_none_conforme(self):
        """CF101244 simulé : ligne matchée, tout None → TypeEcart=None."""
        lignes = [
            {
                "Reference": "RSSPEC0123456",
                "QuantiteAR": None,
                "PrixAR": None,
                "PrixERP": None,
                "TypeEcart": "ecart_qte",  # verdict LLM erroné
                "Situation": "NOK",
            }
        ]
        pmi_lines = [
            {
                "lcctcode": "100065",
                "lcctcodart": "RSSPEC0123456",
                "lcctrefext": "RSSPEC0123456",
                "lcctqty": 5.0,
                "lcctpunet": 100.0,
                "lcctexpu": " ",
            }
        ]
        result = _reconcile_lines(lignes, pmi_lines)
        assert len(result) == 1
        line = result[0]
        assert line["TypeEcart"] is None, (
            f"Ligne tout-None attendue conforme, got TypeEcart={line['TypeEcart']!r}"
        )
        assert line["Situation"] == "OK"

    def test_qty_ar_presente_ecart_preserve(self):
        """Si qty_ar est présente et diffère → ecart_qte conservé (non masqué par FIX D)."""
        lignes = [
            {
                "Reference": "RSSPEC0123456",
                "QuantiteAR": 3.0,
                "PrixAR": None,
                "PrixERP": None,
                "TypeEcart": "ecart_qte",
                "Situation": "NOK",
            }
        ]
        pmi_lines = [
            {
                "lcctcode": "100065",
                "lcctcodart": "RSSPEC0123456",
                "lcctrefext": "RSSPEC0123456",
                "lcctqty": 5.0,
                "lcctpunet": None,
                "lcctexpu": " ",
            }
        ]
        result = _reconcile_lines(lignes, pmi_lines)
        assert len(result) == 1
        line = result[0]
        assert line["TypeEcart"] == "ecart_qte"

    def test_prix_ar_present_sans_pdf_conforme(self):
        """FIX 1 iter6 : sans pdf_text, pas de verdict prix via recorder LLM.

        MISE A JOUR iter6 : le verdict ecart_prix ne se declenche QUE si le
        prix est lu depuis le PDF. Sans pdf_text, _pdf_prix_found=False.
        qty_ar=None -> FIX D -> conforme.
        """
        lignes = [
            {
                "Reference": "RSSPEC0123456",
                "QuantiteAR": None,
                "PrixAR": 999.0,
                "PrixERP": 100.0,
                "TypeEcart": "ecart_prix",
                "Situation": "NOK",
            }
        ]
        pmi_lines = [
            {
                "lcctcode": "100065",
                "lcctcodart": "RSSPEC0123456",
                "lcctrefext": "RSSPEC0123456",
                "lcctqty": 5.0,
                "lcctpunet": 100.0,
                "lcctexpu": " ",
            }
        ]
        result = _reconcile_lines(lignes, pmi_lines)
        assert len(result) == 1
        line = result[0]
        # QuantiteAR=None ET _pdf_prix_found=False -> FIX D: conforme
        assert line["TypeEcart"] is None

    def test_qty_ar_differe_prix_ar_none_ecart_qte(self):
        """FIX D guard : qty_ar=10 diffère de lcctqty=5, prix_ar=None → ecart_qte (pas masqué)."""
        lignes = [
            {
                "Reference": "RSSPEC0123456",
                "QuantiteAR": 10.0,
                "PrixAR": None,
                "PrixERP": None,
                "TypeEcart": None,
                "Situation": "OK",
            }
        ]
        pmi_lines = [
            {
                "lcctcode": "100065",
                "lcctcodart": "RSSPEC0123456",
                "lcctrefext": "RSSPEC0123456",
                "lcctqty": 5.0,
                "lcctpunet": None,
                "lcctexpu": " ",
            }
        ]
        result = _reconcile_lines(lignes, pmi_lines)
        assert len(result) == 1
        line = result[0]
        assert line["TypeEcart"] == "ecart_qte"
        assert line["Situation"] == "NOK"


# ===========================================================================
# FIX C CRITICAL — prix_ar TOTAL vs prix_erp_unit (conservateur unitaire|total)
# ===========================================================================

class TestFixCPrixArTotal:
    """_reconcile_lines : prix_ar peut être TOTAL (qty×unit) ou UNITAIRE.
    La comparaison doit accepter les deux → conservatrice (préfère conforme).
    Données prod prouvées CF101221 et CF101227.
    """

    def test_cf101221_prix_ar_total_conforme(self):
        """CF101221 GV2P07 : PrixAR=390.5 = 39.05×10 (total LLM).
        lcctpunet=3905.0 expu='C' lcctqty=10 → unit_PMI=3905/100=39.05.
        prix_erp_total = 39.05 × 10 = 390.5.

        FIX C actuel : abs(390.5 - 39.05) = 351.45 >= 0.01 → FAUX ecart_prix.
        Attendu : conforme car prix_ar=390.5 == prix_erp_total=390.5.
        """
        lignes = [
            {
                "Reference": "GV2P07",
                "QuantiteAR": 10.0,
                "PrixAR": 390.5,  # total LLM
                "PrixERP": 39.05,
                "TypeEcart": None,
                "Situation": "OK",
            }
        ]
        pmi_lines = [
            {
                "lcctcode": "100042",
                "lcctcodart": "SCHTGV2P07",
                "lcctrefext": "GV2P07",
                "lcctqty": 10.0,
                "lcctpunet": 3905.0,  # centièmes → unit=39.05
                "lcctexpu": "C",
            }
        ]
        result = _reconcile_lines(lignes, pmi_lines)
        assert len(result) == 1
        line = result[0]
        assert line["TypeEcart"] is None, (
            f"CF101221 attendu conforme (prix_ar=total=39.05×10), got {line['TypeEcart']!r}"
        )
        assert line["Situation"] == "OK"
        # PrixERP doit afficher le prix unitaire = 39.05
        assert abs(line["PrixERP"] - 39.05) < 0.01

    def test_cf101221_prix_ar_total_sans_pdf_conforme(self):
        """FIX 1 iter6 : sans pdf_text, pas de normalisation ni verdict prix.

        MISE A JOUR iter6 : matches_total SUPPRIME. Sans pdf_text, _pdf_prix_found=False.
        qty=10==PMI 10 -> conforme. PrixAR reste a sa valeur recorder (pas normalisee).
        Le prix n'est plus juge sans source PDF fiable.
        """
        lignes = [
            {
                "Reference": "GV2P07B",
                "QuantiteAR": 10.0,
                "PrixAR": 390.5,  # total recorder
                "PrixERP": 39.05,
                "TypeEcart": "ecart_prix",
                "Situation": "NOK",
            }
        ]
        pmi_lines = [
            {
                "lcctcode": "100042",
                "lcctcodart": "SCHTGV2P07B",
                "lcctrefext": "GV2P07B",
                "lcctqty": 10.0,
                "lcctpunet": 3905.0,
                "lcctexpu": "C",
            }
        ]
        result = _reconcile_lines(lignes, pmi_lines)
        assert len(result) == 1
        line = result[0]
        # Sans pdf_text: _pdf_prix_found=False, qty=10==PMI 10 -> conforme
        assert line["TypeEcart"] is None, (
            f"GV2P07B attendu conforme (qty match, pas de verdict prix sans PDF), "
            f"got {line['TypeEcart']!r}"
        )
        assert line["Situation"] == "OK"

    def test_cf101227_19002392_prix_ar_total_expu_espace(self):
        """CF101227 19002392 : PrixAR=280.35, unit_PMI=5.607, qty=50 → total=280.35.
        lcctpunet=280.35 expu=' ' → unit=280.35.
        abs(280.35 - 280.35) < 0.01 → conforme (matche unitaire).
        NOTE: Ce cas matche déjà l'unitaire car expu=' ' et lcctpunet=280.35.
        """
        lignes = [
            {
                "Reference": "19002392",
                "QuantiteAR": 50.0,
                "PrixAR": 280.35,
                "PrixERP": 5.607,  # recorder a calculé faux unitaire
                "TypeEcart": "ecart_prix",
                "Situation": "NOK",
            }
        ]
        pmi_lines = [
            {
                "lcctcode": "100073",
                "lcctcodart": "ART19002392",
                "lcctrefext": "19002392",
                "lcctqty": 50.0,
                "lcctpunet": 280.35,  # expu ' ' → unit=280.35 (FIX C recalcule)
                "lcctexpu": " ",
            }
        ]
        result = _reconcile_lines(lignes, pmi_lines)
        assert len(result) == 1
        line = result[0]
        assert line["TypeEcart"] is None, (
            f"CF101227-19002392 attendu conforme, got {line['TypeEcart']!r}"
        )
        assert line["Situation"] == "OK"

    def test_cf101227_19002392_expu_c_prix_ar_total(self):
        """CF101227 19002392 (variante EXPU C) : PrixAR=280.35 = total.
        lcctpunet=28035.0 expu='C' → unit=280.35. Prix_ar=280.35 == unit → conforme déjà.
        Variante critique : lcctpunet=560.7 expu='C' → unit=5.607, total=280.35.
        prix_ar=280.35 == total=280.35 → conforme (FAUX ecart_prix sans ce fix).
        """
        lignes = [
            {
                "Reference": "19002392B",
                "QuantiteAR": 50.0,
                "PrixAR": 280.35,  # total = 5.607 × 50
                "PrixERP": 5.607,
                "TypeEcart": "ecart_prix",
                "Situation": "NOK",
            }
        ]
        pmi_lines = [
            {
                "lcctcode": "100073",
                "lcctcodart": "ART19002392B",
                "lcctrefext": "19002392B",
                "lcctqty": 50.0,
                "lcctpunet": 560.7,   # centièmes → unit=5.607, total=280.35
                "lcctexpu": "C",
            }
        ]
        result = _reconcile_lines(lignes, pmi_lines)
        assert len(result) == 1
        line = result[0]
        assert line["TypeEcart"] is None, (
            f"CF101227-19002392B attendu conforme (prix_ar=total), got {line['TypeEcart']!r}"
        )
        assert line["Situation"] == "OK"

    def test_cf101227_19002355_prix_ar_total_expu_c(self):
        """CF101227 19002355 : PrixAR=880.9 = 8.809×100 (total).
        lcctpunet=880.9 expu='C' → unit=8.809, total=880.9.
        abs(880.9 - 8.809) >= 0.01 → FAUX ecart_prix sans ce fix.
        Attendu : conforme car prix_ar=880.9 == prix_erp_total=880.9.
        """
        lignes = [
            {
                "Reference": "19002355",
                "QuantiteAR": 100.0,
                "PrixAR": 880.9,  # total
                "PrixERP": 8.809,
                "TypeEcart": "ecart_prix",
                "Situation": "NOK",
            }
        ]
        pmi_lines = [
            {
                "lcctcode": "100073",
                "lcctcodart": "ART19002355",
                "lcctrefext": "19002355",
                "lcctqty": 100.0,
                "lcctpunet": 880.9,   # centièmes → unit=8.809
                "lcctexpu": "C",
            }
        ]
        result = _reconcile_lines(lignes, pmi_lines)
        assert len(result) == 1
        line = result[0]
        assert line["TypeEcart"] is None, (
            f"CF101227-19002355 attendu conforme, got {line['TypeEcart']!r}"
        )
        assert line["Situation"] == "OK"

    def test_cf101227_19002350_prix_ar_total_expu_c(self):
        """CF101227 19002350 : PrixAR=1522.5 = 15.225×100 (total).
        lcctpunet=1522.5 expu='C' → unit=15.225, total=1522.5.
        abs(1522.5 - 15.225) >= 0.01 → FAUX ecart_prix sans ce fix.
        Attendu : conforme car prix_ar=1522.5 == prix_erp_total=1522.5.
        """
        lignes = [
            {
                "Reference": "19002350",
                "QuantiteAR": 100.0,
                "PrixAR": 1522.5,  # total
                "PrixERP": 15.225,
                "TypeEcart": "ecart_prix",
                "Situation": "NOK",
            }
        ]
        pmi_lines = [
            {
                "lcctcode": "100073",
                "lcctcodart": "ART19002350",
                "lcctrefext": "19002350",
                "lcctqty": 100.0,
                "lcctpunet": 1522.5,  # centièmes → unit=15.225
                "lcctexpu": "C",
            }
        ]
        result = _reconcile_lines(lignes, pmi_lines)
        assert len(result) == 1
        line = result[0]
        assert line["TypeEcart"] is None, (
            f"CF101227-19002350 attendu conforme, got {line['TypeEcart']!r}"
        )
        assert line["Situation"] == "OK"

    def test_vrai_ecart_prix_sans_pdf_conforme_via_qty(self):
        """FIX 1 iter6 : sans pdf_text, pas de verdict prix via recorder LLM.

        MISE A JOUR iter6 : matches_total SUPPRIME. Sans pdf_text, _pdf_prix_found=False.
        qty=10==PMI 10 -> conforme meme si PrixAR recorder ne matche pas PrixERP.
        Le verdict ecart_prix via recorder LLM est supprime par decision David.
        """
        lignes = [
            {
                "Reference": "ARTVRAIECART12",
                "QuantiteAR": 10.0,
                "PrixAR": 50.0,
                "PrixERP": 30.0,
                "TypeEcart": "ecart_prix",
                "Situation": "NOK",
            }
        ]
        pmi_lines = [
            {
                "lcctcode": "100042",
                "lcctcodart": "ARTVRAIECART12",
                "lcctrefext": "ARTVRAIECART12",
                "lcctqty": 10.0,
                "lcctpunet": 30.0,
                "lcctexpu": " ",
            }
        ]
        result = _reconcile_lines(lignes, pmi_lines)
        assert len(result) == 1
        line = result[0]
        # Sans pdf_text: _pdf_prix_found=False, qty=10==PMI 10 -> conforme
        assert line["TypeEcart"] is None, (
            f"Conforme attendu (qty match, pas de verdict prix sans PDF), "
            f"got {line['TypeEcart']!r}"
        )
        assert line["Situation"] == "OK"


    def test_lcctqty_none_prix_ar_ne_matche_sans_pdf_conforme(self):
        """FIX 1 iter6 : sans pdf_text, aucun verdict prix (meme lcctqty=None).

        MISE A JOUR iter6 : Sans pdf_text, _pdf_prix_found=False -> pas de verdict prix.
        qty_ar=None, lcctqty=None -> FIX D (qty=None AND prix_ar sans PDF -> conforme).
        Comportement : conforme (ref trouvee dans PMI, aucun ecart mesurable sans PDF).
        """
        lignes = [
            {
                "Reference": "ARTQTYNONE1234",
                "QuantiteAR": None,
                "PrixAR": 50.0,
                "PrixERP": 30.0,
                "TypeEcart": "ecart_prix",
                "Situation": "NOK",
            }
        ]
        pmi_lines = [
            {
                "lcctcode": "100042",
                "lcctcodart": "ARTQTYNONE1234",
                "lcctrefext": "ARTQTYNONE1234",
                "lcctqty": None,
                "lcctpunet": 30.0,
                "lcctexpu": " ",
            }
        ]
        result = _reconcile_lines(lignes, pmi_lines)
        assert len(result) == 1
        line = result[0]
        # qty_ar=None, _pdf_prix_found=False -> FIX D: conforme
        assert line["TypeEcart"] is None


# ===========================================================================
# FIX A — lcctpunet=0.0 ne doit pas basculer sur lccnpunet (falsy trap)
# ===========================================================================

class TestFixALcctpunetZero:
    """lcctpunet=0.0 est falsy en Python → `or` bascule à tort sur lccnpunet.
    Vérifier que _reconcile_lines traite 0.0 comme un prix valide (pas None).
    """

    def test_lcctpunet_zero_prix_erp_zero_pas_none(self):
        """lcctpunet=0.0 → prix_erp_unit=0.0 (pas None). prix_ar=0.0 → conforme."""
        lignes = [
            {
                "Reference": "ARTZEROPRIX12",
                "QuantiteAR": 5.0,
                "PrixAR": 0.0,
                "PrixERP": 99.0,  # recorder avait mis une valeur non nulle
                "TypeEcart": "ecart_prix",
                "Situation": "NOK",
            }
        ]
        pmi_lines = [
            {
                "lcctcode": "100042",
                "lcctcodart": "ARTZEROPRIX12",
                "lcctrefext": "ARTZEROPRIX12",
                "lcctqty": 5.0,
                "lcctpunet": 0.0,    # zéro — falsy en Python
                "lccnpunet": 999.0,  # ne doit PAS être lu à la place
                "lcctexpu": " ",
            }
        ]
        result = _reconcile_lines(lignes, pmi_lines)
        assert len(result) == 1
        line = result[0]
        # prix_erp_unit doit être 0.0 (lcctpunet=0.0 expu=' ')
        assert line["PrixERP"] == 0.0
        # prix_ar=0.0 == prix_erp_unit=0.0 → conforme
        assert line["TypeEcart"] is None
        assert line["Situation"] == "OK"
