"""Critiqueur corrections A & B sur build_attachment_pdf_gate_callback.

A (multi-PDF, collecte PARTIELLE): le try/except doit etre PAR PDF. Un PDF
qui leve est saute (warning) ; les PDF deja extraits sont conserves dans
state["intake_pdf_text"].

B (anti-32k): le texte de chaque PDF est cape a _INTAKE_PDF_TEXT_PER_PDF_CAP
avant concatenation, puis le resultat concatene est cape a
_INTAKE_PDF_TEXT_TOTAL_CAP. Un marqueur lisible signale la troncature.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch



def _ctx(state: dict):
    class _C:
        pass
    c = _C()
    c.state = state
    return c


def _pdf_att(fn):
    return {"filename": fn, "content_type": "application/pdf"}


# === CORRECTION A ===
def test_multi_pdf_partial_failure_keeps_successful():
    from th2customers.scei import gates as callbacks
    state = {
        "attachments": [_pdf_att("ar.pdf"), _pdf_att("cgv.pdf")],
        "webhook_log_id": 9,
    }
    cb = callbacks.build_attachment_pdf_gate_callback("ar_intake")

    def _resolve(log_id, fn):
        if fn == "cgv.pdf":
            raise ValueError("path introuvable pour la CGV")
        return Path(f"/x/{fn}")

    def _extract(path):
        return {"text": "AR TEXT CF1", "has_text_layer": True,
                "char_count": 11, "total_pages": 1}

    with patch.object(callbacks, "resolve_attachment_path", side_effect=_resolve), \
         patch.object(callbacks, "extract_first_page_text", side_effect=_extract):
        out = cb(_ctx(state))

    assert out is None
    assert "intake_pdf_text" in state
    assert "AR TEXT CF1" in state["intake_pdf_text"]
    assert "cgv.pdf" not in state["intake_pdf_text"]
    assert "ar_intake" not in state  # pas de not_ar


def test_extract_failure_on_one_pdf_keeps_other():
    from th2customers.scei import gates as callbacks
    state = {
        "attachments": [_pdf_att("ar.pdf"), _pdf_att("cgv.pdf")],
        "webhook_log_id": 3,
    }
    cb = callbacks.build_attachment_pdf_gate_callback("ar_intake")

    def _extract(path):
        if path.endswith("cgv.pdf"):
            raise RuntimeError("extraction boom")
        return {"text": "AR OK", "has_text_layer": True,
                "char_count": 5, "total_pages": 1}

    with patch.object(callbacks, "resolve_attachment_path",
                      side_effect=lambda i, fn: Path(f"/x/{fn}")), \
         patch.object(callbacks, "extract_first_page_text", side_effect=_extract):
        out = cb(_ctx(state))

    assert out is None
    assert state["intake_pdf_text"].count("=== PDF:") == 1
    assert "AR OK" in state["intake_pdf_text"]


# === CORRECTION B ===
def test_intake_pdf_text_capped_per_pdf():
    from th2customers.scei import gates as callbacks
    cap = callbacks._INTAKE_PDF_TEXT_PER_PDF_CAP
    big = "A" * (cap + 5000)
    state = {"attachments": [_pdf_att("ar.pdf")], "webhook_log_id": 1}
    cb = callbacks.build_attachment_pdf_gate_callback("ar_intake")
    with patch.object(callbacks, "resolve_attachment_path",
                      return_value=Path("/x/ar.pdf")), \
         patch.object(callbacks, "extract_first_page_text",
                      return_value={"text": big, "has_text_layer": True,
                                    "char_count": len(big), "total_pages": 1}):
        cb(_ctx(state))
    txt = state["intake_pdf_text"]
    a_run = txt.count("A")
    assert a_run <= cap
    assert "tronqu" in txt  # marqueur de troncature


def test_intake_pdf_text_total_capped():
    from th2customers.scei import gates as callbacks
    per = callbacks._INTAKE_PDF_TEXT_PER_PDF_CAP
    total = callbacks._INTAKE_PDF_TEXT_TOTAL_CAP
    atts = [_pdf_att(f"p{i}.pdf") for i in range(5)]
    state = {"attachments": atts, "webhook_log_id": 1}
    cb = callbacks.build_attachment_pdf_gate_callback("ar_intake")
    chunk = "B" * per
    with patch.object(callbacks, "resolve_attachment_path",
                      side_effect=lambda i, fn: Path(f"/x/{fn}")), \
         patch.object(callbacks, "extract_first_page_text",
                      return_value={"text": chunk, "has_text_layer": True,
                                    "char_count": per, "total_pages": 1}):
        cb(_ctx(state))
    txt = state["intake_pdf_text"]
    assert len(txt) <= total + 40  # total cap + marge marqueur
    assert "tronqu" in txt
