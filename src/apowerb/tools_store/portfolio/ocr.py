"""OCR tools — extract text (markdown) from documents and images.

Providers (in priority order):
1. dots.ocr, self-hosted behind vLLM — uses ``DOTS_OCR_BASE_URL`` (optional
   ``DOTS_OCR_API_KEY``, ``DOTS_OCR_MODEL``). Tried first: documents stay on our
   own server. Local images and PDFs only (see ``_ocr_dots``).
2. Mistral Document OCR (``mistral-ocr-latest``) — uses ``MISTRAL_OCR_API_KEY``
   (its own variable: ``MISTRAL_API_KEY`` may hold a chat-gateway token)
3. Azure AI Document Intelligence (``prebuilt-read``) — uses
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
* the bare name of a file the user uploaded to the agent (``cv.pdf``, as shown
  in ``[Uploaded files: …]``). The agent never knows where uploads live, so the
  name is looked up in the root agent's upload store — S3 input artifacts or
  ``uploads/agent{id}/`` on disk — the same places ``read_uploaded_file`` reads.

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
import json
import os
import time
from contextvars import ContextVar
from logging import getLogger
from pathlib import Path

logger = getLogger(__name__)

# API keys / endpoints used by this module — declared at module level so the
# ToolsStore parameter scanner (regex on os.getenv) can discover them for the UI.
_MISTRAL_KEY = os.getenv("MISTRAL_OCR_API_KEY", "")
_AZURE_KEY = os.getenv("AZURE_DOC_INTELLIGENCE_KEY", "")
_AZURE_ENDPOINT = os.getenv("AZURE_DOC_INTELLIGENCE_ENDPOINT", "")
# dots.ocr: read for the scanner only, so not bound to a name.
os.getenv("DOTS_OCR_BASE_URL", "")
os.getenv("DOTS_OCR_API_KEY", "")
os.getenv("DOTS_OCR_MODEL", "")

# Secrets that are not a provider's "is it configured" variable but must still
# be redacted from errors and logs.
_EXTRA_SECRET_ENVS = ("DOTS_OCR_API_KEY",)

# dots.ocr (rednote-hilab, MIT), served by vLLM's OpenAI-compatible API. The
# request mirrors the project's own client (dots_ocr/model/inference.py, read
# 2026-11-18): image as a data URL, prompt prefixed with the image tokens, and
# its "prompt_layout_all_en" prompt, which returns every layout element
# (headers and footers included) as JSON. "model" is the --served-model-name of
# the project's documented `vllm serve` command.
_DOTS_DEFAULT_MODEL = "model"
_DOTS_IMAGE_TOKENS = "<|img|><|imgpad|><|endofimg|>"
_DOTS_PROMPT = """Please output the layout information from the PDF image, including each layout element's bbox, its category, and the corresponding text content within the bbox.

1. Bbox format: [x1, y1, x2, y2]

2. Layout Categories: The possible categories are ['Caption', 'Footnote', 'Formula', 'List-item', 'Page-footer', 'Page-header', 'Picture', 'Section-header', 'Table', 'Text', 'Title'].

3. Text Extraction & Formatting Rules:
    - Picture: For the 'Picture' category, the text field should be omitted.
    - Formula: Format its text as LaTeX.
    - Table: Format its text as HTML.
    - All Others (Text, Title, etc.): Format their text as Markdown.

4. Constraints:
    - The output text must be the original text from the image, with no translation.
    - All layout elements must be sorted according to human reading order.

5. Final Output: The entire output must be a single JSON object.
"""
# PDF pages are rendered at the project's own default resolution. One request
# per page, so a long PDF is cut to keep a run bounded.
_DOTS_PDF_DPI = 200
_DOTS_MAX_PAGES = 30
_DOTS_TIMEOUT_S = 180

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
# host / LLM context. The text is returned once (combined); ``pages`` only
# carries per-page metadata.
_MAX_MARKDOWN_CHARS = 50_000

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

def _env(name: str) -> str:
    """Environment value with surrounding whitespace removed.

    Keys pasted into an env file often keep a trailing ``\r`` or space, which
    httpx refuses as a header value before anything is sent.
    """
    return _raw_env(name).strip()


def _raw_env(name: str) -> str:
    """Environment value exactly as set, or an empty string."""
    return os.environ.get(name) or ""


def _redact(text: str) -> str:
    """``text`` with every configured provider secret replaced by ``***``.

    httpx echoes a rejected header value (``Bearer <key>``) in its exception,
    and a provider may echo the key in its error body. The keys are those of
    ``_PROVIDERS``, so a new provider is covered as soon as it is registered.
    """
    names = [name for _label, _fn, name in _PROVIDERS.values()]
    for name in (*names, *_EXTRA_SECRET_ENVS):
        raw = _raw_env(name)
        for value in {raw, raw.strip()}:
            if len(value) >= 4:
                text = text.replace(value, "***")
    return text


def _is_remote(source: str) -> bool:
    """True if ``source`` is an http(s) URL (vs a local file path)."""
    return source.strip().lower().startswith(("http://", "https://"))


def _read_uploaded_file(name: str) -> bytes | None:
    """Bytes of the file ``name`` uploaded to the current root agent.

    Returns ``None`` when ``name`` is not a bare filename or no agent is bound
    to the invocation, so the caller keeps its plain "File not found". Raises
    ValueError when the agent has no such upload, listing what it does have so
    the model can retry with the right name.
    """
    from apowerb.artifacts.agent_uploads import is_bare_filename, read_agent_upload
    from apowerb.core.invocation_context import get_root_agent_id

    root = get_root_agent_id()
    if not is_bare_filename(name) or not root.isdigit():
        return None

    folder = f"agent{root}"
    content, available = read_agent_upload(folder, name)
    if content is None:
        listing = f" Uploaded files: {', '.join(available)}." if available else ""
        raise ValueError(f"File not found: {name} in {folder}.{listing}")
    return content


def _own_upload_path(source: str) -> Path | None:
    """``source`` resolved, if it points into the running agent's uploads folder.

    A path is accepted only when it resolves (links followed) directly inside
    ``uploads/agent{root}/`` of the root agent running the tool; any other path,
    or no agent bound, gives ``None``.
    """
    from apowerb.configs import paths
    from apowerb.core.invocation_context import get_root_agent_id

    root = get_root_agent_id()
    if not root.isdigit():
        return None
    base = (paths.uploads_dir() / f"agent{root}").resolve()
    resolved = Path(source).resolve()
    return resolved if resolved.parent == base else None


# File already vetted by a server-side caller (``ocr_trusted_file``), never by a
# tool argument: the one extra path ``_read_supported_file`` accepts while set.
_trusted_file: ContextVar[Path | None] = ContextVar("ocr_trusted_file", default=None)


def _read_supported_file(source: str) -> tuple[str, str, bytes]:
    """Read an upload of the running agent, validating it is supported.

    ``source`` is the bare name of a file uploaded to the agent, or a path into
    that agent's own uploads folder. Other paths are never opened. Returns
    ``(mime, chunk_type, raw_bytes)``. Raises ValueError for a missing,
    out-of-scope or unsupported file so the caller can report it as a fatal error.
    """
    from apowerb.artifacts.agent_uploads import is_bare_filename

    not_found = ValueError(
        f"File not found: {source}. Provide a readable file path or an "
        f"http(s) URL."
    )
    ext = Path(source).suffix.lower()
    if ext not in _DOC_TYPES:
        raise ValueError(
            f"Unsupported file type '{ext}'. Supported: {_SUPPORTED_EXTS}."
        )
    mime, chunk_type = _DOC_TYPES[ext]
    if is_bare_filename(source):
        name = source
    else:
        path = _own_upload_path(source)
        trusted = _trusted_file.get()
        if path is None and trusted is not None and Path(source).resolve() == trusted:
            # Read from disk or fail: no lookup by name in an agent's uploads.
            if not trusted.is_file():
                raise not_found
            path = trusted
        if path is None:
            raise not_found
        if path.is_file():
            # Staged copies live on local disk even with S3 storage.
            return mime, chunk_type, path.read_bytes()
        name = path.name
    data = _read_uploaded_file(name)
    if data is None:
        raise not_found
    return mime, chunk_type, data


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

    api_key = _env("MISTRAL_OCR_API_KEY")
    if not api_key:
        raise EnvironmentError("MISTRAL_OCR_API_KEY not set")

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

    api_key = _env("AZURE_DOC_INTELLIGENCE_KEY")
    endpoint = _env("AZURE_DOC_INTELLIGENCE_ENDPOINT").rstrip("/")
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


def _dots_page_images(source: str) -> tuple[list[bytes], bool]:
    """PNG/JPEG/AVIF bytes to send to dots.ocr, one per page, and a cut flag.

    dots.ocr reads images only: a PDF is rendered page by page. Remote URLs are
    refused because the self-hosted server would fetch them itself, from inside
    our network (SSRF); Office files are refused because there is no renderer.
    Both raise RuntimeError, not ValueError, so ``auto`` tries the next provider.
    """
    if _is_remote(source):
        raise RuntimeError("dots.ocr does not fetch remote URLs; give it a local file")
    mime, _chunk_type, data = _read_supported_file(source)
    if mime.startswith("image/"):
        return [data], False
    if mime != "application/pdf":
        raise RuntimeError("dots.ocr reads images and PDFs only")

    import fitz

    with fitz.open(stream=data, filetype="pdf") as doc:
        count = min(doc.page_count, _DOTS_MAX_PAGES)
        images = [
            doc[i].get_pixmap(dpi=_DOTS_PDF_DPI).tobytes("png") for i in range(count)
        ]
        return images, doc.page_count > count


def _dots_layout_to_markdown(content: str) -> str:
    """Markdown of one page from dots.ocr's layout JSON, in reading order.

    Every element's text is kept, page headers and footers included: on an
    invoice they hold the supplier and the invoice number. Pictures carry no
    text. A reply that is not a JSON list of elements is returned as it is.
    """
    try:
        elements = json.loads(content)
    except (TypeError, ValueError):
        return (content or "").strip()
    if not isinstance(elements, list):
        return (content or "").strip()
    parts = [
        str(el.get("text") or "").strip()
        for el in elements
        if isinstance(el, dict) and el.get("category") != "Picture"
    ]
    return "\n\n".join(p for p in parts if p)


def _ocr_dots(source: str, include_images: bool) -> dict:
    """Run a self-hosted dots.ocr (vLLM) on a local image or PDF.

    Raises EnvironmentError (no server configured), ValueError (bad source),
    RuntimeError (source dots.ocr cannot read) or httpx errors (handled by the
    caller). ``include_images`` is ignored: the layout prompt returns no crops.
    """
    import httpx

    del include_images

    base = _env("DOTS_OCR_BASE_URL").rstrip("/")
    if not base:
        raise EnvironmentError("DOTS_OCR_BASE_URL not set")
    model = _env("DOTS_OCR_MODEL") or _DOTS_DEFAULT_MODEL
    headers = {"Content-Type": "application/json"}
    api_key = _env("DOTS_OCR_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    images, cut = _dots_page_images(source)
    pages = []
    for index, image in enumerate(images):
        mime = "image/jpeg" if image[:2] == b"\xff\xd8" else "image/png"
        if image[4:12] in (b"ftypavif", b"ftypavis"):
            mime = "image/avif"
        data_url = f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}"
        resp = httpx.post(
            f"{base}/chat/completions",
            headers=headers,
            json={
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image_url", "image_url": {"url": data_url}},
                            {"type": "text", "text": _DOTS_IMAGE_TOKENS + _DOTS_PROMPT},
                        ],
                    }
                ],
                "temperature": 0.1,
                "top_p": 0.9,
                "max_completion_tokens": 16384,
            },
            timeout=_DOTS_TIMEOUT_S,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"]
        pages.append(
            {"index": index, "markdown": _dots_layout_to_markdown(content), "images": []}
        )
    return {"pages": pages, "model": model, "pages_cut": cut}


# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

_PROVIDERS = {
    "dots": ("dots.ocr", _ocr_dots, "DOTS_OCR_BASE_URL"),
    "mistral": ("Mistral Document OCR", _ocr_mistral, "MISTRAL_OCR_API_KEY"),
    "azure": ("Azure Document Intelligence", _ocr_azure, "AZURE_DOC_INTELLIGENCE_KEY"),
}

_PROVIDER_ORDER = ["dots", "mistral", "azure"]


def _collect_pages(raw: dict, include_images: bool) -> tuple[list[dict], str, bool]:
    """Normalise a provider response into (pages, combined_markdown, truncated).

    Each page is ``{"index", "image_count"}``: the text lives only in the
    combined string (capped), never duplicated per page. The image crops
    (base64) are never returned; ``include_images`` only asks the provider for
    them so ``image_count`` is meaningful.
    """
    pages_out: list[dict] = []
    combined_parts: list[str] = []
    truncated = False

    for page in raw.get("pages", []) or []:
        images = page.get("images") or []
        pages_out.append(
            {"index": page.get("index"), "image_count": len(images)}
        )
        combined_parts.append(page.get("markdown") or "")

    combined = "\n\n".join(combined_parts)
    if raw.get("pages_cut"):
        truncated = True
    if len(combined) > _MAX_MARKDOWN_CHARS:
        combined = combined[:_MAX_MARKDOWN_CHARS] + "\n...[output truncated]"
        truncated = True
    return pages_out, combined, truncated


def ocr_trusted_file(path: str | Path, provider: str = "auto") -> dict:
    """OCR one local file the caller has already vetted (owner, location).

    For server code only, e.g. the workflow ``document`` node, which resolves an
    attachment ref to a file outside any agent's uploads folder. The file is
    trusted for the duration of this call only; same result shape as
    ``tool_ocr_document``.
    """
    token = _trusted_file.set(Path(path).resolve())
    try:
        return tool_ocr_document(source=str(path), provider=provider)
    finally:
        _trusted_file.reset(token)


def tool_ocr_document(
    source: str,
    provider: str = "auto",
    include_images: bool = False,
) -> dict:
    """Extract text from a document or image using OCR.

    Reads a PDF, Office document (PPTX/DOCX) or image and returns the recognised
    text as markdown in a single combined string (capped at 50,000 characters).

    Args:
        source (str): What to OCR. Either an ``http(s)`` URL (the provider fetches
            it), the name of a file the user uploaded (e.g. "cv.pdf", exactly as
            listed in [Uploaded files: …]), or a path to one of those uploads. Supported
            file types: PDF, PPTX, DOCX, PNG, JPG/JPEG, AVIF.
        provider (str): Which OCR provider to use. Options: "auto" (tries available
            providers in order), "dots", "mistral" or "azure". Default: "auto".
        include_images (bool): When true, ask the provider to extract image crops
            from each page. The base64 crops are NOT returned (too large for the
            model context); only their count per page (``image_count``) is.
            Default: False.

    Returns:
        dict: On success, ``status`` "success", ``markdown`` (combined text),
        ``pages`` (list of ``{index, image_count}``, no text), ``page_count``,
        ``provider_used``, ``model``, ``output_truncated`` (true if the text was cut). On failure, ``status``
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

        if not _env(env_key):
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
            detail = _redact(resp.text)[:500] if resp is not None else ""
            logger.warning("[OCR] %s HTTP error: %s", prov_name, _redact(str(exc)))
            errors.append(f"{prov_name}: HTTP {status} {detail}")
            continue
        except Exception as exc:
            message = _redact(str(exc))
            logger.warning("[OCR] %s failed: %s", prov_name, message)
            errors.append(f"{prov_name}: {message}")
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
            + "\n\nMake sure at least one provider is configured: DOTS_OCR_BASE_URL, "
            "MISTRAL_OCR_API_KEY, or AZURE_DOC_INTELLIGENCE_KEY + "
            "AZURE_DOC_INTELLIGENCE_ENDPOINT."
        ),
    }
