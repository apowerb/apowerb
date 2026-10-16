"""Agent-bound ``pdf_to_images`` tool factory.

Renders the first pages of an uploaded PDF to JPEG images that reach the
model as images (see ``apowerb.core.tool_images``), so a vision model can
"see" the document. Works on both text PDFs and scanned PDFs.
"""
from __future__ import annotations

import importlib.util
import io
import os

from apowerb.configs.th2logger import setup_logging
from apowerb.artifacts.agent_uploads import agent_upload_path
from apowerb.core.tool_images import attach_image


logger = setup_logging(__name__)

_MAX_PDF_BYTES = 25_000_000
_MAX_PAGES = 10  # rendered pages per call


def extract_first_page_text(path: str) -> dict:
    """Extract the first-page text of a PDF at ``path``. Pure, no agent
    binding. Never raises: any failure yields a dict with an ``error`` key.

    Returns on success::

        {"text": str, "has_text_layer": bool,
         "char_count": int, "total_pages": int}

    On a guarded failure::

        {"text": "", "has_text_layer": False, "error": <str>}
    """
    try:
        import fitz  # pymupdf
    except ImportError as exc:
        return {"text": "", "has_text_layer": False, "error": f"no_pymupdf: {exc}"}

    try:
        if os.path.getsize(path) >= _MAX_PDF_BYTES:
            return {"text": "", "has_text_layer": False, "error": "too_large"}
    except OSError as exc:
        return {"text": "", "has_text_layer": False, "error": f"stat_failed: {exc}"}

    try:
        doc = fitz.open(path)
        try:
            if doc.page_count == 0:
                return {"text": "", "has_text_layer": False, "error": "no_pages"}
            text = doc[0].get_text()
            return {
                "text": text,
                "has_text_layer": bool(text.strip()),
                "char_count": len(text),
                "total_pages": doc.page_count,
            }
        finally:
            doc.close()
    except Exception as exc:  # noqa: BLE001 -- never propagate, gate must not crash
        return {"text": "", "has_text_layer": False, "error": str(exc)}


def extract_all_pages_text(path: str) -> dict:
    """Extract the concatenated text of ALL pages of a PDF at ``path``. Pure,
    no agent binding. Never raises: any failure yields a dict with an ``error``
    key.

    Returns on success::

        {"text": str, "has_text_layer": bool,
         "char_count": int, "total_pages": int}

    On a guarded failure::

        {"text": "", "has_text_layer": False, "error": <str>}

    Use this instead of ``extract_first_page_text`` when the content of
    interest (e.g. AR lines in Schneider/SAP PDFs) may start on page 2+.
    """
    try:
        import fitz  # pymupdf
    except ImportError as exc:
        return {"text": "", "has_text_layer": False, "error": f"no_pymupdf: {exc}"}

    try:
        if os.path.getsize(path) >= _MAX_PDF_BYTES:
            return {"text": "", "has_text_layer": False, "error": "too_large"}
    except OSError as exc:
        return {"text": "", "has_text_layer": False, "error": f"stat_failed: {exc}"}

    try:
        doc = fitz.open(path)
        try:
            if doc.page_count == 0:
                return {"text": "", "has_text_layer": False, "error": "no_pages"}
            text = "".join(page.get_text() for page in doc)
            return {
                "text": text,
                "has_text_layer": bool(text.strip()),
                "char_count": len(text),
                "total_pages": doc.page_count,
            }
        finally:
            doc.close()
    except Exception as exc:  # noqa: BLE001 -- never propagate, gate must not crash
        return {"text": "", "has_text_layer": False, "error": str(exc)}


def extract_all_pages_words(path: str) -> dict:
    """Extract every word of ALL pages of a PDF at ``path`` with its page-local
    bounding box. Pure, no agent binding. Never raises: any failure yields a
    dict with an ``error`` key and an empty ``words`` list.

    Returns on success::

        {"words": [{"page": int, "x0": float, "y0": float,
                    "x1": float, "y1": float, "text": str}, ...],
         "total_pages": int}

    On a guarded failure::

        {"words": [], "error": <str>}

    ``page`` is 0-based. This is the coordinate-aware sibling of
    ``extract_all_pages_text``: use it when downstream logic reconstructs
    positioned rows from word coordinates (an overlay's coordinate-based line
    extraction, the overlay's coordinate extractor) rather than from a flat
    text layer.
    """
    try:
        import fitz  # pymupdf
    except ImportError as exc:
        return {"words": [], "error": f"no_pymupdf: {exc}"}

    try:
        if os.path.getsize(path) >= _MAX_PDF_BYTES:
            return {"words": [], "error": "too_large"}
    except OSError as exc:
        return {"words": [], "error": f"stat_failed: {exc}"}

    try:
        doc = fitz.open(path)
        try:
            if doc.page_count == 0:
                return {"words": [], "error": "no_pages"}
            words: list = []
            for page_index, page in enumerate(doc):
                # get_text("words") -> list of tuples
                # (x0, y0, x1, y1, "word", block_no, line_no, word_no)
                for x0, y0, x1, y1, word, *_rest in page.get_text("words"):
                    words.append(
                        {
                            "page": page_index,
                            "x0": float(x0),
                            "y0": float(y0),
                            "x1": float(x1),
                            "y1": float(y1),
                            "text": str(word),
                        }
                    )
            return {"words": words, "total_pages": doc.page_count}
        finally:
            doc.close()
    except Exception as exc:  # noqa: BLE001 -- never propagate, gate must not crash
        return {"words": [], "error": str(exc)}


def _make_pdf_to_images(folder_name: str):
    """Return a ``pdf_to_images`` tool with the agent folder pre-bound."""

    def pdf_to_images(
        filename: str,
        max_pages: int = 2,
        dpi: int = 84,
        max_dimension: int = 768,
    ) -> dict:
        """Render the first pages of an uploaded PDF as images you can look at.

        Use it when you need to see a document: scans, complex tables or
        layouts, or when reading its text returned little or nothing. For a
        PDF with a text layer, reading the file is cheaper. Each page is
        attached to this result as an image.

        Args:
            filename: Name of the PDF in the agent's uploads dir (no path).
            max_pages: Number of pages to render, from the first (default 2, at most 10).
            dpi: Render DPI (default 84).
            max_dimension: Longest side of each image in pixels (default 768).
                Raise it only when small print is unreadable.

        Returns:
            dict: status, page_count, total_pages_in_pdf, truncated, pages
            (list of {page_number, image_ref}).
        """
        try:
            import fitz  # pymupdf
        except ImportError as exc:
            return {
                "status": "error",
                "message": (
                    "pymupdf is not installed. Add 'pymupdf>=1.24' to "
                    f"pyproject.toml and redeploy. ({exc})"
                ),
            }

        try:
            from PIL import Image
        except ImportError as exc:
            return {
                "status": "error",
                "message": f"Pillow is not installed: {exc}",
            }

        with agent_upload_path(folder_name, filename) as (file_path, available):
            if file_path is None:
                return _not_found(filename, folder_name, available)
            try:
                pages = max(1, min(int(max_pages), _MAX_PAGES))
            except (TypeError, ValueError):
                pages = 2
            return _render_pages(
                fitz, Image, file_path, filename, pages, dpi, max_dimension,
            )

    pdf_to_images.__name__ = "tool_pdf_to_images"
    return pdf_to_images


def _not_found(filename: str, folder_name: str, available: list[str]) -> dict:
    return {
        "status": "error",
        "message": f"File '{filename}' not found in {folder_name}",
        "available_files": available,
    }


def _render_pages(fitz, Image, file_path, filename, max_pages, dpi, max_dimension) -> dict:
    """Render up to ``max_pages`` pages of the PDF at ``file_path``."""
    try:
        doc = fitz.open(file_path)
    except Exception as exc:
        return {
            "status": "error",
            "message": f"Could not open PDF '{filename}': {exc}",
        }

    try:
        zoom = max(0.5, min(4.0, dpi / 72.0))
        matrix = fitz.Matrix(zoom, zoom)
        pages: list[dict] = []
        total_pages = doc.page_count

        for i, page in enumerate(doc):
            if i >= max_pages:
                break
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

            # Resize if longest side > max_dimension (token efficiency).
            longest = max(img.width, img.height)
            if longest > max_dimension:
                ratio = max_dimension / longest
                new_size = (int(img.width * ratio), int(img.height * ratio))
                img = img.resize(new_size, Image.Resampling.LANCZOS)

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=85, optimize=True)
            pages.append(
                {
                    "page_number": i + 1,
                    "image_ref": attach_image(buf.getvalue(), "image/jpeg"),
                }
            )

        truncated = total_pages > len(pages)
        if truncated:
            logger.warning(
                "PDF '%s' truncated: %d page(s) ignored (kept %d of %d, max_pages=%d). "
                "Operator should validate that no critical AR data lives past page %d.",
                filename,
                total_pages - len(pages),
                len(pages),
                total_pages,
                max_pages,
                max_pages,
            )
        return {
            "status": "success",
            "page_count": len(pages),
            "total_pages_in_pdf": total_pages,
            "truncated": truncated,
            "pages": pages,
        }
    finally:
        doc.close()


def _make_pdf_first_page(folder_name: str):
    """Return a ``pdf_first_page`` tool with the agent folder pre-bound.

    Extracts ONLY the first page of an uploaded PDF as a NATIVE single-page
    PDF (no image rendering, no conversion) so a multimodal LLM reads the
    document directly. Built for a client overlay's intake redesign: an AR's first
    page carries the order number, supplier and key fields, and native PDF
    preserves text + layout fidelity (avoids the PNG/JPEG render bugs).
    """

    def pdf_first_page(filename: str) -> dict:
        """Extract the TEXT of the first page of an uploaded PDF.

        Use this to read an acknowledgment-of-receipt (AR) document: the
        first page holds the order number, supplier and lines. We return
        the page's native text layer (no image conversion) — that overlay's document
        corpus is 100% text-native. If a PDF were a pure scan, ``text`` is
        empty and ``has_text_layer`` is False, a signal the caller can act
        on explicitly.

        Args:
            filename: Name of the PDF in the agent's uploads dir (no path).

        Returns:
            dict: status, total_pages_in_pdf, text (first-page text),
            char_count, has_text_layer.
        """
        # Sanitize: attachment filenames are operator/sender-controlled.
        # Strip any path component so a crafted name (``../../etc/passwd``)
        # cannot escape the agent's uploads dir.
        filename = os.path.basename(filename or "")
        if not filename:
            return {"status": "error", "message": "Invalid filename"}

        if importlib.util.find_spec("fitz") is None:
            return {
                "status": "error",
                "message": (
                    "pymupdf is not installed. Add 'pymupdf>=1.24' to "
                    "pyproject.toml and redeploy."
                ),
            }

        with agent_upload_path(folder_name, filename) as (file_path, available):
            if file_path is None:
                return _not_found(filename, folder_name, available)
            extracted = extract_first_page_text(file_path)
        if "error" in extracted:
            return {
                "status": "error",
                "message": f"Could not read PDF '{filename}': {extracted['error']}",
            }
        return {
            "status": "success",
            "total_pages_in_pdf": extracted["total_pages"],
            "text": extracted["text"],
            "char_count": extracted["char_count"],
            "has_text_layer": extracted["has_text_layer"],
        }

    pdf_first_page.__name__ = "tool_pdf_first_page"
    return pdf_first_page
