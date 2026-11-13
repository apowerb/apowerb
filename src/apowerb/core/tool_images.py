"""Images produced by tools, sent to the model as images instead of base64 text.

A function response is serialized to JSON text before it reaches the model,
so a base64 image inside it is billed as text (~69k tokens for one 768 px
PDF page, tiktoken o200k) and the model cannot look at it. Tools call
:func:`attach_image` instead and return the short ``image_ref`` it gives
back; :func:`create_inline_tool_images_callback` then attaches the bytes as
an ``inline_data`` part next to the tool result of the current model call.
LiteLLM moves such parts into a user message that follows the tool message.

The bytes stay in this process only: the tool and the model call that reads
its result run in the same invocation. Older turns keep the reference alone,
as the history compaction already does for stripped base64.
"""
from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from logging import getLogger
from typing import Optional

from google.genai import types

IMAGE_REF_KEY = "image_ref"
# Shared by every agent of the process: sized so that concurrent calls do not
# evict each other's pages before their model call (a page is ~50-150 KB).
_MAX_IMAGES = 512
_MAX_TOTAL_BYTES = 256 * 1024 * 1024

_images: "OrderedDict[str, tuple[bytes, str]]" = OrderedDict()
_total_bytes = 0
_lock = threading.Lock()


def attach_image(data: bytes, mime_type: str) -> str:
    """Keep ``data`` for the next model call and return its reference."""
    global _total_bytes
    ref = f"img-{uuid.uuid4().hex[:12]}"
    with _lock:
        _images[ref] = (data, mime_type)
        _total_bytes += len(data)
        while len(_images) > 1 and (
            len(_images) > _MAX_IMAGES or _total_bytes > _MAX_TOTAL_BYTES
        ):
            _, (old, _) = _images.popitem(last=False)
            _total_bytes -= len(old)
    return ref


def get_image(ref: str) -> Optional[tuple[bytes, str]]:
    with _lock:
        return _images.get(ref)


def _collect_refs(obj, refs: list[str]) -> None:
    if isinstance(obj, dict):
        ref = obj.get(IMAGE_REF_KEY)
        if isinstance(ref, str):
            refs.append(ref)
        for value in obj.values():
            _collect_refs(value, refs)
    elif isinstance(obj, list):
        for item in obj:
            _collect_refs(item, refs)


def _accepts_images(model) -> bool:
    """LiteLLM's model map; an unknown model is treated as text only.

    A model behind a custom endpoint is renamed ``openai/<name>`` by
    ``llm_model_builder``, which hides OVH models such as Qwen2.5-VL from the
    map: their ``ovhcloud/<name>`` entry is checked as well.
    """
    if not isinstance(model, str) or not model:
        return False
    import litellm

    for name in (model, "ovhcloud/" + model.split("/", 1)[-1]):
        try:
            if litellm.supports_vision(model=name):
                return True
        except Exception:  # noqa: BLE001 -- unknown to the map
            continue
    return False


def create_inline_tool_images_callback(agent_name: str):
    """Attach the images referenced by the latest tool results."""
    _logger = getLogger(f"apowerb.tool_images.{agent_name}")

    def before_model_callback(*, callback_context, llm_request):
        contents = getattr(llm_request, "contents", None) or []
        if not contents:
            return None
        last = contents[-1]
        refs: list[str] = []
        for part in last.parts or []:
            fr = part.function_response
            if fr is not None and fr.response is not None:
                _collect_refs(fr.response, refs)
        if not refs:
            return None
        images = []
        missing = 0
        for ref in refs:
            image = get_image(ref)
            if image is None:
                missing += 1
                _logger.warning("[TOOL_IMAGES] %s is no longer in memory; not attached", ref)
            else:
                images.append(image)
        extra: list[types.Part] = []
        if images and _accepts_images(getattr(llm_request, "model", None)):
            extra = [
                types.Part(inline_data=types.Blob(data=data, mime_type=mime))
                for data, mime in images
            ]
        elif images:
            # A text-only model would reject the request: say so instead.
            extra = [types.Part(text=f"[{len(images)} image(s) not shown: this model does not accept images.]")]
        if missing:
            # Never let the model believe it sees an image it was not sent.
            extra.append(types.Part(text=f"[{missing} image(s) no longer available: call the tool again to see them.]"))
        # A new Content: the one in the request is the session event itself.
        contents[-1] = types.Content(role=last.role, parts=list(last.parts) + extra)
        _logger.info("[TOOL_IMAGES] %d image(s) for the tool result", len(images))
        return None

    return before_model_callback
