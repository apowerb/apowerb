"""F1 — tool_einvoice_extract(source) : sortir le XML d'un PDF Factur-X et dire son profil.

Contrôles 1 à 6 de la spec. Le module testé est importé DANS chaque test : tant
qu'il n'existe pas, chaque test échoue seul, sans casser la collecte.
"""

from __future__ import annotations

import importlib
import json

import pytest

from tests.einvoice import einvoice_fixtures as fx

MODULE = "apowerb.tools_store.portfolio.einvoice"


def _extract():
    from apowerb.tools_store.portfolio.einvoice import tool_einvoice_extract

    return tool_einvoice_extract


def _msg(result: dict) -> str:
    return str(result.get("error_message", "")).replace("’", "'").lower()


def _xml_ref() -> bytes:
    return fx.cii(fx.facture()).encode("utf-8")


def _xml_crlf() -> bytes:
    """2e facture (autre contenu), fins de ligne CRLF et accents."""
    xml = fx.cii(fx.facture2()).replace("\n", "\r\n").encode("utf-8")
    assert b"\r\n" in xml and "É".encode() in xml
    return xml


# ── Contrôle 1 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("fabrique", [_xml_ref, _xml_crlf], ids=["facture1", "facture2_crlf"])
def test_c01_le_xml_rendu_est_octet_pour_octet_celui_attache(agent_dir, fabrique):
    """Contrôle 1 : un PDF Factur-X généré rend le XML exact qui y a été attaché."""
    extract = _extract()
    xml = fabrique()
    assert "é".encode() in xml  # accents : l'aller-retour d'encodage est exercé
    fx.ecrire(agent_dir, "facture.pdf", fx.facturx(xml))

    result = extract("facture.pdf")

    assert result["status"] == "success", result
    assert isinstance(result["xml"], str)
    assert result["xml"].encode("utf-8") == xml


# ── Contrôle 2 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("profil", ["MINIMUM", "BASIC", "EN 16931", "EXTENDED"])
def test_c02_le_profil_est_reconnu(agent_dir, profil):
    """Contrôle 2 : profil MINIMUM, BASIC, EN 16931, EXTENDED lu dans le XML attaché."""
    extract = _extract()
    xml = fx.cii(fx.facture(profile_urn=fx.URN_PROFILS[profil])).encode("utf-8")
    fx.ecrire(agent_dir, "facture.pdf", fx.facturx(xml))

    result = extract("facture.pdf")

    assert result["status"] == "success", result
    assert result["profile"] == profil


@pytest.mark.parametrize("urn", [None, "urn:exemple:profil-inconnu"], ids=["absent", "inconnu"])
def test_c02_profil_absent_ou_inconnu_rend_none(agent_dir, urn):
    """Contrôle 2 (décision) : profil absent ou inconnu -> extraction réussie, profile None."""
    extract = _extract()
    xml = fx.cii(fx.facture(profile_urn=urn)).encode("utf-8")
    fx.ecrire(agent_dir, "facture.pdf", fx.facturx(xml))

    result = extract("facture.pdf")

    assert result["status"] == "success", result
    assert result["profile"] is None
    assert result["xml"].encode("utf-8") == xml


# ── Contrôle 3 ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "pieces",
    [{}, {"notes.txt": b"Note interne fictive"}],
    ids=["sans_piece_jointe", "piece_jointe_autre_que_factur-x"],
)
def test_c03_pdf_sans_xml_rend_une_erreur_claire(agent_dir, pieces):
    """Contrôle 3 : un PDF sans XML Factur-X rend « ce PDF n'est pas une facture électronique »."""
    extract = _extract()
    pdf = fx.avec_pieces(fx.pdf_vierge(), pieces) if pieces else fx.pdf_vierge()
    fx.ecrire(agent_dir, "simple.pdf", pdf)

    result = extract("simple.pdf")

    assert result["status"] == "error", result
    assert "n'est pas une facture électronique" in _msg(result)


# ── Contrôle 4 ────────────────────────────────────────────────────────────────


def test_c04_pdf_chiffre_rend_une_erreur_claire(agent_dir):
    """Contrôle 4 : un PDF Factur-X chiffré par mot de passe rend une erreur claire en français."""
    extract = _extract()
    fx.ecrire(agent_dir, "chiffre.pdf", fx.chiffrer(fx.facturx(_xml_ref())))

    result = extract("chiffre.pdf")

    assert result["status"] == "error", result
    msg = _msg(result)
    assert any(m in msg for m in ("chiffr", "mot de passe", "protégé")), msg
    assert "FA-2026-0042" not in json.dumps(result, default=str)


@pytest.mark.parametrize("cas", ["tronque", "pas_un_pdf"])
def test_c04_pdf_corrompu_rend_une_erreur_claire(agent_dir, cas):
    """Contrôle 4 : un PDF corrompu (tronqué, ou octets quelconques) rend une erreur claire."""
    extract = _extract()
    complet = fx.facturx(_xml_ref())
    contenu = complet[: len(complet) // 2] if cas == "tronque" else b"%PDF-1.7\n\x00\x01 pas un pdf"
    fx.ecrire(agent_dir, "abime.pdf", contenu)

    result = extract("abime.pdf")

    assert result["status"] == "error", result
    msg = _msg(result)
    assert any(m in msg for m in ("illisible", "corrompu", "endommag")), msg


# ── Contrôle 5 ────────────────────────────────────────────────────────────────


def _fichier_creux(chemin, taille: int) -> None:
    with open(chemin, "wb") as f:
        f.truncate(taille)


@pytest.mark.parametrize("par_chemin", [False, True], ids=["nom_nu", "chemin"])
def test_c05_fichier_de_plus_de_10_mo_refuse_avant_lecture(agent_dir, monkeypatch, par_chemin):
    """Contrôle 5 : > 10 Mo refusé AVANT toute ouverture du fichier.

    Fichier creux de 10 Mo + 1 octet (truncate, rien n'est écrit). Toute
    ouverture/lecture de ce fichier (open, io.open, os.open, Path.open,
    Path.read_bytes) et tout appel à pypdf.PdfReader sont enregistrés : la
    liste doit rester vide. Seuls stat/getsize sont permis.
    """
    import pypdf

    mod = importlib.import_module(MODULE)
    assert mod._MAX_FILE_BYTES == 10 * 1024 * 1024

    gros = agent_dir / "enorme.pdf"
    _fichier_creux(gros, 10 * 1024 * 1024 + 1)
    appels_pdf: list[str] = []

    class LecteurInterdit:
        def __init__(self, *a, **k):
            appels_pdf.append("PdfReader")
            raise RuntimeError("PdfReader appelé sur un fichier trop gros")

    monkeypatch.setattr(pypdf, "PdfReader", LecteurInterdit)
    monkeypatch.setattr(mod, "PdfReader", LecteurInterdit, raising=False)

    with fx.espion_lectures(lambda p: p.endswith("enorme.pdf")) as lectures:
        result = mod.tool_einvoice_extract(str(gros) if par_chemin else "enorme.pdf")

    assert lectures == [] and appels_pdf == [], f"fichier lu avant le refus : {lectures + appels_pdf}"
    assert result["status"] == "error", result
    assert "10 mo" in _msg(result), result


def test_c05_exactement_10_mo_n_est_pas_refuse_pour_la_taille(agent_dir):
    """Contrôle 5 (frontière) : exactement 10 × 1024 × 1024 octets n'est PAS refusé pour la taille.

    Le fichier creux n'est pas un PDF : il est illisible, mais le message ne
    parle pas de la limite de 10 Mo.
    """
    extract = _extract()
    _fichier_creux(agent_dir / "limite.pdf", 10 * 1024 * 1024)

    result = extract("limite.pdf")

    assert result["status"] == "error", result
    assert "10 mo" not in _msg(result), result


# ── Contrôle 6 ────────────────────────────────────────────────────────────────


def test_c06_nom_d_upload_retrouve_dans_le_magasin(agent_dir):
    """Contrôle 6 : le nom nu d'un fichier envoyé à l'agent est retrouvé dans uploads/agent{root}/."""
    extract = _extract()
    xml = _xml_ref()
    fx.ecrire(agent_dir, "recue.pdf", fx.facturx(xml))

    result = extract("recue.pdf")

    assert result["status"] == "success", result
    assert result["xml"].encode("utf-8") == xml


def test_c06_chemin_dans_les_uploads_de_l_agent_accepte(agent_dir):
    """Contrôle 6 : un chemin qui pointe dans uploads/agent{root}/ est lu."""
    extract = _extract()
    xml = _xml_ref()
    fx.ecrire(agent_dir, "recue.pdf", fx.facturx(xml))

    result = extract(str(agent_dir / "recue.pdf"))

    assert result["status"] == "success", result
    assert result["xml"].encode("utf-8") == xml


def test_c06_chemins_hors_des_uploads_refuses_sans_lecture(agent_dir, tmp_path):
    """Contrôle 6 : autre dossier, autre agent, ../, lien sortant = refusé, et le fichier n'est jamais ouvert."""
    extract = _extract()
    pdf = fx.facturx(fx.cii(fx.facture(number="FA-HORS-DOSSIER")))
    sources, vise = fx.sources_hors_dossier(agent_dir, tmp_path, ".pdf", pdf)

    with fx.espion_lectures(vise) as lectures:
        resultats = [(s, extract(s)) for s in sources]

    assert lectures == [], lectures
    for source, result in resultats:
        assert result["status"] == "error", (source, result)
        assert "FA-HORS-DOSSIER" not in json.dumps(result, default=str), source


def test_c06_url_distante_refusee_sans_reseau(agent_dir, monkeypatch):
    """Contrôle 6 : une URL http(s) est refusée en v1, sans aucun appel réseau."""
    import socket

    appels: list[str] = []

    def interdit(*a, **k):
        appels.append("réseau")
        raise OSError("réseau interdit pendant le test")

    monkeypatch.setattr(socket, "create_connection", interdit)
    monkeypatch.setattr(socket.socket, "connect", interdit)
    extract = _extract()

    for url in fx.URLS:
        result = extract(url)
        assert result["status"] == "error", result
        assert "url" in _msg(result), result
    assert appels == []


@pytest.mark.parametrize("source", ["", "   ", "absente.pdf", "notes.txt"],
                         ids=["vide", "blancs", "absente", "extension_txt"])
def test_c06_source_invalide_rend_une_erreur_sans_exception(agent_dir, source):
    """Contrôle 6 : source vide, fichier absent ou extension non acceptée -> status error, pas d'exception."""
    extract = _extract()
    fx.ecrire(agent_dir, "notes.txt", "texte quelconque")

    result = extract(source)

    assert result["status"] == "error", result
    assert str(result.get("error_message", "")).strip()
