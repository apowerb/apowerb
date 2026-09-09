"""After-model callback that records LLM token usage per model call.

Persists one row per completed model turn to the ``llm_usage`` table
(:class:`apowerb.models.LlmUsage`). Best-effort ABSOLUTE: any failure
(DB down, malformed usage_metadata, missing session) is caught, logged
as a warning, and never propagated — a usage-accounting hiccup must
never break an agent response.
"""
from __future__ import annotations

import asyncio
import inspect
from typing import Any, Callable, Optional

from apowerb.configs.th2logger import setup_logging

logger = setup_logging(__name__)

AfterModelCallback = Callable[..., Optional[Any]]

# asyncio.create_task() does NOT keep a strong reference to the task it
# returns — per the stdlib docs ("Important: Save a reference to the
# result [...] to avoid a task disappearing mid-execution"), an
# unreferenced task can be garbage-collected before its INSERT lands,
# silently dropping usage rows. Every write task is added here on
# creation and removed via its own done-callback once it completes
# (success or failure — failure is still handled/logged inside
# ``_persist_usage_row``).
_pending_writes: set[asyncio.Task] = set()


def _extract_usage(usage_metadata: Any) -> Optional[dict]:
    """Normalize ``usage_metadata`` (pydantic object, dict, or ``None``)
    into a plain dict with the 5 token counts. Returns ``None`` when
    ``usage_metadata`` is absent — the caller treats that as "nothing to
    record" (e.g. an intermediate streaming chunk, or an error response).
    """
    if usage_metadata is None:
        return None

    if isinstance(usage_metadata, dict):
        raw = usage_metadata
    elif hasattr(usage_metadata, "model_dump"):
        raw = usage_metadata.model_dump()
    else:
        raw = {
            "prompt_token_count": getattr(usage_metadata, "prompt_token_count", None),
            "candidates_token_count": getattr(usage_metadata, "candidates_token_count", None),
            "thoughts_token_count": getattr(usage_metadata, "thoughts_token_count", None),
            "cached_content_token_count": getattr(usage_metadata, "cached_content_token_count", None),
            "total_token_count": getattr(usage_metadata, "total_token_count", None),
        }

    return {
        "input_tokens": raw.get("prompt_token_count") or 0,
        "output_tokens": raw.get("candidates_token_count") or 0,
        "thoughts_tokens": raw.get("thoughts_token_count") or 0,
        "cached_tokens": raw.get("cached_content_token_count") or 0,
        "total_tokens": raw.get("total_token_count") or 0,
    }


def _extract_tool_names(llm_response: Any) -> Optional[str]:
    """Comma-separated names of the tools this turn asked to call, or
    ``None`` when the turn produced no function call (plain text answer).

    Attribution model: a tool named here does NOT cost tokens in THIS row
    -- its result is appended to the conversation and re-sent as part of
    the NEXT turn's prompt. The ``per_tool`` driver therefore measures a
    tool's cost as the input growth on the following turn (see the LAG
    window in the usage dashboard, which is a commercial screen). Order is
    preserved and duplicates are kept out, so a turn calling the same tool
    twice records it once.

    Comma-separated because tool names are function identifiers: both ADK
    and the OpenAI-compatible function-calling schemas constrain them to
    ``[A-Za-z0-9_-]``, so a comma cannot appear inside one and the
    ``split(",")`` on the read side is unambiguous. A name that did
    contain a comma would be split into two phantom tools -- hence the
    filter below, which drops any name carrying the separator rather than
    silently corrupting the attribution.
    """
    try:
        content = getattr(llm_response, "content", None)
        parts = getattr(content, "parts", None) or []
        names: list[str] = []
        for part in parts:
            fc = getattr(part, "function_call", None)
            name = getattr(fc, "name", None) if fc is not None else None
            if not name or "," in name:
                continue
            if name not in names:
                names.append(name)
        return ",".join(names) if names else None
    except Exception:
        return None


# Observers called AFTER a consumption event has been recorded. The
# billing extension registers one to debit credits for the overage.
# Without it, the list stays empty.
_post_usage_observers: list = []


def register_post_usage(observer) -> None:
    """Registers an observer -- ``async fn(db, owner_id, plan=None)``."""
    _post_usage_observers.append(observer)


async def _notify_post_usage_observers(db, owner_id: Optional[str]) -> None:
    """Best-effort: a failing observer does not lose the usage row,
    already committed above."""
    if not owner_id:
        return
    for observer in _post_usage_observers:
        try:
            await observer(db, owner_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[USAGE_RECORDER] post-usage observer failed for %s: %s",
                owner_id,
                exc,
            )


async def _persist_usage_row(**fields: Any) -> None:
    """Insert one ``llm_usage`` row. Never raises."""
    try:
        from apowerb.helpers.database import sessionmanager
        from apowerb.models import LlmUsage

        async with sessionmanager.session() as db:
            db.add(LlmUsage(**fields))
            await db.commit()
            await _notify_post_usage_observers(db, fields.get("owner_id"))
    except Exception as exc:
        logger.warning("[USAGE_RECORDER] failed to persist llm_usage row: %s", exc)


def create_usage_recorder_callback(
    agent_id: int,
    agent_name: str,
    owner_id: Optional[str],
    model_name: Optional[str],
    billed_to_thaink2: bool = False,
) -> AfterModelCallback:
    """Build an ``after_model_callback`` that records token usage for one
    completed model turn.

    Streaming safety — this repo's only model path: ``to_agent()`` always
    builds its model via ``build_litellm_model()``
    (``core/agent_helpers/agent_utils.py``, ``model = build_litellm_model(...)``),
    i.e. every model call goes through ``google.adk.models.lite_llm.LiteLlm
    .generate_content_async`` (venv:
    ``google/adk/models/lite_llm.py:1927``). ``google_llm.py`` /
    ``StreamingResponseAggregator`` (``google/adk/utils/streaming_utils.py``)
    are ADK's native-Gemini streaming path and are NOT reachable from this
    codebase — citing them here would document behavior we never execute.

    Reading ``LiteLlm.generate_content_async`` (stream branch, lines
    1978-2145): intermediate SSE chunks are yielded with ``partial=True``
    explicitly (``TextChunk`` -> ``_message_to_generate_content_response(...,
    is_partial=True, ...)`` at line 2070; ``ReasoningChunk`` -> raw
    ``LlmResponse(..., partial=True, ...)`` at line 2078). The aggregated
    final response (``_finalize_tool_call_response`` at line 1990,
    ``_finalize_text_response`` at line 2019) calls
    ``_message_to_generate_content_response`` WITHOUT ``is_partial``, whose
    default is ``is_partial: bool = False`` (line 1450) -> ``partial=False``
    explicitly on the return statement at line 1489 -> the aggregated
    response is ``partial=False``, and it is populated with the turn's
    ``usage_metadata`` right before being yielded (lines 2138 / 2144). The
    non-streaming branch (``stream=False``) goes through
    ``_model_response_to_generate_content_response`` (line 1394), which also
    calls ``_message_to_generate_content_response`` without ``is_partial`` ->
    ``partial=False`` too. So on the actual code path exercised by this
    repo, every non-partial ``llm_response`` — streaming or not — carries
    the authoritative ``usage_metadata`` for the whole turn, and every
    partial chunk does not. The guard below (skip whenever ``partial`` is
    truthy) is therefore correct and sufficient to record exactly one row
    per model turn.

    Persistence scheduling: ADK always ``await``s an async
    ``after_model_callback`` (see
    ``google.adk.flows.llm_flows.base_llm_flow._handle_after_model_callback``:
    ``if inspect.isawaitable(callback_response): callback_response = await
    callback_response``), so this callback runs inside a live asyncio
    event loop. The DB layer here is native asyncio (``AsyncSession`` /
    ``create_async_engine`` in ``apowerb.helpers.database``) — there is
    no sync code to bridge into a worker thread, unlike
    ``integrations/helpers.py::fetch_integration_configs`` (which spawns a
    ``ThreadPoolExecutor`` + a brand-new event loop specifically to call
    async code from *synchronous* callers). Scheduling the write via
    ``asyncio.create_task`` keeps it off the response's critical path
    (SSE keeps streaming while the INSERT lands) without that overhead.
    The created task is held in the module-level ``_pending_writes`` set
    (removed via its own done-callback) so it cannot be garbage-collected
    before the INSERT completes.
    """

    async def _usage_recorder_callback(*, callback_context, llm_response):
        try:
            if getattr(llm_response, "partial", False):
                return None

            usage = _extract_usage(getattr(llm_response, "usage_metadata", None))
            if usage is None:
                return None

            # Cached-prompt tokens are recorded in llm_usage but ADK does not
            # expose them on its OTel span (only input/output/reasoning). Mirror
            # only the cached count onto the current span so the observability
            # pipeline can compute a cache-hit ratio without reading this
            # private table. We deliberately do NOT re-set input_tokens: ADK
            # already sets it on the generate_content span, and the stats query
            # sums it across all spans — duplicating it here would double-count.
            # Best-effort: telemetry must never break an agent response.
            try:
                from opentelemetry import trace as _otel_trace

                _span = _otel_trace.get_current_span()
                if _span is not None and _span.is_recording():
                    _span.set_attribute(
                        "gen_ai.usage.cached_input_tokens", usage["cached_tokens"]
                    )
            except Exception:  # noqa: BLE001
                pass

            try:
                session_id = callback_context.session.id
            except Exception:
                session_id = None

            # ``invocation_id`` groups the successive model turns of one
            # user request (ReadonlyContext.invocation_id, inherited by
            # CallbackContext). Without it a multi-turn tool loop is
            # indistinguishable from N unrelated one-shot calls -- and
            # that loop is precisely the thing that burns tokens.
            try:
                invocation_id = callback_context.invocation_id
            except Exception:
                invocation_id = None

            try:
                from apowerb.core.invocation_context import resolve_integration_user
                invocation_source = resolve_integration_user(prefer_invoker=True)
            except Exception:
                invocation_source = None

            task = asyncio.create_task(
                _persist_usage_row(
                    agent_id=agent_id,
                    agent_name=agent_name,
                    owner_id=owner_id,
                    session_id=session_id,
                    invocation_id=invocation_id,
                    tool_names=_extract_tool_names(llm_response),
                    invocation_source=invocation_source,
                    model=model_name,
                    # Marks turns paid for by the shared thaink2 key:
                    # only those are capped by the monthly quota.
                    billed_to_thaink2=billed_to_thaink2,
                    input_tokens=usage["input_tokens"],
                    output_tokens=usage["output_tokens"],
                    thoughts_tokens=usage["thoughts_tokens"],
                    cached_tokens=usage["cached_tokens"],
                    total_tokens=usage["total_tokens"],
                )
            )
            _pending_writes.add(task)
            task.add_done_callback(_pending_writes.discard)
        except Exception as exc:
            logger.warning("[USAGE_RECORDER] callback failed: %s", exc)
        return None

    return _usage_recorder_callback
