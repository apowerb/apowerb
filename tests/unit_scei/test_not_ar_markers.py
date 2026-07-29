"""Unit tests for the deterministic NOT-AR marker detector (levier 2, shadow).

Pure functions, no DB, no ADK. Safe to run standalone:
    .venv/bin/pytest tests/unit_scei/test_not_ar_markers.py
"""
from th2customers.scei.superagents.scei_not_ar_markers import detect_not_ar_markers


def test_bon_de_livraison_with_cf_would_skip():
    text = "BON DE LIVRAISON\nClient SCEI\nCommande CF101234\nColis 1/2"
    v = detect_not_ar_markers(text)
    assert v["would_skip"] is True
    assert v["not_ar_marker"] == "bon_de_livraison"
    assert v["ar_marker"] is None


def test_facture_with_cf_would_skip():
    text = "FACTURE N 2026-0042\nVotre commande CF100898\nTotal HT 1200"
    v = detect_not_ar_markers(text)
    assert v["would_skip"] is True
    assert v["not_ar_marker"] == "facture_num"


def test_real_ar_not_skipped():
    text = "ACCUSE DE RECEPTION DE COMMANDE\nReference CF101082\nLigne 1 ..."
    v = detect_not_ar_markers(text)
    assert v["would_skip"] is False
    assert v["ar_marker"] == "accuse_de_reception"


def test_ambiguous_both_markers_defers():
    # A doc carrying BOTH an AR header and a delivery word -> defer to LLM.
    text = "Accuse de reception de commande CF101082\n(bon de livraison joint)"
    v = detect_not_ar_markers(text)
    assert v["would_skip"] is False
    assert v["not_ar_marker"] == "bon_de_livraison"
    assert v["ar_marker"] == "accuse_de_reception"


def test_accents_and_case_avis_expedition():
    v = detect_not_ar_markers("Avis d’expedition\nCF100777")
    assert v["would_skip"] is True
    assert v["not_ar_marker"] == "avis_expedition"


def test_bare_facture_word_does_not_trigger():
    # "facturation" / bare "facture" must NOT match the strong invoice form.
    text = "Confirmation de commande CF101000\nConditions de facturation: 30j"
    v = detect_not_ar_markers(text)
    assert v["would_skip"] is False
    assert v["not_ar_marker"] is None
    assert v["ar_marker"] == "confirmation_de_commande"


def test_german_lieferschein():
    v = detect_not_ar_markers("LIEFERSCHEIN\nBestellung CF100123")
    assert v["would_skip"] is True
    assert v["not_ar_marker"] == "lieferschein"


def test_english_delivery_note():
    v = detect_not_ar_markers("DELIVERY NOTE\nPO CF100456")
    assert v["would_skip"] is True
    assert v["not_ar_marker"] == "delivery_note"


def test_marker_below_header_window_is_ignored():
    # NOT-AR marker only AFTER the header window -> not detected (high precision).
    filler = "ligne de produit ref X qty 1 prix 2 eur " * 60  # > 1200 chars
    assert len(filler) > 1200
    text = "Document SCEI CF100999\n" + filler + "\nbon de livraison"
    v = detect_not_ar_markers(text)
    assert v["not_ar_marker"] is None
    assert v["would_skip"] is False


def test_empty_and_none():
    assert detect_not_ar_markers(None)["would_skip"] is False
    assert detect_not_ar_markers("")["would_skip"] is False
    assert detect_not_ar_markers("   ")["would_skip"] is False
