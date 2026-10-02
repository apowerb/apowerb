"""Tests for the OCR tool (ocr.py, issue #144).

The issue has no acceptance criteria, so these pin the scope we chose:
- missing key → error dict, no network call;
- bad input (empty source, unknown provider, missing/unsupported file) → error;
- remote URL vs local file produce the right Mistral ``document`` chunk;
- happy path parses ``pages[].markdown`` into a combined string;
- ``include_images`` toggles the request flag and the returned crops;
- oversized output is capped and flagged;
- an HTTP error surfaces as an error dict, not an exception.

The Mistral request/response shape is mocked from the contract documented at
https://docs.mistral.ai/api/endpoint/ocr (verified 2026-10-02).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from apowerb.tools_store.portfolio.ocr import (
    _MISTRAL_OCR_MODEL,
    _MISTRAL_OCR_URL,
    _PROVIDER_ORDER,
    _PROVIDERS,
    _build_document_chunk,
    tool_ocr_document,
)


def _fake_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


def _ocr_payload(markdowns: list[str], images_per_page: int = 0) -> dict:
    return {
        "model": _MISTRAL_OCR_MODEL,
        "pages": [
            {
                "index": i,
                "markdown": md,
                "images": [{"id": f"img{i}_{j}"} for j in range(images_per_page)],
                "dimensions": {"width": 1000, "height": 1400},
            }
            for i, md in enumerate(markdowns)
        ],
        "usage_info": {"pages_processed": len(markdowns)},
    }


# ── Provider registry sanity ──────────────────────────────────────────────
class TestProviderRegistry:
    def test_providers_registered(self):
        assert "mistral" in _PROVIDERS
        assert "azure" in _PROVIDERS
        assert _PROVIDER_ORDER == ["mistral", "azure"]

    def test_tuple_structure(self):
        for _key, (name, fn, env_key) in _PROVIDERS.items():
            assert isinstance(name, str)
            assert callable(fn)
            assert isinstance(env_key, str)


# ── Input validation ──────────────────────────────────────────────────────
class TestValidation:
    def test_empty_source(self):
        result = tool_ocr_document("   ")
        assert result["status"] == "error"
        assert "source" in result["error_message"]

    def test_unknown_provider(self):
        result = tool_ocr_document("https://x/y.pdf", provider="tesseract")
        assert result["status"] == "error"
        assert "Unknown provider" in result["error_message"]


# ── Missing key: error, and NO network call ───────────────────────────────
class TestMissingKey:
    def test_no_key_no_network(self, monkeypatch):
        monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
        with patch("httpx.post") as mock_post:
            result = tool_ocr_document("https://example.com/doc.pdf")
        assert result["status"] == "error"
        assert "MISTRAL_API_KEY" in result["error_message"]
        mock_post.assert_not_called()


# ── _build_document_chunk ─────────────────────────────────────────────────
class TestBuildChunk:
    def test_remote_pdf_is_document_url(self):
        chunk = _build_document_chunk("https://arxiv.org/pdf/2201.04234")
        assert chunk == {
            "type": "document_url",
            "document_url": "https://arxiv.org/pdf/2201.04234",
        }

    def test_remote_png_is_image_url(self):
        chunk = _build_document_chunk("https://host/receipt.png")
        assert chunk["type"] == "image_url"
        assert chunk["image_url"] == "https://host/receipt.png"

    def test_remote_unknown_ext_defaults_to_document_url(self):
        chunk = _build_document_chunk("https://host/scan")
        assert chunk["type"] == "document_url"

    def test_remote_url_with_query_string(self):
        chunk = _build_document_chunk("https://host/a.png?sig=abc")
        assert chunk["type"] == "image_url"

    def test_local_file_becomes_base64_data_url(self, tmp_path):
        f = tmp_path / "scan.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        chunk = _build_document_chunk(str(f))
        assert chunk["type"] == "document_url"
        assert chunk["document_url"].startswith("data:application/pdf;base64,")

    def test_local_image_uses_image_url_data(self, tmp_path):
        f = tmp_path / "pic.jpg"
        f.write_bytes(b"\xff\xd8\xff fake jpeg")
        chunk = _build_document_chunk(str(f))
        assert chunk["type"] == "image_url"
        assert chunk["image_url"].startswith("data:image/jpeg;base64,")

    def test_missing_local_file_raises(self):
        with pytest.raises(ValueError, match="File not found"):
            _build_document_chunk("/no/such/file.pdf")

    def test_unsupported_local_ext_raises(self, tmp_path):
        f = tmp_path / "data.xyz"
        f.write_text("nope")
        with pytest.raises(ValueError, match="Unsupported file type"):
            _build_document_chunk(str(f))


# ── Happy path: request building + response parsing ───────────────────────
class TestHappyPath:
    def test_remote_pdf_success(self, monkeypatch):
        monkeypatch.setenv("MISTRAL_API_KEY", "fake-key")
        payload = _ocr_payload(["# Page one", "Second page text"])
        with patch("httpx.post", return_value=_fake_response(payload)) as mock_post:
            result = tool_ocr_document("https://example.com/doc.pdf")

        assert result["status"] == "success"
        assert result["page_count"] == 2
        assert result["provider_used"] == "Mistral Document OCR"
        assert result["model"] == _MISTRAL_OCR_MODEL
        assert "# Page one" in result["markdown"]
        assert "Second page text" in result["markdown"]
        assert result["output_truncated"] is False
        assert result["pages"][0]["index"] == 0
        assert "images" not in result["pages"][0]  # include_images defaults off

        # Request shape matches the documented contract.
        args, kwargs = mock_post.call_args
        assert args[0] == _MISTRAL_OCR_URL
        assert kwargs["headers"]["Authorization"] == "Bearer fake-key"
        body = kwargs["json"]
        assert body["model"] == _MISTRAL_OCR_MODEL
        assert body["document"] == {
            "type": "document_url",
            "document_url": "https://example.com/doc.pdf",
        }
        assert body["include_image_base64"] is False

    def test_local_file_through_tool(self, monkeypatch, tmp_path):
        # The branch Farid hits first: an uploaded file OCR'd end-to-end.
        monkeypatch.setenv("MISTRAL_API_KEY", "fake-key")
        f = tmp_path / "upload.pdf"
        f.write_bytes(b"%PDF-1.4 fake")
        payload = _ocr_payload(["extracted text"])
        with patch("httpx.post", return_value=_fake_response(payload)) as mock_post:
            result = tool_ocr_document(str(f))

        assert result["status"] == "success"
        assert result["markdown"] == "extracted text"
        body = mock_post.call_args.kwargs["json"]
        assert body["document"]["type"] == "document_url"
        assert body["document"]["document_url"].startswith(
            "data:application/pdf;base64,"
        )

    def test_include_images_toggles_flag_and_payload(self, monkeypatch):
        monkeypatch.setenv("MISTRAL_API_KEY", "fake-key")
        payload = _ocr_payload(["text"], images_per_page=2)
        with patch("httpx.post", return_value=_fake_response(payload)) as mock_post:
            result = tool_ocr_document(
                "https://example.com/doc.pdf", include_images=True
            )

        assert mock_post.call_args.kwargs["json"]["include_image_base64"] is True
        assert result["pages"][0]["image_count"] == 2
        assert result["pages"][0]["images"] == [{"id": "img0_0"}, {"id": "img0_1"}]


# ── Output is bounded ─────────────────────────────────────────────────────
class TestTruncation:
    def test_oversized_page_is_truncated(self, monkeypatch):
        monkeypatch.setenv("MISTRAL_API_KEY", "fake-key")
        from apowerb.tools_store.portfolio import ocr

        monkeypatch.setattr(ocr, "_MAX_PAGE_MARKDOWN_CHARS", 100, raising=False)
        payload = _ocr_payload(["x" * 5000])
        with patch("httpx.post", return_value=_fake_response(payload)):
            result = tool_ocr_document("https://example.com/big.pdf")

        assert result["status"] == "success"
        assert result["output_truncated"] is True
        assert "[page truncated]" in result["pages"][0]["markdown"]
        assert len(result["pages"][0]["markdown"]) < 1000

    def test_combined_cap_truncates(self, monkeypatch):
        # Pages each under the per-page cap, but summing over the combined cap.
        monkeypatch.setenv("MISTRAL_API_KEY", "fake-key")
        from apowerb.tools_store.portfolio import ocr

        monkeypatch.setattr(ocr, "_MAX_PAGE_MARKDOWN_CHARS", 10_000, raising=False)
        monkeypatch.setattr(ocr, "_MAX_MARKDOWN_CHARS", 50, raising=False)
        payload = _ocr_payload(["a" * 30, "b" * 30, "c" * 30])
        with patch("httpx.post", return_value=_fake_response(payload)):
            result = tool_ocr_document("https://example.com/multi.pdf")

        assert result["status"] == "success"
        assert result["output_truncated"] is True
        assert "[output truncated]" in result["markdown"]
        # Per-page markdown is untouched (only the combined string is capped).
        assert result["pages"][0]["markdown"] == "a" * 30


# ── Azure Document Intelligence provider ──────────────────────────────────
def _azure_accepted(op_location: str = "https://az/op/123") -> MagicMock:
    resp = MagicMock()
    resp.status_code = 202
    resp.headers = {"Operation-Location": op_location}
    resp.raise_for_status.return_value = None
    return resp


def _azure_result(
    status: str, pages: list | None = None, error_msg: str = ""
) -> MagicMock:
    payload: dict = {"status": status}
    if pages is not None:
        payload["analyzeResult"] = {"modelId": "prebuilt-read", "pages": pages}
    if error_msg:
        payload["error"] = {"message": error_msg}
    resp = MagicMock()
    resp.json.return_value = payload
    resp.raise_for_status.return_value = None
    return resp


class TestAzureProvider:
    def test_azure_success_remote_url(self, monkeypatch):
        monkeypatch.setenv("AZURE_DOC_INTELLIGENCE_KEY", "az-key")
        monkeypatch.setenv("AZURE_DOC_INTELLIGENCE_ENDPOINT", "https://my.az.com/")
        pages = [
            {"pageNumber": 1, "lines": [{"content": "Hello"}, {"content": "World"}]}
        ]
        with patch("httpx.post", return_value=_azure_accepted()) as mock_post, patch(
            "httpx.get", return_value=_azure_result("succeeded", pages)
        ) as mock_get:
            result = tool_ocr_document("https://ex.com/doc.pdf", provider="azure")

        assert result["status"] == "success"
        assert result["provider_used"] == "Azure Document Intelligence"
        assert result["pages"][0]["markdown"] == "Hello\nWorld"
        assert result["pages"][0]["index"] == 0  # pageNumber 1 → 0-based
        assert result["page_count"] == 1
        # analyze URL + auth header + body shape
        post_args, post_kwargs = mock_post.call_args
        assert post_args[0] == (
            "https://my.az.com/documentintelligence/documentModels/"
            "prebuilt-read:analyze?api-version=2024-11-30"
        )
        assert post_kwargs["headers"]["Ocp-Apim-Subscription-Key"] == "az-key"
        assert post_kwargs["json"] == {"urlSource": "https://ex.com/doc.pdf"}
        # polled the Operation-Location
        assert mock_get.call_args.args[0] == "https://az/op/123"

    def test_azure_local_file_uses_base64_source(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AZURE_DOC_INTELLIGENCE_KEY", "az-key")
        monkeypatch.setenv("AZURE_DOC_INTELLIGENCE_ENDPOINT", "https://my.az.com")
        f = tmp_path / "scan.png"
        f.write_bytes(b"\x89PNG fake")
        pages = [{"pageNumber": 1, "lines": [{"content": "text"}]}]
        with patch("httpx.post", return_value=_azure_accepted()) as mock_post, patch(
            "httpx.get", return_value=_azure_result("succeeded", pages)
        ):
            result = tool_ocr_document(str(f), provider="azure")

        assert result["status"] == "success"
        body = mock_post.call_args.kwargs["json"]
        assert "base64Source" in body and "urlSource" not in body

    def test_azure_missing_endpoint_errors(self, monkeypatch):
        monkeypatch.setenv("AZURE_DOC_INTELLIGENCE_KEY", "az-key")
        monkeypatch.delenv("AZURE_DOC_INTELLIGENCE_ENDPOINT", raising=False)
        with patch("httpx.post") as mock_post:
            result = tool_ocr_document("https://ex.com/doc.pdf", provider="azure")
        assert result["status"] == "error"
        assert "AZURE_DOC_INTELLIGENCE_ENDPOINT" in result["error_message"]
        mock_post.assert_not_called()

    def test_azure_failed_status_errors(self, monkeypatch):
        monkeypatch.setenv("AZURE_DOC_INTELLIGENCE_KEY", "az-key")
        monkeypatch.setenv("AZURE_DOC_INTELLIGENCE_ENDPOINT", "https://my.az.com")
        with patch("httpx.post", return_value=_azure_accepted()), patch(
            "httpx.get", return_value=_azure_result("failed", error_msg="bad doc")
        ):
            result = tool_ocr_document("https://ex.com/doc.pdf", provider="azure")
        assert result["status"] == "error"
        assert "bad doc" in result["error_message"]

    def test_azure_timeout_errors(self, monkeypatch):
        monkeypatch.setenv("AZURE_DOC_INTELLIGENCE_KEY", "az-key")
        monkeypatch.setenv("AZURE_DOC_INTELLIGENCE_ENDPOINT", "https://my.az.com")
        from apowerb.tools_store.portfolio import ocr

        monkeypatch.setattr(ocr, "_AZURE_POLL_TIMEOUT_S", 0, raising=False)
        with patch("httpx.post", return_value=_azure_accepted()), patch(
            "httpx.get", return_value=_azure_result("running")
        ):
            result = tool_ocr_document("https://ex.com/doc.pdf", provider="azure")
        assert result["status"] == "error"
        assert "did not finish" in result["error_message"]


# ── HTTP error becomes an error dict ──────────────────────────────────────
class TestHttpError:
    def test_http_error_returns_error_dict(self, monkeypatch):
        monkeypatch.setenv("MISTRAL_API_KEY", "fake-key")
        err_resp = MagicMock()
        err_resp.status_code = 401
        err_resp.text = "Unauthorized"
        bad = MagicMock()
        bad.json.return_value = {}
        bad.raise_for_status.side_effect = httpx.HTTPStatusError(
            "401", request=MagicMock(), response=err_resp
        )
        with patch("httpx.post", return_value=bad):
            result = tool_ocr_document("https://example.com/doc.pdf")

        assert result["status"] == "error"
        assert "401" in result["error_message"]
        assert "Unauthorized" in result["error_message"]

    def test_missing_local_file_is_fatal_error(self, monkeypatch):
        monkeypatch.setenv("MISTRAL_API_KEY", "fake-key")
        with patch("httpx.post") as mock_post:
            result = tool_ocr_document("/no/such/file.pdf")
        assert result["status"] == "error"
        assert "File not found" in result["error_message"]
        mock_post.assert_not_called()
