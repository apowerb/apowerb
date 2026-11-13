"""F3 — tool_einvoice_check(source) : sous-ensemble nommé de règles, erreurs en français.

Contrôles 13 à 18 de la spec (et la partie « source » du contrôle 6 pour check).
Liste figée des 14 règles (identifiants exacts) : BR-02, BR-03, BR-04, BR-05,
BR-06, BR-07, BR-16, BR-CO-10, BR-CO-15, BR-S-09, FR-SIREN, FR-TVA, IBAN,
DATE-ECHEANCE. BR-S-08 n'en fait pas partie.
"""

from __future__ import annotations

import json
import re
from decimal import Decimal

import pytest

from tests.einvoice import einvoice_fixtures as fx

REGLES = [
    "BR-02", "BR-03", "BR-04", "BR-05", "BR-06", "BR-07", "BR-16",
    "BR-CO-10", "BR-CO-15", "BR-S-09", "FR-SIREN", "FR-TVA", "IBAN", "DATE-ECHEANCE",
]
CLES_ERREUR = {"règle", "champ", "valeur_lue", "attendu", "explication", "correction"}
IBAN_FAUX = "FR1420041010050500013M02607"  # IBAN de la norme, dernier chiffre changé
SIREN_FAUX = fx.siren_faux(fx.SIREN_VENDEUR)
SIREN_FAUX_ACHETEUR = fx.siren_faux(fx.SIREN_ACHETEUR)
TVA_FAUSSE = fx.tva_fr(fx.SIREN_VENDEUR, decalage=1)
VENDEUR, ACHETEUR = ("seller", "vendeur"), ("buyer", "acheteur")
ABSENT = object()


def _outil():
    from apowerb.tools_store.portfolio.einvoice import tool_einvoice_check

    return tool_einvoice_check


def _check(agent_dir, nom: str, contenu) -> dict:
    check = _outil()
    fx.ecrire(agent_dir, nom, contenu)
    return check(nom)


def _regles(result: dict) -> list:
    return [e["règle"] for e in result["errors"]]


def _modif(base=None, **chemins):
    """Facture modifiée ; clé 'a__b__0__c' = data['a']['b'][0]['c']."""
    data = base or fx.facture()
    for chemin, valeur in chemins.items():
        parts = [int(p) if p.isdigit() else p for p in chemin.split("__")]
        cible = data
        for p in parts[:-1]:
            cible = cible[p]
        cible[parts[-1]] = valeur
    return data


def _sans_lignes():
    return fx.facture(
        lines=[], vat_breakdown=[],
        totals={"net": "0.00", "vat": "0.00", "gross": "0.00", "due": "0.00"},
    )


def _nombres(v) -> set:
    """Nombres contenus dans une valeur (Decimal, nombre ou texte « 540,00 € »), en Decimal."""
    texte = str(v).replace(" ", "").replace(" ", "")
    return {Decimal(n.replace(",", ".")) for n in re.findall(r"-?\d+(?:[.,]\d+)?", texte)}


# Facture fausse sur UN SEUL point. (id, règle, fabrique, attentes sur l'erreur)
# attentes : champ = mots dont l'un doit figurer dans « champ » ; lue / attendu =
# Decimal (comparé en valeur), str (égalité pour lue, inclusion pour attendu) ou ABSENT.
CAS = [
    ("BR-02", "BR-02", lambda: fx.facture(number=None), {"lue": ABSENT}),
    ("BR-03", "BR-03", lambda: fx.facture(issue_date=None), {"lue": ABSENT}),
    ("BR-04", "BR-04", lambda: fx.facture(type_code=None), {"lue": ABSENT}),
    ("BR-04-code-325", "BR-04", lambda: fx.facture(type_code="325"), {}),
    ("BR-05", "BR-05", lambda: fx.facture(currency=None), {"lue": ABSENT}),
    ("BR-06", "BR-06", lambda: _modif(seller__name=None), {"lue": ABSENT, "champ": VENDEUR}),
    ("BR-07", "BR-07", lambda: _modif(buyer__name=None), {"lue": ABSENT, "champ": ACHETEUR}),
    ("BR-16", "BR-16", _sans_lignes, {}),
    ("BR-CO-10", "BR-CO-10", lambda: _modif(lines__1__net_amount="210.00"),
     {"lue": Decimal("530.00"), "attendu": Decimal("540.00")}),
    ("BR-CO-15", "BR-CO-15", lambda: _modif(totals__gross="641.65"),
     {"lue": Decimal("641.65"), "attendu": Decimal("631.65")}),
    ("BR-S-09-taux-20", "BR-S-09", lambda: _modif(vat_breakdown__0__amount="110.00"),
     {"lue": Decimal("110.00"), "attendu": Decimal("100.00")}),
    ("BR-S-09-taux-5.5", "BR-S-09", lambda: _modif(vat_breakdown__1__amount="1.75"),
     {"lue": Decimal("1.75"), "attendu": Decimal("1.65")}),
    # SIREN à clé de Luhn fausse ; la TVA est recalculée sur ce SIREN (clé TVA juste).
    ("FR-SIREN", "FR-SIREN",
     lambda: _modif(seller__siren=SIREN_FAUX, seller__vat_id=fx.tva_fr(SIREN_FAUX)),
     {"lue": SIREN_FAUX, "champ": VENDEUR}),
    ("FR-TVA", "FR-TVA", lambda: _modif(seller__vat_id=TVA_FAUSSE),
     {"lue": TVA_FAUSSE, "champ": VENDEUR, "attendu": fx.tva_fr(fx.SIREN_VENDEUR)}),
    ("IBAN", "IBAN", lambda: fx.facture(iban=IBAN_FAUX), {"lue": IBAN_FAUX}),
    ("DATE-ECHEANCE", "DATE-ECHEANCE", lambda: fx.facture(due_date="2026-09-15"),
     {"lue": "2026-09-15", "attendu": "2026-10-01"}),
]
assert {c[1] for c in CAS} == set(REGLES)
IDS = [c[0] for c in CAS]
FAUTE = {}  # un cas par règle : le premier de CAS
for _id, _regle, _fab, _att in CAS:
    FAUTE.setdefault(_regle, _fab)

RENDUS = {"cii": fx.cii, "ubl": fx.ubl}


def _verifier_attentes(err: dict, attentes: dict) -> None:
    if "lue" in attentes:
        lue = attentes["lue"]
        if lue is ABSENT:
            assert err["valeur_lue"] in (None, ""), err
        elif isinstance(lue, Decimal):
            assert lue in _nombres(err["valeur_lue"]), err
        else:
            assert str(err["valeur_lue"]) == lue, err
    if "attendu" in attentes:
        voulu = attentes["attendu"]
        if isinstance(voulu, Decimal):
            assert voulu in _nombres(err["attendu"]), err
        else:
            assert voulu in str(err["attendu"]), err
    if "champ" in attentes:
        assert any(m in str(err["champ"]).lower() for m in attentes["champ"]), err


# ── Contrôle 6 (check) ────────────────────────────────────────────────────────


def test_c06_check_refuse_les_chemins_hors_des_uploads_sans_lecture(agent_dir, tmp_path):
    """Contrôle 6 : check refuse autre dossier, autre agent, ../, lien sortant et URL, sans ouvrir le fichier."""
    check = _outil()
    xml = fx.cii(fx.facture(number="FA-HORS-DOSSIER")).encode("utf-8")
    sources, vise = fx.sources_hors_dossier(agent_dir, tmp_path, ".xml", xml)

    with fx.espion_lectures(vise) as lectures:
        resultats = [(s, check(s)) for s in sources + fx.URLS]

    assert lectures == [], lectures
    for source, result in resultats:
        assert result["status"] == "error", (source, result)
        assert "FA-HORS-DOSSIER" not in json.dumps(result, default=str), source


@pytest.mark.parametrize("source", ["", "   ", "absente.xml", "notes.txt"],
                         ids=["vide", "blancs", "absente", "extension_txt"])
def test_c06_check_source_invalide_rend_une_erreur(agent_dir, source):
    """Contrôle 6 : source vide, absente ou extension non acceptée -> status error, pas d'exception."""
    check = _outil()
    fx.ecrire(agent_dir, "notes.txt", "texte quelconque")

    result = check(source)

    assert result["status"] == "error", result
    assert str(result.get("error_message", "")).strip()


# ── Contrôle 13 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "nom,fabrique",
    [
        ("ok.xml", lambda: fx.cii(fx.facture())),
        ("ok_ubl.xml", lambda: fx.ubl(fx.facture())),
        ("ok.pdf", lambda: fx.facturx(fx.cii(fx.facture()))),
        ("ok2.xml", lambda: fx.cii(fx.facture2())),
        ("ok2_ubl.xml", lambda: fx.ubl(fx.facture2())),
    ],
    ids=["cii", "ubl", "factur-x", "facture2_cii", "facture2_ubl"],
)
def test_c13_facture_correcte_aucune_erreur_et_mention_controle_partiel(agent_dir, nom, fabrique):
    """Contrôle 13 : facture correcte -> aucune erreur, « contrôle partiel (14 règles) », liste des règles."""
    result = _check(agent_dir, nom, fabrique())

    assert result["status"] == "success", result
    assert result["errors"] == []
    assert sorted(result["rules"]) == sorted(REGLES)
    assert "contrôle partiel (14 règles)" in result["mention"]


def test_c13_donnees_facultatives_absentes_aucune_erreur(agent_dir):
    """Contrôle 13 : SIREN, TVA, IBAN et échéance absents -> règle non évaluée, aucune erreur."""
    data = _modif(seller__siren=None, seller__vat_id=None, buyer__siren=None, buyer__vat_id=None)
    data.update(iban=None, due_date=None)
    for fmt, rendu in RENDUS.items():
        result = _check(agent_dir, f"partielle.{fmt}.xml", rendu(data))
        assert result["status"] == "success", result
        assert result["errors"] == [], (fmt, result["errors"])


def test_c13_echeance_egale_a_l_emission_passe(agent_dir):
    """Contrôle 13 : une échéance le jour même de l'émission est acceptée (>=)."""
    result = _check(agent_dir, "jour.xml", fx.cii(fx.facture(due_date="2026-10-01")))

    assert result["status"] == "success", result
    assert result["errors"] == []


# ── Contrôle 14 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("fmt", list(RENDUS))
@pytest.mark.parametrize("cas_id,regle,fabrique,attentes", CAS, ids=IDS)
def test_c14_chaque_regle_a_un_cas_qui_echoue_seul(agent_dir, cas_id, regle, fabrique, attentes, fmt):
    """Contrôle 14 : une facture fausse sur ce seul point rend exactement cette règle, une fois,
    avec la valeur lue, la valeur attendue et le champ (vendeur/acheteur) justes."""
    result = _check(agent_dir, f"faute.{fmt}.xml", RENDUS[fmt](fabrique()))

    assert result["status"] == "success", result
    assert _regles(result) == [regle], result["errors"]
    (err,) = result["errors"]
    assert CLES_ERREUR <= set(err), err
    _verifier_attentes(err, attentes)


@pytest.mark.parametrize("fmt", list(RENDUS))
def test_c14_deux_defauts_rendent_deux_erreurs(agent_dir, fmt):
    """Contrôle 14 : TTC faux ET IBAN faux -> exactement 2 erreurs, BR-CO-15 et IBAN (pas d'arrêt à la première)."""
    data = _modif(totals__gross="641.65")
    data["iban"] = IBAN_FAUX
    result = _check(agent_dir, f"deux.{fmt}.xml", RENDUS[fmt](data))

    assert result["status"] == "success", result
    assert len(result["errors"]) == 2, result["errors"]
    assert set(_regles(result)) == {"BR-CO-15", "IBAN"}


@pytest.mark.parametrize("fmt", list(RENDUS))
def test_c14_siren_vendeur_et_acheteur_faux_deux_erreurs(agent_dir, fmt):
    """Contrôle 14 : SIREN vendeur ET acheteur faux -> 2 erreurs FR-SIREN, champs vendeur et acheteur distincts."""
    data = _modif(
        seller__siren=SIREN_FAUX, seller__vat_id=fx.tva_fr(SIREN_FAUX),
        buyer__siren=SIREN_FAUX_ACHETEUR, buyer__vat_id=fx.tva_fr(SIREN_FAUX_ACHETEUR),
    )
    result = _check(agent_dir, f"sirens.{fmt}.xml", RENDUS[fmt](data))

    assert _regles(result) == ["FR-SIREN", "FR-SIREN"], result["errors"]
    par_valeur = {str(e["valeur_lue"]): str(e["champ"]).lower() for e in result["errors"]}
    assert set(par_valeur) == {SIREN_FAUX, SIREN_FAUX_ACHETEUR}
    assert any(m in par_valeur[SIREN_FAUX] for m in VENDEUR), par_valeur
    assert any(m in par_valeur[SIREN_FAUX_ACHETEUR] for m in ACHETEUR), par_valeur


# ── Contrôle 15 ───────────────────────────────────────────────────────────────


ARRONDIS = [
    # (règle, chemin modifié, valeurs à 0,01 près, valeurs à 0,02)
    ("BR-CO-15", "totals__gross", ["631.66", "631.64"], ["631.67", "631.63"]),
    ("BR-S-09", "vat_breakdown__0__amount", ["100.01", "99.99"], ["100.02", "99.98"]),
    ("BR-S-09", "vat_breakdown__1__amount", ["1.66", "1.64"], ["1.67", "1.63"]),
    ("BR-CO-10", "lines__1__net_amount", ["200.01", "199.99"], ["200.02", "199.98"]),
]


@pytest.mark.parametrize("regle,chemin,toleres,refuses", ARRONDIS,
                         ids=["BR-CO-15", "BR-S-09-taux-20", "BR-S-09-taux-5.5", "BR-CO-10"])
def test_c15_ecart_de_0_01_passe_ecart_de_0_02_echoue(agent_dir, regle, chemin, toleres, refuses):
    """Contrôle 15 : une différence d'arrondi de 0,01 passe ; une différence de 0,02 échoue."""
    for valeur in toleres:
        ok = _check(agent_dir, "tolere.xml", fx.cii(_modif(**{chemin: valeur})))
        assert ok["status"] == "success", ok
        assert ok["errors"] == [], (valeur, ok["errors"])

    for valeur in refuses:
        ko = _check(agent_dir, "refuse.xml", fx.cii(_modif(**{chemin: valeur})))
        assert _regles(ko) == [regle], (valeur, ko["errors"])


# ── Contrôle 16 ───────────────────────────────────────────────────────────────


VALIDES_CONNUS = [
    # SIREN et TVA FR synthétiques vérifiés à la main ; IBAN d'exemple de la norme et IBAN FR fictif.
    ("112233440", "FR43112233440", "556677888", "FR02556677888", "FR1420041010050500013M02606"),
    ("556677888", "FR02556677888", "112233440", "FR43112233440", "FR6630001000640000000000123"),
]


@pytest.mark.parametrize("vs,vt,as_,at,iban", VALIDES_CONNUS)
def test_c16_valeurs_valides_connues_passent(agent_dir, vs, vt, as_, at, iban):
    """Contrôle 16 : des SIREN, TVA et IBAN valides connus ne déclenchent aucune erreur."""
    data = _modif(seller__siren=vs, seller__vat_id=vt, buyer__siren=as_, buyer__vat_id=at)
    data["iban"] = iban
    result = _check(agent_dir, "valide.xml", fx.cii(data))

    assert result["status"] == "success", result
    assert result["errors"] == []


FAUX = [
    ("FR-SIREN", "buyer__siren", "556677880", ACHETEUR),     # clé de Luhn fausse (acheteur)
    ("FR-SIREN", "seller__siren", "11223344", VENDEUR),      # 8 chiffres
    ("FR-SIREN", "seller__siren", "11223344A", VENDEUR),     # lettre
    ("FR-TVA", "buyer__vat_id", "FR03556677888", ACHETEUR),  # clé TVA fausse (acheteur)
    ("FR-TVA", "seller__vat_id", "FR00112233440", VENDEUR),  # clé TVA fausse (vendeur)
    ("IBAN", "iban", "FR1420041010050500013M02607", None),   # dernier chiffre changé
    ("IBAN", "iban", "FR1420041010050500013M0260", None),    # tronqué
    ("IBAN", "iban", "FR7620041010050500013M02606", None),   # clé de contrôle changée
]


@pytest.mark.parametrize("regle,chemin,valeur,partie", FAUX, ids=[f"{f[0]}-{f[2]}" for f in FAUX])
def test_c16_siren_tva_iban_faux_detectes(agent_dir, regle, chemin, valeur, partie):
    """Contrôle 16 : un SIREN, une TVA ou un IBAN faux est détecté, avec la valeur lue et la bonne partie."""
    result = _check(agent_dir, "faux.xml", fx.cii(_modif(**{chemin: valeur})))

    assert result["status"] == "success", result
    errs = [e for e in result["errors"] if e["règle"] == regle and str(e["valeur_lue"]) == valeur]
    assert errs, result["errors"]
    if partie:
        assert any(m in str(errs[0]["champ"]).lower() for m in partie), errs


# ── Contrôle 17 ───────────────────────────────────────────────────────────────

MOTS_FRANCAIS = re.compile(r"\b(le|la|les|du|de|des|un|une|doit|est|sont|au|aux|et|à|en)\b", re.IGNORECASE)


@pytest.mark.parametrize("cas_id,regle,fabrique,attentes", CAS, ids=IDS)
def test_c17_chaque_erreur_porte_explication_et_correction_en_francais(agent_dir, cas_id, regle, fabrique, attentes):
    """Contrôle 17 : chaque erreur a une explication et une correction non vides, en français."""
    result = _check(agent_dir, "faute.xml", fx.cii(fabrique()))

    assert result["errors"], result
    for err in result["errors"]:
        expl, corr = err["explication"], err["correction"]
        assert isinstance(expl, str) and len(expl.strip()) >= 10, err
        assert isinstance(corr, str) and len(corr.strip()) >= 10, err
        assert expl.strip() != corr.strip(), err
        assert MOTS_FRANCAIS.search(expl) and MOTS_FRANCAIS.search(corr), err
        assert isinstance(err["champ"], str) and err["champ"].strip(), err


@pytest.mark.parametrize(
    "regle,juste",
    [("BR-CO-15", Decimal("631.65")), ("BR-CO-10", Decimal("540.00")), ("BR-S-09", Decimal("100.00"))],
)
def test_c17_la_correction_donne_la_valeur_juste(agent_dir, regle, juste):
    """Contrôle 17 : rien à deviner — quand la valeur juste se calcule, la correction l'écrit."""
    result = _check(agent_dir, "montant.xml", fx.cii(FAUTE[regle]()))

    (err,) = result["errors"]
    assert juste in _nombres(err["correction"]), err


# ── Contrôle 18 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("cas_id,fabrique", [("correcte", fx.facture)] + [(c[0], c[2]) for c in CAS],
                         ids=["correcte"] + IDS)
def test_c18_la_sortie_ne_contient_jamais_conform(agent_dir, cas_id, fabrique):
    """Contrôle 18 : « conform » (conforme, non conforme, conformité, conformément…) absent de la sortie."""
    result = _check(agent_dir, "f.xml", fx.cii(fabrique()))

    assert result["status"] == "success", result
    texte = json.dumps(result, default=str, ensure_ascii=False)
    assert "conform" not in texte.lower(), texte
