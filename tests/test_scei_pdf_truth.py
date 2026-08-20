"""Tests TDD PDF-truth — lot 2 itération finale.

FIX 1 : _extract_qty_price_for_ref(ref_variants, pdf_text) -> (qty|None, prix|None)
         Helper pur réutilisé par _reextract_lines_from_pmi_and_text et _reconcile_lines.

FIX 2 : _reconcile_lines(lignes, pmi_lines, pdf_text=None)
         Quand pdf_text fourni, le PDF remplace le recorder LLM comme source de
         vérité pour qty/prix dans le verdict.

FIX 3 : tool_persist_ar_record câble _load_full_pdf_text -> _reconcile_lines
         (testé en intégration légère via monkeypatch).

Fixtures synthétiques au format SAP réel ("réf ... N PCE ... punet 1 PCE montant EUR").
"""
from __future__ import annotations

import pytest

from th2customers.scei.tools.scei_ar_persist import (
    _extract_qty_price_for_ref,  # type: ignore[attr-defined]  # RED — n'existe pas encore
    _reconcile_lines,
)


# ---------------------------------------------------------------------------
# Fixtures PDF synthétiques (format SAP Schneider prouvé live)
# ---------------------------------------------------------------------------

# CF101221 : GV2P07, prix PDF=43.26, qté=1 ; PMI lcctpunet=3905 expu='C' (unit=39.05)
# → ecart_prix attendu, PrixAR=43.26 (valeur PDF, PAS 390.5 du recorder)
_PDF_CF101221 = """\
GV2P07
43,26 1 PCE 43,26 EUR
1 PCE 43,26 EUR
"""

_PMI_CF101221 = [
    {
        "lcctcode": "100050",
        "lcctcodart": "GV2P07AE",
        "lcctrefext": "GV2P07",
        "lcctqty": 1.0,
        "lcctpunet": 3905.0,
        "lcctexpu": "C",
    }
]

# → conforme (prix PDF == prix_erp_unit)


# CF101232 L1 (457-4757) : prix PDF=3.09, PMI lcctpunet=309 expu='C' (unit=3.09)
# → conforme (3.09 == 3.09)
# CF101232 L2 (758-1650) : qté PDF=20, PMI lcctqty=60 → ecart_qte
# Les deux réfs sont séparées dans le texte (fenêtre bornée)
_PDF_CF101232 = """\
4574757
10 PCE
3,09 1 PCE 30,90 EUR
7581650
20 PCE
3,09 1 PCE 61,80 EUR
"""

_PMI_CF101232 = [
    {
        "lcctcode": "100065",
        "lcctcodart": "RS0213",
        "lcctrefext": "457-4757",
        "lcctqty": 10.0,
        "lcctpunet": 309.0,
        "lcctexpu": "C",
    },
    {
        "lcctcode": "100065",
        "lcctcodart": "SPEC02622",
        "lcctrefext": "758-1650",
        "lcctqty": 60.0,
        "lcctpunet": 309.0,
        "lcctexpu": "C",
    },
]


# ===========================================================================
# FIX 1 — _extract_qty_price_for_ref
# ===========================================================================

class TestExtractQtyPriceForRef:
    """_extract_qty_price_for_ref(ref_variants, pdf_text) -> (qty, prix)."""

    def test_returns_tuple(self):
        """La fonction retourne bien un tuple (qty, prix)."""
        result = _extract_qty_price_for_ref(["GV2P07"], _PDF_CF101221)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_cf101221_prix_et_qty_extraits(self):
        """GV2P07 : prix PDF=43.26, qté=1 extraits correctement."""
        qty, prix = _extract_qty_price_for_ref(["GV2P07"], _PDF_CF101221)
        assert qty == pytest.approx(1.0, abs=1e-3)
        assert prix == pytest.approx(43.26, abs=1e-2)


    def test_ref_absente_retourne_none_none(self):
        """Réf absente du PDF → (None, None)."""
        qty, prix = _extract_qty_price_for_ref(["REFABSENTE99"], _PDF_CF101221)
        assert qty is None
        assert prix is None

    def test_ref_courte_moins_6_chars_retourne_none_none(self):
        """Réf < 6 chars n'est pas cherchée (garde len >= 6)."""
        qty, prix = _extract_qty_price_for_ref(["GV2P0"], _PDF_CF101221)
        assert qty is None
        assert prix is None

    def test_fallback_second_variant(self):
        """Si la première variante n'est pas trouvée, essaie la deuxième."""
        qty, prix = _extract_qty_price_for_ref(["REFABSENTE", "GV2P07"], _PDF_CF101221)
        assert qty == pytest.approx(1.0, abs=1e-3)
        assert prix == pytest.approx(43.26, abs=1e-2)

    def test_pdf_vide_retourne_none_none(self):
        """PDF vide → (None, None)."""
        qty, prix = _extract_qty_price_for_ref(["GV2P07"], "")
        assert qty is None
        assert prix is None

    def test_ref_sans_contexte_pce_retourne_none_none(self):
        """Réf présente mais sans 'N PCE' dans la fenêtre → (None, None)."""
        pdf = "GV2P07 est une référence produit Schneider Electric"
        qty, prix = _extract_qty_price_for_ref(["GV2P07"], pdf)
        assert qty is None
        assert prix is None

    def test_cf101232_l1_prix_309_centimes(self):
        """457-4757 (normalisé 4574757) : prix=3.09, qté=10."""
        qty, prix = _extract_qty_price_for_ref(["457-4757", "RS0213"], _PDF_CF101232)
        assert qty == pytest.approx(10.0, abs=1e-3)
        assert prix == pytest.approx(3.09, abs=1e-2)

    def test_cf101232_l2_qte_20_prix_309(self):
        """758-1650 (normalisé 7581650) : qté=20 (pas 10), prix=3.09."""
        qty, prix = _extract_qty_price_for_ref(["758-1650", "SPEC02622"], _PDF_CF101232)
        assert qty == pytest.approx(20.0, abs=1e-3)
        assert prix == pytest.approx(3.09, abs=1e-2)


# ===========================================================================
# FIX 2 — _reconcile_lines avec pdf_text
# ===========================================================================

class TestReconcileLinesWithPdfText:
    """_reconcile_lines(lignes, pmi_lines, pdf_text) utilise PDF comme source de vérité."""

    def _lignes_cf101221_recorder_wrong(self):
        """Lignes telles que le recorder LLM les hallucine (PrixAR=390.5 = 10× trop)."""
        return [
            {
                "Reference": "GV2P07",
                "QuantiteAR": 1.0,
                "PrixAR": 390.5,  # hallucination LLM — INCORRECT
                "TypeEcart": "ecart_prix",
                "Situation": "NOK",
            }
        ]

    def test_sans_pdf_text_fallback_recorder(self):
        """pdf_text=None : pas de verdict prix via le recorder LLM (FIX 1 iter6).

        MISE A JOUR iter6 : ce test verificait l'ancien comportement matches_total
        avec le prix recorder. La decision David (itération 6 finale) supprime
        tout verdict prix basé sur le recorder LLM. Sans pdf_text, _pdf_prix_found
        reste False → pas de ecart_prix même si PrixAR recorder diffère de PrixERP.
        Comportement : qty=1.0==PMI 1.0 → conforme (pas de ecart_qte ni ecart_prix).
        """
        lignes = self._lignes_cf101221_recorder_wrong()
        result = _reconcile_lines(lignes, _PMI_CF101221)  # sans pdf_text
        # Sans pdf_text, _pdf_prix_found=False -> pas de verdict prix via recorder
        # qty=1.0 == PMI 1.0 -> conforme
        assert result[0]["TypeEcart"] is None

    def test_cf101221_pdf_text_corrige_vers_ecart_prix_correct(self):
        """Avec pdf_text, PrixAR=43.26 (PDF) détecte un vrai écart vs PMI 39.05."""
        lignes = self._lignes_cf101221_recorder_wrong()
        result = _reconcile_lines(lignes, _PMI_CF101221, pdf_text=_PDF_CF101221)
        assert result[0]["TypeEcart"] == "ecart_prix"

    def test_cf101221_prixar_corrige_a_valeur_pdf(self):
        """Avec pdf_text, ln['PrixAR'] doit être 43.26 (valeur PDF, PAS 390.5)."""
        lignes = self._lignes_cf101221_recorder_wrong()
        result = _reconcile_lines(lignes, _PMI_CF101221, pdf_text=_PDF_CF101221)
        assert result[0]["PrixAR"] == pytest.approx(43.26, abs=1e-2)

    def test_cf101221_quantitear_corrige_depuis_pdf(self):
        """Avec pdf_text, QuantiteAR=1.0 (PDF, conforme PMI)."""
        lignes = self._lignes_cf101221_recorder_wrong()
        result = _reconcile_lines(lignes, _PMI_CF101221, pdf_text=_PDF_CF101221)
        assert result[0]["QuantiteAR"] == pytest.approx(1.0, abs=1e-3)


    def test_cf101232_l1_conforme(self):
        """CF101232 L1 (457-4757) : prix PDF=3.09, PMI unit=3.09 → conforme."""
        lignes = [
            {
                "Reference": "457-4757",
                "QuantiteAR": 10.0,
                "PrixAR": 999.0,  # recorder wrong
                "TypeEcart": "ecart_prix",
                "Situation": "NOK",
            },
            {
                "Reference": "758-1650",
                "QuantiteAR": 20.0,
                "PrixAR": 3.09,
                "TypeEcart": None,
                "Situation": "OK",
            },
        ]
        result = _reconcile_lines(lignes, _PMI_CF101232, pdf_text=_PDF_CF101232)
        l1 = next(r for r in result if r["Reference"] == "457-4757")
        assert l1["TypeEcart"] is None, f"L1 doit être conforme, got {l1['TypeEcart']!r}"

    def test_cf101232_l2_ecart_qte(self):
        """CF101232 L2 (758-1650) : qté PDF=20, PMI lcctqty=60 → ecart_qte."""
        lignes = [
            {
                "Reference": "457-4757",
                "QuantiteAR": 10.0,
                "PrixAR": 3.09,
                "TypeEcart": None,
                "Situation": "OK",
            },
            {
                "Reference": "758-1650",
                "QuantiteAR": 20.0,
                "PrixAR": 3.09,
                "TypeEcart": None,
                "Situation": "OK",
            },
        ]
        result = _reconcile_lines(lignes, _PMI_CF101232, pdf_text=_PDF_CF101232)
        l2 = next(r for r in result if r["Reference"] == "758-1650")
        assert l2["TypeEcart"] == "ecart_qte", f"L2 doit être ecart_qte, got {l2['TypeEcart']!r}"
        assert l2["QuantiteAR"] == pytest.approx(20.0, abs=1e-3)

    def test_pdf_non_lisible_fallback_recorder(self):
        """pdf_text absent du PDF (None) → fallback recorder (rétro-compat)."""
        lignes = [
            {
                "Reference": "GV2P07",
                "QuantiteAR": 1.0,
                "PrixAR": 39.05,  # recorder correct cette fois
                "TypeEcart": None,
                "Situation": "OK",
            }
        ]
        result = _reconcile_lines(lignes, _PMI_CF101221, pdf_text=None)
        assert result[0]["TypeEcart"] is None  # conforme via recorder


    def test_signature_retrocompat_sans_pdf_text(self):
        """_reconcile_lines accepte 2 args (sans pdf_text) — rétro-compatibilité."""
        lignes = [
            {
                "Reference": "GV2P07",
                "QuantiteAR": 1.0,
                "PrixAR": 39.05,
                "TypeEcart": None,
                "Situation": "OK",
            }
        ]
        # Ne doit pas lever TypeError
        result = _reconcile_lines(lignes, _PMI_CF101221)
        assert isinstance(result, list)

    def test_qty_ar_none_prix_ar_none_conforme_inchange(self):
        """FIX D préservé : qty_ar=None ET prix_ar=None → conforme même avec pdf_text."""
        lignes = [
            {
                "Reference": "GV2P07",
                "QuantiteAR": None,
                "PrixAR": None,
                "TypeEcart": "ecart_prix",  # recorder wrong
                "Situation": "NOK",
            }
        ]
        result = _reconcile_lines(lignes, _PMI_CF101221, pdf_text=_PDF_CF101221)
        # Le PDF parse qty=1, prix=43.26 → ecart_prix correct (43.26 vs 39.05)
        # NB : pdf_qty=1.0 → qty_ar devient 1.0, pas None → FIX D ne s'applique pas
        # mais pdf_prix=43.26 vs prix_erp_unit=39.05 → ecart_prix
        assert result[0]["TypeEcart"] == "ecart_prix"


# ===========================================================================
# FIXES FINAUX (critiqueur) — itération 6
# ===========================================================================

# ---------------------------------------------------------------------------
# FIX 1 BLOQUANT — pas de verdict ecart_prix via prix recorder quand pdf_prix=None
# ---------------------------------------------------------------------------

class TestFix1NoPrixRecorderVerdict:
    """Le verdict ecart_prix ne se déclenche QUE si pdf_prix is not None.

    Quand la ref est absente du PDF (pdf_prix=None), aucun verdict prix
    n'est emis meme si prix_ar (recorder) differe de prix_erp_unit.
    La ligne peut rester conforme ou avoir ecart_qte, mais jamais ecart_prix
    sur la foi du seul recorder.
    """

    _PDF_SANS_RS = """\
AUTREREF123456
10 PCE
3,09 1 PCE 30,90 EUR
"""

    _PMI_RS = [
        {
            "lcctcode": "100065",
            "lcctcodart": "RSCODART",
            "lcctrefext": "758-1650",
            "lcctqty": 60.0,
            "lcctpunet": 309.0,
            "lcctexpu": "C",
        }
    ]

    def test_ref_absente_pdf_pas_de_ecart_prix(self):
        """Ref absente du PDF -> pdf_prix=None -> PAS de ecart_prix, meme si recorder wrong."""
        lignes = [
            {
                "Reference": "758-1650",
                "QuantiteAR": 60.0,
                "PrixAR": 999.99,
                "TypeEcart": None,
                "Situation": "OK",
            }
        ]
        result = _reconcile_lines(lignes, self._PMI_RS, pdf_text=self._PDF_SANS_RS)
        assert result[0]["TypeEcart"] != "ecart_prix", (
            "ecart_prix NE doit PAS se declencher quand pdf_prix=None, "
            f"got TypeEcart={result[0]['TypeEcart']!r}"
        )

    def test_ref_absente_pdf_ecart_qte_possible(self):
        """Ref absente du PDF -> qty reste recorder -> ecart_qte si qty recorder differe PMI."""
        lignes = [
            {
                "Reference": "758-1650",
                "QuantiteAR": 20.0,
                "PrixAR": 999.99,
                "TypeEcart": None,
                "Situation": "OK",
            }
        ]
        result = _reconcile_lines(lignes, self._PMI_RS, pdf_text=self._PDF_SANS_RS)
        assert result[0]["TypeEcart"] == "ecart_qte", (
            "ecart_qte attendu quand qty recorder differe PMI, "
            f"got TypeEcart={result[0]['TypeEcart']!r}"
        )

    def test_matches_total_supprime_faux_conforme(self):
        """matches_total ne doit PAS masquer un ecart reel.

        Cas : pdf_prix=None (ref absente PDF), recorder PrixAR=185.40 (= prix_erp_total 3.09*60).
        matches_total marcherait conforme MAIS pdf_prix=None -> on ne doit pas jugement prix.
        qty=60 (recorder) == PMI 60 -> conforme via qty match uniquement.
        """
        lignes = [
            {
                "Reference": "758-1650",
                "QuantiteAR": 60.0,
                "PrixAR": 185.40,
                "TypeEcart": None,
                "Situation": "OK",
            }
        ]
        result = _reconcile_lines(lignes, self._PMI_RS, pdf_text=self._PDF_SANS_RS)
        assert result[0]["TypeEcart"] is None, (
            "Conforme attendu (qty match, pas de verdict prix sans pdf_prix), "
            f"got TypeEcart={result[0]['TypeEcart']!r}"
        )

    def test_prix_ar_none_pas_de_verdict_prix(self):
        """PrixAR=None ET pdf_prix=None -> aucun verdict prix (FIX D preserve)."""
        lignes = [
            {
                "Reference": "758-1650",
                "QuantiteAR": 60.0,
                "PrixAR": None,
                "TypeEcart": None,
                "Situation": "OK",
            }
        ]
        result = _reconcile_lines(lignes, self._PMI_RS, pdf_text=self._PDF_SANS_RS)
        assert result[0]["TypeEcart"] is None


# ---------------------------------------------------------------------------
# FIX 2 BLOQUANT — fenetre bornee depuis _reconcile (_all_positions passe)
# ---------------------------------------------------------------------------

class TestFix2BoundedWindow:
    """_reconcile_lines calcule _all_positions et le passe a _extract_qty_price_for_ref.

    Vrai cas de contamination : ref A apparait dans le PDF SANS ligne tarifaire
    propre (description seule), ref B apparait juste apres avec une ligne tarifaire.
    Sans fenetre bornee, _extract_qty_price_for_ref de A capturera le prix de B.
    Avec fenetre bornee, A retourne (None, None) -> pas de verdict prix sur A.
    """

    # Ref A : apparait dans une description (pas de ligne tarifaire propre)
    # Ref B : a sa propre ligne tarifaire 99,00 1 PCE 99,00 EUR
    # Sans borne, la fenetre de A englobe la ligne tarifaire de B
    # -> prix de A = 99.00 (contamination). PMI A = 100.0/C = 1.00 -> ecart_prix FAUX
    _PDF_CONTAMINATION = """\
REFAAA111111 Description produit A sans prix propre
REFBBB222222
1 PCE
99,00 1 PCE 99,00 EUR
"""

    _PMI_CONTAMINATION = [
        {
            "lcctcode": "100001",
            "lcctcodart": "CODART_A",
            "lcctrefext": "REFAAA111111",
            "lcctqty": 1.0,
            "lcctpunet": 100.0,   # prix_erp_unit = 1.00 (expu='C')
            "lcctexpu": "C",
        },
        {
            "lcctcode": "100002",
            "lcctcodart": "CODART_B",
            "lcctrefext": "REFBBB222222",
            "lcctqty": 1.0,
            "lcctpunet": 9900.0,  # prix_erp_unit = 99.00 (expu='C')
            "lcctexpu": "C",
        },
    ]

    def test_ref_a_sans_ligne_tarifaire_pas_de_ecart_prix(self):
        """Ref A n'a pas de ligne tarifaire propre dans le PDF.

        Sans fenetre bornee : _extract capturait 99.00 (ligne de B)
        -> pdf_prix=99.00, vs PMI 1.00 -> FAUX ecart_prix.
        Avec fenetre bornee : la fenetre de A s'arrete avant B
        -> pas de match tarifaire -> pdf_prix=None -> PAS de ecart_prix.

        Ce test doit ECHOUER sans le FIX 2 (fenetre non bornee dans _reconcile).
        """
        lignes = [
            {
                "Reference": "REFAAA111111",
                "QuantiteAR": 1.0,
                "PrixAR": 1.00,  # recorder correct
                "TypeEcart": None,
                "Situation": "OK",
            },
            {
                "Reference": "REFBBB222222",
                "QuantiteAR": 1.0,
                "PrixAR": 99.00,
                "TypeEcart": None,
                "Situation": "OK",
            },
        ]
        result = _reconcile_lines(lignes, self._PMI_CONTAMINATION, pdf_text=self._PDF_CONTAMINATION)
        ligne_a = next(r for r in result if r["Reference"] == "REFAAA111111")
        # A n'a pas de ligne tarifaire -> pdf_prix=None -> pas de verdict prix
        # qty recorder (1) == PMI (1) -> conforme
        assert ligne_a["TypeEcart"] is None, (
            "Ref A sans ligne tarifaire propre ne doit PAS avoir ecart_prix (contamination fenetre). "
            f"got TypeEcart={ligne_a['TypeEcart']!r}, PrixAR={ligne_a.get('PrixAR')}"
        )

    def test_ref_b_avec_ligne_tarifaire_conforme(self):
        """Ref B a sa propre ligne tarifaire, conforme vs PMI 99.00."""
        lignes = [
            {
                "Reference": "REFAAA111111",
                "QuantiteAR": 1.0,
                "PrixAR": 1.00,
                "TypeEcart": None,
                "Situation": "OK",
            },
            {
                "Reference": "REFBBB222222",
                "QuantiteAR": 1.0,
                "PrixAR": 99.00,
                "TypeEcart": None,
                "Situation": "OK",
            },
        ]
        result = _reconcile_lines(lignes, self._PMI_CONTAMINATION, pdf_text=self._PDF_CONTAMINATION)
        ligne_b = next(r for r in result if r["Reference"] == "REFBBB222222")
        assert ligne_b["TypeEcart"] is None, (
            "Ref B doit etre conforme (prix 99.00 == PMI 99.00), "
            f"got TypeEcart={ligne_b['TypeEcart']!r}"
        )

    def test_ref_b_ecart_prix_detecte_sans_contamination(self):
        """Ref B avec un vrai ecart de prix est bien detecte, meme apres fix."""
        lignes = [
            {
                "Reference": "REFAAA111111",
                "QuantiteAR": 1.0,
                "PrixAR": 1.00,
                "TypeEcart": None,
                "Situation": "OK",
            },
            {
                "Reference": "REFBBB222222",
                "QuantiteAR": 1.0,
                "PrixAR": 99.00,
                "TypeEcart": None,
                "Situation": "OK",
            },
        ]
        # PMI modifie: B a prix_erp_unit=50.00, pdf=99.00 -> ecart_prix reel
        pmi_modifie = [
            {
                "lcctcode": "100001",
                "lcctcodart": "CODART_A",
                "lcctrefext": "REFAAA111111",
                "lcctqty": 1.0,
                "lcctpunet": 100.0,
                "lcctexpu": "C",
            },
            {
                "lcctcode": "100002",
                "lcctcodart": "CODART_B",
                "lcctrefext": "REFBBB222222",
                "lcctqty": 1.0,
                "lcctpunet": 5000.0,  # prix_erp_unit=50.00, pdf=99.00 -> ecart
                "lcctexpu": "C",
            },
        ]
        result = _reconcile_lines(lignes, pmi_modifie, pdf_text=self._PDF_CONTAMINATION)
        ligne_b = next(r for r in result if r["Reference"] == "REFBBB222222")
        assert ligne_b["TypeEcart"] == "ecart_prix", (
            "Ref B doit avoir ecart_prix (pdf=99.00 vs PMI=50.00), "
            f"got TypeEcart={ligne_b['TypeEcart']!r}"
        )


# ---------------------------------------------------------------------------
# FIX 3 MAJEUR — regex Net robuste (cas remise)
# ---------------------------------------------------------------------------

class TestFix3PunetRegexRemise:
    """_PUNET_RE capture le prix NET (juste avant '1 PCE'), pas le prix brut.

    Format Schneider reel avec remise :
      '204,35 66,00 69,48 1 PCE 625,31 EUR'
       brut   remise net         montant
    -> doit capturer 69,48 (net), pas 204,35 (brut).
    """

    def test_remise_capture_net_pas_brut(self):
        """Fenetre avec remise : extrait 69.48 (net), PAS 204.35 (brut)."""
        pdf = """\
GV2L1610AA
1 PCE
204,35 66,00 69,48 1 PCE 625,31 EUR
"""
        qty, prix = _extract_qty_price_for_ref(["GV2L1610AA"], pdf)
        assert prix == pytest.approx(69.48, abs=1e-2), (
            f"Prix net attendu 69.48, got {prix!r}. "
            "Le regex doit ancrer sur le nombre juste avant '1 PCE'."
        )

    def test_sans_remise_capture_unique_prix(self):
        """Fenetre sans remise : prix unique capture correctement."""
        pdf = """\
GV2P07ABCD
1 PCE
43,26 1 PCE 43,26 EUR
"""
        _, prix = _extract_qty_price_for_ref(["GV2P07ABCD"], pdf)
        assert prix == pytest.approx(43.26, abs=1e-2)

    def test_deux_refs_voisines_avec_remise_pas_de_contamination(self):
        """Format 2 refs voisines + remise : chacune son prix (fenetre bornee)."""
        import re as _re
        pdf = """\
LC1D1500F7AB
1 PCE
204,35 66,00 69,48 1 PCE 625,31 EUR
GV2P07ABCDEF
1 PCE
43,26 1 PCE 43,26 EUR
"""
        pdf_upper = pdf.upper()
        all_pos = []
        for ref in ["LC1D1500F7AB", "GV2P07ABCDEF"]:
            m = _re.search(_re.escape(ref), pdf_upper)
            if m:
                all_pos.append(m.start())
        all_pos.sort()

        _, prix_a = _extract_qty_price_for_ref(
            ["LC1D1500F7AB"], pdf, _all_positions=all_pos
        )
        _, prix_b = _extract_qty_price_for_ref(
            ["GV2P07ABCDEF"], pdf, _all_positions=all_pos
        )
        assert prix_a == pytest.approx(69.48, abs=1e-2), f"Ref A net attendu 69.48, got {prix_a}"
        assert prix_b == pytest.approx(43.26, abs=1e-2), f"Ref B net attendu 43.26, got {prix_b}"
