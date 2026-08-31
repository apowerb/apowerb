"""
Configuration and utilities for LiteLLM compatibility with various providers.
"""

import os
import sys
from functools import wraps
from typing import List, Dict, Any
from logging import getLogger

import litellm
from litellm import ModelResponse

def _strip_thought_signatures(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Retire les signatures de *thinking* Gemini (``...__thought__<sig>``) des
    identifiants de tool call. Ces signatures, injectees par Gemini 2.5 sur les
    modeles a raisonnement, cassent l'appariement appel/reponse en multi-tours
    (litellm: ``Missing corresponding tool call for tool response message``).

    Strictement SCOPE : ne touche QUE les ids contenant ``__thought__`` -> aucun
    effet sur les modeles non-Gemini (Mistral, etc.). Nettoie les DEUX cotes
    (``tool_calls[].id`` cote assistant ET ``tool_call_id`` cote reponse outil)
    pour qu'ils restent coherents et s'apparient."""
    messages = kwargs.get("messages")
    if not isinstance(messages, list):
        return kwargs

    def _clean(tid):
        if isinstance(tid, str) and "__thought__" in tid:
            return tid.split("__thought__", 1)[0]
        return tid

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if msg.get("tool_call_id"):
            msg["tool_call_id"] = _clean(msg["tool_call_id"])
        for tc in (msg.get("tool_calls") or []):
            if isinstance(tc, dict) and tc.get("id"):
                tc["id"] = _clean(tc["id"])
    return kwargs


def _drop_orphan_tool_results(kwargs: "Dict[str, Any]") -> "Dict[str, Any]":
    """Filet de derniere ligne (TOUS modeles) : retire tout message tool-result
    dont le ``tool_call_id`` n'a pas d'appel apparie cote assistant. Sinon
    LiteLLM/Gemini leve ``Missing corresponding tool call``. Defense en profondeur
    DERRIERE la garde de troncature ADK (Layer A) : couvre les orphelins nes
    autrement (ids ``__thought__`` non apparies, troncature residuelle, batch
    multi-outils). A appeler APRES ``_strip_thought_signatures`` (ids normalises).
    NO-OP s'il n'y a aucun orphelin. On ne touche jamais aux appels assistant :
    un ``tool_call`` sans reponse (call dangling) est accepte par Gemini.
    Tout est garde par ``isinstance(..., str)`` -> ne leve JAMAIS (le hook tourne
    sur chaque completion). Accepte le fallback ``call_id`` (cf _fix_tool_call_sequences)."""
    messages = kwargs.get("messages")
    if not isinstance(messages, list):
        return kwargs
    call_ids = set()
    for msg in messages:
        if isinstance(msg, dict):
            for tc in (msg.get("tool_calls") or []):
                if isinstance(tc, dict):
                    tid = tc.get("id") or tc.get("call_id")
                    if isinstance(tid, str):
                        call_ids.add(tid)
    kept = []
    dropped = 0
    for msg in messages:
        if isinstance(msg, dict) and msg.get("role") == "tool":
            tid = msg.get("tool_call_id")
            if not isinstance(tid, str) or tid not in call_ids:
                dropped += 1
                continue
        kept.append(msg)
    if dropped:
        kwargs["messages"] = kept
        logger.warning(
            "[LITELLM] dropped %d orphan tool-result(s) without matching tool_call",
            dropped,
        )
    return kwargs


def _strip_extra_tool_calls_for_ovh(kwargs):
    """Ensure no assistant message carries more than one tool_call.

    OVH Llama-3.3 refuses both batched tool_calls in a single assistant turn
    AND the ``parallel_tool_calls=False`` request. The only workaround is to
    rewrite our outgoing message history so each assistant turn has at most
    one tool_call. We keep the FIRST tool_call (chronologically the one the
    LLM tried to run first); the tool response messages that match the
    kept call are preserved, the ones tied to dropped calls are removed.

    This is a defensive sanitisation — the prompt itself instructs the agent
    to chain tool calls sequentially, but if the LLM batches anyway, we make
    the request fit OVH's contract instead of crashing the run.
    """
    model = kwargs.get("model", "")
    if not (model.startswith("openai/Mistral") or model.startswith("ovhcloud/")):
        return kwargs

    messages = kwargs.get("messages")
    if not messages:
        return kwargs

    kept_call_ids = set()  # tool_call ids we keep (have a matching call left)
    new_messages = []
    for msg in messages:
        if not isinstance(msg, dict):
            new_messages.append(msg)
            continue
        role = msg.get("role")
        tool_calls = msg.get("tool_calls")
        if role == "assistant" and tool_calls and len(tool_calls) > 1:
            first = tool_calls[0]
            new_msg = dict(msg)
            new_msg["tool_calls"] = [first]
            new_messages.append(new_msg)
            tcid = first.get("id") if isinstance(first, dict) else None
            if tcid:
                kept_call_ids.add(tcid)
            continue
        if role == "assistant" and tool_calls and len(tool_calls) == 1:
            tcid = tool_calls[0].get("id") if isinstance(tool_calls[0], dict) else None
            if tcid:
                kept_call_ids.add(tcid)
            new_messages.append(msg)
            continue
        if role == "tool":
            tcid = msg.get("tool_call_id")
            if tcid and tcid not in kept_call_ids:
                # Tool reply whose call we stripped — skip
                continue
        new_messages.append(msg)

    if len(new_messages) != len(messages):
        kwargs["messages"] = new_messages
    return kwargs


logger = getLogger(__name__)

# Guard against configure_litellm_for_ovhcloud() being called more than once
# (e.g. hot-reload, test runners) — double-patching wraps an already-patched
# function and silently duplicates all message-formatting logic.
_configured = False


# Number of messages from the tail of the conversation to keep image blobs
# in. The current user turn always has its images intact; previous turns
# get their image content swapped for a small text placeholder so the model
# still knows an attachment was there but does not re-pay the token cost
# of re-reading it. ``IMAGE_KEEP_TAIL=1`` is the right default for most
# vision flows; raise it (e.g. 2) if the model needs to compare the
# current PDF with the immediately previous one.
IMAGE_KEEP_TAIL = int(os.environ.get("LLM_IMAGE_KEEP_TAIL", "1"))


def _block_is_image(block: Dict[str, Any]) -> bool:
    """Return True for any content block that holds raw image bytes.

    Covers the two main wire formats seen in the wild:
    - OpenAI / litellm normalised: ``{"type": "image_url", ...}``
    - Gemini native: ``{"inline_data": {"mime_type": ..., "data": ...}}``
    """
    if not isinstance(block, dict):
        return False
    if block.get("type") in ("image_url", "image", "input_image"):
        return True
    if "inline_data" in block:
        return True
    if "image_url" in block and not block.get("type"):
        # Bare ``{"image_url": {...}}`` without a ``type`` field
        return True
    return False


def drop_old_image_blobs_from_messages(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """Strip image content blocks from messages older than the tail window.

    Vision LLMs re-read the entire conversation on every turn — including
    the base64 image attachments that were already inspected on the
    *first* turn they appeared. Keeping all of them in scope is the
    fastest way to saturate Gemini's per-minute input-token quota
    (cf. one deployment, 2026-05-07: a single AR run was burning ~5 M tokens
    over 5+ chain-of-thought turns because each turn re-sent the same
    PDF page images).

    This helper rewrites the messages in place so that:
    - the last ``IMAGE_KEEP_TAIL`` messages keep their image blobs
    - every earlier message has its image blocks replaced by a small
      ``[image dropped from history]`` text placeholder

    The replacement preserves the message structure (still a ``content``
    list of typed blocks) so downstream LiteLLM normalisation stays
    unchanged. Mutating in place keeps the original kwargs object
    identity, which some providers rely on.

    Returns ``kwargs`` for chaining.
    """
    messages = kwargs.get("messages")
    if not isinstance(messages, list) or len(messages) <= IMAGE_KEEP_TAIL:
        return kwargs

    cutoff = len(messages) - IMAGE_KEEP_TAIL
    total_dropped = 0
    for msg in messages[:cutoff]:
        content = msg.get("content")
        if not isinstance(content, list):
            continue
        rewritten = []
        msg_dropped = 0
        for block in content:
            if _block_is_image(block):
                rewritten.append(
                    {"type": "text", "text": "[image dropped from history]"}
                )
                msg_dropped += 1
                continue
            rewritten.append(block)
        if msg_dropped:
            msg["content"] = rewritten
            total_dropped += msg_dropped

    if total_dropped:
        logger.info(
            "[LiteLLM Config] Dropped %d image block(s) from %d-message "
            "conversation history (kept tail of %d)",
            total_dropped, len(messages), IMAGE_KEEP_TAIL,
        )
    return kwargs


# Custom callback to modify messages before sending to providers that don't support system messages
class OVHCloudMessageHandler:
    """Handler to modify messages for OVHCloud compatibility."""

    def modify_messages_for_ovhcloud(self, kwargs):
        """
        Modify messages to be compatible with OVHCloud's requirements:
        1. At most ONE system message at the beginning
        2. After that, strict user/assistant alternation
        """
        logger.debug("[OVHCloud Handler] *** Handler called! ***")

        if "messages" not in kwargs:
            logger.warning("[OVHCloud Handler] No messages in kwargs")
            return kwargs

        # Fix: normalize message content lists — LiteLLM crashes when a content
        # list contains raw strings instead of {"type": "text", "text": ...} dicts.
        # This happens in SSE/chat mode when tool results accumulate as plain strings.
        raw_messages = kwargs["messages"]
        normalized = []
        for msg in raw_messages:
            content = msg.get("content")
            if isinstance(content, list):
                fixed = []
                for item in content:
                    if isinstance(item, str):
                        fixed.append({"type": "text", "text": item})
                    elif isinstance(item, dict):
                        fixed.append(item)
                    # drop anything else (None, etc.)
                msg = {**msg, "content": fixed}
            normalized.append(msg)
        kwargs["messages"] = normalized

        messages = kwargs["messages"]
        model = kwargs.get("model", "")

        logger.debug(f"[OVHCloud Handler] Model: {model}")

        # Only process for OVHCloud models
        if not model.startswith("ovhcloud/") and not model.startswith("openai/Mistral"):
            logger.debug("[OVHCloud Handler] Not an OVHCloud/Mistral model, skipping")
            return kwargs

        logger.debug(f"[OVHCloud Handler] Processing model: {model}")
        logger.debug(f"[OVHCloud Handler] Original messages count: {len(messages)}")
        logger.debug(f"[OVHCloud Handler] Original messages: {messages}")

        modified_messages = []
        system_content = []

        # First pass: collect all system messages
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                system_content.append(content)
                logger.debug(f"[OVHCloud Handler] Found system message: {content[:100]}...")

        # If we have system messages, add ONE combined system message at the start
        if system_content:
            combined_system = "\n\n".join(system_content)
            modified_messages.append({
                "role": "system",
                "content": combined_system
            })
            logger.debug("[OVHCloud Handler] Added combined system message")

        # Second pass: add non-system messages and ensure alternation
        last_role = "system" if system_content else None

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                continue  # Already handled

            # Never merge tool_call assistant messages or tool result messages —
            # they must stay as individual entries for the model to correlate them.
            has_tool_calls = bool(msg.get("tool_calls"))
            is_tool_result = role == "tool"
            prev_has_tool_calls = bool(modified_messages and modified_messages[-1].get("tool_calls"))

            can_merge = (
                last_role == role
                and not has_tool_calls
                and not is_tool_result
                and not prev_has_tool_calls
                and content  # don't merge empty/None content
            )

            if can_merge:
                # Consecutive plain text messages of same role — merge.
                # Must flatten list content before string concat,
                # otherwise list += "string" iterates chars and adds them individually.
                prev = modified_messages[-1]["content"]
                new_text = content if isinstance(content, str) else str(content)
                if isinstance(prev, list):
                    flat = " ".join(
                        item.get("text", str(item)) if isinstance(item, dict) else str(item)
                        for item in prev
                    )
                    modified_messages[-1]["content"] = flat + f"\n\n{new_text}"
                else:
                    modified_messages[-1]["content"] = str(prev) + f"\n\n{new_text}"
                logger.debug(f"[OVHCloud Handler] Merged consecutive {role} messages")
            else:
                modified_messages.append(msg)
                last_role = role

        # Third pass: fix any string items inside content lists introduced by merge loop.
        # Converts raw strings → {"type": "text", "text": "..."} dicts so LiteLLM
        # doesn't crash with 'str' object has no attribute 'get'.
        final_normalized = []
        for fmsg in modified_messages:
            fcontent = fmsg.get("content")
            if isinstance(fcontent, list):
                fixed_items = []
                for item in fcontent:
                    if isinstance(item, str):
                        fixed_items.append({"type": "text", "text": item})
                    elif isinstance(item, dict):
                        fixed_items.append(item)
                    # skip None / other garbage
                fmsg = {**fmsg, "content": fixed_items}
            final_normalized.append(fmsg)
        modified_messages = final_normalized

        # Fourth pass: fix tool-call sequences for strict Mistral alternation.
        # 1) Insert placeholder tool results for missing tool_call_ids
        # 2) Insert synthetic assistant between tool→user transitions
        modified_messages = self._fix_tool_call_sequences(modified_messages)

        kwargs["messages"] = modified_messages
        logger.debug(f"[OVHCloud Handler] Modified messages count: {len(modified_messages)}")
        logger.debug(f"[OVHCloud Handler] Modified messages: {modified_messages}")

        return kwargs

    @staticmethod
    def _fix_tool_call_sequences(messages: list[dict]) -> list[dict]:
        """Ensure tool-call sequences satisfy Mistral's strict ordering rules.

        Fixes two issues:
        1. Every tool_call_id in an assistant(tool_calls) message must have a
           matching tool-result message.  Missing ones get a placeholder.
        2. A ``user`` message must never directly follow a ``tool`` message.
           Mistral requires: assistant(tool_calls) → tool(results) → assistant → user.
           A synthetic assistant is inserted when this transition is detected.
        """
        fixed: list[dict] = []
        i = 0
        while i < len(messages):
            msg = messages[i]
            role = msg.get("role", "")

            # --- Fix 1: fill in missing tool results ---
            if role == "assistant" and msg.get("tool_calls"):
                fixed.append(msg)
                expected_ids: set[str] = {
                    tc.get("id") or tc.get("call_id", "")
                    for tc in msg["tool_calls"]
                    if tc.get("id") or tc.get("call_id")
                }

                # Consume all immediately-following tool-result messages
                found_ids: set[str] = set()
                j = i + 1
                while j < len(messages) and messages[j].get("role") == "tool":
                    tool_call_id = messages[j].get("tool_call_id", "")
                    if tool_call_id:
                        found_ids.add(tool_call_id)
                    fixed.append(messages[j])
                    j += 1

                # Insert placeholders for any missing tool results
                for missing_id in expected_ids - found_ids:
                    fixed.append({
                        "role": "tool",
                        "tool_call_id": missing_id,
                        "content": "No result available",
                    })
                    logger.debug(
                        f"[OVHCloud Handler] Inserted placeholder tool result for {missing_id}"
                    )

                i = j
                continue

            # --- Fix 2: bridge tool → user with a synthetic assistant ---
            if role == "user" and fixed and fixed[-1].get("role") == "tool":
                fixed.append({
                    "role": "assistant",
                    "content": "Understood.",
                })
                logger.debug(
                    "[OVHCloud Handler] Inserted synthetic assistant between tool and user"
                )

            fixed.append(msg)
            i += 1

        return fixed


# Global handler instance
_ovhcloud_handler = OVHCloudMessageHandler()


def configure_litellm_for_ovhcloud():
    """
    Configure LiteLLM to work with OVHCloud and other providers that don't support system messages.

    OVHCloud requires strict user/assistant message alternation and doesn't support system messages.
    This configuration ensures messages are properly formatted.
    """
    global _configured
    if _configured:
        logger.debug("[LiteLLM Config] Already configured, skipping.")
        return

    # Enable debug mode only when explicitly requested via environment variable.
    # Always-on debug floods production logs and hurts performance.
    if os.environ.get("LITELLM_LOG", "").upper() == "DEBUG":
        litellm._turn_on_debug()

    # Allow LiteLLM to modify parameters for compatibility
    litellm.modify_params = True

    # Drop unsupported params instead of failing
    litellm.drop_params = True

    # Register OVH-hosted Mistral so litellm knows it supports function calling
    litellm.register_model({
        "openai/Mistral-Small-3.2-24B-Instruct-2506": {
            "max_tokens": 128000,
            "max_input_tokens": 128000,  # was 32768 — caused heavy tool-chain failures
            "max_output_tokens": 8192,
            "input_cost_per_token": 0.0,
            "output_cost_per_token": 0.0,
            "litellm_provider": "openai",
            "mode": "chat",
            "supports_function_calling": True,
            "supports_tool_choice": True,
        }
    })

    # Monkey patch both litellm.acompletion AND the one used by Google ADK
    original_acompletion = litellm.acompletion

    @wraps(original_acompletion)
    async def patched_acompletion(*args, **kwargs):
        # Modify kwargs for OVHCloud compatibility
        kwargs = _ovhcloud_handler.modify_messages_for_ovhcloud(kwargs)
        # Drop image blobs from non-last messages so vision-LLM costs
        # do not multiply by the conversation length on long tool chains
        # (cf. one deployment, 2026-05-07: a single AR was burning ~5M Gemini
        # input tokens per run because each of the 5+ chain-of-thought
        # turns re-sent the same base64 PDF pages).
        kwargs = drop_old_image_blobs_from_messages(kwargs)
        kwargs = _strip_extra_tool_calls_for_ovh(kwargs)
        kwargs = _strip_thought_signatures(kwargs)  # Gemini __thought__ fix (multi-tours)
        kwargs = _drop_orphan_tool_results(kwargs)
        # OVH Mistral streaming is broken for tool calls — force non-streaming
        model = kwargs.get("model", "")
        if model.startswith("openai/Mistral") or model.startswith("ovhcloud/"):
            if kwargs.get("stream", False):
                logger.info(f"[OVHCloud Handler] Disabling streaming for {model} (tool call fix)")
                kwargs["stream"] = False
                kwargs.pop("stream_options", None)
                response = await original_acompletion(*args, **kwargs)
                _response = response  # capture to avoid late-binding closure bug
                async def _response_as_stream():
                    yield _response
                    # Terminal chunk so ADK's async-for loop ends cleanly
                    terminal = ModelResponse(
                        id=_response.id,
                        choices=[{"finish_reason": "stop", "index": 0, "delta": {}}],
                        model=_response.model,
                    )
                    terminal.choices[0].finish_reason = "stop"
                    yield terminal
                return _response_as_stream()
        # Call original function with modified kwargs
        return await original_acompletion(*args, **kwargs)

    # Patch at multiple levels to ensure it's intercepted
    litellm.acompletion = patched_acompletion

    # Also patch the acompletion function imported by google.adk
    try:
        if 'litellm' in sys.modules:
            sys.modules['litellm'].acompletion = patched_acompletion
        logger.info("[LiteLLM Config] Patched litellm module acompletion")
    except Exception as e:
        logger.warning(f"[LiteLLM Config] Could not patch module-level acompletion: {e}")

    # Also patch Google ADK's LiteLlm client directly
    try:
        from google.adk.models.lite_llm import LiteLlm

        original_adk_acompletion = LiteLlm.acompletion if hasattr(LiteLlm, 'acompletion') else None

        if original_adk_acompletion:
            @wraps(original_adk_acompletion)
            async def patched_adk_acompletion(self, *args, **kwargs):
                # Modify kwargs for OVHCloud compatibility
                kwargs = _ovhcloud_handler.modify_messages_for_ovhcloud(kwargs)
                # Drop image blobs from non-last messages — see comment
                # in patched_acompletion above for the rationale.
                kwargs = drop_old_image_blobs_from_messages(kwargs)
                kwargs = _strip_extra_tool_calls_for_ovh(kwargs)
                kwargs = _strip_thought_signatures(kwargs)  # Gemini __thought__ fix (multi-tours)
                kwargs = _drop_orphan_tool_results(kwargs)
                # OVH Mistral streaming is broken for tool calls — force non-streaming
                model = kwargs.get("model", "")
                if model.startswith("openai/Mistral") or model.startswith("ovhcloud/"):
                    if kwargs.get("stream", False):
                        logger.info(f"[OVHCloud Handler] Disabling streaming for {model} (ADK patch)")
                        kwargs["stream"] = False
                        kwargs.pop("stream_options", None)
                        response = await original_adk_acompletion(self, *args, **kwargs)
                        _response = response  # capture to avoid late-binding closure bug
                        async def _response_as_stream():
                            yield _response
                            # Terminal chunk so ADK's async-for loop ends cleanly
                            terminal = ModelResponse(
                                id=_response.id,
                                choices=[{"finish_reason": "stop", "index": 0, "delta": {}}],
                                model=_response.model,
                            )
                            terminal.choices[0].finish_reason = "stop"
                            yield terminal
                        return _response_as_stream()
                # Call original method with modified kwargs
                return await original_adk_acompletion(self, *args, **kwargs)

            LiteLlm.acompletion = patched_adk_acompletion
            logger.info("[LiteLLM Config] Patched Google ADK LiteLlm.acompletion")
    except Exception as e:
        logger.warning(f"[LiteLLM Config] Could not patch Google ADK LiteLlm: {e}")

    _configured = True
    logger.info("[LiteLLM Config] Configured for OVHCloud compatibility")
    logger.info("[LiteLLM Config] Patched acompletion to handle OVHCloud message formatting")
    logger.info("[LiteLLM Config] System messages will be converted and messages will alternate properly")


def convert_system_message_to_user(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Convert system messages to user messages for providers that don't support them.

    Args:
        messages: List of message dictionaries with 'role' and 'content' keys

    Returns:
        Modified list of messages with system messages converted to user messages
    """
    converted_messages = []

    for msg in messages:
        if msg.get("role") == "system":
            # Convert system message to user message with a prefix
            converted_msg = {
                "role": "user",
                "content": f"System instructions: {msg.get('content', '')}"
            }
            converted_messages.append(converted_msg)
            logger.debug("[LiteLLM] Converted system message to user message")
        else:
            converted_messages.append(msg)

    # Ensure alternation: if we have consecutive user messages, merge them
    final_messages = []
    for i, msg in enumerate(converted_messages):
        if i > 0 and msg.get("role") == "user" and final_messages[-1].get("role") == "user":
            # Merge with previous user message
            final_messages[-1]["content"] += f"\n\n{msg.get('content', '')}"
            logger.debug("[LiteLLM] Merged consecutive user messages")
        else:
            final_messages.append(msg)

    return final_messages