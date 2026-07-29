"""Tests for extract_all_pages_words — word-level coordinate extraction across
ALL pages of a PDF (SCEI coord-based line reconstruction, _load_pdf_rows).

The scei overlay's ``_load_pdf_rows`` imports this from the core and feeds each
returned word into ``reconstruct_rows``. The contract it relies on:

  * success -> {"words": [ {"page": int, "x0": float, "y0": float,
                            "x1": float, "y1": float, "text": str}, ... ],
                "total_pages": int}
  * guarded failure -> {"error": <str>, "words": []}

``reconstruct_rows`` reads ``w["page"]`` (grouping) and, per word,
``w.get("x0", w.get("x"))`` / ``w.get("y0", w.get("y"))`` /
``w.get("text", w.get("word", ""))``. These keys are the load-bearing part of
the contract and are asserted below.
"""

from __future__ import annotations

import os

import pytest

fitz = pytest.importorskip("fitz")  # pymupdf; real extraction tests


@pytest.fixture
def two_page_words_pdf(tmp_path):
    """Two-page PDF with known words at known coordinates on each page."""
    doc = fitz.open()
    doc.new_page()
    doc.new_page()
    # Distinct, single-token strings so get_text("words") yields clean words.
    doc[0].insert_text((72, 100), "ALPHA")
    doc[0].insert_text((200, 100), "BETA")
    doc[1].insert_text((72, 100), "GAMMA")
    path = os.path.join(str(tmp_path), "words.pdf")
    doc.save(path)
    doc.close()
    return path


class TestExtractAllPagesWords:
    def test_returns_words_from_all_pages_with_page_index(self, two_page_words_pdf):
        from th2agent.core.agent_helpers.pdf_to_images_tool import (
            extract_all_pages_words,
        )

        res = extract_all_pages_words(two_page_words_pdf)

        assert "error" not in res, res
        assert res["total_pages"] == 2
        words = res["words"]
        assert isinstance(words, list) and words

        texts_by_page = {}
        for w in words:
            texts_by_page.setdefault(w["page"], []).append(w["text"])

        # Page indices are 0-based (what _load_pdf_rows groups on).
        assert set(texts_by_page) == {0, 1}
        assert "ALPHA" in texts_by_page[0]
        assert "BETA" in texts_by_page[0]
        assert "GAMMA" in texts_by_page[1]
        # No cross-page leak.
        assert "GAMMA" not in texts_by_page[0]
        assert "ALPHA" not in texts_by_page[1]

    def test_word_dict_matches_reconstruct_rows_contract(self, two_page_words_pdf):
        """Every word must carry the exact keys reconstruct_rows reads."""
        from th2agent.core.agent_helpers.pdf_to_images_tool import (
            extract_all_pages_words,
        )

        words = extract_all_pages_words(two_page_words_pdf)["words"]
        for w in words:
            assert set(("page", "x0", "y0", "x1", "y1", "text")) <= set(w)
            assert isinstance(w["page"], int)
            assert isinstance(w["x0"], float)
            assert isinstance(w["y0"], float)
            assert isinstance(w["text"], str)
        # Coordinates are sane: BETA (x=200) sits right of ALPHA (x=72).
        p0 = {w["text"]: w for w in words if w["page"] == 0}
        assert p0["BETA"]["x0"] > p0["ALPHA"]["x0"]

    def test_missing_pymupdf_returns_error(self, monkeypatch, two_page_words_pdf):
        import th2agent.core.agent_helpers.pdf_to_images_tool as mod

        real_import = __import__

        def fake_import(name, *args, **kwargs):
            if name == "fitz":
                raise ImportError("simulated: no pymupdf")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr("builtins.__import__", fake_import)
        res = mod.extract_all_pages_words(two_page_words_pdf)
        assert res["words"] == []
        assert "no_pymupdf" in res["error"]

    def test_corrupt_pdf_returns_error_not_raise(self, tmp_path):
        from th2agent.core.agent_helpers.pdf_to_images_tool import (
            extract_all_pages_words,
        )

        bad = os.path.join(str(tmp_path), "broken.pdf")
        with open(bad, "wb") as f:
            f.write(b"this is not a real pdf")
        res = extract_all_pages_words(bad)
        assert res["words"] == []
        assert "error" in res
