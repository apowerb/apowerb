"""TDD — ref_client (code article acheteur) : matching deterministe par codart.

Fondation ref_client : quand l'intake capte le Customer Item (= PMI LCCTCODART)
en ref_client, _reconcile_lines le matche via le codart exact, meme si l'AR ne
porte que la ref fournisseur (DOVITECH mono, RELLY multi). Robuste, sans proximite.

Fonctions pures, pas de DB.
Run: .venv/bin/pytest tests/unit_scei/test_ref_client.py
"""
from th2customers.scei.tools.scei_ar_persist import (
    _reconcile_lines,
    _derive_statut_global,
    _inject_ref_client,
)


def test_refclient_variant_matches_codart_dovitech():
    pmi = [{"lcctcodart": "570999860001", "lcctrefext": "", "lcctqty": 140.0}]
    lignes = [{"Reference": "D0160R30N01-01", "ref_client": "570999860001", "QuantiteAR": 140}]
    recon = _reconcile_lines(lignes, pmi)  # pas de pdf_text : match via variante codart
    assert recon[0]["TypeEcart"] != "LINE_NOT_IN_PO"
    assert recon[0]["Situation"] == "OK"
    assert _derive_statut_global("matched", recon) == "conforme"


def test_refclient_multiline_relly_all_match():
    # 5 lignes RELLY : ref fournisseur distincte, ref_client = codart -> 5/5 match
    # par codart exact (la ou la proximite mal-appariait).
    pmi = [{"lcctcodart": c, "lcctrefext": "", "lcctqty": 20.0} for c in
           ["574092110001", "574092120001", "574092130001", "574092140001", "574096990001"]]
    lignes = [
        {"Reference": "7MOLL1179", "ref_client": "574092110001", "QuantiteAR": 20},
        {"Reference": "7MOLL1180", "ref_client": "574092120001", "QuantiteAR": 20},
        {"Reference": "7MOLL1178", "ref_client": "574092130001", "QuantiteAR": 20},
        {"Reference": "7MOLL1181", "ref_client": "574092140001", "QuantiteAR": 20},
        {"Reference": "7MOLL1182", "ref_client": "574096990001", "QuantiteAR": 20},
    ]
    recon = _reconcile_lines(lignes, pmi)
    assert all(l["TypeEcart"] != "LINE_NOT_IN_PO" for l in recon), [l["TypeEcart"] for l in recon]
    assert _derive_statut_global("matched", recon) == "conforme"


def test_no_refclient_falls_back_unchanged():
    # Sans ref_client : comportement inchange (ref fournisseur ne matche pas le codart).
    pmi = [{"lcctcodart": "570999860001", "lcctrefext": "", "lcctqty": 140.0}]
    lignes = [{"Reference": "D0160R30N01-01", "QuantiteAR": 140}]
    recon = _reconcile_lines(lignes, pmi)
    assert recon[0]["TypeEcart"] == "LINE_NOT_IN_PO"


class _Ctx:
    def __init__(self, state):
        self.state = state


def test_inject_ref_client_by_ligne_numero():
    intake = {"extraction_results": {"lines": [
        {"ligne_numero": 1, "ref_fournisseur": "D0160R30N01-01", "ref_client": "570999860001"},
    ]}}
    ctx = _Ctx({"ar_intake": intake})
    lignes = [{"NumeroLigne": 1, "Reference": "D0160R30N01-01", "QuantiteAR": 140}]
    out = _inject_ref_client(lignes, ctx)
    assert out[0]["ref_client"] == "570999860001"


def test_inject_ref_client_fallback_by_ref():
    intake = {"extraction_results": {"lines": [
        {"ligne_numero": 7, "ref_fournisseur": "7MOLL1179", "ref_client": "574092110001"},
    ]}}
    ctx = _Ctx({"ar_intake": intake})
    # NumeroLigne ne correspond pas (1 vs 7) -> fallback par ref fournisseur
    lignes = [{"NumeroLigne": 1, "Reference": "7MOLL1179", "QuantiteAR": 20}]
    out = _inject_ref_client(lignes, ctx)
    assert out[0]["ref_client"] == "574092110001"


def test_inject_noop_without_intake():
    ctx = _Ctx({})
    lignes = [{"Reference": "X", "QuantiteAR": 1}]
    out = _inject_ref_client(lignes, ctx)
    assert "ref_client" not in out[0]

from th2customers.scei.schemas import ARLine


def test_short_ref_client_no_mismap():
    # ref_client court (8 chiffres) ne doit PAS sous-chainer un codart 12-digits.
    pmi = [{"lcctcodart": "123456780001", "lcctrefext": "", "lcctqty": 5.0}]
    lignes = [{"Reference": "WRONG-REF", "ref_client": "12345678", "QuantiteAR": 5}]
    recon = _reconcile_lines(lignes, pmi)
    assert recon[0]["TypeEcart"] == "LINE_NOT_IN_PO", "ref_client court ne doit pas matcher"


def test_arline_coerce_6_positional_with_ref_client():
    obj = ARLine.model_validate([1, "RE-110-35", "570999860001", 120, 5.15, "20260612"])
    assert obj.ref_client == "570999860001"
    assert obj.ref_fournisseur == "RE-110-35"
    assert obj.qty == 120


def test_arline_coerce_5_positional_still_works():
    obj = ARLine.model_validate([2, "RE-130-45", 180, 5.80, "20260612"])
    assert obj.ref_client is None
    assert obj.qty == 180


def test_inject_ambiguous_ref_not_used():
    # 2 lignes intake meme ref_fournisseur mais ref_client differents -> ambigu,
    # le fallback par ref ne doit PAS injecter (eviter le mauvais ref_client).
    intake = {"extraction_results": {"lines": [
        {"ligne_numero": 1, "ref_fournisseur": "DUP", "ref_client": "111111111111"},
        {"ligne_numero": 2, "ref_fournisseur": "DUP", "ref_client": "222222222222"},
    ]}}
    ctx = _Ctx({"ar_intake": intake})
    lignes = [{"NumeroLigne": 99, "Reference": "DUP", "QuantiteAR": 1}]  # pas de match par num
    out = _inject_ref_client(lignes, ctx)
    assert "ref_client" not in out[0], "conflit ambigu -> pas d'injection par ref"
