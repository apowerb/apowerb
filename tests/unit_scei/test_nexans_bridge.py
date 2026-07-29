"""TDD — pont NEXANS via LCCTREFEXT dans le bloc article.

Live 2026-06-02 CF101254 art30/40 : le "V/Code Produit" du PDF (19010033001/2)
DIVERGE du codart PMI (19020203001/2), mais le code fournisseur (10055586/8 =
PMI LCCTREFEXT, peuple) est dans le meme bloc. _bridge_unmatched_via_pdf 2e passe
ponte via REFEXT-dans-le-bloc.

Fonctions pures, pas de DB.
Run: .venv/bin/pytest tests/unit_scei/test_nexans_bridge.py
"""
from th2customers.scei.tools.scei_ar_persist import (
    _match_lines_to_pmi,
    _bridge_unmatched_via_pdf,
)

PMI = [
    {"lcctcodart": "19010033000", "lcctrefext": "10055590", "lcctqty": 675.0},
    {"lcctcodart": "19020203001", "lcctrefext": "10055586", "lcctqty": 800.0},
    {"lcctcodart": "19020203002", "lcctrefext": "10055588", "lcctqty": 600.0},
]

_PDF = """\
20
A3
10055590
TITANEX 1X70 MBH GL3000 MAX
675,00 M
V/Code Produit : 19010033000
3*225 M
30
A3
10055586
TITANEX 1X35 MBH GL4000 MAX
800,00 M
V/Code Produit : 19010033001
2*400 M
40
A3
10055588
TITANEX 1X50 MBH GL3000 MAX
600,00 M
V/Code Produit : 19010033002
2*300 M
"""


def test_bridge_via_refext_block_nexans():
    # art30/40 : ref AR = V/Code divergent -> non matche par codart.
    ar_refs = [["19010033001"], ["19010033002"]]
    matches = _match_lines_to_pmi(ar_refs, PMI)
    assert matches == [None, None], f"codart ne doit pas matcher ; got {matches}"
    matches = _bridge_unmatched_via_pdf(ar_refs, PMI, matches, _PDF)
    assert matches[0] is PMI[1], "art30 -> L003 (refext 10055586)"
    assert matches[1] is PMI[2], "art40 -> L004 (refext 10055588)"


def test_no_bridge_when_refext_absent_from_pdf():
    pmi = [{"lcctcodart": "19020203099", "lcctrefext": "99999999", "lcctqty": 10.0}]
    ar_refs = [["19010033001"]]
    matches = _match_lines_to_pmi(ar_refs, pmi)
    matches = _bridge_unmatched_via_pdf(ar_refs, pmi, matches, _PDF)
    assert matches[0] is None  # refext 99999999 absent du bloc -> pas de pont


def test_no_markers_no_refext_bridge():
    # PDF sans marqueur "V/Code Produit" -> 2e passe inerte.
    pmi = [{"lcctcodart": "ZZ", "lcctrefext": "10055586", "lcctqty": 1.0}]
    ar_refs = [["19010033001"]]
    pdf = "10055586 some text 19010033001 here"
    matches = _match_lines_to_pmi(ar_refs, pmi)
    matches = _bridge_unmatched_via_pdf(ar_refs, pmi, matches, pdf)
    assert matches[0] is None


def test_short_refext_no_bridge():
    # Un REFEXT < 8 chars ne doit pas ponter (anti-coincidence de sous-chaine).
    pmi = [{"lcctcodart": "19020203001", "lcctrefext": "12345", "lcctqty": 800.0}]
    ar_refs = [["19010033001"]]
    pdf = (
        "30\nA3\n12345\nTITANEX\n800,00 M\n"
        "V/Code Produit : 19010033001\n2*400 M\n"
    )
    matches = _match_lines_to_pmi(ar_refs, pmi)
    matches = _bridge_unmatched_via_pdf(ar_refs, pmi, matches, pdf)
    assert matches[0] is None, "refext 5 chars -> pas de pont (seuil 8)"
