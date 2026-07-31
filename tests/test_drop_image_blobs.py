"""Tests for ``drop_old_image_blobs_from_messages``.

The helper is what stops vision-LLM agents (SCEI ARs in particular)
from saturating Gemini's per-minute input-token quota by re-sending
the same PDF page images on every chain-of-thought turn. These tests
nail down the wire-format cases we know about (OpenAI / litellm and
Gemini native), the tail-keep semantics, and the no-op paths so a
future contributor cannot accidentally re-introduce the cost.
"""

from __future__ import annotations

import pytest

from apowerb.helpers.litellm_config import (
    IMAGE_KEEP_TAIL,
    drop_old_image_blobs_from_messages,
)


_OPENAI_IMAGE_BLOCK = {
    "type": "image_url",
    "image_url": {"url": "data:image/png;base64,QUFB"},
}
_GEMINI_IMAGE_BLOCK = {
    "inline_data": {"mime_type": "image/png", "data": "QUFB"},
}
_PLACEHOLDER = {"type": "text", "text": "[image dropped from history]"}


def _make_text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def test_no_op_when_messages_missing():
    """Helper must tolerate kwargs without ``messages`` (e.g. an
    interrupted call before the prompt was assembled)."""
    out = drop_old_image_blobs_from_messages({})
    assert out == {}


def test_no_op_on_short_conversation():
    """When the conversation is shorter than the keep-tail, every
    message is current and we do not touch anything."""
    msgs = [{"role": "user", "content": [_OPENAI_IMAGE_BLOCK]}]
    kwargs = {"messages": msgs}

    drop_old_image_blobs_from_messages(kwargs)

    assert msgs[0]["content"] == [_OPENAI_IMAGE_BLOCK]


def test_drops_openai_image_blocks_from_old_messages():
    """The classic litellm format: image_url blocks on a non-tail
    message must be replaced by the placeholder."""
    msgs = [
        {
            "role": "user",
            "content": [_make_text_block("look at this AR"), _OPENAI_IMAGE_BLOCK],
        },
        {"role": "assistant", "content": [_make_text_block("ok extracting…")]},
        {"role": "user", "content": [_make_text_block("what's the total?")]},
    ]

    drop_old_image_blobs_from_messages({"messages": msgs})

    assert msgs[0]["content"] == [_make_text_block("look at this AR"), _PLACEHOLDER]
    assert msgs[1]["content"] == [_make_text_block("ok extracting…")]
    assert msgs[2]["content"] == [_make_text_block("what's the total?")]


def test_drops_gemini_native_image_blocks():
    """Gemini's own ``inline_data`` payload must also be detected —
    Google ADK sometimes builds messages in this format directly
    instead of going through the OpenAI normalisation."""
    msgs = [
        {
            "role": "user",
            "content": [_GEMINI_IMAGE_BLOCK, _make_text_block("ar.pdf")],
        },
        {"role": "assistant", "content": [_make_text_block("got it")]},
    ]

    drop_old_image_blobs_from_messages({"messages": msgs})

    assert msgs[0]["content"] == [_PLACEHOLDER, _make_text_block("ar.pdf")]


def test_keeps_image_on_last_message():
    """The current user turn must keep its image so the model can
    actually look at the freshly-attached document."""
    msgs = [
        {"role": "user", "content": [_OPENAI_IMAGE_BLOCK]},
        {"role": "assistant", "content": [_make_text_block("processing")]},
        {"role": "user", "content": [_OPENAI_IMAGE_BLOCK]},
    ]

    drop_old_image_blobs_from_messages({"messages": msgs})

    # First (old) image dropped, last (current) image preserved.
    assert msgs[0]["content"] == [_PLACEHOLDER]
    assert msgs[-1]["content"] == [_OPENAI_IMAGE_BLOCK]


def test_string_content_left_untouched():
    """Plain-string ``content`` (no list of blocks) is the standard
    text-only message shape — must not be mutated."""
    msgs = [
        {"role": "user", "content": "first turn"},
        {"role": "assistant", "content": "second turn"},
        {"role": "user", "content": "third turn"},
    ]
    snapshot = [dict(m) for m in msgs]

    drop_old_image_blobs_from_messages({"messages": msgs})

    assert msgs == snapshot


def test_mixed_old_message_keeps_text_drops_image():
    """A multi-block old message must keep its text blocks but lose
    its image blocks — partial rewrite, not a full replacement."""
    msgs = [
        {
            "role": "user",
            "content": [
                _make_text_block("page 1 of the AR"),
                _OPENAI_IMAGE_BLOCK,
                _make_text_block("page 2"),
                _GEMINI_IMAGE_BLOCK,
            ],
        },
        {"role": "user", "content": [_make_text_block("now?")]},
    ]

    drop_old_image_blobs_from_messages({"messages": msgs})

    assert msgs[0]["content"] == [
        _make_text_block("page 1 of the AR"),
        _PLACEHOLDER,
        _make_text_block("page 2"),
        _PLACEHOLDER,
    ]


def test_tail_keep_default_is_one():
    """Sanity: the env-tunable default keeps exactly one message tail.
    If this changes the test breaks loudly so the operator notices."""
    assert IMAGE_KEEP_TAIL == 1
