"""OCR tools — extract text (markdown) from documents and images.

Providers (in priority order):
1. Mistral Document OCR (``mistral-ocr-latest``) — uses ``MISTRAL_API_KEY``
2. Azure AI Document Intelligence (``prebuilt-read``) — uses
   ``AZURE_DOC_INTELLIGENCE_KEY`` + ``AZURE_DOC_INTELLIGENCE_ENDPOINT``

The tool sends a document or image to the provider and returns the extracted
text as markdown, one entry per page, plus a combined ``markdown`` string.
Mistral returns rich per-page markdown; Azure's read model returns recognised
text lines (no table/heading structure), exposed through the same fields.

Input handling
--------------
``source`` is either:

* a remote ``http(s)`` URL — passed straight to the provider, which fetches it.
  We never download the URL ourselves (that would be an SSRF vector from the
  worker), so only the provider's own fetcher sees it.
* a local file path — read from disk and sent inline as a base64 ``data:`` URL.
  This mirrors how ``audio.tool_speech_to_text`` accepts an uploaded file path.

Output is bounded: a huge scan can return megabytes of markdown (and, with
``include_images=True``, base64 image crops). The combined markdown is capped
and flagged with ``output_truncated`` so a large document cannot balloon the
host's memory or the LLM context — the same concern as the Python Script tool.

API contract (Mistral)
-----------------------
Verified against https://docs.mistral.ai/api/endpoint/ocr (2026-10-02):
``POST https://api.mistral.ai/v1/ocr``, header ``Authorization: Bearer <key>``,
body ``{"model": "mistral-ocr-latest", "document": {"type": "document_url"|
"image_url", ...}, "include_image_base64": bool}``; response
``{"pages": [{"index", "markdown", "images", "dimensions", ...}], "model",
"usage_info"}``. Inline base64 ``data:`` URLs for local files follow Mistral's
SDK convention; the documented examples show only remote URLs, so that path
should be confirmed against a live key before relying on it in production.
"""

from __future__ import annotations

import base64
import os
import time
from logging import getLogger
from pathlib import Path

logger = getLogger(__name__)

# API keys / endpoints used by this module — declared at module level so the
# ToolsStore parameter scanner (regex on os.getenv) can discover them for the UI.
_MISTRAL_KEY = os.getenv("MISTRAL_API_KEY", "")
_AZURE_KEY = os.getenv("AZURE_DOC_INTELLIGENCE_KEY", "")
_AZURE_ENDPOINT = os.getenv("AZURE_DOC_INTELLIGENCE_ENDPOINT", "")

# Mistral's public OCR endpoint. Deliberately NOT MISTRAL_API_BASE: in this repo
# that variable points at an OVH OpenAI-compat chat endpoint with no OCR route.
_MISTRAL_OCR_URL = "https://api.mistral.ai/v1/ocr"
_MISTRAL_OCR_MODEL = "mistral-ocr-latest"

# Azure AI Document Intelligence (v4.0 / api-version 2024-11-30). The endpoint is
# per-resource, so it is a user-supplied parameter, not a constant. "prebuilt-read"
# is the OCR (text-extraction) model. Analyze is async: POST returns 202 with an
# Operation-Location header that we poll until the result is ready.
_AZURE_API_VERSION = "2024-11-30"
_AZURE_MODEL = "prebuilt-read"
_AZURE_POLL_INTERVAL_S = 1.5
_AZURE_POLL_TIMEOUT_S = 120

# Combined-markdown cap (characters). Keeps a 1000-page scan from blowing up the
# host / LLM context; the per-page list is still returned, each page capped too.
_MAX_MARKDOWN_CHARS = 1_000_000
_MAX_PAGE_MARKDOWN_CHARS = 200_000

# Extension → (mime, chunk type). "image_url" for raster images, "document_url"
# for everything the provider treats as a document.
_DOC_TYPES: dict[str, tuple[str, str]] = {
    ".pdf": ("application/pdf", "document_url"),
    ".pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "document_url",
    ),
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "document_url",
    ),
    ".png": ("image/png", "image_url"),
    ".jpg": ("image/jpeg", "image_url"),
    ".jpeg": ("image/jpeg", "image_url"),
    ".avif": ("image/avif", "image_url"),
}

_SUPPORTED_EXTS = ", ".join(sorted(_DOC_TYPES))


def _is_remote(source: str) -> bool:
    """True if ``source`` is an http(s) URL (vs a local file path)."""
    return source.strip().lower().startswith(("http://", "https://"))


def _read_supported_file(source: str) -> tuple[str, str, bytes]:
    """Read a local file, validating it exists and is a supported type.

    Returns ``(mime, chunk_type, raw_bytes)``. Raises ValueError for a
    missing or unsupported file so the caller can report it as a fatal error.
    """
    if not os.path.isfile(source):
        raise ValueError(
            f"File not found: {source}. Provide a readable file path or an "
            f"http(s) URL."
        )
    ext = Path(source).suffix.lower()
    if ext not in _DOC_TYPES:
        raise ValueError(
            f"Unsupported file type '{ext}'. Supported: {_SUPPORTED_EXTS}."
        )
    mime, chunk_type = _DOC_TYPES[ext]
    with open(source, "rb") as fh:
        return mime, chunk_type, fh.read()


def _build_document_chunk(source: str) -> dict:
    """Turn ``source`` (remote URL or local file path) into a Mistral chunk.

    Remote URLs are passed through untouched — the provider fetches them, we do
    not. Local files are read and inlined as a base64 ``data:`` URL. Raises
    ValueError for a missing/unsupported local file so the caller can report it.
    """
    if _is_remote(source):
        # Pick the chunk type from the URL's extension; default to document_url
        # (works for PDFs and most documents). We never fetch the URL ourselves.
        ext = Path(source.split("?", 1)[0]).suffix.lower()
        _mime, chunk_type = _DOC_TYPES.get(ext, ("", "document_url"))
        return {"type": chunk_type, chunk_type: source}

    mime, chunk_type, data = _read_supported_file(source)
    b64 = base64.b64encode(data).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"
    return {"type": chunk_type, chunk_type: data_url}


def _ocr_mistral(source: str, include_images: bool) -> dict:
    """Run Mistral Document OCR on ``source``. Returns the raw parsed response.

    Raises EnvironmentError if the key is missing, ValueError for a bad source,
    and propagates httpx errors (handled by the caller).
    """
    import httpx

    api_key = os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise EnvironmentError("MISTRAL_API_KEY not set")

    document = _build_document_chunk(source)
    resp = httpx.post(
        _MISTRAL_OCR_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": _MISTRAL_OCR_MODEL,
            "document": document,
            "include_image_base64": bool(include_images),
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()


def _ocr_azure(source: str, include_images: bool) -> dict:
    """Run Azure AI Document Intelligence (prebuilt-read) on ``source``.

    Azure's analyze is asynchronous: POST returns 202 with an
    ``Operation-Location`` header, which we poll until the result is ready.
    Returns a normalised ``{"pages": [...], "model": ...}`` dict (same shape
    the combined-markdown collector expects). ``include_images`` is accepted for
    signature parity but ignored — the read model returns text, not image crops.

    Raises EnvironmentError (missing key/endpoint), ValueError (bad source),
    TimeoutError (analysis did not finish) or httpx errors (handled by caller).
    """
    import httpx

    del include_images  # read model returns no image crops

    api_key = os.environ.get("AZURE_DOC_INTELLIGENCE_KEY")
    endpoint = (os.environ.get("AZURE_DOC_INTELLIGENCE_ENDPOINT") or "").rstrip("/")
    if not api_key:
        raise EnvironmentError("AZURE_DOC_INTELLIGENCE_KEY not set")
    if not endpoint:
        raise EnvironmentError("AZURE_DOC_INTELLIGENCE_ENDPOINT not set")

    if _is_remote(source):
        body = {"urlSource": source}
    else:
        _mime, _chunk_type, data = _read_supported_file(source)
        body = {"base64Source": base64.b64encode(data).decode("ascii")}

    headers = {
        "Ocp-Apim-Subscription-Key": api_key,
        "Content-Type": "application/json",
    }
    analyze_url = (
        f"{endpoint}/documentintelligence/documentModels/{_AZURE_MODEL}:analyze"
        f"?api-version={_AZURE_API_VERSION}"
    )
    resp = httpx.post(analyze_url, headers=headers, json=body, timeout=60)
    resp.raise_for_status()
    op_location = resp.headers.get("Operation-Location") or resp.headers.get(
        "operation-location"
    )
    if not op_location:
        raise RuntimeError("Azure did not return an Operation-Location to poll")

    # Poll the operation until it succeeds or fails (bounded).
    poll_headers = {"Ocp-Apim-Subscription-Key": api_key}
    deadline = time.monotonic() + _AZURE_POLL_TIMEOUT_S
    result: dict = {}
    while True:
        poll = httpx.get(op_location, headers=poll_headers, timeout=60)
        poll.raise_for_status()
        result = poll.json()
        status = (result.get("status") or "").lower()
        if status == "succeeded":
            break
        if status == "failed":
            err = result.get("error", {})
            raise RuntimeError(
                f"Azure analysis failed: {err.get('message', 'unknown error')}"
            )
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Azure analysis did not finish within {_AZURE_POLL_TIMEOUT_S}s"
            )
        time.sleep(_AZURE_POLL_INTERVAL_S)

    analyze_result = result.get("analyzeResult", {})
    pages = []
    for i, page in enumerate(analyze_result.get("pages", []) or []):
        lines = page.get("lines", []) or []
        text = "\n".join(line.get("content", "") for line in lines)
        pages.append(
            {
                "index": page.get("pageNumber", i + 1) - 1,
                "markdown": text,
                "images": [],
            }
        )
    return {"pages": pages, "model": analyze_result.get("modelId", _AZURE_MODEL)}


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

_PROVIDERS = {
    "mistral": ("Mistral Document OCR", _ocr_mistral, "MISTRAL_API_KEY"),
    "azure": ("Azure Document Intelligence", _ocr_azure, "AZURE_DOC_INTELLIGENCE_KEY"),
}

_PROVIDER_ORDER = ["mistral", "azure"]


def _collect_pages(raw: dict, include_images: bool) -> tuple[list[dict], str, bool]:
    """Normalise a provider response into (pages, combined_markdown, truncated).

    Each page is ``{"index", "markdown", "image_count"}`` (plus ``images`` when
    requested). Both the per-page markdown and the combined string are capped.
    """
    pages_out: list[dict] = []
    combined_parts: list[str] = []
    truncated = False

    for page in raw.get("pages", []) or []:
        md = page.get("markdown") or ""
        if len(md) > _MAX_PAGE_MARKDOWN_CHARS:
            md = md[:_MAX_PAGE_MARKDOWN_CHARS] + "\n...[page truncated]"
            truncated = True
        images = page.get("images") or []
        entry = {
            "index": page.get("index"),
            "markdown": md,
            "image_count": len(images),
        }
        if include_images:
            entry["images"] = images
        pages_out.append(entry)
        combined_parts.append(md)

    combined = "\n\n".join(combined_parts)
    if len(combined) > _MAX_MARKDOWN_CHARS:
        combined = combined[:_MAX_MARKDOWN_CHARS] + "\n...[output truncated]"
        truncated = True
    return pages_out, combined, truncated


def tool_ocr_document(
    source: str,
    provider: str = "auto",
    include_images: bool = False,
) -> dict:
    """Extract text from a document or image using OCR.

    Reads a PDF, Office document (PPTX/DOCX) or image and returns the recognised
    text as markdown, one entry per page plus a combined string.

    Args:
        source (str): What to OCR. Either an ``http(s)`` URL (the provider fetches
            it) or a path to a local file. Supported file types: PDF, PPTX, DOCX,
            PNG, JPG/JPEG, AVIF.
        provider (str): Which OCR provider to use. Options: "auto" (tries available
            providers in order), "mistral" or "azure". Default: "auto".
        include_images (bool): When true, include base64-encoded image crops that
            the provider extracted from each page. Off by default to keep the
            response small. Default: False.

    Returns:
        dict: On success, ``status`` "success", ``markdown`` (combined text),
        ``pages`` (list of ``{index, markdown, image_count}``), ``page_count``,
        ``provider_used``, ``model``, ``output_truncated``. On failure, ``status``
        "error" with ``error_message`` (and the per-provider errors tried).
    """
    import httpx

    if not isinstance(source, str) or not source.strip():
        return {
            "status": "error",
            "error_message": "`source` must be a non-empty URL or file path.",
        }

    if provider == "auto":
        order = _PROVIDER_ORDER
    elif provider in _PROVIDERS:
        order = [provider]
    else:
        return {
            "status": "error",
            "error_message": (
                f"Unknown provider '{provider}'. "
                f"Valid: auto, {', '.join(_PROVIDERS)}"
            ),
        }

    errors: list[str] = []
    for prov_key in order:
        prov_name, ocr_fn, env_key = _PROVIDERS[prov_key]

        if not os.environ.get(env_key):
            errors.append(f"{prov_name}: {env_key} not set")
            continue

        try:
            logger.info("[OCR] Running %s on %.80s", prov_name, source)
            raw = ocr_fn(source, include_images)
        except ValueError as exc:
            # Bad input (missing/unsupported file) — fatal, not a provider retry.
            return {"status": "error", "error_message": str(exc)}
        except httpx.HTTPStatusError as exc:
            resp = exc.response
            status = resp.status_code if resp is not None else "?"
            detail = resp.text[:500] if resp is not None else ""
            logger.warning("[OCR] %s HTTP error: %s", prov_name, exc)
            errors.append(f"{prov_name}: HTTP {status} {detail}")
            continue
        except Exception as exc:
            logger.warning("[OCR] %s failed: %s", prov_name, exc)
            errors.append(f"{prov_name}: {exc}")
            continue

        pages, combined, truncated = _collect_pages(raw, include_images)
        logger.info("[OCR] %s returned %d page(s)", prov_name, len(pages))
        return {
            "status": "success",
            "markdown": combined,
            "pages": pages,
            "page_count": len(pages),
            "provider_used": prov_name,
            "model": raw.get("model"),
            "output_truncated": truncated,
        }

    return {
        "status": "error",
        "error_message": (
            "OCR failed with all providers.\n"
            + "\n".join(f"  - {err}" for err in errors)
            + "\n\nMake sure at least one provider is configured: MISTRAL_API_KEY, "
            "or AZURE_DOC_INTELLIGENCE_KEY + AZURE_DOC_INTELLIGENCE_ENDPOINT."
        ),
    }
