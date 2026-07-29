"""Tests TDD — nouveaux layouts fournisseurs pour _extract_qty_price_for_ref.

Familles testées :
  1. DANFOSS   — layout SAP vertical (pce minuscule, Pos./Quantité/Unité sur lignes séparées)
  2. CATS      — colonnes tabulées sans unité (décimal .)
  3. TILCO     — AR court sans unité, n° poste préfixe la réf
  4. RS COMPO  — unité UTE / PQT de N (conditionnement à multiplier)
  5. SOCOMEC   — colonnes jumbled, UNE seule Quantité (décimal virgule), Ligne = n° de ligne (entier)

Textes verbatim extraits avec fitz depuis les vrais PDFs prod (extract_all_pages_text).
Log IDs : Danfoss=4916, CATS=4709, TILCO CF101244=4430, TILCO CF101245=4434,
          RS=4461, SOCOMEC CF101270=4657, SOCOMEC CF101274=4886.

Correction 2026-06-01 : la colonne "accusée" de SOCOMEC est la DATE de départ accusée,
PAS une seconde quantité. La fixture _PDF_SOCOMEC_* et les tests ont été corrigés en
conséquence. L'ancien skip SOCOMEC (_SOCOMEC_DOUBLE_QTY_RE → None) était basé sur une
mauvaise lecture du tableau. Les tests qui attendaient None ont été remplacés.
"""
from __future__ import annotations

import pytest

from th2customers.scei.tools.scei_ar_persist import (
    _extract_qty_price_for_ref,
)


# ---------------------------------------------------------------------------
# Fixtures verbatim — extrait du texte réel (fitz.get_text()) de chaque PDF
# ---------------------------------------------------------------------------

# --- DANFOSS CF099446 (log 4916) ---
# Layout SAP : une valeur par ligne. Ordre : Pos / Quantité / Unité(pce) / Article
# Section ligne 1 (Pos=10, qté=110, article=130B5458, "Numéro d'article client 13170983010")
# Prix : 15,12 / 1 pce  (format "prix / 1 pce")
# Ref PMI côté SCEI : 13170983010 (numéro d'article client)
_PDF_DANFOSS = """\
Détails de la facture
Pos.
Quantité
Unité
Numéro d'article et Description
Prix unitaire
Montant HT
10
110
pce
130B5458
TR150 Control Panel LCP
Numéro d'article client 13170983010
Pays d'origine Chine
Code douanier 85049090
15,12
/
1
pce
1 663,20
20
15
pce
134H4926
TR-150P4K0T4E20H4XXCXXXSXXXXAXBXCXXXXDX
Numéro d'article client 13170984010
Pays d'origine Chine
137,49
/
1
pce
2 062,35
"""

# --- CATS CF100586 (log 4709) ---
# Colonnes tabulées : POS / NumArt / Desc / SPECxxxx / Quantité / Prix / Montant
# Pas d'unité. Décimal "."
# Ligne 1 : POS=1.00, ref=PR5MEC11, code=SPEC02160, qté=3.00
# Ligne 2 : POS=2.00, ref=PR1KS210, code=SPEC02161, qté=3.00
_PDF_CATS = """\
POS
Numéro de l'article
Description de l'article
Quantité
Prix
Montant
1.00
PR5MEC11
Compact plastic DIN Rail adapter kit 70 mm x 25 mm x
14,5 mm
SPEC02160

3.00
0.00
0.00
Fabricant: Teltonika Networks
Code Douanier: 3926909790
Pays d'origine: Lithuania
ROHS: Conforme
Garantie: 12 Months Bring-In


2.00
PR1KS210
MOBILE MAGNETIC SMA ANTENNA
SPEC02161

3.00
0.00
0.00
Fabricant: Teltonika Networks
Code Douanier: 8529106900
Pays d'origine: Lithuania
ROHS: Conforme
Garantie: 12 Months Bring-In


3.00
RUT200010000
RUT200, 4G/LTE (Cat 4), 3G, 2G
SPEC03039

3.00
0.00
0.00
Fabricant: Teltonika Networks
Code Douanier: 8517620000
Pays d'origine: Lithuania
ROHS: Conforme
Garantie: 12 Months Bring-In
"""

# --- TILCO CF101244 (log 4430) ---
# AR court. Pas d'unité. Décimal ".".
# Poste N°: 1 (séparé) puis "1 5735-1214-0001" (le "1" = n° de poste/ligne)
# Qté = 2 (juste avant les 2 montants PU=18.28 et total=36.56)
_PDF_TILCO_4430 = """\
03.29.38.98.67
03.29.38.98.69
Tél :
Fax :
SCEI CHARMES
Rue des Charmottes
Zone de l'Hermitage
88130
CHARMES
N°Offre :
26-1056
du
28/05/2026
CGAX
ACCUSE   DE   RECEPTION
Date de validité de l'offre :
27/06/2026
Conditions de paiement :
/ 1
Page 1/1
Chargé d'affaires :
Romain BALLAND
Code client: 00002
Vos références :
CF101244
Traite
60 Jours Date de facture
Montant Net HT
Référence
Désignation
Qté
PU Public
Lig.
Délai
Poste N°:
1
28/05/2026
1 5735-1214-0001
MONTANT W/O AFF CGAX REV E
PEINT UNE FACE
RAL 9002 PSST
2
18.28
36.56
18.28
36.56
03/06/2026
36.56
Total HT :
36.56
TILCO - Aéroport d'Epinal Mirecourt - 88500 JUVAINCOURT
"""

# --- TILCO CF101245 (log 4434) ---
# Même layout. Réf = 5739-2401. Qté = 2.
_PDF_TILCO_4434 = """\
03.29.38.98.67
03.29.38.98.69
Tél :
Fax :
SCEI CHARMES
Rue des Charmottes
Zone de l'Hermitage
88130
CHARMES
N°Offre :
26-1057
du
28/05/2026
COND
ACCUSE   DE   RECEPTION
Date de validité de l'offre :
27/06/2026
Conditions de paiement :
/ 1
Page 1/1
Chargé d'affaires :
Romain BALLAND
Code client: 00002
Vos références :
CF101245
Traite
60 Jours Date de facture
Montant Net HT
Référence
Désignation
Qté
PU Public
Lig.
Délai
Poste N°:
1
28/05/2026
1 5739-2401
TOLE DE FOND BATT CONDO CGAF REV F
PEINT UNE FACE
RAL 9002 PSST
2
56.37
112.74
56.37
112.74
03/06/2026
112.74
Total HT :
112.74
TILCO - Aéroport d'Epinal Mirecourt - 88500 JUVAINCOURT
"""

# --- RS COMPONENTS CF101232 (log 4461) ---
# Unités : UTE (unité) et "PQT de 10" (paquet de 10 → qté réelle = N × 10)
# Ligne 10 : ref=4574757, qté=10 UTE  → qté réelle = 10
# Ligne 20 : ref=7581650, qté=6 PQT de 10  → qté réelle = 60
# Piège : les N° de ligne (10, 20) sont en tête
_PDF_RS = """\
N°
Ligne
N° Article
Description
Qté U.D.V.

PU HT

Montant
HT
T.V.A
10
4574757
Panasonic CR-2032/BN Pile bouton
    10 UTE
      3,09
     30,90
20,00%
Code d'exportation HS 85065090
Pays d'origine Divers
*** SERVICE STANDARD ***
20
7581650
WAGO 222-412 Connecteur à levier
     6 PQT de 10
      4,80
     28,80
20,00%
Code d'exportation HS 85369010
Code d'importation HS 8536901000
Pays d'origine Allemagne
*** SERVICE STANDARD ***
"""

# --- SOCOMEC CF101270 (log 4657) ---
# Colonnes jumbled par pymupdf : ordre en-têtes puis valeurs.
# Colonnes réelles : Ligne | Quantité | Un. | Référence article | Date départ (demandée | accusée) | PU tarif | PU net | Montant
# En-tête "accusée" = colonne DATE de départ accusée (02.06.2026), PAS une quantité.
# Ligne=10 (n° de ligne), Quantité=4,00, Un.=3P, Réf=67120250.
# Texte verbatim fitz (contexte pertinent, extrait depuis le vrai PDF log 4657).
_PDF_SOCOMEC_4657 = """\
Désignation
Référence article
Un.
Quantité
Date de
arrondi HT
Montant
PU net
PU tarif
Ligne
accusée
Date de départ
planifiée
réception
demandée
02.06.2026
03.06.2026
02.06.2026
4,00
35,31
FUS gG T1 E 250A
141,24
11,77
67120250
3P
10
      1,59
Poids:
HU
Origine:
"""

# --- SOCOMEC CF101274 (log 4886) ---
# Même layout SOCOMEC. Ligne=10, Quantité=2,00, Un.=PC, Réf=15994064.
# CF101274 a une colonne Remise % supplémentaire → 67,00 = remise % (flanquée de dates).
# Texte verbatim fitz (extrait depuis le vrai PDF log 4886).
_PDF_SOCOMEC_4886 = """\
Désignation
Référence article
Un.
Remise %
Quantité
Date de
arrondi HT
Montant
PU net
PU tarif
Référence article
Ligne
accusée
Date de départ
planifiée
réception
demandée
02.06.2026
         67,00
03.06.2026
02.06.2026
2,00
149,06
AUTO TRANSFO 400/230V 200VA
298,11
451,68
15994064
PC
10
      4,25
"""


# ===========================================================================
# FAMILLE 1 — DANFOSS (layout SAP vertical, unité "pce" minuscule)
# ===========================================================================

class TestDanfossLayout:
    """Layout Danfoss : valeurs sur lignes séparées, unité = pce (minuscule)."""

    def test_danfoss_ref_130B5458_qty_110(self):
        """Ref Danfoss 130B5458 : qté réelle = 110 (Pos=10, ne pas prendre le 10)."""
        qty, _ = _extract_qty_price_for_ref(["130B5458"], _PDF_DANFOSS)
        assert qty == pytest.approx(110.0, abs=1e-3), (
            f"Attendu 110.0, got {qty!r}. Ne pas prendre le Pos=10."
        )

    def test_danfoss_ref_client_13170983010_qty_110(self):
        """Ref Danfoss par numéro d'article client 13170983010 : qté = 110."""
        qty, _ = _extract_qty_price_for_ref(["13170983010"], _PDF_DANFOSS)
        assert qty == pytest.approx(110.0, abs=1e-3)

    def test_danfoss_ref_134H4926_qty_15(self):
        """Ligne 2 Danfoss 134H4926 : qté = 15 (Pos=20, ne pas prendre le 20)."""
        qty, _ = _extract_qty_price_for_ref(["134H4926"], _PDF_DANFOSS)
        assert qty == pytest.approx(15.0, abs=1e-3), (
            f"Attendu 15.0, got {qty!r}. Ne pas prendre le Pos=20."
        )

    # Tests anti-faux-conforme

    def test_danfoss_ne_prend_pas_pos_10_comme_qty(self):
        """ANTI-FAUX-CONFORME : 130B5458 ne doit PAS renvoyer qty=10 (c'est le Pos.)."""
        qty, _ = _extract_qty_price_for_ref(["130B5458"], _PDF_DANFOSS)
        assert qty != pytest.approx(10.0, abs=1e-3), (
            "qty=10 est le numéro de poste Pos., PAS la quantité."
        )

    def test_danfoss_ne_prend_pas_1_comme_qty(self):
        """ANTI-FAUX-CONFORME : 130B5458 ne doit PAS renvoyer qty=1 (dénominateur prix '/ 1 pce')."""
        qty, _ = _extract_qty_price_for_ref(["130B5458"], _PDF_DANFOSS)
        assert qty != pytest.approx(1.0, abs=1e-3), (
            "qty=1 vient de '/ 1 pce' (dénominateur de prix), PAS la quantité."
        )


# ===========================================================================
# FAMILLE 2 — CATS (colonnes tabulées, sans unité, décimal .)
# ===========================================================================

class TestCatsLayout:
    """Layout CATS : POS / ref / desc / SPECxxxx / qté / prix / montant."""

    def test_cats_ref_PR5MEC11_qty_3(self):
        """CATS PR5MEC11 : qté = 3.00 (après SPEC02160)."""
        qty, _ = _extract_qty_price_for_ref(["PR5MEC11"], _PDF_CATS)
        assert qty == pytest.approx(3.0, abs=1e-3), f"Attendu 3.0, got {qty!r}"

    def test_cats_ref_PR1KS210_qty_3(self):
        """CATS PR1KS210 : qté = 3.00."""
        qty, _ = _extract_qty_price_for_ref(["PR1KS210"], _PDF_CATS)
        assert qty == pytest.approx(3.0, abs=1e-3), f"Attendu 3.0, got {qty!r}"

    def test_cats_ref_RUT200010000_qty_3(self):
        """CATS RUT200010000 : qté = 3.00."""
        qty, _ = _extract_qty_price_for_ref(["RUT200010000"], _PDF_CATS)
        assert qty == pytest.approx(3.0, abs=1e-3), f"Attendu 3.0, got {qty!r}"

    # Anti-faux-conforme

    def test_cats_ne_prend_pas_pos_1_comme_qty(self):
        """ANTI-FAUX-CONFORME : PR5MEC11 ne doit PAS renvoyer qty=1.00 (c'est le POS)."""
        qty, _ = _extract_qty_price_for_ref(["PR5MEC11"], _PDF_CATS)
        assert qty != pytest.approx(1.0, abs=1e-3), (
            "qty=1.00 est le numéro POS, PAS la quantité commandée."
        )


# ===========================================================================
# FAMILLE 3 — TILCO (AR court, sans unité, préfixe poste devant la réf)
# ===========================================================================

class TestTilcoLayout:
    """Layout TILCO : Poste N°: N / date / N ref / desc / qté / PU / total."""

    def test_tilco_4430_ref_5735_1214_0001_qty_2(self):
        """TILCO CF101244 : ref 5735-1214-0001, qté = 2."""
        qty, _ = _extract_qty_price_for_ref(["5735-1214-0001"], _PDF_TILCO_4430)
        assert qty == pytest.approx(2.0, abs=1e-3), f"Attendu 2.0, got {qty!r}"

    def test_tilco_4434_ref_5739_2401_qty_2(self):
        """TILCO CF101245 : ref 5739-2401, qté = 2."""
        qty, _ = _extract_qty_price_for_ref(["5739-2401"], _PDF_TILCO_4434)
        assert qty == pytest.approx(2.0, abs=1e-3), f"Attendu 2.0, got {qty!r}"

    # Anti-faux-conforme

    def test_tilco_ne_prend_pas_poste_1_comme_qty(self):
        """ANTI-FAUX-CONFORME : ne doit PAS renvoyer qty=1 (Poste N°: 1 ou préfixe '1 5735...')."""
        qty, _ = _extract_qty_price_for_ref(["5735-1214-0001"], _PDF_TILCO_4430)
        assert qty != pytest.approx(1.0, abs=1e-3), (
            "qty=1 est le numéro de poste ou le '1' devant la réf, PAS la qté."
        )

    def test_tilco_4434_ne_prend_pas_poste_1_comme_qty(self):
        """ANTI-FAUX-CONFORME CF101245 : ne doit PAS renvoyer qty=1."""
        qty, _ = _extract_qty_price_for_ref(["5739-2401"], _PDF_TILCO_4434)
        assert qty != pytest.approx(1.0, abs=1e-3), (
            "qty=1 est le numéro de poste, PAS la qté."
        )


# ===========================================================================
# FAMILLE 4 — RS COMPONENTS (unité UTE / PQT de N, conditionnement)
# ===========================================================================

class TestRSComponentsLayout:
    """Layout RS Components : Qté U.D.V. = N UTE ou N PQT de M."""

    def test_rs_ref_4574757_qty_10_ute(self):
        """RS 4574757 : 10 UTE → qté réelle = 10."""
        qty, _ = _extract_qty_price_for_ref(["4574757"], _PDF_RS)
        assert qty == pytest.approx(10.0, abs=1e-3), f"Attendu 10.0, got {qty!r}"

    def test_rs_ref_7581650_qty_60_pqt_de_10(self):
        """RS 7581650 : 6 PQT de 10 → qté réelle = 6 × 10 = 60."""
        qty, _ = _extract_qty_price_for_ref(["7581650"], _PDF_RS)
        assert qty == pytest.approx(60.0, abs=1e-3), f"Attendu 60.0 (6 PQT×10), got {qty!r}"

    # Anti-faux-conforme

    def test_rs_ne_prend_pas_n_ligne_10_comme_qty_pour_4574757(self):
        """ANTI-FAUX-CONFORME : ne doit PAS renvoyer 10 parce que c'est le N° de ligne."""
        # La ref 4574757 a qty=10 UTE qui est aussi le N° de ligne — on doit quand même
        # retourner 10.0 mais pour la bonne raison (UTE, pas N° de ligne).
        # Le vrai piège : 7581650 ne doit pas prendre le N° de ligne 20 comme qté.
        qty, _ = _extract_qty_price_for_ref(["7581650"], _PDF_RS)
        assert qty != pytest.approx(20.0, abs=1e-3), (
            "qty=20 est le N° de ligne, PAS la quantité de 7581650."
        )


# ===========================================================================
# FAMILLE 5 — SOCOMEC (colonnes jumbled, UNE seule Quantité décimale virgule)
# ===========================================================================

class TestSocomecLayout:
    """SOCOMEC : layout jumbled, Quantité = premier décimal-virgule après la dernière date.

    Vérité terrain (capture PDF client 2026-06-01, MAJ règle facteur Un.) :
      - CF101270 / 67120250 → Quantité = 4,00 × Un.=3P (facteur=3) → effective=12.0
      - CF101274 / 15994064 → Quantité = 2,00 × Un.=PC (facteur=1) → effective=2.0
      - Ligne = 10 (n° de ligne, entier, APRÈS l'unité dans le texte jumbled)
      - "accusée" = colonne DATE de départ accusée, PAS une quantité

    Règle PMI confirmée (source autoritaire, 2026-06-01) :
      QuantiteERP = Quantite (col) × facteur(Un.)
      facteur = int(chiffre de tête de Un.) si présent, sinon 1.
      "3P"→3, "2P"→2, "PC"/"1P"/sans chiffre→1.

    Tests anti-faux-conforme : ne pas renvoyer 10 (Ligne), ni un prix, ni une date.
    """

    def test_socomec_4657_ref_67120250_qty_12(self):
        """CF101270 / 67120250 : Quantité=4,00 × Un.=3P (facteur=3) → effective=12.0.

        MISE A JOUR : anciennement attendu 4.0 (facteur ignoré). Règle facteur prouvée
        par PMI 2026-06-01 sur 6 ARs réels. CF101270 est le seul avec Un.≠PC (3P).
        """
        qty, _ = _extract_qty_price_for_ref(["67120250"], _PDF_SOCOMEC_4657)
        assert qty == pytest.approx(12.0, abs=1e-3), (
            f"SOCOMEC CF101270 : attendu 12.0 (4 × facteur 3P=3), got {qty!r}"
        )

    def test_socomec_4886_ref_15994064_qty_2(self):
        """CF101274 / 15994064 : Quantité = 2,00 → 2.0."""
        qty, _ = _extract_qty_price_for_ref(["15994064"], _PDF_SOCOMEC_4886)
        assert qty == pytest.approx(2.0, abs=1e-3), (
            f"SOCOMEC CF101274 : attendu 2.0, got {qty!r}"
        )

    def test_socomec_4657_ne_renvoie_pas_ligne_10(self):
        """ANTI-FAUX-CONFORME : ne doit PAS renvoyer 10 (c'est le n° de Ligne)."""
        qty, _ = _extract_qty_price_for_ref(["67120250"], _PDF_SOCOMEC_4657)
        assert qty != pytest.approx(10.0, abs=1e-3), (
            "10 est le numéro de Ligne (colonne Ligne), PAS la Quantité."
        )

    def test_socomec_4657_ne_renvoie_pas_pu_net_35_31(self):
        """ANTI-FAUX-CONFORME : ne doit PAS renvoyer 35.31 (PU net)."""
        qty, _ = _extract_qty_price_for_ref(["67120250"], _PDF_SOCOMEC_4657)
        assert qty != pytest.approx(35.31, abs=1e-3), (
            "35.31 est le PU net, PAS la Quantité."
        )

    def test_socomec_4657_ne_renvoie_pas_montant_141_24(self):
        """ANTI-FAUX-CONFORME : ne doit PAS renvoyer 141.24 (Montant total)."""
        qty, _ = _extract_qty_price_for_ref(["67120250"], _PDF_SOCOMEC_4657)
        assert qty != pytest.approx(141.24, abs=1e-3), (
            "141.24 est le Montant total, PAS la Quantité."
        )

    def test_socomec_4886_ne_renvoie_pas_remise_67(self):
        """ANTI-FAUX-CONFORME CF101274 : ne doit PAS renvoyer 67.0 (Remise %)."""
        qty, _ = _extract_qty_price_for_ref(["15994064"], _PDF_SOCOMEC_4886)
        assert qty != pytest.approx(67.0, abs=1e-3), (
            "67.0 est la Remise %, PAS la Quantité."
        )

    def test_socomec_4886_ne_renvoie_pas_ligne_10(self):
        """ANTI-FAUX-CONFORME CF101274 : ne doit PAS renvoyer 10 (n° de Ligne)."""
        qty, _ = _extract_qty_price_for_ref(["15994064"], _PDF_SOCOMEC_4886)
        assert qty != pytest.approx(10.0, abs=1e-3), (
            "10 est le numéro de Ligne, PAS la Quantité."
        )


# ===========================================================================
# CORRECTIONS TDD — tests adversariaux (bugs 1–5)
# ===========================================================================

# ---------------------------------------------------------------------------
# Texte CATS verbatim extrait du VRAI PDF (log 4709) — bytes exacts
# Le format réel : "SPEC02160 \n \n3.00 \n" (espace après code + espace seul sur la ligne)
# Différent de la fixture synthétique _PDF_CATS qui utilise "\n\n" (sans espaces)
# ---------------------------------------------------------------------------
_PDF_CATS_REAL = (
    "POS \n"
    "Numéro de l'article \n"
    "Description de l'article  \n"
    "Quantité \n"
    "Prix \n"
    "Montant \n"
    "1.00 \n"
    "PR5MEC11 \n"
    "Compact plastic DIN Rail adapter kit 70 mm x 25 mm x \n"
    "14,5 mm \n"
    "SPEC02160 \n"
    " \n"
    "3.00 \n"
    "0.00 \n"
    "0.00 \n"
    " \n"
    "Fabricant: Teltonika Networks \n"
    "Code Douanier: 3926909790 \n"
    "Pays d'origine: Lithuania \n"
    "ROHS: Conforme \n"
    "Garantie: 12 Months Bring-In \n"
    " \n"
    " \n"
    " \n"
    " \n"
    "2.00 \n"
    "PR1KS210 \n"
    "MOBILE MAGNETIC SMA ANTENNA \n"
    "SPEC02161 \n"
    " \n"
    "3.00 \n"
    "0.00 \n"
    "0.00 \n"
    " \n"
    "Fabricant: Teltonika Networks \n"
    "Code Douanier: 8529106900 \n"
    "Pays d'origine: Lithuania \n"
    "ROHS: Conforme \n"
    "Garantie: 12 Months Bring-In \n"
    " \n"
    " \n"
    " \n"
    " \n"
    "3.00 \n"
    "RUT200010000 \n"
    "RUT200, 4G/LTE (Cat 4), 3G, 2G \n"
    "SPEC03039 \n"
    " \n"
    "3.00 \n"
    "0.00 \n"
    "0.00 \n"
)


# Bug 1 : CATS rate le vrai format \n \n (newline + espace + newline)
class TestBug1CatsRealFormat:
    """Bug 1 MAJEUR : _QTY_CATS_RE doit matcher le vrai format PDF CATS avec \\n \\n."""

    def test_cats_real_PR5MEC11_qty_3(self):
        """CATS réel log4709 : PR5MEC11 qté=3.0 — échoue avec \\n\\n strict."""
        qty, _ = _extract_qty_price_for_ref(["PR5MEC11"], _PDF_CATS_REAL)
        assert qty == pytest.approx(3.0, abs=1e-3), (
            f"Bug1: CATS vrai PDF a \\n \\n entre SPEC et la qté, got {qty!r}"
        )

    def test_cats_real_PR1KS210_qty_3(self):
        """CATS réel log4709 : PR1KS210 qté=3.0."""
        qty, _ = _extract_qty_price_for_ref(["PR1KS210"], _PDF_CATS_REAL)
        assert qty == pytest.approx(3.0, abs=1e-3), (
            f"Bug1: CATS vrai PDF PR1KS210, got {qty!r}"
        )

    def test_cats_real_RUT200010000_qty_3(self):
        """CATS réel log4709 : RUT200010000 qté=3.0."""
        qty, _ = _extract_qty_price_for_ref(["RUT200010000"], _PDF_CATS_REAL)
        assert qty == pytest.approx(3.0, abs=1e-3), (
            f"Bug1: CATS vrai PDF RUT200010000, got {qty!r}"
        )


# Bug 2 : _QTY_PCE_RE IGNORECASE capte "pce" minuscule parasite
class TestBug2PCECaseSensitive:
    """Bug 2 CRITICAL : PCE en minuscule dans une description ne doit pas être capté."""

    # Vrai vecteur : "8 pce" DANS la ligne de description qui suit la réf (window forward).
    # Avec IGNORECASE, findall retourne ['8', '5', '1'] → prend 8 en premier (FAUX).
    # Avec CASE-SENSITIVE, findall retourne ['5', '1'] → prend 5 (CORRECT).
    _TEXT_PCE_PARASITE = (
        "7AC1234567 MOTEUR BRUSHLESS 8 pce en boite\n"
        "5 PCE\n"
        "69,48 1 PCE 347,40 EUR\n"
    )

    def test_pce_minuscule_parasite_ne_capte_pas(self):
        """Un '8 pce' minuscule dans la description apres la ref ne doit PAS donner qty=8."""
        qty, _ = _extract_qty_price_for_ref(["7AC1234567"], self._TEXT_PCE_PARASITE)
        assert qty != pytest.approx(8.0, abs=1e-3), (
            "Bug2: pce minuscule dans description capte a tort comme qte"
        )
        assert qty == pytest.approx(5.0, abs=1e-3), (
            f"Bug2: la qte doit etre 5 (PCE majuscule sur ligne dediee), got {qty!r}"
        )


# Bug 3 : UTE/PQT vole la qté d'une réf SAP (contamination cross-famille)
class TestBug3UTEContamination:
    """Bug 3 CRITICAL : UTE dans la zone <150 chars ne doit pas écraser PCE SAP."""

    # Texte adversarial : "10 UTE" à ~60 chars d'une réf SAP qui a "5 PCE"
    _TEXT_UTE_BEFORE_SAP = (
        "Description condensateur: 10 UTE\n"
        "4574758 CONDENSATEUR SMD\n"
        "5 PCE\n"
        "12,50 1 PCE 62,50 EUR\n"
    )

    def test_ute_proche_ref_sap_ne_vole_pas_qty(self):
        """10 UTE à <150 chars d'une réf SAP avec 5 PCE → qté=5, pas 10."""
        qty, _ = _extract_qty_price_for_ref(["4574758"], self._TEXT_UTE_BEFORE_SAP)
        assert qty != pytest.approx(10.0, abs=1e-3), (
            "Bug3: 10 UTE dans la zone locale vole la qté SAP (5 PCE)"
        )
        assert qty == pytest.approx(5.0, abs=1e-3), (
            f"Bug3: la qté doit être 5 (PCE majuscule prime), got {qty!r}"
        )


# Bug 4 : _find_ref_pos fallback `in` sans frontière de mot
class TestBug4RefPosWordBoundary:
    """Bug 4 : une réf incluse dans une plus longue ne doit pas matcher."""

    # Texte où "ABCDEF-1234" est inclus dans "LONG-ABCDEF-1234XYZ" (plus longue)
    # La réf courte ne doit pas matcher dans la sous-chaîne de la longue
    _TEXT_EMBEDDED_REF = (
        "LONG-ABCDEF-1234XYZ description article\n"
        "5 PCE\n"
        "10,00 1 PCE 50,00 EUR\n"
    )
    # Texte avec la vraie réf courte, bien isolée
    _TEXT_REAL_REF = (
        "ABCDEF-1234 description article\n"
        "7 PCE\n"
        "10,00 1 PCE 70,00 EUR\n"
    )

    def test_ref_courte_ne_matche_pas_dans_ref_longue(self):
        """'ABCDEF-1234' ne doit PAS matcher dans 'LONG-ABCDEF-1234XYZ'."""
        qty, _ = _extract_qty_price_for_ref(["ABCDEF-1234"], self._TEXT_EMBEDDED_REF)
        assert qty is None, (
            f"Bug4: ABCDEF-1234 ne doit pas matcher dans LONG-ABCDEF-1234XYZ, got qty={qty!r}"
        )

    def test_ref_courte_matche_quand_isolee(self):
        """'ABCDEF-1234' doit bien matcher quand la réf est isolée."""
        qty, _ = _extract_qty_price_for_ref(["ABCDEF-1234"], self._TEXT_REAL_REF)
        assert qty == pytest.approx(7.0, abs=1e-3), (
            f"Bug4: ABCDEF-1234 isolée doit donner qty=7, got {qty!r}"
        )


# Bug 5 : guard SOCOMEC trop large — "ACCUSE DE RECEPTION" ne doit pas déclencher le skip
class TestBug5SocomecGuardTooWide:
    """Bug 5 : 'ACCUSE DE RECEPTION' (TILCO) ne doit pas déclencher le skip SOCOMEC.

    Le vrai vecteur : ACCUSE DE RECEPTION dans les 300 chars de la ref
    (PDF compact ou texte extrait sans saut de page).
    """

    # Texte adversarial : ACCUSE DE RECEPTION juste avant la ref (< 300 chars)
    _TEXT_TILCO_COMPACT = (
        "ACCUSE   DE   RECEPTION\n"
        "CF101244\n"
        "Poste N°:\n"
        "1\n"
        "28/05/2026\n"
        "1 5735-1214-0001\n"
        "MONTANT W/O AFF\n"
        "2\n"
        "18.28\n"
        "36.56\n"
    )

    def test_tilco_accuse_de_reception_compact_ne_skip_pas(self):
        """TILCO compact : ACCUSE DE RECEPTION a moins de 300 chars de la ref → qte=2."""
        qty, _ = _extract_qty_price_for_ref(["5735-1214-0001"], self._TEXT_TILCO_COMPACT)
        assert qty == pytest.approx(2.0, abs=1e-3), (
            f"Bug5: ACCUSE DE RECEPTION (TILCO compact) declenche a tort le skip SOCOMEC, got {qty!r}"
        )

    def test_tilco_4430_accuse_de_reception_ne_skip_pas(self):
        """TILCO CF101244 fixture : ACCUSE DE RECEPTION dans en-tete → qte=2."""
        qty, _ = _extract_qty_price_for_ref(["5735-1214-0001"], _PDF_TILCO_4430)
        assert qty == pytest.approx(2.0, abs=1e-3), (
            f"Bug5: ACCUSE DE RECEPTION (TILCO) declenche a tort le skip SOCOMEC, got {qty!r}"
        )

    def test_tilco_4434_accuse_de_reception_ne_skip_pas(self):
        """TILCO CF101245 avec 'ACCUSE DE RECEPTION' → qte=2, pas skippe."""
        qty, _ = _extract_qty_price_for_ref(["5739-2401"], _PDF_TILCO_4434)
        assert qty == pytest.approx(2.0, abs=1e-3), (
            f"Bug5: ACCUSE DE RECEPTION (TILCO) declenche a tort le skip SOCOMEC, got {qty!r}"
        )

    def test_socomec_4657_extrait_qty_correcte_apres_fix(self):
        """SOCOMEC vrai (colonne 'accusee' = date) doit renvoyer 12.0 avec facteur 3P.

        Modifié 2026-06-01 v1 : 'accusée' est une DATE, pas une quantité. Le skip était faux.
        Modifié 2026-06-01 v2 : règle facteur Un. prouvée par PMI → 4.0 × 3P = 12.0.
        """
        qty, _ = _extract_qty_price_for_ref(["67120250"], _PDF_SOCOMEC_4657)
        assert qty == pytest.approx(12.0, abs=1e-3), (
            f"Bug5+SOCOMEC fix+facteur: attendu 12.0 (4 × 3P), got {qty!r}"
        )

    def test_socomec_4886_extrait_qty_correcte_apres_fix(self):
        """SOCOMEC CF101274 doit renvoyer 2.0 apres suppression du skip.

        Modifié 2026-06-01 : même raison que 4657.
        """
        qty, _ = _extract_qty_price_for_ref(["15994064"], _PDF_SOCOMEC_4886)
        assert qty == pytest.approx(2.0, abs=1e-3), (
            f"Bug5+SOCOMEC fix: attendu 2.0, got {qty!r}"
        )


# ===========================================================================
# SOCOMEC — facteur unité (règle métier confirmée par PMI, 2026-06-01)
# ===========================================================================
# Règle : QuantiteERP = Quantite (col) × facteur(Un.)
# facteur = int(premier chiffre de tête de l'unité) si présent, sinon 1
# "3P" → 3, "2P" → 2, "PC" / "1P" / rien → 1
# L'unité est le token sur la ligne suivant immédiatement la référence dans
# le texte jumbled SOCOMEC : "\n<ref>\n<Un.>\n<Ligne>\n".
#
# Vérifié sur les 6 vrais PDFs prod (extract_all_pages_text sur SCEI_PROD_VM) :
#   4657 / 67120250 : Quantité=4,00 / Un.=3P  → effective=12
#   4886 / 15994064 : Quantité=2,00 / Un.=PC  → effective=2
#   5073 / 26T33025 : Quantité=20,00 / Un.=PC → effective=20
#   5081 / 260R3080 : Quantité=15,00 / Un.=PC → effective=15
#   5082 / 48250400 : Quantité=20,00 / Un.=PC → effective=20
#   5085 / 260R3040 : Quantité=15,00 / Un.=PC → effective=15
# ===========================================================================

# Fixtures verbatim depuis les vrais PDFs (extrait pertinent autour de la ref)
_PDF_SOCOMEC_5073 = (
    "hors taxe\n€\nDate de départ\nplanifiée\nréception\ndemandée\n"
    "Ligne\naccusée\nDate de\narrondi HT\n€\n€\nMontant\nPU net\nPU tarif\n"
    "Désignation\nRéférence article\nUn.\nQuantité\n(*)\n"
    "30.06.2026\n01.07.2026\n30.06.2026\n"
    "20,00\n50,66\nSIRCO 3X250A FE SPECIAL\n1.013,20\n50,66\n"
    "26T33025\nPC\n10\n     49,08\n"
)

_PDF_SOCOMEC_5081 = (
    "hors taxe\n€\nDate de départ\nplanifiée\nréception\ndemandée\n"
    "Ligne\naccusée\nDate de\narrondi HT\n€\n€\nMontant\nPU net\nPU tarif\n"
    "Désignation\nRéférence article\nUn.\nQuantité\n(*)\n"
    "30.06.2026\n01.07.2026\n30.06.2026\n"
    "15,00\n220,67\nSIRCO 3X800A FE AVEC ACC\n3.310,05\n220,67\n"
    "260R3080\nPC\n10\n    142,48\n"
)

_PDF_SOCOMEC_5082 = (
    "hors taxe\n€\nDate de départ\nplanifiée\nréception\ndemandée\n"
    "Ligne\naccusée\nDate de\narrondi HT\n€\n€\nMontant\nPU net\nPU tarif\n"
    "Désignation\nRéférence article\nUn.\nQuantité\n(*)\n"
    "30.06.2026\n01.07.2026\n30.06.2026\n"
    "20,00\n100,94\nDIRIS A10 W/O RS485 MODBUS COM\n2.018,80\n100,94\n"
    "48250400\nPC\n10\n      5,96\n"
)

_PDF_SOCOMEC_5085 = (
    "hors taxe\n€\nDate de départ\nplanifiée\nréception\ndemandée\n"
    "Ligne\naccusée\nDate de\narrondi HT\n€\n€\nMontant\nPU net\nPU tarif\n"
    "Désignation\nRéférence article\nUn.\nQuantité\n(*)\n"
    "30.06.2026\n01.07.2026\n30.06.2026\n"
    "15,00\n86,81\nSIRCO 3X400A FE AVEC ACC\n1.302,15\n86,81\n"
    "260R3040\nPC\n10\n     67,93\n"
)


class TestSocomecFacteurUnite:
    """Règle métier SOCOMEC : QuantiteERP = Quantité × facteur(Un.).

    facteur extrait du token Un. qui suit immédiatement la référence dans
    le texte jumbled : \n<ref>\n<Un.>\n. Chiffre de tête → facteur, sinon 1.
    Tests sur les 6 vrais AR SOCOMEC (PMI source autoritaire, 2026-06-01).
    """

    # --- CF101270 (log 4657) : 67120250 / Un.=3P → 4 × 3 = 12 ---

    def test_socomec_4657_facteur_3P_qty_12(self):
        """CF101270 / 67120250 : Un.=3P → facteur=3 → effective = 4 × 3 = 12."""
        qty, _ = _extract_qty_price_for_ref(["67120250"], _PDF_SOCOMEC_4657)
        assert qty == pytest.approx(12.0, abs=1e-3), (
            f"SOCOMEC CF101270 facteur 3P : attendu 12.0, got {qty!r}"
        )

    def test_socomec_4657_ne_renvoie_pas_4_sans_facteur(self):
        """ANTI-REGRESSION : 4.0 sans facteur = bug. L'effective doit être 12."""
        qty, _ = _extract_qty_price_for_ref(["67120250"], _PDF_SOCOMEC_4657)
        assert qty != pytest.approx(4.0, abs=1e-3), (
            "4.0 = Quantité brute sans facteur 3P → bug non corrigé"
        )

    # --- CF101274 (log 4886) : 15994064 / Un.=PC → facteur=1 → 2 × 1 = 2 ---

    def test_socomec_4886_facteur_PC_qty_2(self):
        """CF101274 / 15994064 : Un.=PC → facteur=1 → effective = 2 × 1 = 2."""
        qty, _ = _extract_qty_price_for_ref(["15994064"], _PDF_SOCOMEC_4886)
        assert qty == pytest.approx(2.0, abs=1e-3), (
            f"SOCOMEC CF101274 Un.=PC : attendu 2.0, got {qty!r}"
        )

    # --- CF101313 (log 5073) : 26T33025 / Un.=PC → facteur=1 → 20 × 1 = 20 ---

    def test_socomec_5073_facteur_PC_qty_20(self):
        """CF101313 / 26T33025 : Un.=PC → facteur=1 → effective = 20."""
        qty, _ = _extract_qty_price_for_ref(["26T33025"], _PDF_SOCOMEC_5073)
        assert qty == pytest.approx(20.0, abs=1e-3), (
            f"SOCOMEC CF101313 Un.=PC : attendu 20.0, got {qty!r}"
        )

    # --- CF101315 (log 5081) : 260R3080 / Un.=PC → facteur=1 → 15 × 1 = 15 ---

    def test_socomec_5081_facteur_PC_qty_15(self):
        """CF101315 / 260R3080 : Un.=PC → facteur=1 → effective = 15."""
        qty, _ = _extract_qty_price_for_ref(["260R3080"], _PDF_SOCOMEC_5081)
        assert qty == pytest.approx(15.0, abs=1e-3), (
            f"SOCOMEC CF101315 Un.=PC : attendu 15.0, got {qty!r}"
        )

    # --- CF101317 (log 5082) : 48250400 / Un.=PC → facteur=1 → 20 × 1 = 20 ---

    def test_socomec_5082_facteur_PC_qty_20(self):
        """CF101317 / 48250400 : Un.=PC → facteur=1 → effective = 20."""
        qty, _ = _extract_qty_price_for_ref(["48250400"], _PDF_SOCOMEC_5082)
        assert qty == pytest.approx(20.0, abs=1e-3), (
            f"SOCOMEC CF101317 Un.=PC : attendu 20.0, got {qty!r}"
        )

    # --- CF101318 (log 5085) : 260R3040 / Un.=PC → facteur=1 → 15 × 1 = 15 ---

    def test_socomec_5085_facteur_PC_qty_15(self):
        """CF101318 / 260R3040 : Un.=PC → facteur=1 → effective = 15."""
        qty, _ = _extract_qty_price_for_ref(["260R3040"], _PDF_SOCOMEC_5085)
        assert qty == pytest.approx(15.0, abs=1e-3), (
            f"SOCOMEC CF101318 Un.=PC : attendu 15.0, got {qty!r}"
        )

    # --- Anti-faux-conforme : facteur ne s'applique pas à tort sur PC ---

    def test_socomec_PC_ne_multiplie_pas_par_chiffre_parasite(self):
        """ANTI-FAUX-CONFORME : Un.=PC → facteur=1, jamais un chiffre parasite."""
        # 15994064 avec PC → doit être 2.0, pas 20.0 ni autre
        qty, _ = _extract_qty_price_for_ref(["15994064"], _PDF_SOCOMEC_4886)
        assert qty == pytest.approx(2.0, abs=1e-3), (
            f"PC ne doit pas multiplier par autre chose que 1, got {qty!r}"
        )

    def test_socomec_ne_prend_pas_ligne_10_apres_facteur(self):
        """ANTI-FAUX-CONFORME : la Ligne (10 juste après l'unité) ne contamine pas."""
        # 67120250 : texte = "67120250\n3P\n10\n" — 10 = n° de Ligne, pas facteur
        qty, _ = _extract_qty_price_for_ref(["67120250"], _PDF_SOCOMEC_4657)
        assert qty != pytest.approx(40.0, abs=1e-3), (
            "40.0 = 4 × 10 (Ligne) → facteur mal extrait depuis la Ligne"
        )
        assert qty != pytest.approx(10.0, abs=1e-3), (
            "10 est le n° de Ligne, pas le facteur"
        )


# ===========================================================================
# REGEX FACTEUR RESSERRÉE — token numérique pur ne doit pas être facteur
# ===========================================================================
# Bug : re.match(r"(\d+)", "10") → facteur=10. Un n° de Ligne ("10") après la
# ref dans le texte jumbled SOCOMEC donnerait qty*10. Fix attendu : la regex
# doit n'accepter QUE la forme "NP" (chiffres suivis de P/p).
# Tokens réels SOCOMEC : "3P"→3, "1P"→1, "12P"→12, "PC"→1, "10"→1 (Ligne).
# ===========================================================================

# Fixture minimaliste qui place chaque token-unité directement après la réf
# dans le contexte jumbled.

def _socomec_fixture_with_unit(unit_token: str, qty_str: str = "4,00") -> tuple[str, str]:
    """Retourne (pdf_text, ref) pour un mini-fixture SOCOMEC avec le token donné."""
    ref = "TESTREF001"
    text = (
        "Ligne\naccusée\nDate de\narrondi HT\nMontant\nPU net\nPU tarif\n"
        "Désignation\nRéférence article\nUn.\nQuantité\n"
        "30.06.2026\n01.07.2026\n30.06.2026\n"
        f"{qty_str}\n50,00\nDescription produit test\n100,00\n50,00\n"
        f"{ref}\n{unit_token}\n10\n      9,99\n"
    )
    return text, ref


class TestFacteurRegexResserree:
    """Vérifie que la regex du facteur n'accepte QUE la forme NP (chiffres+P).

    Un token numérique pur (ex: '10' = n° de Ligne) ne doit JAMAIS devenir facteur.
    Seuls 'NP' (3P, 1P, 12P) donnent un facteur > 1. 'PC' et les nombres purs -> facteur=1.
    """

    def test_token_numerique_pur_10_facteur_1(self):
        """Token '10' apres la ref -> facteur=1 (jamais facteur=10). Qty=4*1=4."""
        text, ref = _socomec_fixture_with_unit("10", qty_str="4,00")
        qty, _ = _extract_qty_price_for_ref([ref], text)
        assert qty == pytest.approx(4.0, abs=1e-3), (
            f"Token '10' (nombre pur) ne doit PAS donner facteur=10 -> attendu 4.0, got {qty!r}"
        )

    def test_token_numerique_pur_10_ne_donne_pas_40(self):
        """ANTI-REGRESSION : qty != 40.0 (ce que donnerait facteur=10 sur qty=4)."""
        text, ref = _socomec_fixture_with_unit("10", qty_str="4,00")
        qty, _ = _extract_qty_price_for_ref([ref], text)
        assert qty != pytest.approx(40.0, abs=1e-3), (
            "40.0 = 4 * 10 -> la regex r'(\\d+)' capture '10' comme facteur -> BUG"
        )

    def test_token_PC_facteur_1(self):
        """Token 'PC' -> facteur=1 (pas de chiffres). Qty=4*1=4."""
        text, ref = _socomec_fixture_with_unit("PC", qty_str="4,00")
        qty, _ = _extract_qty_price_for_ref([ref], text)
        assert qty == pytest.approx(4.0, abs=1e-3), (
            f"Token 'PC' -> facteur=1 -> attendu 4.0, got {qty!r}"
        )

    def test_token_3P_facteur_3(self):
        """Token '3P' -> facteur=3 -> qty=4*3=12."""
        text, ref = _socomec_fixture_with_unit("3P", qty_str="4,00")
        qty, _ = _extract_qty_price_for_ref([ref], text)
        assert qty == pytest.approx(12.0, abs=1e-3), (
            f"Token '3P' -> facteur=3 -> attendu 12.0, got {qty!r}"
        )

    def test_token_12P_facteur_12(self):
        """Token '12P' -> facteur=12 -> qty=1*12=12."""
        text, ref = _socomec_fixture_with_unit("12P", qty_str="1,00")
        qty, _ = _extract_qty_price_for_ref([ref], text)
        assert qty == pytest.approx(12.0, abs=1e-3), (
            f"Token '12P' -> facteur=12 -> attendu 12.0, got {qty!r}"
        )
