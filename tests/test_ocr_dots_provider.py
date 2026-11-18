"""Tests for the self-hosted dots.ocr provider of the OCR tool (ocr.py).

dots.ocr (rednote-hilab, MIT) is served by vLLM behind an OpenAI-compatible
``/chat/completions`` route. The request shape mirrors the project's own client
(``dots_ocr/model/inference.py``: image as a data URL, prompt prefixed with
``<|img|><|imgpad|><|endofimg|>``); the response is the layout JSON asked for by
its ``prompt_layout_all_en`` prompt. Pinned here:
- not configured → skipped, no network call;
- a local image or PDF (one request per page) becomes one page each;
- page headers and footers are kept (they carry the supplier and the invoice
  number), pictures are dropped, tables stay HTML;
- a reply that is not layout JSON is kept as plain text;
- remote URLs are refused (the self-hosted server would fetch them from inside
  our network), and ``auto`` then moves on to the next provider;
- the optional key is sent as a bearer token and never leaks into errors.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import MagicMock, patch

import fitz
import httpx
import pytest

from apowerb.tools_store.portfolio import ocr
from apowerb.tools_store.portfolio.ocr import (
    _PROVIDER_ORDER,
    _PROVIDERS,
    ocr_trusted_file,
    tool_ocr_document,
)

BASE = "http://gpu.internal:8000/v1"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
)

LAYOUT = [
    {"bbox": [58, 49, 102, 92], "category": "Picture"},
    {"bbox": [111, 54, 330, 96], "category": "Page-header", "text": "Brightwell Supplies"},
    {"bbox": [657, 97, 788, 114], "category": "Page-header", "text": "No. BW-2026-1184"},
    {"bbox": [577, 167, 789, 183], "category": "Text", "text": "Invoice date 9 November 2026"},
    {
        "bbox": [56, 310, 785, 575],
        "category": "Table",
        "text": "<table><tr><td>Total due</td><td>GBP 7,074.00</td></tr></table>",
    },
    {"bbox": [50, 1087, 442, 1101], "category": "Page-footer", "text": "VAT GB 000 0000 00"},
]


def _reply(content: str) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"choices": [{"message": {"content": content}}]}
    return resp


@pytest.fixture(autouse=True)
def _only_dots(monkeypatch):
    for name in ("DOTS_OCR_API_KEY", "DOTS_OCR_MODEL", "MISTRAL_OCR_API_KEY",
                 "AZURE_DOC_INTELLIGENCE_KEY", "AZURE_DOC_INTELLIGENCE_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("DOTS_OCR_BASE_URL", BASE + "/")
    monkeypatch.delenv("ROOT_AGENT_ID", raising=False)


@pytest.fixture
def png(tmp_path):
    f = tmp_path / "invoice.png"
    f.write_bytes(PNG)
    return f


def _pdf(tmp_path, pages: int):
    doc = fitz.open()
    for i in range(pages):
        doc.new_page().insert_text((72, 72), f"page {i + 1}")
    f = tmp_path / "invoice.pdf"
    doc.save(f)
    return f


class TestRegistry:
    def test_dots_is_registered_and_tried_first(self):
        assert _PROVIDERS["dots"][0] == "dots.ocr"
        assert _PROVIDERS["dots"][2] == "DOTS_OCR_BASE_URL"
        assert _PROVIDER_ORDER == ["dots", "mistral", "azure"]

    def test_not_configured_is_skipped_without_a_call(self, monkeypatch, png):
        monkeypatch.delenv("DOTS_OCR_BASE_URL")
        with patch("httpx.post") as post:
            result = ocr_trusted_file(png)
        assert result["status"] == "error"
        assert "DOTS_OCR_BASE_URL not set" in result["error_message"]
        post.assert_not_called()


class TestRequest:
    def test_image_is_sent_as_the_project_client_does(self, png):
        with patch("httpx.post", return_value=_reply(json.dumps(LAYOUT))) as post:
            result = ocr_trusted_file(png, provider="dots")

        assert result["status"] == "success", result
        assert post.call_args.args[0] == BASE + "/chat/completions"
        body = post.call_args.kwargs["json"]
        assert body["model"] == "model"
        image, prompt = body["messages"][0]["content"]
        assert image["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(PNG).decode()
        assert prompt["text"].startswith("<|img|><|imgpad|><|endofimg|>Please output the layout")
        assert "Authorization" not in post.call_args.kwargs["headers"]

    def test_model_name_and_key_come_from_the_environment(self, monkeypatch, png):
        monkeypatch.setenv("DOTS_OCR_MODEL", "rednote-hilab/dots.ocr")
        monkeypatch.setenv("DOTS_OCR_API_KEY", "vllm-key\r\n")
        with patch("httpx.post", return_value=_reply(json.dumps(LAYOUT))) as post:
            ocr_trusted_file(png, provider="dots")
        assert post.call_args.kwargs["json"]["model"] == "rednote-hilab/dots.ocr"
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer vllm-key"

    def test_each_pdf_page_is_one_request(self, tmp_path):
        f = _pdf(tmp_path, 2)
        replies = [_reply(json.dumps([{"category": "Text", "text": f"p{i}"}])) for i in (1, 2)]
        with patch("httpx.post", side_effect=replies) as post:
            result = ocr_trusted_file(f, provider="dots")

        assert result["status"] == "success", result
        assert post.call_count == 2
        sent = post.call_args_list[0].kwargs["json"]["messages"][0]["content"][0]
        assert sent["image_url"]["url"].startswith("data:image/png;base64,")
        assert result["page_count"] == 2
        assert result["markdown"] == "p1\n\np2"

    def test_long_pdf_reads_only_the_first_pages(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ocr, "_DOTS_MAX_PAGES", 2)
        f = _pdf(tmp_path, 3)
        with patch("httpx.post", return_value=_reply("[]")) as post:
            result = ocr_trusted_file(f, provider="dots")
        assert post.call_count == 2
        assert result["output_truncated"] is True


class TestLayoutToMarkdown:
    def test_headers_kept_pictures_dropped_tables_html(self, png):
        with patch("httpx.post", return_value=_reply(json.dumps(LAYOUT))):
            result = ocr_trusted_file(png, provider="dots")

        md = result["markdown"]
        assert md.startswith("Brightwell Supplies\n\nNo. BW-2026-1184\n\nInvoice date")
        assert "<td>GBP 7,074.00</td>" in md
        assert md.endswith("VAT GB 000 0000 00")
        assert "Picture" not in md
        assert result["provider_used"] == "dots.ocr"
        assert result["model"] == "model"

    def test_reply_that_is_not_layout_json_is_kept_as_text(self, png):
        with patch("httpx.post", return_value=_reply("Total due GBP 7,074.00")):
            result = ocr_trusted_file(png, provider="dots")
        assert result["status"] == "success"
        assert result["markdown"] == "Total due GBP 7,074.00"


class TestRefusedSources:
    def test_remote_url_is_never_sent_to_the_server(self):
        with patch("httpx.post") as post:
            result = tool_ocr_document("https://example.com/invoice.png", provider="dots")
        assert result["status"] == "error"
        assert "remote" in result["error_message"]
        post.assert_not_called()

    def test_auto_moves_on_to_mistral_for_a_remote_url(self, monkeypatch):
        monkeypatch.setenv("MISTRAL_OCR_API_KEY", "fake-key")
        mistral = MagicMock()
        mistral.raise_for_status.return_value = None
        mistral.json.return_value = {"model": "m", "pages": [{"index": 0, "markdown": "ok"}]}
        with patch("httpx.post", return_value=mistral) as post:
            result = tool_ocr_document("https://example.com/invoice.pdf")
        assert result["provider_used"] == "Mistral Document OCR"
        assert post.call_args.args[0] == ocr._MISTRAL_OCR_URL

    def test_office_file_falls_back_to_the_next_provider(self, tmp_path, monkeypatch):
        f = tmp_path / "note.docx"
        f.write_bytes(b"PK docx")
        monkeypatch.setenv("MISTRAL_OCR_API_KEY", "fake-key")
        mistral = MagicMock()
        mistral.raise_for_status.return_value = None
        mistral.json.return_value = {"model": "m", "pages": [{"index": 0, "markdown": "ok"}]}
        with patch("httpx.post", return_value=mistral) as post:
            result = ocr_trusted_file(f)
        assert result["provider_used"] == "Mistral Document OCR"
        post.assert_called_once()


class TestNoSecretInErrors:
    SECRET = "vllm-very-secret-value"

    def test_key_echoed_by_the_server_is_redacted(self, monkeypatch, png, caplog):
        monkeypatch.setenv("DOTS_OCR_API_KEY", self.SECRET)
        bad = MagicMock()
        bad.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"401 {self.SECRET}",
            request=MagicMock(),
            response=MagicMock(status_code=401, text=f"bad key {self.SECRET}"),
        )
        with caplog.at_level("DEBUG"), patch("httpx.post", return_value=bad):
            result = ocr_trusted_file(png, provider="dots")
        assert result["status"] == "error"
        assert "401" in result["error_message"]
        assert self.SECRET not in result["error_message"]
        assert self.SECRET not in caplog.text
