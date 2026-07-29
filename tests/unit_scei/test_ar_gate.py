"""Tests de la cascade déterministe AR/NOT_AR — fonction pure (pas de DB/LLM).
Cas RÉELS mesurés en prod (webhook_logs).
"""
from th2customers.scei.superagents.scei_ar_gate import classify_email


def _c(subject="", body="", sender="x@f.fr", names=None, has_pdf=False, fpt=None):
    return classify_email(
        subject=subject, body=body, sender=sender,
        attachment_names=names or [], has_pdf_attachment=has_pdf, first_page_text=fpt,
    )


# --- étape 1 transporteur ---
def test_carrier_notar():
    r = _c("Votre colis", sender="x@chronopost.fr", names=["bl.pdf"], has_pdf=True)
    assert r["verdict"] == "NOT_AR" and r["layer"] == 1


# --- étape 5 ambigu ---
def test_no_signal_no_pdf_notar():
    r = _c("Bonjour", sender="x@y.fr")
    assert r["verdict"] == "NOT_AR" and r["layer"] == 5


def test_no_signal_with_pdf_quarantine():
    r = _c("empty", names=["doc.pdf"], has_pdf=True)
    assert r["verdict"] == "QUARANTINE" and r["layer"] == 5


# --- étape 2 NOT_AR fort prime sur CF faible (faux positif mesuré) ---
def test_facture_with_cf_is_notar():
    r = _c("Envoi de la facture FC21366 (etiquettes CF101132)",
           names=["facture_FC21366.pdf"], has_pdf=True)
    assert r["verdict"] == "NOT_AR" and r["layer"] == 2


def test_danfoss_billing_with_cf_is_notar():
    r = _c("Your Danfoss billing document 6100628396 for your PO CF101035",
           sender="x@danfoss.com", names=["billing.pdf"], has_pdf=True)
    assert r["verdict"] == "NOT_AR" and r["layer"] == 2


# --- étape 3 libellé AR explicite + modulation PDF ---
def test_cc_num_with_pdftext_is_ar():
    r = _c("CC n° 11090361 V/R:CF101259", sender="x@socomec.com",
           names=["CC_011090361.PDF"], has_pdf=True, fpt="ACCUSE DE RECEPTION de commande")
    assert r["verdict"] == "AR" and r["layer"] == 3


def test_cc_num_scanned_pdf_quarantine():
    r = _c("CC n° 11090361 V/R:CF101259", sender="x@dupont-est.fr",
           names=["CC_011090361.PDF"], has_pdf=True, fpt=None)  # scan illisible
    assert r["verdict"] == "QUARANTINE" and r["layer"] == 3


# --- étape 4 AR faible (CF) + modulation PDF ---
def test_notification_commande_cf_with_pdf_is_ar():
    r = _c("Notification de commande - CF101240", sender="x@rs-components.com",
           names=["AR.pdf"], has_pdf=True, fpt="bon de commande détail lignes")
    assert r["verdict"] == "AR" and r["layer"] == 4


def test_notification_commande_cf_without_pdf_quarantine():
    r = _c("Notification de commande - CF101240", sender="x@rs-components.com")
    assert r["verdict"] == "QUARANTINE" and r["layer"] == 4


# --- transféré interne @scei88.fr (transitaire transparent) ---
def test_forwarded_internal_danfoss_quarantine():
    r = _c("TR: Danfoss Order Confirmation 1609426516 for your PO CF101120",
           sender="j.fruchart@scei88.fr")
    assert r["verdict"] == "QUARANTINE"  # AR reconnu mais pas de PDF


# --- robustesse ---
def test_none_inputs_no_crash():
    r = _c(subject=None, body=None, sender=None, names=None, has_pdf=False, fpt=None)
    assert r["verdict"] in ("NOT_AR", "QUARANTINE")
