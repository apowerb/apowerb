"""TÂCHE 1 — TDD: fonction pure extract_first_page_text.

Extrait une fonction de module qui ouvre un PDF avec fitz et retourne
{text, has_text_layer, char_count, total_pages}. Garde-fous: fichier trop
gros -> too_large ; absent/corrompu -> error ; jamais d exception propagee.
La closure pdf_first_page DOIT appeler cette fonction (zero regression du
tool_pdf_first_page existant).
"""
from __future__ import annotations


import fitz


def _make_pdf(path: str, text: str = "HELLO CF101082") -> None:
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), text)
    doc.save(path)
    doc.close()


def test_extract_text_pdf_with_text_layer(tmp_path):
    from th2agent.core.agent_helpers.pdf_to_images_tool import (
        extract_first_page_text,
    )
    p = str(tmp_path / "ar.pdf")
    _make_pdf(p, "HELLO CF101082")
    out = extract_first_page_text(p)
    assert out["has_text_layer"] is True
    assert "CF101082" in out["text"]
    assert out["char_count"] == len(out["text"])
    assert out["total_pages"] == 1
    assert "error" not in out


def test_extract_too_large(tmp_path, monkeypatch):
    from th2agent.core.agent_helpers import pdf_to_images_tool as mod
    p = str(tmp_path / "big.pdf")
    _make_pdf(p)
    monkeypatch.setattr(mod.os.path, "getsize", lambda _p: 25_000_001)
    out = mod.extract_first_page_text(p)
    assert out == {
        "text": "",
        "has_text_layer": False,
        "error": "too_large",
    }


def test_extract_missing_file_returns_error_no_exception():
    from th2agent.core.agent_helpers.pdf_to_images_tool import (
        extract_first_page_text,
    )
    out = extract_first_page_text("/nonexistent/path/nope.pdf")
    assert out["has_text_layer"] is False
    assert out["text"] == ""
    assert "error" in out


def test_extract_corrupt_file_returns_error_no_exception(tmp_path):
    from th2agent.core.agent_helpers.pdf_to_images_tool import (
        extract_first_page_text,
    )
    p = tmp_path / "corrupt.pdf"
    p.write_bytes(b"not a real pdf at all")
    out = extract_first_page_text(str(p))
    assert out["has_text_layer"] is False
    assert out["text"] == ""
    assert "error" in out


def test_pdf_first_page_tool_still_works(tmp_path, monkeypatch):
    """Non-regression: la closure tool_pdf_first_page reste fonctionnelle
    et renvoie le contrat existant (status, text, has_text_layer, ...)."""
    from th2agent.core.agent_helpers.pdf_to_images_tool import (
        _make_pdf_first_page,
    )
    folder = "agentX"
    updir = tmp_path / "uploads" / folder
    updir.mkdir(parents=True)
    _make_pdf(str(updir / "ar.pdf"), "FIRST PAGE CF999999")
    monkeypatch.chdir(tmp_path)
    tool = _make_pdf_first_page(folder)
    res = tool("ar.pdf")
    assert res["status"] == "success"
    assert "CF999999" in res["text"]
    assert res["has_text_layer"] is True
    assert res["total_pages_in_pdf"] == 1
