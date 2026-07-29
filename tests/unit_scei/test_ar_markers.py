"""Tests patterns AR/NOT_AR — fonctions pures (pas de DB)."""
from th2customers.scei.superagents.scei_not_ar_markers import (
    classify_text_signals,
    detect_ar_markers,
    detect_not_ar_markers,
)


def test_ar_marker_cc_num():
    assert detect_ar_markers("CC n° 11090361 V/R:CF101259")["ar_marker"] == "cc_num"


def test_ar_marker_existing_accuse():
    assert (
        detect_ar_markers("ACCUSE DE RECEPTION de votre commande")["ar_marker"]
        == "accuse_de_reception"
    )


def test_ar_marker_none_on_facture():
    assert detect_ar_markers("FACTURE N° 2026-118")["ar_marker"] is None


def test_notar_avoir_added():
    assert detect_not_ar_markers("AVOIR N 2026-001 montant remboursé")["not_ar_marker"] == "avoir"


def test_notar_billing_document_added():
    assert (
        detect_not_ar_markers("Your Danfoss billing document 6100628396")["not_ar_marker"]
        == "billing_document"
    )


# --- classify_text_signals (jeu souple objet/corps/PJ) ---

def test_facture_loose_subject():
    s = classify_text_signals("Envoi de la facture FC21366 (etiquettes CF101132)")
    assert s["not_ar_strong"] and s["ar_weak"]


def test_billing_document_signal():
    assert classify_text_signals("Your Danfoss billing document 6100628396")["not_ar_strong"]


def test_cc_num_ar_explicit():
    assert classify_text_signals("CC n° 11090361 V/R:CF101259")["ar_explicit"]
    assert classify_text_signals("CC n° 11090361 V/R:CF101259")["ar_weak"]


def test_colis_logistics():
    assert classify_text_signals("Votre colis RS COMPONENTS est en chemin")["not_ar_logistics"]


def test_garbage_no_signal():
    s = classify_text_signals("Bonjour, merci de votre retour")
    assert not any(
        [s["not_ar_strong"], s["not_ar_logistics"], s["ar_explicit"], s["ar_weak"]]
    )
