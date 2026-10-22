"""F4 — tool_einvoice_to_table(sources) : un lot de factures -> une table BI / CSV.

Contrôles 19 à 21 de la spec.
"""

from __future__ import annotations

import json
from decimal import Decimal

from tests.einvoice import einvoice_fixtures as fx

COLONNES = ["numero", "date", "vendeur", "siren", "ht", "tva", "ttc", "echeance", "nb_erreurs", "statut"]


def _table(sources):
    from apowerb.tools_store.portfolio.einvoice import tool_einvoice_to_table

    return tool_einvoice_to_table(sources)


def _montant(v) -> Decimal:
    assert not isinstance(v, float), f"montant en float : {v!r}"
    return Decimal(str(v))


# ── Contrôle 19 ───────────────────────────────────────────────────────────────


def test_c19_lot_de_3_factures_rend_3_lignes_aux_colonnes_fixes(agent_dir):
    """Contrôle 19 : 3 factures différentes (CII, UBL avec une erreur, Factur-X) -> 3 lignes justes, dans l'ordre."""
    b = fx.facture2()
    b["iban"] = "FR1420041010050500013M02607"  # IBAN faux : 1 erreur
    c = fx.facture_une_ligne("FA-C-003", "3", "19.99", "20.00",
                             issue_date="2026-12-02", due_date="2027-01-02")
    fx.ecrire(agent_dir, "a.xml", fx.cii(fx.facture(number="FA-A-001")))
    fx.ecrire(agent_dir, "b.xml", fx.ubl(b))
    fx.ecrire(agent_dir, "c.pdf", fx.facturx(fx.cii(c)))

    result = _table(["a.xml", "b.xml", "c.pdf"])

    assert result["status"] == "success", result
    assert result["columns"] == COLONNES
    assert result["output_truncated"] is False
    rows = result["rows"]
    assert len(rows) == 3
    for row in rows:
        assert list(row) == COLONNES, row
    voulu = [
        ["FA-A-001", "2026-10-01", "Société Exemple Fictive SAS", fx.SIREN_VENDEUR,
         "530.00", "101.65", "631.65", "2026-10-31", 0, "ok"],
        ["INV-2026-0999", "2026-11-15", "Bureau d'Études Fictif SARL", fx.SIREN_VENDEUR_2,
         "150.00", "15.00", "165.00", "2026-12-15", 1, "erreurs"],
        ["FA-C-003", "2026-12-02", "Société Exemple Fictive SAS", fx.SIREN_VENDEUR,
         "59.97", "11.99", "71.96", "2027-01-02", 0, "ok"],
    ]
    for row, attendu in zip(rows, voulu):
        lu = [row[c] for c in COLONNES]
        for i in (4, 5, 6):
            lu[i] = _montant(lu[i])
            attendu[i] = Decimal(attendu[i])
        assert lu == attendu, row


def test_c06_lot_hors_des_uploads_lignes_illisibles_sans_lecture(agent_dir, tmp_path):
    """Contrôle 6 (to_table) : chemins hors uploads et URL -> lignes « illisible », fichier jamais ouvert, rien n'en fuit."""
    xml = fx.cii(fx.facture(number="FA-HORS-DOSSIER")).encode("utf-8")
    sources, vise = fx.sources_hors_dossier(agent_dir, tmp_path, ".xml", xml)
    sources += fx.URLS
    from apowerb.tools_store.portfolio.einvoice import tool_einvoice_to_table

    with fx.espion_lectures(vise) as lectures:
        result = tool_einvoice_to_table(sources)

    assert lectures == [], lectures
    assert result["status"] == "success", result
    assert [r["statut"] for r in result["rows"]] == ["illisible"] * len(sources)
    assert "FA-HORS-DOSSIER" not in json.dumps(result, default=str)


# ── Contrôle 20 ───────────────────────────────────────────────────────────────


def test_c20_facture_illisible_donne_une_ligne_illisible_sans_arreter_le_lot(agent_dir):
    """Contrôle 20 : illisibles (pas du XML, absent, autre format, PDF tronqué) = ligne « illisible », le lot continue."""
    fx.ecrire(agent_dir, "bonne1.xml", fx.cii(fx.facture(number="FA-1")))
    fx.ecrire(agent_dir, "casse.xml", "pas du xml <<<")
    fx.ecrire(agent_dir, "rss.xml", '<?xml version="1.0"?><rss><channel/></rss>')
    pdf = fx.facturx(fx.cii(fx.facture()))
    fx.ecrire(agent_dir, "tronque.pdf", pdf[: len(pdf) // 2])
    fx.ecrire(agent_dir, "bonne2.xml", fx.ubl(fx.facture(number="FA-2")))

    result = _table(["bonne1.xml", "casse.xml", "absente.xml", "rss.xml", "tronque.pdf", "bonne2.xml"])

    assert result["status"] == "success", result
    rows = result["rows"]
    assert [r["statut"] for r in rows] == ["ok", "illisible", "illisible", "illisible", "illisible", "ok"]
    assert rows[0]["numero"] == "FA-1" and rows[-1]["numero"] == "FA-2"
    for row in rows:
        assert list(row) == COLONNES, row


# ── Contrôle 21 ───────────────────────────────────────────────────────────────


def test_c21_au_dela_de_200_fichiers_le_lot_est_tronque_avec_un_drapeau(agent_dir):
    """Contrôle 21 : 201 sources -> 200 lignes et output_truncated vrai ; l'ordre est gardé."""
    fx.ecrire(agent_dir, "premiere.xml", fx.cii(fx.facture(number="FA-PREMIERE")))
    sources = ["premiere.xml"] + [f"absente-{i}.xml" for i in range(200)]

    result = _table(sources)

    assert result["status"] == "success", result
    assert len(result["rows"]) == 200
    assert result["output_truncated"] is True
    assert result["rows"][0]["numero"] == "FA-PREMIERE"


def test_c21_200_fichiers_exactement_ne_sont_pas_tronques(agent_dir):
    """Contrôle 21 : 200 sources -> 200 lignes, output_truncated faux."""
    result = _table([f"absente-{i}.xml" for i in range(200)])

    assert result["status"] == "success", result
    assert len(result["rows"]) == 200
    assert result["output_truncated"] is False
