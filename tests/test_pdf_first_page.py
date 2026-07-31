"""Tests for tool_pdf_first_page — first-page TEXT extraction
(SCEI intake redesign, Option A). No image conversion: the tool returns
the page's native text layer (the SCEI AR corpus is 100% text-native)."""

from __future__ import annotations

import os

import pytest

fitz = pytest.importorskip("fitz")  # pymupdf; real extraction tests


@pytest.fixture
def two_page_pdf():
    """Create uploads/<folder>/sample.pdf (relative to the repo cwd, where
    the tool resolves 'uploads/...'). No chdir — chdir breaks settings/.env
    loading. Unique folder + cleanup keeps the repo tree clean."""
    import shutil

    folder = "agent_test_pdf_first_page"
    updir = os.path.join("uploads", folder)
    os.makedirs(updir, exist_ok=True)
    doc = fitz.open()
    doc.new_page()
    doc.new_page()
    doc[0].insert_text((72, 72), "PAGE ONE AR CF101011 SUPPLIER AMS")
    doc[1].insert_text((72, 72), "PAGE TWO terms and conditions")
    doc.save(os.path.join(updir, "sample.pdf"))
    doc.close()
    try:
        yield folder
    finally:
        shutil.rmtree(updir, ignore_errors=True)


class TestPdfFirstPage:
    def test_extracts_first_page_text(self, two_page_pdf):
        from apowerb.core.agent_helpers.pdf_to_images_tool import (
            _make_pdf_first_page,
        )

        tool = _make_pdf_first_page(two_page_pdf)
        res = tool("sample.pdf")

        assert res["status"] == "success"
        assert res["total_pages_in_pdf"] == 2
        assert res["has_text_layer"] is True
        assert res["char_count"] > 0
        # first-page text only — page 2 must NOT leak
        assert "PAGE ONE" in res["text"]
        assert "CF101011" in res["text"]
        assert "PAGE TWO" not in res["text"]
        # no base64 payload in the response (Option A: text only)
        assert "data" not in res and "page" not in res

    def test_missing_file_returns_error(self, two_page_pdf):
        from apowerb.core.agent_helpers.pdf_to_images_tool import (
            _make_pdf_first_page,
        )

        res = _make_pdf_first_page(two_page_pdf)("nope.pdf")
        assert res["status"] == "error"
        assert "not found" in res["message"]
        assert "sample.pdf" in res["available_files"]

    def test_tool_name(self):
        from apowerb.core.agent_helpers.pdf_to_images_tool import (
            _make_pdf_first_page,
        )

        assert _make_pdf_first_page("x").__name__ == "tool_pdf_first_page"


class TestBindPdfFirstPage:
    def test_replaces_placeholder(self):
        from apowerb.core.agent_helpers.tools_binder import (
            bind_pdf_first_page,
        )
        from apowerb.tools_store.portfolio.basic import tool_pdf_first_page

        funcs = [tool_pdf_first_page]
        out = bind_pdf_first_page("agent12", funcs)
        assert out[0].__name__ == "tool_pdf_first_page"
        assert out[0] is not tool_pdf_first_page  # replaced by bound closure

    def test_noop_when_not_declared(self):
        """Replace-only: agents that don't declare the tool don't get it."""
        from apowerb.core.agent_helpers.tools_binder import (
            bind_pdf_first_page,
        )

        def other():
            return None

        funcs = [other]
        out = bind_pdf_first_page("agent12", funcs)
        assert out == [other]  # untouched, no auto-append


class TestPdfFirstPageRobustness:
    def test_corrupt_pdf_returns_error(self, two_page_pdf):
        from apowerb.core.agent_helpers.pdf_to_images_tool import (
            _make_pdf_first_page,
        )

        with open(os.path.join("uploads", two_page_pdf, "broken.pdf"), "wb") as f:
            f.write(b"this is not a real pdf")
        res = _make_pdf_first_page(two_page_pdf)("broken.pdf")
        assert res["status"] == "error"
        assert "Could not open" in res["message"]

    def test_path_traversal_filename_is_neutralised(self, two_page_pdf):
        """A crafted '../' filename must not escape the uploads dir."""
        from apowerb.core.agent_helpers.pdf_to_images_tool import (
            _make_pdf_first_page,
        )

        res = _make_pdf_first_page(two_page_pdf)("../../../../etc/passwd")
        # basename() reduces it to 'passwd', not found in the agent dir
        assert res["status"] == "error"
        assert "not found" in res["message"]
