"""PDF writing helpers using fpdf2."""
from __future__ import annotations

import os

from apowerb.core.agent_helpers.text_utils import _md_to_html


def _setup_pdf_font(pdf) -> str:
    """Register a Unicode-capable font on the PDF, return its family name."""
    # Try common system font paths (Windows, Linux, macOS)
    candidates = [
        (
            "Arial",
            {
                "": r"C:\Windows\Fonts\arial.ttf",
                "B": r"C:\Windows\Fonts\arialbd.ttf",
                "I": r"C:\Windows\Fonts\ariali.ttf",
            },
        ),
        (
            "DejaVuSans",
            {
                "": "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "B": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "I": "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
            },
        ),
    ]
    for family, styles in candidates:
        if all(os.path.exists(p) for p in styles.values()):
            for style, path in styles.items():
                pdf.add_font(family, style, path)
            return family
    # Fallback: core font (no full Unicode, but won't crash)
    return "Helvetica"


def _write_pdf(file_path: str, content: str) -> None:
    """Generate a PDF file from markdown-like text content using fpdf2."""
    from fpdf import FPDF

    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()
    font = _setup_pdf_font(pdf)
    pdf.set_font(font, size=11)
    pdf.write_html(_md_to_html(content))
    pdf.output(file_path)


def _extract_pdf_text(file_path: str) -> str | None:
    """Extract text from a PDF file. Returns None if extraction fails."""
    try:
        from PyPDF2 import PdfReader

        reader = PdfReader(file_path)
        pages = []
        for i, page in enumerate(reader.pages):
            text = page.extract_text() or ""
            if text.strip():
                pages.append(f"--- Page {i + 1} ---\n{text.strip()}")
        if pages:
            return "\n\n".join(pages)
        return None
    except Exception:
        return None
