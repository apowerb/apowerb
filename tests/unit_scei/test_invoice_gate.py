"""Invoice-gate : court-circuit not_ar restreint aux documents commerciaux.

Mesure prod 2026-06-02 (161 AR) : le marqueur ``facture_num`` attrape 5 factures
(REXEL, ELECTRO VOSGES), 0 vrai AR conforme. Les marqueurs LOGISTIQUES
(``bon de livraison``...) sont EXCLUS car de vrais AR SOCOMEC/CARLO GAVAZZI les
portent en entête (faux positif). Le court-circuit est gardé derrière le
kill-switch ``SCEI_INVOICE_GATE_ACTIVE``.

Fonctions pures, pas de DB ni ADK.
Run: .venv/bin/pytest tests/unit_scei/test_invoice_gate.py
"""
import json

from th2customers.scei.superagents.scei_not_ar_markers import (
    ACTIVE_NOT_AR_MARKERS,
    should_shortcircuit_invoice,
)


def _ctx(state):
    return type("C", (), {"state": state})()

FACTURE = "FACTURE N° 998922444\nLe 30/05/2026\nVotre commande : CF100939\nREXEL EPINAL\n"
FACTURE_EV = "ELECTRO VOSGES\nDevise EUR\nFACTURE N°\n29/05/2026\n053891\nSCEI SAS\n"
BON_LIVRAISON = "BON DE LIVRAISON N° BL-8801-519678812\nSOCOMEC\nVotre commande CF101114\n"
AR_TE = "ORDER ACKNOWLEDGEMENT\nOrder no. 3093125233\nYour purchase order: CF097383\nQuantity ordered 20\n"
AR_SOCOMEC = "ACCUSE DE RECEPTION\nCC n° 11090361\nConfirmation de commande CF101259\n"


def test_facture_shortcircuit_when_gate_on():
    r = should_shortcircuit_invoice(FACTURE, gate_active=True)
    assert r["shortcircuit"] is True
    assert r["marker"] == "facture_num"


def test_facture_electro_vosges_shortcircuit():
    r = should_shortcircuit_invoice(FACTURE_EV, gate_active=True)
    assert r["shortcircuit"] is True


def test_facture_not_shortcircuit_when_gate_off():
    # Kill-switch OFF : détecté (would_skip) mais JAMAIS court-circuité.
    r = should_shortcircuit_invoice(FACTURE, gate_active=False)
    assert r["shortcircuit"] is False
    assert r["would_skip"] is True


def test_bon_de_livraison_never_shortcircuit_even_gate_on():
    # Marqueur LOGISTIQUE : de vrais AR le portent -> JAMAIS court-circuité
    # (protège CF101114 SOCOMEC, CF100443 CARLO GAVAZZI, etc.).
    r = should_shortcircuit_invoice(BON_LIVRAISON, gate_active=True)
    assert r["shortcircuit"] is False
    assert r["would_skip"] is True            # détecté, reste en shadow
    assert r["marker"] == "bon_de_livraison"


def test_real_ar_te_connectivity_passes():
    r = should_shortcircuit_invoice(AR_TE, gate_active=True)
    assert r["shortcircuit"] is False
    assert r["would_skip"] is False           # marqueur AR -> DEFER


def test_real_ar_socomec_passes():
    r = should_shortcircuit_invoice(AR_SOCOMEC, gate_active=True)
    assert r["shortcircuit"] is False
    assert r["would_skip"] is False


def test_empty_or_none_text_is_safe():
    for txt in (None, "", "   "):
        r = should_shortcircuit_invoice(txt, gate_active=True)
        assert r["shortcircuit"] is False
        assert r["would_skip"] is False


def test_devis_detected_but_not_shortcircuit():
    # "devis" est détecté en shadow (would_skip) mais PAS court-circuité : un AR
    # peut rappeler un n° de devis en entête -> on ne le jette pas (réserve
    # critiqueur 2026-06-02).
    r = should_shortcircuit_invoice("DEVIS N° D-2025-0042\nArticle X\n", gate_active=True)
    assert r["would_skip"] is True
    assert r["shortcircuit"] is False
    assert r["marker"] == "devis"


def test_proforma_detected_but_not_shortcircuit():
    r = should_shortcircuit_invoice("PROFORMA INVOICE\nItem 1\n", gate_active=True)
    assert r["shortcircuit"] is False


def test_active_markers_restricted_to_invoice_titles():
    # Périmètre actif = titres de facturation prouvés/structurels uniquement.
    assert "facture_num" in ACTIVE_NOT_AR_MARKERS
    assert "invoice_no" in ACTIVE_NOT_AR_MARKERS
    assert "rechnung_nr" in ACTIVE_NOT_AR_MARKERS
    # Logistiques EXCLUS (faux positifs sur de vrais AR SOCOMEC/CARLO GAVAZZI).
    assert "bon_de_livraison" not in ACTIVE_NOT_AR_MARKERS
    assert "delivery_note" not in ACTIVE_NOT_AR_MARKERS
    assert "bordereau_de_livraison" not in ACTIVE_NOT_AR_MARKERS
    assert "packing_list" not in ACTIVE_NOT_AR_MARKERS
    # Commerciaux AMBIGUS EXCLUS (non prouvés -> shadow).
    assert "devis" not in ACTIVE_NOT_AR_MARKERS
    assert "proforma" not in ACTIVE_NOT_AR_MARKERS
    assert "avoir" not in ACTIVE_NOT_AR_MARKERS


# --- Tests d'intégration du callback (attrapent le NameError + le strip préfixe) ---

def _patch_pdf(monkeypatch, text):
    from th2customers.scei import gates as cbmod
    monkeypatch.setattr(cbmod, "resolve_attachment_path", lambda lid, fn: f"/fake/{fn}", raising=False)
    monkeypatch.setattr(
        cbmod, "extract_first_page_text",
        lambda p: {"has_text_layer": True, "text": text}, raising=False,
    )
    return cbmod


def _state(filename):
    return {
        "attachments": [{"filename": filename, "content_type": "application/pdf"}],
        "webhook_log_id": 999,
    }


def test_callback_shortcircuits_invoice_end_to_end(monkeypatch):
    # Bout-en-bout : flag ON + contenu FACTURE -> court-circuit not_ar.
    # (Ce test échoue si `os` n'est pas importable dans le callback.)
    cbmod = _patch_pdf(monkeypatch, "FACTURE N° 998922444\nVotre commande CF100939\n")
    monkeypatch.setenv("SCEI_INVOICE_GATE_ACTIVE", "1")
    cb = cbmod.build_attachment_pdf_gate_callback("intake_out")
    state = _state("998922444.pdf")
    r = cb(_ctx(state))
    assert r is not None, "le gate facture doit court-circuiter (Content), pas None"
    assert json.loads(state["intake_out"])["email_classification"] == "not_ar"


def test_callback_no_shortcircuit_when_gate_off(monkeypatch):
    cbmod = _patch_pdf(monkeypatch, "FACTURE N° 998922444\n")
    monkeypatch.delenv("SCEI_INVOICE_GATE_ACTIVE", raising=False)
    cb = cbmod.build_attachment_pdf_gate_callback("intake_out")
    state = _state("998922444.pdf")
    assert cb(_ctx(state)) is None          # flag off -> pas de court-circuit
    assert "intake_out" not in state


def test_callback_filename_facture_does_not_falsematch(monkeypatch):
    # Nom de fichier "Facture_N_1234.pdf" mais contenu NON-facture : le préfixe
    # filename est strippé -> aucun faux match -> pas de court-circuit.
    cbmod = _patch_pdf(monkeypatch, "Bon pour accord\nArticle X quantite 20 prix 5.00\n")
    monkeypatch.setenv("SCEI_INVOICE_GATE_ACTIVE", "1")
    cb = cbmod.build_attachment_pdf_gate_callback("intake_out")
    state = _state("Facture_N_1234.pdf")
    assert cb(_ctx(state)) is None
    assert "intake_out" not in state
