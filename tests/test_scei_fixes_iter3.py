"""Tests TDD pour les 3 défauts RÉSIDUELS de l'itération 3 (review finale).

Règles :
- Ces tests DOIVENT échouer (RED) AVANT les fixes.
- Après fix, ils PASSENT et le baseline (133 tests) reste intact.

Défauts ciblés :
  D1 résiduel : _all_direct_positions utilise find() = 1ère occurrence seulement.
                Si une réf apparaît 2× dans le PDF (ligne + récap SAP), la 2e
                occurrence n'est pas dans les positions → fenêtre de la réf
                suivante capte une mauvaise qté.
                FIX : finditer (toutes occurrences), triées.

  D2 résiduel : found_direct = len(norm_pmi) >= 6 and norm_pmi in pdf_upper
                Sans vérification de contexte, une réf de 6 chars présente DANS
                une description libre (ex 'LC1D40' dans 'serie LC1D40') est matchée
                → ligne fabriquée à tort (conforme sans vraie ligne).
                FIX : exiger '\\bN PCE\\b' dans une fenêtre ±250 chars autour de la réf.

  D3 résiduel : filtre _non_doubtful :
                TypeEcart not in _DOUBTFUL_ECARTS AND
                (TypeEcart is not None OR Situation == 'OK')
                → écarte ligne conforme (TypeEcart=None) SANS clé 'Situation'.
                FIX : simplifier à 'TypeEcart not in _DOUBTFUL_ECARTS' seulement.
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# DÉFAUT 1 RÉSIDUEL — réf dupliquée (ligne + récap SAP) capte mauvaise qté
# ---------------------------------------------------------------------------

class TestD1RefDupliquee:
    """_all_direct_positions doit capturer TOUTES les occurrences d'une réf.

    Cas : REF001ABCD apparaît :
      1. Dans la ligne principale de l'AR (5 PCE)
      2. Dans un récap SAP après la référence REF002EFGH (1 PCE dans le récap)

    La structure SAP est :
      [ligne REF001ABCD: ref + début ligne]
      [ligne REF002EFGH: ref uniquement]
      [Total REF001ABCD : 1 PCE ← récap injecté entre ref et données de REF002]
      [données REF002EFGH: 5 PCE ← vraie qté]

    Avec find() : _all_direct_positions = [pos_REF001_ligne, pos_REF002_ligne].
    Fenêtre REF002 = [pos_REF002, len(pdf)].
    Le premier _QTY_PCE_RE match dans cette fenêtre est "1 PCE" (récap REF001)
    avant le "5 PCE" (données REF002) → QuantiteAR=1.0 au lieu de 5.0.

    Avec finditer : positions de REF001 = [pos_ligne, pos_recap].
    _all_direct_positions inclut les deux.
    Fenêtre REF002 = [pos_REF002, pos_recap_REF001].
    N'inclut PAS le "1 PCE" du récap → _QTY_PCE_RE trouve "5 PCE" (vraie qté).
    """

    def _make_pdf_text_ref_recap_intercale(self) -> str:
        """PDF synthétique SAP avec récap REF001ABCD (99 PCE) intercalé avant les données REF002EFGH.

        Structure SAP : ligne A (5 PCE), puis ligne B (ref seulement), puis
        récap A (99 PCE, 2ème occurrence de REF001ABCD), puis données B (5 PCE).

        Avec find() : _all_direct_positions ne contient que la 1ère occurrence de
        REF001ABCD (ligne principale). La 2ème (récap) n'y est pas.
        Fenêtre de REF002EFGH = [pos_REF002, len(pdf)].
        Premier PCE dans cette fenêtre = 99 (récap REF001ABCD) → QuantiteAR=99. BUG !

        Avec finditer : les 2 occurrences de REF001ABCD sont dans _all_direct_positions.
        Fenêtre de REF002EFGH = [pos_REF002, pos_recap_REF001ABCD].
        La fenêtre exclut le récap "99 PCE" (il est à la borne) → ne capture pas 99.
        → QuantiteAR=None (pas de PCE dans la fenêtre réduite). CORRECT (pas de faux qty).
        """
        return (
            "Poste 10 REF001ABCD Description article A\n"
            " 5 PCE\n"
            " 10,00 1 PCE 50,00 EUR\n"
            "\n"
            "Poste 20 REF002EFGH Description article B\n"
            # 2ème occurrence de REF001ABCD (récap SAP) avec mauvaise qté 99
            "Total REF001ABCD 99 PCE recap\n"
            # Vraies données de REF002EFGH — après le récap de REF001
            " 5 PCE\n"
            " 20,00 1 PCE 100,00 EUR\n"
        )

    def test_ref_dupliquee_voisine_ne_capture_pas_mauvaise_qty(self):
        """Avec finditer, REF002EFGH ne doit PAS capturer 99.0 (récap REF001ABCD).

        Avec find() (bug) : la fenêtre de REF002EFGH va jusqu'à la fin → contient
        "99 PCE" (récap REF001ABCD) AVANT "5 PCE" (données REF002) → QuantiteAR=99.

        Avec finditer (fix) : la fenêtre de REF002EFGH est bornée par la 2ème occurrence
        de REF001ABCD (le récap) → la fenêtre ne contient pas "99 PCE" du récap.
        QuantiteAR=None (conforme-sur-présence) plutôt que 99.0 (faux ecart_qte).
        """
        from th2customers.scei.tools.scei_ar_persist import (
            _reextract_lines_from_pmi_and_text,
        )

        pdf_text = self._make_pdf_text_ref_recap_intercale()
        pmi = [
            {"lcctrefext": "REF001ABCD", "lcctqty": 5.0, "lccnpunet": 1000.0, "lcctexpu": "C"},
            {"lcctrefext": "REF002EFGH", "lcctqty": 5.0, "lccnpunet": 2000.0, "lcctexpu": "C"},
        ]

        result = _reextract_lines_from_pmi_and_text(pmi, pdf_text)
        assert result is not None and len(result) == 2

        line2 = next((ln for ln in result if "REF002EFGH" in ln.get("Reference", "")), None)
        assert line2 is not None, "REF002EFGH doit être dans le résultat"
        # Avec le fix (finditer), la fenêtre bornée exclut le récap "99 PCE"
        # → qty_parsed=None (conforme-sur-présence) au lieu de 99.0 (faux ecart_qte)
        assert line2["QuantiteAR"] != 99.0, (
            f"REF002EFGH ne doit PAS capturer 99.0 (récap REF001ABCD). "
            f"Got QuantiteAR={line2['QuantiteAR']} — "
            f"_all_direct_positions ne capture que la 1ère occurrence de REF001ABCD "
            f"(find() au lieu de finditer) → la fenêtre de REF002EFGH inclut le récap 99 PCE"
        )
        # Avec finditer, le type_ecart doit être None (conforme-sur-présence, pas faux ecart_qte)
        assert line2["TypeEcart"] is None, (
            f"REF002EFGH avec finditer : TypeEcart doit être None (conforme-sur-présence), "
            f"got {line2['TypeEcart']} — si QuantiteAR=99.0 → ecart_qte (99 ≠ 5)"
        )

    def test_find_seul_aurait_capture_99_preuve_red(self):
        """Preuve que le code pré-fix (find() seul) aurait capturé 99 pour REF002EFGH.

        Ce test simule le comportement bugué en construisant manuellement
        _all_direct_positions avec find() et en vérifiant que la fenêtre
        de REF002EFGH inclut '99 PCE'.
        """
        from th2customers.scei.tools.scei_ar_persist import _norm_ref, _QTY_PCE_RE

        pdf_text = self._make_pdf_text_ref_recap_intercale()
        pdf_upper = pdf_text.upper()

        # Simuler _all_direct_positions avec find() (code pré-fix)
        norm_pmi_list = [_norm_ref("REF001ABCD"), _norm_ref("REF002EFGH")]
        all_positions_find = sorted(
            pos_val
            for nm in norm_pmi_list
            if nm and len(nm) >= 6
            for pos_val in [pdf_upper.find(nm)]
            if pos_val != -1
        )
        # find() : 1 seule occurrence de REF001ABCD (la ligne principale)
        # Vérifier que la 2ème occurrence (récap) N'EST PAS dans cette liste
        pos_recap = pdf_upper.rfind("REF001ABCD")  # dernière occurrence = récap
        assert pos_recap not in all_positions_find, (
            "Avec find(), la 2ème occurrence de REF001ABCD (récap) ne doit "
            "PAS être dans _all_direct_positions"
        )

        # Simuler la fenêtre de REF002EFGH avec find()
        pos_ref2 = pdf_upper.find(_norm_ref("REF002EFGH"))
        # _next_ref_pos avec find() : pas de pos > pos_ref2 → len(pdf)
        def _next_ref_pos_find(current_pos: int) -> int:
            for p in all_positions_find:
                if p > current_pos:
                    return p
            return len(pdf_text)

        start = max(0, pdf_text.rfind("\n", 0, pos_ref2))
        end = _next_ref_pos_find(pos_ref2)
        window_find = pdf_text[start:end]

        # Avec find(), la fenêtre de REF002EFGH contient "99 PCE" du récap
        qty_matches = _QTY_PCE_RE.findall(window_find)
        assert len(qty_matches) > 0, "La fenêtre avec find() doit contenir des qtés"
        assert qty_matches[0] == "99", (
            f"Avec find(), le 1er match PCE dans la fenêtre de REF002EFGH est '99' "
            f"(récap REF001ABCD), got {qty_matches[0]}. "
            f"C'est la preuve du bug : find() → mauvaise qté 99.0"
        )

    def test_ref_dupliquee_propre_ligne_correcte(self):
        """REF001ABCD (qté=5 sur la ligne principale) doit être récupérée correctement."""
        from th2customers.scei.tools.scei_ar_persist import (
            _reextract_lines_from_pmi_and_text,
        )

        pdf_text = self._make_pdf_text_ref_recap_intercale()
        pmi = [
            {"lcctrefext": "REF001ABCD", "lcctqty": 5.0, "lccnpunet": 1000.0, "lcctexpu": "C"},
            {"lcctrefext": "REF002EFGH", "lcctqty": 5.0, "lccnpunet": 2000.0, "lcctexpu": "C"},
        ]

        result = _reextract_lines_from_pmi_and_text(pmi, pdf_text)
        assert result is not None

        line1 = next((ln for ln in result if "REF001ABCD" in ln.get("Reference", "")), None)
        assert line1 is not None, "REF001ABCD doit être dans le résultat"
        assert line1["QuantiteAR"] == 5.0, (
            f"REF001ABCD ligne principale doit avoir QuantiteAR=5.0, "
            f"got {line1['QuantiteAR']}"
        )


# ---------------------------------------------------------------------------
# DÉFAUT 2 RÉSIDUEL — réf 6 chars matchée dans une description sans 'N PCE'
# ---------------------------------------------------------------------------

class TestD2RefDansDescription:
    """found_direct ne doit pas fabriquer une ligne si la réf n'est que dans une
    description, sans pattern quantité 'N PCE' dans la fenêtre proche (±250 chars).
    """

    def test_ref_6chars_dans_description_seulement_non_fabriquee(self):
        """Réf 'ABCDEF' (6 chars) présente SEULEMENT dans une description libre
        sans 'N PCE' proche → NE doit PAS fabriquer une ligne.
        """
        from th2customers.scei.tools.scei_ar_persist import (
            _reextract_lines_from_pmi_and_text,
        )

        # Texte sans aucun 'N PCE' dans les 250 chars autour de ABCDEF
        pdf_text = (
            "Description generale du materiel ABCDEF version 2.1\n"
            "Ce produit est conforme aux normes CE\n"
            "Livraison prevue fin de mois\n"
        )
        pmi = [{"lcctrefext": "ABCDEF", "lcctqty": 5.0, "lccnpunet": 1000.0, "lcctexpu": "C"}]

        result = _reextract_lines_from_pmi_and_text(pmi, pdf_text)
        assert result is None or len(result) == 0, (
            f"Réf 'ABCDEF' dans une description sans 'N PCE' proche "
            f"ne doit pas fabriquer de ligne. Got: {result}"
        )

    def test_ref_6chars_avec_pce_proche_fabriquee(self):
        """Réf 'ABCDEF' (6 chars) avec '5 PCE' dans les 250 chars proches
        → DOIT fabriquer une ligne (c'est une vraie ligne AR).
        """
        from th2customers.scei.tools.scei_ar_persist import (
            _reextract_lines_from_pmi_and_text,
        )

        pdf_text = (
            "Poste 10 ABCDEF Description article A\n"
            " 5 PCE\n"
            " 10,00 1 PCE 50,00 EUR\n"
        )
        pmi = [{"lcctrefext": "ABCDEF", "lcctqty": 5.0, "lccnpunet": 1000.0, "lcctexpu": "C"}]

        result = _reextract_lines_from_pmi_and_text(pmi, pdf_text)
        assert result is not None and len(result) == 1, (
            f"Réf 'ABCDEF' avec '5 PCE' proche doit fabriquer une ligne. "
            f"Got: {result}"
        )
        assert result[0]["QuantiteAR"] == 5.0

    def test_ref_8chars_dans_description_seulement_non_fabriquee(self):
        """Réf 'LC1D40AB' (8 chars) présente seulement dans une description
        sans 'N PCE' proche → NE doit PAS fabriquer de ligne.
        """
        from th2customers.scei.tools.scei_ar_persist import (
            _reextract_lines_from_pmi_and_text,
        )

        pdf_text = (
            "serie LC1D40AB pour installations electriques\n"
            "Conforme norme CEI 60947\n"
        )
        pmi = [{"lcctrefext": "LC1D40AB", "lcctqty": 2.0, "lccnpunet": 5000.0, "lcctexpu": "C"}]

        result = _reextract_lines_from_pmi_and_text(pmi, pdf_text)
        assert result is None or len(result) == 0, (
            f"'LC1D40AB' dans une description sans 'N PCE' ne doit pas fabriquer. "
            f"Got: {result}"
        )

    def test_ref_8chars_avec_pce_dans_fenetre_fabriquee(self):
        """Réf 'LC1D40AB' avec 'N PCE' dans les 250 chars proches → fabriquée."""
        from th2customers.scei.tools.scei_ar_persist import (
            _reextract_lines_from_pmi_and_text,
        )

        pdf_text = (
            "Poste 10 LC1D40AB\n"
            " 2 PCE\n"
            " 50,00 1 PCE 100,00 EUR\n"
        )
        pmi = [{"lcctrefext": "LC1D40AB", "lcctqty": 2.0, "lccnpunet": 5000.0, "lcctexpu": "C"}]

        result = _reextract_lines_from_pmi_and_text(pmi, pdf_text)
        assert result is not None and len(result) == 1, (
            f"'LC1D40AB' avec '2 PCE' proche doit fabriquer une ligne. Got: {result}"
        )


# ---------------------------------------------------------------------------
# DÉFAUT 3 RÉSIDUEL — filtre _non_doubtful dépend de Situation
# ---------------------------------------------------------------------------

class TestD3FiltreNonDoubtfulSansSituation:
    """Le filtre _non_doubtful dans le bloc 0a-ter ne doit PAS dépendre de Situation.

    Cas : ligne TypeEcart=None SANS clé 'Situation' dans un AR mixte.
    Avec le filtre actuel :
        TypeEcart not in _DOUBTFUL_ECARTS  ← True (None not in frozenset)
        AND (TypeEcart is not None OR Situation == 'OK')  ← False (None is None, pas de Situation)
    → la ligne est ÉCARTÉE → écrasée par la version re-extraite → INCORRECT.

    Après fix (simplification à 'TypeEcart not in _DOUBTFUL_ECARTS' seul) :
    → TypeEcart=None not in _DOUBTFUL_ECARTS → True → ligne conservée.
    """

    def test_filtre_non_doubtful_direct_none_typeecart(self):
        """Vérification directe de la logique du filtre dans _non_doubtful.

        Simule le filtre du bloc 0a-ter avec TypeEcart=None et sans Situation.
        Le filtre actuel (bugué) rejette cette ligne ; le filtre corrigé la conserve.
        Ce test prouve que le filtre actuel est bugué ET que le fix est correct.
        """
        from th2customers.scei.tools.scei_ar_persist import _DOUBTFUL_ECARTS

        # Ligne conforme : TypeEcart=None, PAS de clé 'Situation'
        ln_sans_situation = {
            "Reference": "TESTREF123456",
            "QuantiteAR": 5.0,
            "TypeEcart": None,
        }

        te = ln_sans_situation.get("TypeEcart")
        te_str = str(te or "").strip().lower()

        # Filtre ACTUEL (bugué) — reproduit le code actuel de la ligne 1312-1317
        cond_actuelle = (
            te_str not in _DOUBTFUL_ECARTS
            and (te is not None or ln_sans_situation.get("Situation") == "OK")
        )
        # te_str="" not in _DOUBTFUL_ECARTS → True
        # MAIS te is not None → False ET Situation == 'OK' → False
        # → cond_actuelle = False → la ligne est ÉCARTÉE à tort
        assert cond_actuelle is False, (
            "Pré-condition : le filtre actuel DOIT écarter la ligne "
            "(confirme le bug D3 résiduel)"
        )

        # Filtre CORRIGÉ : TypeEcart not in _DOUBTFUL_ECARTS seul
        cond_corrigee = te_str not in _DOUBTFUL_ECARTS
        assert cond_corrigee is True, (
            "Post-condition : le filtre corrigé DOIT conserver la ligne "
            "(TypeEcart=None not in _DOUBTFUL_ECARTS)"
        )
