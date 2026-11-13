"""F2 — tool_einvoice_parse(source) : CII ou UBL -> la même facture normalisée.

Contrôles 7 à 12 de la spec (et la partie « source » du contrôle 6 pour parse).
Forme attendue de ``result["invoice"]`` : voir ``einvoice_fixtures.attendu``
(clés fixes) + ``source_format`` et ``profile``.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from tests.einvoice import einvoice_fixtures as fx

EXCLUES_DE_LA_COMPARAISON = {"source_format", "profile"}
FACTURES = [fx.facture, fx.facture2]
IDS_FACTURES = ["facture1", "facture2"]


def _outil():
    from apowerb.tools_store.portfolio.einvoice import tool_einvoice_parse

    return tool_einvoice_parse


def _parse(agent_dir, nom: str, contenu) -> dict:
    parse = _outil()
    fx.ecrire(agent_dir, nom, contenu)
    return parse(nom)


def _sans(d: dict, cles: set) -> dict:
    return {k: v for k, v in d.items() if k not in cles}


# ── Contrôle 6 (parse) ────────────────────────────────────────────────────────


def test_c06_parse_refuse_les_chemins_hors_des_uploads_sans_lecture(agent_dir, tmp_path):
    """Contrôle 6 : parse refuse autre dossier, autre agent, ../, lien sortant, sans ouvrir le fichier."""
    parse = _outil()
    xml = fx.cii(fx.facture(number="FA-HORS-DOSSIER")).encode("utf-8")
    sources, vise = fx.sources_hors_dossier(agent_dir, tmp_path, ".xml", xml)

    with fx.espion_lectures(vise) as lectures:
        resultats = [(s, parse(s)) for s in sources + fx.URLS]

    assert lectures == [], lectures
    for source, result in resultats:
        assert result["status"] == "error", (source, result)
        assert "FA-HORS-DOSSIER" not in json.dumps(result, default=str), source


@pytest.mark.parametrize("source", ["", "   ", "absente.xml", "notes.txt"],
                         ids=["vide", "blancs", "absente", "extension_txt"])
def test_c06_parse_source_invalide_rend_une_erreur(agent_dir, source):
    """Contrôle 6 : source vide, absente ou extension non acceptée -> status error, pas d'exception."""
    parse = _outil()
    fx.ecrire(agent_dir, "notes.txt", "texte quelconque")

    result = parse(source)

    assert result["status"] == "error", result
    assert str(result.get("error_message", "")).strip()


# ── Contrôle 7 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("fabrique", FACTURES, ids=IDS_FACTURES)
def test_c07_facture_cii_rend_tous_les_champs_normalises(agent_dir, fabrique):
    """Contrôle 7 : une facture CII générée rend tous les champs normalisés attendus, ni plus ni moins."""
    data = fabrique()
    profil = {v: k for k, v in fx.URN_PROFILS.items()}[data["profile_urn"]]
    result = _parse(agent_dir, "facture.xml", fx.cii(data))

    assert result["status"] == "success", result
    inv = result["invoice"]
    voulu = {**fx.attendu(data), "source_format": "cii", "profile": profil}
    assert set(inv) == set(voulu)
    for cle, valeur in voulu.items():
        assert inv[cle] == valeur, f"{cle} : lu {inv[cle]!r}, attendu {valeur!r}"


@pytest.mark.parametrize("profil", ["MINIMUM", "BASIC", "EN 16931", "EXTENDED"])
def test_c07_profil_cii_lu_par_parse(agent_dir, profil):
    """Contrôle 7 : le profil d'une facture CII est rendu par parse pour les 4 profils."""
    result = _parse(agent_dir, "f.xml", fx.cii(fx.facture(profile_urn=fx.URN_PROFILS[profil])))

    assert result["status"] == "success", result
    assert result["invoice"]["profile"] == profil


def test_c07_pdf_factur_x_lu_comme_son_xml(agent_dir):
    """Contrôle 7 : un PDF Factur-X passé à parse rend la même facture que son XML CII."""
    data = fx.facture()
    depuis_xml = _parse(agent_dir, "facture.xml", fx.cii(data))
    depuis_pdf = _parse(agent_dir, "facture.pdf", fx.facturx(fx.cii(data)))

    assert depuis_pdf["status"] == "success", depuis_pdf
    assert depuis_pdf["invoice"] == depuis_xml["invoice"]


@pytest.mark.parametrize("rendu", [fx.cii, fx.ubl], ids=["cii", "ubl"])
def test_c07_champ_absent_rendu_none_sans_erreur(agent_dir, rendu):
    """Contrôle 7 : un champ absent (échéance, IBAN, numéro) vaut None ; la lecture ne s'arrête pas."""
    data = fx.facture(due_date=None, iban=None, number=None)
    result = _parse(agent_dir, "partielle.xml", rendu(data))

    assert result["status"] == "success", result
    inv = result["invoice"]
    assert inv["due_date"] is None and inv["iban"] is None and inv["number"] is None
    assert inv["totals"]["gross"] == Decimal("631.65")


# ── Contrôle 8 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("type_code", ["380", "381"])
@pytest.mark.parametrize("fabrique", FACTURES, ids=IDS_FACTURES)
def test_c08_la_meme_facture_en_ubl_rend_un_dictionnaire_identique(agent_dir, fabrique, type_code):
    """Contrôle 8 : CII et UBL de la même facture -> dict identique, hors source_format et profile."""
    data = fabrique()
    data["type_code"] = type_code
    r_cii = _parse(agent_dir, "f_cii.xml", fx.cii(data))
    r_ubl = _parse(agent_dir, "f_ubl.xml", fx.ubl(data))

    assert r_cii["status"] == "success", r_cii
    assert r_ubl["status"] == "success", r_ubl
    assert r_cii["invoice"]["source_format"] == "cii"
    assert r_ubl["invoice"]["source_format"] == "ubl"
    assert r_ubl["invoice"]["profile"] is None
    assert _sans(r_ubl["invoice"], EXCLUES_DE_LA_COMPARAISON) == _sans(
        r_cii["invoice"], EXCLUES_DE_LA_COMPARAISON
    )
    assert _sans(r_ubl["invoice"], EXCLUES_DE_LA_COMPARAISON) == fx.attendu(data)


# ── Contrôle 9 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("rendu", [fx.cii, fx.ubl], ids=["cii_381", "ubl_creditnote"])
def test_c09_un_avoir_381_est_reconnu(agent_dir, rendu):
    """Contrôle 9 : un avoir (TypeCode 381 en CII, CreditNote 381 en UBL) a le type « credit_note »."""
    result = _parse(agent_dir, "avoir.xml", rendu(fx.facture(type_code="381")))

    assert result["status"] == "success", result
    assert result["invoice"]["type"] == "credit_note"


@pytest.mark.parametrize("rendu", [fx.cii, fx.ubl], ids=["cii", "ubl"])
@pytest.mark.parametrize("code,voulu", [("380", "invoice"), ("384", "invoice"), ("325", None)])
def test_c09_type_de_document_selon_le_code(agent_dir, rendu, code, voulu):
    """Contrôle 9 (décision) : 380 et 384 -> invoice ; autre code (325) -> None."""
    result = _parse(agent_dir, "f.xml", rendu(fx.facture(type_code=code)))

    assert result["status"] == "success", result
    assert result["invoice"]["type"] == voulu


# ── Contrôle 10 ───────────────────────────────────────────────────────────────


def _floats(obj, chemin="result"):
    if isinstance(obj, float):
        yield chemin
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _floats(v, f"{chemin}.{k}")
    elif isinstance(obj, (list, tuple)):
        for i, v in enumerate(obj):
            yield from _floats(v, f"{chemin}[{i}]")


@pytest.mark.parametrize("rendu", [fx.cii, fx.ubl], ids=["cii", "ubl"])
def test_c10_montants_en_decimal_jamais_en_float(agent_dir, rendu):
    """Contrôle 10 : montants, quantités et taux sont des Decimal ; aucun float nulle part."""
    result = _parse(agent_dir, "facture.xml", rendu(fx.facture()))

    assert result["status"] == "success", result
    assert list(_floats(result)) == []
    inv = result["invoice"]
    nombres = [inv["totals"][k] for k in ("net", "vat", "gross", "due")]
    for ligne in inv["lines"]:
        nombres += [ligne[k] for k in ("quantity", "unit_price", "vat_rate", "net_amount")]
    for v in inv["vat_breakdown"]:
        nombres += [v[k] for k in ("rate", "base", "amount")]
    assert nombres and all(isinstance(n, Decimal) for n in nombres), nombres


# ── Contrôle 11 ───────────────────────────────────────────────────────────────


def test_c11_entite_externe_refusee_sans_rien_lire(agent_dir, tmp_path):
    """Contrôle 11 : un XML avec DOCTYPE et entité externe SYSTEM est refusé ; rien n'en fuit."""
    secret = tmp_path / "secret.txt"
    secret.write_text("JETON-SECRET-7f3a9c", encoding="utf-8")
    xml = fx.cii(fx.facture()).replace(
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<!DOCTYPE rsm:CrossIndustryInvoice [<!ENTITY xxe SYSTEM "file://{secret}">]>\n',
    ).replace("<ram:ID>FA-2026-0042</ram:ID>", "<ram:ID>&xxe;</ram:ID>")

    result = _parse(agent_dir, "xxe.xml", xml)

    assert result["status"] == "error", result
    assert "JETON-SECRET" not in json.dumps(result, default=str, ensure_ascii=False)


def test_c11_bombe_d_entites_refusee(agent_dir):
    """Contrôle 11 : une (petite) bombe d'entités internes est refusée, sans expansion dans la sortie."""
    doctype = (
        "<!DOCTYPE rsm:CrossIndustryInvoice ["
        '<!ENTITY a "BOUM">'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">'
        '<!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">'
        "]>\n"
    )
    xml = fx.cii(fx.facture()).replace(
        '<?xml version="1.0" encoding="UTF-8"?>\n',
        '<?xml version="1.0" encoding="UTF-8"?>\n' + doctype,
    ).replace("<ram:Name>Société Exemple Fictive SAS</ram:Name>", "<ram:Name>&d;</ram:Name>")

    result = _parse(agent_dir, "bombe.xml", xml)

    assert result["status"] == "error", result
    assert "BOUMBOUM" not in json.dumps(result, default=str, ensure_ascii=False)


# ── Contrôle 12 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "contenu",
    [
        '<?xml version="1.0"?><rss version="2.0"><channel><title>Flux</title></channel></rss>',
        '<?xml version="1.0"?><Invoice><ID>sans espace de noms</ID></Invoice>',
    ],
    ids=["rss", "invoice_sans_namespace"],
)
def test_c12_xml_d_un_autre_format_rend_format_non_reconnu(agent_dir, contenu):
    """Contrôle 12 : un XML qui n'est ni CII ni UBL rend « format non reconnu »."""
    result = _parse(agent_dir, "autre.xml", contenu)

    assert result["status"] == "error", result
    assert "format non reconnu" in result["error_message"].lower()


def test_c12_fichier_qui_n_est_pas_du_xml_rend_une_erreur(agent_dir):
    """Contrôle 12 : un fichier .xml qui n'est pas du XML rend une erreur (pas d'exception)."""
    result = _parse(agent_dir, "casse.xml", "ceci n'est pas du XML <<<")

    assert result["status"] == "error", result
    assert result["error_message"].strip()
