"""TDD — layout quantite NEXANS (cables au metre).

Live 2026-06-02 CF101254 (NEXANS FRANCE, log 4909) : la quantite est en METRES
("175,00 M"), mais l'intake prend le "1" (nombre de coupes, "1*175 M") -> faux
ecart_qte sur les lignes pourtant matchees. _extract_qty_price_for_ref doit lire
la valeur en metres. Fixture verbatim (fitz) du vrai PDF.

Fonctions pures, pas de DB.
Run: .venv/bin/pytest tests/unit_scei/test_nexans_qty.py
"""
from th2customers.scei.tools.scei_ar_persist import _extract_qty_price_for_ref

# Verbatim (extrait fitz) : 2 articles NEXANS.
_PDF_NEXANS = """\
Accuse de reception NEXANS FRANCE
10
A3
10055591
TITANEX 1X95 MBH GL3000 MAX
175,00 M
12.450,50
1
KM
18 Aou 26
18 Aou 26
2.178,84
Des frais de services sont appliques pour toute coupe inferieure a 100 m
V/Code Produit : 19010027000
1*175 M
20
A3
10055590
TITANEX 1X70 MBH GL3000 MAX
675,00 M
9.430,10
1
KM
18 Aou 26
18 Aou 26
6.365,32
Des frais de services sont appliques pour toute coupe inferieure a 100 m
V/Code Produit : 19010033000
3*225 M
"""


def test_nexans_qty_is_meters_not_cuts_line1():
    # ref = [lcctrefext=10055591, lcctcodart=19010027000]
    qty, prix = _extract_qty_price_for_ref(["10055591", "19010027000"], _PDF_NEXANS)
    assert qty == 175.0, f"attendu 175 (metres), pas 1 (coupes) ; got {qty}"
    assert prix is None  # prix laisse a la logique aval (pas de faux ecart_prix)


def test_nexans_qty_is_meters_line2():
    qty, prix = _extract_qty_price_for_ref(["10055590", "19010033000"], _PDF_NEXANS)
    assert qty == 675.0, f"attendu 675 ; got {qty}"


def test_nexans_qty_via_codart_variant():
    # meme resultat si seul le codart (V/Code) est fourni comme variante.
    qty, _ = _extract_qty_price_for_ref(["19010027000"], _PDF_NEXANS)
    assert qty == 175.0


def test_non_nexans_pdf_untouched():
    # Un PDF sans "V/Code Produit" ne declenche PAS la branche NEXANS.
    pdf = "REF12345678\n2 PCE\n15.12\n30.24\n"
    qty, _ = _extract_qty_price_for_ref(["REF12345678"], pdf)
    assert qty != 175.0  # pas de fausse extraction metres


def test_m2_surface_not_captured():
    # Une surface "2,50 M2" en description ne doit PAS etre prise pour la qty.
    pdf = (
        "10\nA3\n10055591\nGAINE SURFACE 2,50 M2 SPECIALE\n175,00 M\n12.450,50\n"
        "1\nKM\nV/Code Produit : 19010027000\n1*175 M\n"
    )
    qty, _ = _extract_qty_price_for_ref(["10055591", "19010027000"], pdf)
    assert qty == 175.0, f"M2 ne doit pas etre capte ; got {qty}"


def test_forward_takes_qty_not_desc_length():
    # Une longueur parasite "50,00 M" en description (avant la qty) ne doit pas
    # etre prise : on veut la derniere qty M de la zone (175,00).
    pdf = (
        "10\nA3\n10055591\nCABLE RESERVE 50,00 M PUIS\n175,00 M\n12.450,50\n"
        "V/Code Produit : 19010027000\n1*175 M\n"
    )
    qty, _ = _extract_qty_price_for_ref(["10055591"], pdf)
    assert qty == 175.0, f"attendu 175 (derniere qty M), pas la longueur desc ; got {qty}"
