"""Agent-bound ``pdf_to_images`` tool factory.

Renders each page of an uploaded PDF to a base64-encoded PNG so the LLM
can "see" the document directly (vision-capable models like Claude
Sonnet 4.5, GPT-4o, Gemini 2.5). Works on both text PDFs and scanned
PDFs — no separate OCR step needed.
"""
from __future__ import annotations

import base64
import importlib.util
import io
import os

from apowerb.configs.th2logger import setup_logging
from apowerb.configs.paths import uploads_dir


logger = setup_logging(__name__)


def _resolve_uploaded(folder_name: str, filename: str) -> tuple[str | None, list[str]]:
    """Find an uploaded file, looking where the WRITERS actually write.

    A reader closure is bound to the agent that DECLARES the tool -- in a
    sequential pipeline, a sub-agent. Every writer, though, resolves
    ``uploads/agent{ROOT_AGENT_ID}``: the agent the run was triggered on.
    ``tool_download_attachment`` says so in its own comment and warns that
    otherwise "downloaded attachments are invisible to subsequent tool calls".

    They were. The intake downloaded an acknowledgment-of-receipt, then
    reported an empty uploads dir for a file sitting one directory away; the
    pipeline carried on and published a verdict on a document nobody had read,
    returning 200 the whole way.

    Returns ``(path or None, folders searched)``. The bound folder wins when
    both hold the name, so nothing that worked before changes.
    """
    candidates = [folder_name]
    root = os.getenv("ROOT_AGENT_ID", "")
    if root and f"agent{root}" != folder_name:
        candidates.append(f"agent{root}")
    for cand in candidates:
        path = str(uploads_dir() / cand / filename)
        if os.path.exists(path):
            return path, candidates
    return None, candidates


def _uploaded_not_found(filename: str, candidates: list[str]) -> dict:
    """Error payload naming every folder searched, not just the first."""
    # Prefix with the folder only when more than one was searched: that is
    # when the reader needs to know WHERE a name was seen. With a single
    # folder the answer is unambiguous, and bare names keep the long-standing
    # contract callers already rely on.
    many = len(candidates) > 1
    available: list[str] = []
    for cand in candidates:
        directory = str(uploads_dir() / cand)
        if os.path.exists(directory):
            available += [
                f"{cand}/{f}" if many else f
                for f in os.listdir(directory)
                if os.path.isfile(os.path.join(directory, f))
            ]
    return {
        "status": "error",
        "message": f"File '{filename}' not found in " + " or ".join(candidates),
        "available_files": available,
    }


_MAX_PDF_BYTES = 25_000_000


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
    positioned rows from word coordinates (SCEI coord-based AR line
    extraction, ``scei_coord_rows.reconstruct_rows``) rather than from a flat
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
        """Render each page of a PDF to a base64 PNG for vision-LLM analysis.

        Use this when the LLM needs to "see" a document — typical for
        scanned PDFs, supplier acknowledgements with complex tables /
        layouts, or any document where text extraction returned little
        or no content. Works on both text PDFs and scanned PDFs.

        The defaults are tuned for cost: ``max_pages=5`` covers the
        large majority of single-AR documents, ``dpi=120`` is enough
        for OCR-quality vision, and ``max_dimension=1024`` keeps each
        page below ~150k input tokens on Gemini Flash. Pass larger
        values explicitly when a specific document genuinely needs
        them — but understand that doubling ``max_dimension`` roughly
        quadruples the token footprint.

        Args:
            filename: Name of the PDF in the agent's uploads dir (no path).
            max_pages: Hard cap on rendered pages (default 5).
            dpi: Render DPI (default 120).
            max_dimension: Resize so longest side ≤ this many pixels (default 1024).

        Returns:
            dict: status, page_count, total_pages_in_pdf, pages (list of
            {page_number, mime_type, data: base64 PNG}).
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

        file_path, _searched = _resolve_uploaded(folder_name, filename)
        if file_path is None:
            return _uploaded_not_found(filename, _searched)

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
                        "mime_type": "image/jpeg",
                        "data": base64.b64encode(buf.getvalue()).decode("ascii"),
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

    pdf_to_images.__name__ = "tool_pdf_to_images"
    return pdf_to_images


def _make_pdf_first_page(folder_name: str):
    """Return a ``pdf_first_page`` tool with the agent folder pre-bound.

    Extracts ONLY the first page of an uploaded PDF as a NATIVE single-page
    PDF (no image rendering, no conversion) so a multimodal LLM reads the
    document directly. Built for the SCEI intake redesign: an AR's first
    page carries the order number, supplier and key fields, and native PDF
    preserves text + layout fidelity (avoids the PNG/JPEG render bugs).
    """

    def pdf_first_page(filename: str) -> dict:
        """Extract the TEXT of the first page of an uploaded PDF.

        Use this to read an acknowledgment-of-receipt (AR) document: the
        first page holds the order number, supplier and lines. We return
        the page's native text layer (no image conversion) — the SCEI AR
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

        file_path, _searched = _resolve_uploaded(folder_name, filename)
        if file_path is None:
            return _uploaded_not_found(filename, _searched)

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
