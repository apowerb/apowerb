"""build_ar_gate_callback : shadow (log-only) vs actif (court-circuit)."""
import json


def _ctx(state):
    return type("C", (), {"state": state})()


def test_ar_gate_shadow_never_shortcircuits():
    from th2customers.scei.gates import build_ar_gate_callback
    cb = build_ar_gate_callback("intake_out", shadow=True)
    state = {
        "email_subject": "Votre colis est en chemin",
        "email_sender": "x@chronopost.fr",
        "attachments": [{"filename": "bl.pdf", "content_type": "application/pdf"}],
    }
    assert cb(_ctx(state)) is None
    assert "intake_out" not in state  # shadow n'écrit rien


def test_ar_gate_active_shortcircuits_notar():
    from th2customers.scei.gates import build_ar_gate_callback
    cb = build_ar_gate_callback("intake_out", shadow=False)
    state = {
        "email_subject": "Votre colis est en chemin",
        "email_sender": "x@chronopost.fr",
        "attachments": [{"filename": "bl.pdf", "content_type": "application/pdf"}],
    }
    r = cb(_ctx(state))
    assert r is not None  # Content de court-circuit
    assert json.loads(state["intake_out"])["email_classification"] == "not_ar"


def test_ar_gate_active_lets_ar_through():
    from th2customers.scei.gates import build_ar_gate_callback
    cb = build_ar_gate_callback("intake_out", shadow=False)
    state = {
        "email_subject": "CC n° 11090361 V/R:CF101259",
        "email_sender": "x@socomec.com",
        "attachments": [{"filename": "CC_011090361.PDF", "content_type": "application/pdf"}],
        "intake_pdf_text": "ACCUSE DE RECEPTION de votre commande ...",
    }
    assert cb(_ctx(state)) is None  # AR -> l'intake tourne
    assert "intake_out" not in state
