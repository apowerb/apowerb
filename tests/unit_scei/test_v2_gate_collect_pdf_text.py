"""TÂCHE 2 — TDD: collecte deterministe dans le gate PDF.

Avant le ``return None`` (PDF present -> LLM), le gate doit:
- pour chaque attachment PDF, resoudre le path via resolve_attachment_path(
  webhook_log_id, filename), extraire le texte (extract_first_page_text),
- ne garder que has_text_layer=True et text non vide,
- concatener TOUS les retenus etiquetes ``=== PDF: <fn> ===\\n<text>``,
- poser state["intake_pdf_text"] si >=1 retenu, sinon ne PAS poser la cle.
GARDE-FOUS: tout dans un try/except non bloquant; jamais de crash; jamais de
court-circuit not_ar a cause d un echec; comportement no-PDF -> not_ar inchange.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch



def _ctx(state: dict):
    c = MagicMock()
    c.state = state
    return c


def _pdf_att(fn="ar.pdf"):
    return {"filename": fn, "content_type": "application/pdf"}


# (a) PDF present + resolve OK + extraction OK -> cle peuplee, return None
def test_collect_populates_state_and_returns_none():
    from th2customers.scei import gates as callbacks
    state = {
        "attachments": [_pdf_att("ar.pdf")],
        "webhook_log_id": 42,
    }
    cb = callbacks.build_attachment_pdf_gate_callback("ar_intake")
    with patch.object(callbacks, "resolve_attachment_path",
                      return_value=Path("/x/ar.pdf")) as mock_resolve, \
         patch.object(callbacks, "extract_first_page_text",
                      return_value={"text": "CF101082 TILCO", "has_text_layer": True,
                                    "char_count": 14, "total_pages": 1}):
        out = cb(_ctx(state))
    assert out is None
    assert "=== PDF: ar.pdf ===" in state["intake_pdf_text"]
    assert "CF101082 TILCO" in state["intake_pdf_text"]
    mock_resolve.assert_called_once_with(42, "ar.pdf")


# (b) extraction leve -> pas de cle, return None, pas d exception, PAS de not_ar
def test_collect_extraction_raises_is_swallowed():
    from th2customers.scei import gates as callbacks
    state = {"attachments": [_pdf_att("ar.pdf")], "webhook_log_id": 7}
    cb = callbacks.build_attachment_pdf_gate_callback("ar_intake")
    with patch.object(callbacks, "resolve_attachment_path",
                      return_value=Path("/x/ar.pdf")), \
         patch.object(callbacks, "extract_first_page_text",
                      side_effect=RuntimeError("boom")):
        out = cb(_ctx(state))
    assert out is None
    assert "intake_pdf_text" not in state
    assert "ar_intake" not in state  # pas de not_ar


# (c) pas de PDF -> not_ar inchange
def test_no_pdf_still_not_ar():
    from th2customers.scei import gates as callbacks
    state = {"attachments": [{"filename": "x.txt", "content_type": "text/plain"}]}
    cb = callbacks.build_attachment_pdf_gate_callback("ar_intake")
    out = cb(_ctx(state))
    assert out is not None  # short-circuit content
    payload = json.loads(state["ar_intake"])
    assert payload["email_classification"] == "not_ar"
    assert payload["raison"] == "no_pdf_attachment"
    assert "intake_pdf_text" not in state


# (d) multi-PDF -> concatenation etiquetee des deux
def test_multi_pdf_concatenated_labeled():
    from th2customers.scei import gates as callbacks
    state = {
        "attachments": [_pdf_att("ar.pdf"), _pdf_att("cgv.pdf")],
        "webhook_log_id": 9,
    }
    cb = callbacks.build_attachment_pdf_gate_callback("ar_intake")

    def _resolve(log_id, fn):
        return Path(f"/x/{fn}")

    def _extract(path):
        if path.endswith("ar.pdf"):
            return {"text": "AR TEXT CF1", "has_text_layer": True,
                    "char_count": 11, "total_pages": 1}
        return {"text": "CGV TEXT", "has_text_layer": True,
                "char_count": 8, "total_pages": 1}

    with patch.object(callbacks, "resolve_attachment_path", side_effect=_resolve), \
         patch.object(callbacks, "extract_first_page_text", side_effect=_extract):
        out = cb(_ctx(state))
    assert out is None
    txt = state["intake_pdf_text"]
    assert "=== PDF: ar.pdf ===\nAR TEXT CF1" in txt
    assert "=== PDF: cgv.pdf ===\nCGV TEXT" in txt
    assert "\n\n" in txt


# (e) PDF sans texte (has_text_layer=False) -> cle NON posee
def test_pdf_no_text_layer_no_key():
    from th2customers.scei import gates as callbacks
    state = {"attachments": [_pdf_att("scan.pdf")], "webhook_log_id": 1}
    cb = callbacks.build_attachment_pdf_gate_callback("ar_intake")
    with patch.object(callbacks, "resolve_attachment_path",
                      return_value=Path("/x/scan.pdf")), \
         patch.object(callbacks, "extract_first_page_text",
                      return_value={"text": "", "has_text_layer": False,
                                    "char_count": 0, "total_pages": 1}):
        out = cb(_ctx(state))
    assert out is None
    assert "intake_pdf_text" not in state


# (f) forme LIVE: attachments sans path + webhook_log_id -> resolve(id, fn)
def test_live_form_resolve_called_with_id_and_filename():
    from th2customers.scei import gates as callbacks
    state = {
        "attachments": [{"filename": "live.pdf", "content_type": "application/pdf"}],
        "webhook_log_id": 314,
    }
    cb = callbacks.build_attachment_pdf_gate_callback("ar_intake")
    with patch.object(callbacks, "resolve_attachment_path",
                      return_value=Path("/x/live.pdf")) as mock_resolve, \
         patch.object(callbacks, "extract_first_page_text",
                      return_value={"text": "LIVE CF2", "has_text_layer": True,
                                    "char_count": 8, "total_pages": 1}):
        out = cb(_ctx(state))
    assert out is None
    mock_resolve.assert_called_once_with(314, "live.pdf")
    assert "LIVE CF2" in state["intake_pdf_text"]


# garde-fou supplementaire: resolve leve (fichier introuvable) -> swallowed
def test_resolve_raises_is_swallowed_no_key():
    from th2customers.scei import gates as callbacks
    state = {"attachments": [_pdf_att("ar.pdf")], "webhook_log_id": 5}
    cb = callbacks.build_attachment_pdf_gate_callback("ar_intake")
    with patch.object(callbacks, "resolve_attachment_path",
                      side_effect=ValueError("not found")):
        out = cb(_ctx(state))
    assert out is None
    assert "intake_pdf_text" not in state
    assert "ar_intake" not in state
