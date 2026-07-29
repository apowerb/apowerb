"""Option A : gate ACTIF -> QUARANTINE ne court-circuite PAS l'intake.

Le gate est cable EN TETE (before_agent_callback prefixe) donc intake_pdf_text
est vide -> un vrai AR dont le contenu est dans le PDF tombe en QUARANTINE
(ar_explicit_no_pdf / ar_weak_no_pdf / ambiguous_with_pdf). Court-circuiter
QUARANTINE = perte silencieuse de vrais AR (mesure prod 2026-05-29 :
4634/4657/4663/4666 = vrais AR). On laisse donc QUARANTINE filer a l'intake ;
seul NOT_AR (transporteur/facture/BL, decidable sans PDF) court-circuite.
"""
import json

from th2customers.scei.gates import build_ar_gate_callback
from th2customers.scei.superagents.scei_ar_gate import classify_email


def _ctx(state):
    return type("C", (), {"state": state})()


def _quarantine_state():
    # "Accuse de Reception" + PJ PDF, SANS texte PDF -> QUARANTINE layer 3
    return {
        "email_subject": "Accuse de Reception - V/Ref. CF101265",
        "email_sender": "commande@relly.fr",
        "attachments": [{"filename": "AR.pdf", "content_type": "application/pdf"}],
    }


def test_quarantine_inputs_really_classify_quarantine():
    """Garde-fou : on teste bien le chemin QUARANTINE (sinon faux-vert)."""
    s = _quarantine_state()
    v = classify_email(
        subject=s["email_subject"], body=None, sender=s["email_sender"],
        attachment_names=["AR.pdf"], has_pdf_attachment=True, first_page_text=None,
    )
    assert v["verdict"] == "QUARANTINE", v


def test_ar_gate_active_lets_quarantine_through():
    cb = build_ar_gate_callback("intake_out", shadow=False)
    state = _quarantine_state()
    assert cb(_ctx(state)) is None        # passe a l'intake, PAS de court-circuit
    assert "intake_out" not in state      # aucun payload not_ar ecrit
    assert "scei_gate_quarantine" not in state
