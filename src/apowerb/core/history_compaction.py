"""Callbacks that compact heavy payloads from the ADK history.

ADK replays the full ``LlmRequest.contents`` on every turn. When a tool
returns base64-encoded images (e.g. ``tool_pdf_to_images``), each page
weighs ~100-150k tokens. After 5 turns the same image has been re-sent
5× — burning Gemini quota for nothing.

Strategy: strip large base64 payloads from ``function_response`` parts
in every Content except the last one. The LLM has already processed
the image at turn N; from turn N+1 onward, its own textual output is
the source of truth.
"""

from __future__ import annotations

from logging import getLogger
from typing import Optional

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest, LlmResponse


_LARGE_PAYLOAD_KEYS = ("data", "image_data", "image_base64", "base64", "content")
_MIN_STRIP_CHARS = 4000


def _strip_payload(obj) -> int:
    """Recursively replace large base64 strings with a placeholder.

    Returns the number of characters dropped — used for logging and tests.
    """
    dropped = 0
    if isinstance(obj, dict):
        for key, value in list(obj.items()):
            if (
                key in _LARGE_PAYLOAD_KEYS
                and isinstance(value, str)
                and len(value) >= _MIN_STRIP_CHARS
            ):
                obj[key] = f"<stripped: {len(value)} chars>"
                dropped += len(value)
            else:
                dropped += _strip_payload(value)
    elif isinstance(obj, list):
        for item in obj:
            dropped += _strip_payload(item)
    return dropped


def create_strip_large_payloads_callback(agent_name: str):
    """Strip large base64 payloads from older history Content blocks.

    The latest Content is preserved verbatim — that's where the LLM is
    "looking" right now. Everything before it gets its base64 blobs
    replaced with a short placeholder so the LLM sees that the tool ran
    and what it produced, minus the raw bytes.
    """
    _logger = getLogger(f"apowerb.strip_history.{agent_name}")

    def before_model_callback(
        *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> Optional[LlmResponse]:
        contents = getattr(llm_request, "contents", None) or []
        if len(contents) < 2:
            return None

        total_dropped = 0
        for content in contents[:-1]:
            for part in getattr(content, "parts", None) or []:
                fr = getattr(part, "function_response", None)
                if fr is None:
                    continue
                response = getattr(fr, "response", None)
                if response is None:
                    continue
                total_dropped += _strip_payload(response)

        if total_dropped:
            _logger.info(
                "[STRIP_HISTORY] dropped %d chars of base64 payloads from older turns",
                total_dropped,
            )
        return None

    return before_model_callback
