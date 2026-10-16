"""Tool images reach the model as images, not as base64 text.

A base64 page inside a function response is tokenized as text: ~69k tokens
for one 768 px PDF page (tiktoken o200k), and the model never sees the
picture. Tools now keep the bytes server side and return an ``image_ref``;
a before-model callback attaches the image next to the tool result.
"""
from __future__ import annotations

import asyncio
import io
import json
from types import SimpleNamespace

import pytest
from google.genai import types

from apowerb.core.tool_images import (
    attach_image,
    create_inline_tool_images_callback,
    get_image,
)


def _png_bytes(size=(64, 48)) -> bytes:
    PIL = pytest.importorskip("PIL.Image")
    buf = io.BytesIO()
    PIL.new("RGB", size, color="red").save(buf, format="PNG")
    return buf.getvalue()


def _request_with_tool_result(response: dict, model: str = "gemini/gemini-2.5-flash") -> SimpleNamespace:
    call = types.Content(
        role="model",
        parts=[types.Part(function_call=types.FunctionCall(id="c1", name="tool_x", args={}))],
    )
    result = types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(id="c1", name="tool_x", response=response)
            )
        ],
    )
    return SimpleNamespace(
        model=model,
        contents=[types.Content(role="user", parts=[types.Part(text="hi")]), call, result],
    )


def test_attach_image_round_trip():
    ref = attach_image(b"abc", "image/png")
    assert get_image(ref) == (b"abc", "image/png")
    assert get_image("img-unknown") is None


def test_callback_attaches_image_to_last_tool_result_without_mutating_history():
    data = _png_bytes()
    ref = attach_image(data, "image/png")
    req = _request_with_tool_result({"status": "success", "pages": [{"page_number": 1, "image_ref": ref}]})
    original_last = req.contents[-1]

    create_inline_tool_images_callback("agentX")(callback_context=None, llm_request=req)

    new_last = req.contents[-1]
    assert new_last is not original_last
    assert len(original_last.parts) == 1  # the stored event is untouched
    blobs = [p.inline_data for p in new_last.parts if p.inline_data]
    assert len(blobs) == 1
    assert blobs[0].data == data and blobs[0].mime_type == "image/png"


def test_callback_ignores_older_turns_and_unknown_refs():
    ref = attach_image(_png_bytes(), "image/png")
    req = _request_with_tool_result({"status": "success", "image_ref": "img-missing"})
    req.contents.insert(1, types.Content(role="user", parts=[types.Part(
        function_response=types.FunctionResponse(id="old", name="tool_x", response={"image_ref": ref}))]))
    before = list(req.contents)

    create_inline_tool_images_callback("agentX")(callback_context=None, llm_request=req)

    assert req.contents[:-1] == before[:-1]  # older turns untouched
    parts = req.contents[-1].parts
    assert not any(p.inline_data for p in parts)  # the old turn's image is not re-sent
    assert "no longer available" in parts[-1].text  # the model is told, not misled


def test_text_only_model_gets_a_note_instead_of_the_image():
    ref = attach_image(_png_bytes(), "image/png")
    req = _request_with_tool_result({"image_ref": ref}, model="groq/llama-3.3-70b-versatile")

    create_inline_tool_images_callback("agentX")(callback_context=None, llm_request=req)

    parts = req.contents[-1].parts
    assert not any(p.inline_data for p in parts)
    assert "not shown" in parts[-1].text


def test_litellm_sends_the_image_as_image_and_the_result_as_short_text():
    from google.adk.models.lite_llm import _content_to_message_param

    ref = attach_image(_png_bytes(), "image/png")
    req = _request_with_tool_result({"status": "success", "pages": [{"page_number": 1, "image_ref": ref}]})
    create_inline_tool_images_callback("agentX")(callback_context=None, llm_request=req)

    messages = asyncio.run(_content_to_message_param(req.contents[-1], provider="gemini", model="gemini/gemini-2.5-flash"))

    assert isinstance(messages, list) and len(messages) == 2
    tool_msg, user_msg = messages
    assert tool_msg["role"] == "tool"
    assert len(tool_msg["content"]) < 300
    assert user_msg["role"] == "user"
    kinds = [c.get("type") for c in user_msg["content"]]
    assert "image_url" in kinds


def test_pdf_to_images_returns_refs_not_base64(tmp_path, monkeypatch):
    fitz = pytest.importorskip("fitz")
    pytest.importorskip("PIL")
    from apowerb.configs import paths
    from apowerb.core.agent_helpers.pdf_to_images_tool import _make_pdf_to_images

    folder = tmp_path / "agent9"
    folder.mkdir()
    doc = fitz.open()
    for i in range(3):
        doc.new_page().insert_text((72, 72), f"page {i}")
    doc.save(folder / "scan.pdf")
    monkeypatch.setattr(paths, "uploads_dir", lambda: tmp_path)

    res = _make_pdf_to_images("agent9")("scan.pdf")

    assert res["status"] == "success", res
    assert res["page_count"] == 2
    assert res["total_pages_in_pdf"] == 3 and res["truncated"] is True
    for page in res["pages"]:
        assert "data" not in page
        data, mime = get_image(page["image_ref"])
        assert mime == "image/jpeg" and data[:2] == b"\xff\xd8"
    assert len(json.dumps(res)) < 500

    capped = _make_pdf_to_images("agent9")("scan.pdf", max_pages=1000)
    assert capped["page_count"] == 3  # clamped to 10, the PDF has 3


def test_read_image_returns_ref_not_base64(tmp_path):
    from apowerb.core.agent_helpers.file_readers import _read_image

    path = tmp_path / "photo.png"
    path.write_bytes(_png_bytes())
    res = _read_image(str(path), path.stat().st_size, "photo.png")

    assert "base64" not in res
    assert get_image(res["image_ref"]) is not None
    assert len(json.dumps(res)) < 500


def test_unreadable_image_is_an_error_without_ref(tmp_path):
    pytest.importorskip("PIL")
    from apowerb.core.agent_helpers.file_readers import _read_image

    path = tmp_path / "broken.png"
    path.write_bytes(b"not an image")
    res = _read_image(str(path), path.stat().st_size, "broken.png")

    assert res["status"] == "error" and "image_ref" not in res


def test_oversized_image_is_refused_before_reading(tmp_path, monkeypatch):
    from apowerb.core.agent_helpers import file_readers

    monkeypatch.setattr(file_readers, "_MAX_IMAGE_BYTES", 10)
    path = tmp_path / "big.png"
    path.write_bytes(_png_bytes())
    res = file_readers._read_image(str(path), path.stat().st_size, "big.png")

    assert res["status"] == "error" and "image_ref" not in res


@pytest.mark.parametrize(
    "model, expected",
    [
        ("gemini/gemini-2.5-flash", True),
        ("openai/gpt-4o", True),
        ("openai/Qwen2.5-VL-72B-Instruct", True),  # OVH behind a custom endpoint
        ("openai/Meta-Llama-3_3-70B-Instruct", False),
        (None, False),
    ],
)
def test_vision_detection_on_the_names_the_builder_produces(model, expected):
    from apowerb.core.tool_images import _accepts_images

    assert _accepts_images(model) is expected


def test_cache_is_bounded_by_bytes(monkeypatch):
    from apowerb.core import tool_images

    monkeypatch.setattr(tool_images, "_MAX_TOTAL_BYTES", 25)
    first = attach_image(b"x" * 10, "image/png")
    attach_image(b"y" * 10, "image/png")
    last = attach_image(b"z" * 10, "image/png")
    assert get_image(first) is None and get_image(last) is not None


def test_animated_gif_is_reencoded_as_a_still_image(tmp_path):
    PIL = pytest.importorskip("PIL.Image")
    from apowerb.core.agent_helpers.file_readers import _read_image

    frames = [PIL.new("RGB", (32, 32), color=c) for c in ("red", "green", "blue")]
    path = tmp_path / "anim.gif"
    frames[0].save(path, save_all=True, append_images=frames[1:])
    res = _read_image(str(path), path.stat().st_size, "anim.gif")

    _data, mime = get_image(res["image_ref"])
    assert mime == "image/png"


def test_pdf_to_images_tolerates_a_non_numeric_page_count(tmp_path, monkeypatch):
    fitz = pytest.importorskip("fitz")
    pytest.importorskip("PIL")
    from apowerb.configs import paths
    from apowerb.core.agent_helpers.pdf_to_images_tool import _make_pdf_to_images

    folder = tmp_path / "agent9"
    folder.mkdir()
    doc = fitz.open()
    for _ in range(3):
        doc.new_page()
    doc.save(folder / "scan.pdf")
    monkeypatch.setattr(paths, "uploads_dir", lambda: tmp_path)

    res = _make_pdf_to_images("agent9")("scan.pdf", max_pages="all")
    assert res["status"] == "success" and res["page_count"] == 2
