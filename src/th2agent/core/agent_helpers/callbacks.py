"""ADK callbacks for SCEI sub-agent communication.

Two building blocks:

* :func:`build_validating_state_writer` — an ``after_agent_callback``
  factory that parses the sub-agent's text output as JSON (raw, fenced,
  or trailing in prose), validates against a Pydantic schema, and
  normalizes ``session.state[state_key]`` to a canonical JSON string.
  On failure, writes a structured error sentinel instead of raising —
  so the SequentialAgent doesn't abort mid-pipeline.

* :func:`is_upstream_skip` — a tiny helper a downstream sub-agent's
  ``before_model_callback`` can call to short-circuit when the intake
  marked the AR as "skip".

Why we don't use ADK's ``output_schema`` directly: see
[[project_th2agent_pr173]] and [[feedback_adk_brace_escape_pitfall]].
``LlmAgent(output_schema=...)`` forbids tools; our sub-agents need them.
And ADK's brace interpolation does ``str(value)``, which for a dict
gives Python ``repr``, not JSON — downstream parsing breaks.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

import litellm
from pydantic import BaseModel, ValidationError

from th2agent.helpers.litellm_config import configure_litellm_for_ovhcloud

_logger = logging.getLogger(__name__)
# Lazy module-level imports for pmi_match_gate (top-level so they are patchable in tests)
try:
    from th2agent.tools_store.tools_helpers import load_tool_config_params  # noqa: F401
    from th2agent.tools_store.portfolio.database import make_database_tools  # noqa: F401
except ImportError:
    load_tool_config_params = None  # type: ignore[assignment]
    make_database_tools = None  # type: ignore[assignment]


# Deterministic PDF collect (V2): imported at module level so tests can patch
# them on the callbacks module. Defensive: a missing import must not break the
# gate (it degrades to the previous LLM-tool behaviour).
try:
    from th2agent.core.agent_helpers.pdf_to_images_tool import (
        extract_first_page_text,
    )
    from th2agent.storage.webhook_attachments import resolve_attachment_path
except ImportError:  # pragma: no cover - defensive
    extract_first_page_text = None  # type: ignore[assignment]
    resolve_attachment_path = None  # type: ignore[assignment]



_FENCED_JSON_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
_DECODER = json.JSONDecoder()


def _decode_balanced_objects(text: str) -> list[dict]:
    """Return every balanced JSON *object* (not array) that the text
    contains, in order. Uses ``json.JSONDecoder.raw_decode`` so nesting
    of arbitrary depth is supported (unlike a regex). Skips non-object
    JSON values (arrays, scalars) — we only ship dict-shaped payloads."""
    found: list[dict] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        try:
            obj, end = _DECODER.raw_decode(text, i)
        except json.JSONDecodeError:
            i += 1
            continue
        if isinstance(obj, dict):
            found.append(obj)
        i = end
    return found


def extract_json(text: str) -> dict | None:
    """Pull a JSON object out of LLM-produced text.

    Tries, in order: raw parse, last markdown-fenced block, last
    standalone ``{...}`` object found via balanced decoding (handles
    arbitrary nesting). Returns ``None`` if nothing parsable.
    """
    if not text or not isinstance(text, str):
        return None

    s = text.strip()

    # 1) Raw JSON
    if s.startswith("{") and s.endswith("}"):
        try:
            obj = json.loads(s)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            pass

    # 2) Markdown-fenced — take the LAST valid fenced block (the final
    # payload usually follows example blocks)
    fenced = _FENCED_JSON_RE.findall(text)
    if fenced:
        for candidate in reversed(fenced):
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue

    # 3) Balanced scan — last dict-shaped object anywhere in the text.
    decoded = _decode_balanced_objects(text)
    if not decoded:
        return None
    # Prefer the last NON-empty object: an LLM often shows the schema
    # template ({}) before the populated payload.
    for obj in reversed(decoded):
        if obj:
            return obj
    return decoded[-1]


def _write_error(state, state_key: str, kind: str, **extra: Any) -> None:
    payload: dict[str, Any] = {"__error__": kind}
    payload.update(extra)
    state[state_key] = json.dumps(payload)


# ---------------------------------------------------------------------------
# Levier C — central JSON repair
# ---------------------------------------------------------------------------
# When a sub-agent emits prose or a narrated tool-call instead of the final
# JSON payload, the writer used to drop an __error__ sentinel that poisons the
# downstream pipeline (faux non_rapproché, recorder writing the wrong status).
# Levier C makes ONE litellm repair attempt on the failure path before the
# sentinel: re-ask the SAME model to emit ONLY a JSON object matching the
# schema. It NEVER crashes the pipeline (try/except, single attempt, timeout)
# and falls back to the unchanged sentinel behaviour on any failure.

_REPAIR_TIMEOUT_S = 30
_REPAIR_RAW_PREVIEW = 4000
_REPAIR_SOURCE_PREVIEW = 6000
_PDF_TOOL_NAME = "tool_pdf_first_page"


def _extract_pdf_source_text(callback_context) -> str | None:
    """Return the ``text`` of the most recent ``tool_pdf_first_page``
    function response in the current session, or ``None``.

    Used by the validating state writer so a JSON-repair round-trip can
    re-extract fields DIRECTLY from the source document (e.g. a clean
    6-digit order number) rather than only from the model draft. Never
    raises: any ADK/shape surprise yields ``None`` (repair degrades to
    draft-only, the previous behaviour).

    Priority: the deterministic gate (V2) may have written the first-page
    text into ``state['intake_pdf_text']`` BEFORE the LLM ran. When the LLM
    is fed that text it no longer calls ``tool_pdf_first_page``, so the event
    scan below would find nothing. Read the state key first; fall back to the
    event scan only when it is absent or empty.
    """
    try:
        gate_text = callback_context.state.get("intake_pdf_text")
        if isinstance(gate_text, str) and gate_text.strip():
            return gate_text
    except Exception:  # noqa: BLE001 - state access must never break repair
        pass
    try:
        events = callback_context._invocation_context.session.events
    except Exception:  # noqa: BLE001 — best-effort, never break pipeline
        return None
    if not events:
        return None
    for event in reversed(list(events)):
        try:
            responses = event.get_function_responses()
        except Exception:  # noqa: BLE001
            continue
        for fr in responses or []:
            if getattr(fr, "name", None) != _PDF_TOOL_NAME:
                continue
            resp = getattr(fr, "response", None)
            if isinstance(resp, dict) and resp.get("status") == "success":
                text = resp.get("text")
                if isinstance(text, str) and text.strip():
                    return text
    return None


def _looks_like_tool_call(data: dict) -> bool:
    """True if a parsed JSON object looks like a narrated tool-call rather
    than a schema payload (e.g. ``{"tool_name": ..., "tool_args": {...}}``
    or an OpenAI-style ``{"type": "function", ...}``)."""
    if not isinstance(data, dict):
        return False
    return (
        "tool_name" in data
        or "tool" in data
        or data.get("type") == "function"
    )


async def _attempt_json_repair(
    schema_class: type[BaseModel],
    raw: str,
    repair_model: str,
    repair_api_key: str | None = None,
    repair_api_base: str | None = None,
    source_text: str | None = None,
) -> BaseModel | None:
    """One best-effort litellm round-trip to coerce ``raw`` into a valid
    ``schema_class`` instance. Returns the validated model on success, or
    ``None`` on any failure (extraction, validation, exception, timeout).
    Never raises.

    Auth is propagated EXPLICITLY (not via global env): this mirrors
    ``build_litellm_model`` so the repair call does not depend on
    ``OVHCLOUD_API_KEY`` being present in the process environment (fragile,
    can be overwritten in recursive runs). When ``repair_api_base`` is set we
    force the ``openai/`` provider prefix and pass ``api_base``; otherwise we
    keep the raw provider model name (e.g. ``ovhcloud/...``). In both cases
    ``api_key`` is passed as a keyword argument to ``litellm.acompletion``."""
    try:
        configure_litellm_for_ovhcloud()
        schema_json = json.dumps(schema_class.model_json_schema())
        source_block = ""
        if source_text and source_text.strip():
            source_block = (
                "Voici le TEXTE SOURCE du document :\n"
                f"{source_text[:_REPAIR_SOURCE_PREVIEW]}\n\n"
            )
        extract_hint = (
            " Extrais les champs DEPUIS LE TEXTE SOURCE ci-dessus."
            if source_block else ""
        )
        prompt = (
            f"{source_block}"
            "Voici ta sortie précédente :\n"
            f"{raw[:_REPAIR_RAW_PREVIEW]}\n\n"
            "Elle doit être un UNIQUE objet JSON conforme à ce schéma JSON :\n"
            f"{schema_json}\n\n"
            "Renvoie UNIQUEMENT le JSON valide correspondant, sans prose, "
            f"sans markdown, sans appel d'outil, sans commentaire.{extract_hint}"
        )
        call_kwargs: dict[str, Any] = {
            "messages": [{"role": "user", "content": prompt}],
            "timeout": _REPAIR_TIMEOUT_S,
            "api_key": repair_api_key,
        }
        if repair_api_base:
            # OpenAI-compat endpoint: force the openai/ provider prefix so
            # litellm uses the OpenAI tool-calling path (cf build_litellm_model).
            _model_name = repair_model
            if not _model_name.startswith("openai/"):
                _model_name = "openai/" + _model_name.split("/", 1)[-1]
            call_kwargs["model"] = _model_name
            call_kwargs["api_base"] = repair_api_base
        else:
            call_kwargs["model"] = repair_model
        response = await litellm.acompletion(**call_kwargs)
        repaired_text = response.choices[0].message.content
        data = extract_json(str(repaired_text))
        if data is None or _looks_like_tool_call(data):
            return None
        return schema_class.model_validate(data)
    except Exception as e:  # noqa: BLE001 — repair must never break the pipeline
        _logger.warning(
            "[CALLBACK] %s: JSON repair attempt failed: %s",
            schema_class.__name__,
            e,
        )
        return None


def build_validating_state_writer(
    schema_class: type[BaseModel],
    state_key: str,
    repair_model: str | None = None,
    repair_api_key: str | None = None,
    repair_api_base: str | None = None,
) -> Callable:
    """Return an ADK ``after_agent_callback`` that validates the agent's
    output against ``schema_class`` and normalizes ``state[state_key]``
    to a JSON string.

    When ``repair_model`` is set, a single litellm repair round-trip is
    attempted on the failure path before writing the error sentinel (Levier
    C). ``repair_model=None`` (default) preserves the previous behaviour
    exactly — no litellm call."""

    async def _try_repair_or_sentinel(
        state, raw: Any, kind: str, source_text: str | None = None,
        **sentinel_extra: Any,
    ) -> None:
        """Common failure tail: attempt repair (if a model is configured),
        else write the unchanged ``kind`` sentinel."""
        if repair_model:
            repaired = await _attempt_json_repair(
                schema_class,
                str(raw),
                repair_model,
                repair_api_key=repair_api_key,
                repair_api_base=repair_api_base,
                source_text=source_text,
            )
            if repaired is not None:
                _logger.info(
                    "[CALLBACK] %s: JSON repair recovered a valid payload "
                    "for state[%r] (was %s).",
                    schema_class.__name__,
                    state_key,
                    kind,
                )
                state[state_key] = json.dumps(repaired.model_dump())
                return
        _write_error(state, state_key, kind, **sentinel_extra)

    async def _callback(callback_context) -> None:
        state = callback_context.state
        raw = state.get(state_key)
        # Source document text (if any) so repair can re-extract fields
        # directly from the PDF rather than only from the model draft.
        source_text = (
            _extract_pdf_source_text(callback_context) if repair_model else None
        )

        if not raw:
            _logger.warning(
                "[CALLBACK] %s: state[%r] is empty — agent produced no "
                "final text. Writing missing_output sentinel.",
                schema_class.__name__,
                state_key,
            )
            _write_error(state, state_key, "missing_output")
            return None

        if isinstance(raw, dict):
            # Already a dict — validate directly.
            try:
                obj = schema_class.model_validate(raw)
            except ValidationError as e:
                _logger.warning(
                    "[CALLBACK] %s: validation failed: %s",
                    schema_class.__name__,
                    e,
                )
                await _try_repair_or_sentinel(
                    state, raw, "validation_failed",
                    source_text=source_text, errors=e.errors()
                )
                return None
            state[state_key] = json.dumps(obj.model_dump())
            return None

        # Text input — extract JSON, then validate.
        data = extract_json(str(raw))
        if data is None:
            _logger.warning(
                "[CALLBACK] %s: could not extract JSON from agent output "
                "(state[%r], %d chars).",
                schema_class.__name__,
                state_key,
                len(str(raw)),
            )
            await _try_repair_or_sentinel(
                state, raw, "extract_failed",
                source_text=source_text, raw_text_preview=str(raw)[:200]
            )
            return None

        try:
            obj = schema_class.model_validate(data)
        except ValidationError as e:
            _logger.warning(
                "[CALLBACK] %s: validation failed: %s",
                schema_class.__name__,
                e,
            )
            await _try_repair_or_sentinel(
                state, raw, "validation_failed",
                source_text=source_text, errors=e.errors()
            )
            return None

        state[state_key] = json.dumps(obj.model_dump())
        return None

    _callback.__name__ = f"validate_{schema_class.__name__}_into_{state_key}"
    return _callback


def is_upstream_skip(state, state_key: str) -> bool:
    """True if ``state[state_key]`` is a JSON object that marks an upstream
    skip. Five forms are recognised:
    - ``status == "skip"`` (legacy ARIntakePayload),
    - ``email_classification == "not_ar"`` (SCEIIntakePayload, intake v2),
    - ``__skipped_upstream__ is True`` (cascade from a previous sub-agent),
    - ``status_final == "SKIPPED"`` (ARRecordPayload: recorder skipped the
      insert because the AR is out-of-scope or already recorded).

    Used by downstream sub-agents' ``before_model_callback`` to
    short-circuit the LLM call without billing tokens."""
    if hasattr(state, "get"):
        raw = state.get(state_key)
    elif state_key in state:
        raw = state[state_key]
    else:
        return False

    if not raw:
        return False
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    return (
        data.get("status") in ("skip", "out_of_scope")
        or data.get("email_classification") == "not_ar"
        or data.get("__skipped_upstream__") is True
        or data.get("status_final") == "SKIPPED"
    )


# ---------------------------------------------------------------------------
# build_skip_short_circuit_callback — Phase 2a
# ---------------------------------------------------------------------------


def is_upstream_absent_or_error(state, state_key: str) -> bool:
    """True if ``state[state_key]`` is missing/empty OR carries an
    ``__error__`` sentinel (written by ``build_validating_state_writer``).

    Distinct from :func:`is_upstream_skip`, which only matches an explicit
    ``status == "skip"``. Used by the downstream skip short-circuit so a
    sub-agent NEVER runs its LLM on a missing/garbage upstream payload --
    which would let the model improvise (e.g. hallucinate a PO and persist
    a phantom line). A missing key here does NOT prove a clean filter: it
    can also mean the upstream crashed, so callers should log it."""
    if hasattr(state, "get"):
        raw = state.get(state_key)
    elif state_key in state:
        raw = state[state_key]
    else:
        return True
    if not raw:
        return True
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(data, dict) and "__error__" in data


def _is_pdf_attachment(att) -> bool:
    """True if an attachment dict (from webhook initial_state) is a PDF.
    Matches on declared content_type or a .pdf filename."""
    if not isinstance(att, dict):
        return False
    ct = (att.get("content_type") or "").lower()
    fn = (att.get("filename") or "").lower()
    return ct == "application/pdf" or fn.endswith(".pdf")


# Anti-32k caps on the deterministic PDF text injected into the intake
# prompt (incident debordement 32k). Per-PDF cap bounds a single dense
# document (e.g. CGV); total cap bounds the concatenation of all PDFs.
_INTAKE_PDF_TEXT_PER_PDF_CAP = 4000
_INTAKE_PDF_TEXT_TOTAL_CAP = 8000
_INTAKE_PDF_TEXT_TRUNC_MARKER = " …[tronqué]"




def build_skip_short_circuit_callback(
    upstream_key: str, downstream_output_key: str
):
    """Return an ADK ``before_model_callback`` that short-circuits the
    LLM call when the upstream sub-agent (in a SequentialAgent) returned
    ``status: 'skip'`` or already wrote a ``__skipped_upstream__``
    sentinel.

    The callback:
    1. Reads ``state[upstream_key]`` (the JSON payload written by the
       upstream's ``after_agent_callback``).
    2. If it's a skip, writes a ``__skipped_upstream__`` sentinel into
       ``state[downstream_output_key]`` so the next sub-agent in the
       chain ALSO short-circuits (cascade).
    3. Returns a synthetic ``LlmResponse`` whose ``content.parts[0].text``
       is the same sentinel JSON. ADK treats it as the model's final
       answer — no tokens billed, no tool call.

    Saves the input-token cost of running the downstream sub-agents on
    an AR that intake already filtered out (e.g. subject pre-filter,
    excluded supplier)."""

    async def _cb(callback_context, llm_request=None):
        state = callback_context.state
        skip = is_upstream_skip(state, upstream_key)
        absent_or_error = is_upstream_absent_or_error(state, upstream_key)
        if not (skip or absent_or_error):
            return None

        if skip:
            reason = f"upstream `{upstream_key}` was skip -- short-circuited"
        else:
            _logger.warning(
                "[CALLBACK] skip_short_circuit: upstream=%r absent or errored "
                "-- short-circuiting downstream=%r to avoid the LLM improvising "
                "on empty input. This is NOT a clean skip; inspect the upstream "
                "sub-agent if this was unexpected.",
                upstream_key,
                downstream_output_key,
            )
            reason = (
                f"upstream `{upstream_key}` absent or errored -- "
                "short-circuited to avoid improvising on empty input"
            )
        sentinel = {
            "__skipped_upstream__": True,
            "reason": reason,
        }
        state[downstream_output_key] = json.dumps(sentinel)

        # Lazy import so unit tests don't require the ADK runtime
        try:
            from google.adk.models.llm_response import LlmResponse
            from google.genai.types import Content, Part
        except ImportError:
            _logger.warning(
                "[CALLBACK] ADK not available — skip short-circuit returns "
                "None; downstream will run a wasted LLM call."
            )
            return None

        _logger.info(
            "[CALLBACK] skip_short_circuit: upstream=%r is skip — "
            "short-circuiting downstream=%r",
            upstream_key,
            downstream_output_key,
        )
        return LlmResponse(
            content=Content(
                role="model", parts=[Part(text=json.dumps(sentinel))]
            )
        )

    _cb.__name__ = (
        f"skip_short_circuit_when_{upstream_key}_skipped_"
        f"writes_{downstream_output_key}"
    )
    return _cb



# ---------------------------------------------------------------------------
# create_truncate_history_callback - Levier 1 : cap contexte 32k OVHcloud
# ---------------------------------------------------------------------------


def _content_has_function_response(content) -> bool:
    """True si ce message ADK porte un ``function_response`` (resultat outil).
    En format Gemini/ADK les resultats outil sont des ``parts`` function_response
    dans un message ``role='user'`` (PAS un message ``role='tool'``). Un tel
    message en TETE de fenetre de troncature est un ORPHELIN (son ``function_call``,
    dans un message ``model`` anterieur, a ete coupe) -> a retirer, sinon
    LiteLLM/Gemini leve ``Missing corresponding tool call``."""
    parts = getattr(content, "parts", None) or []
    return any(getattr(p, "function_response", None) is not None for p in parts)


def create_truncate_history_callback(keep_recent: int = 14):
    """Retourne un before_model_callback qui tronque l'historique ADK.

    Conserve : le 1er message user (payload webhook initial) + les
    ``keep_recent`` derniers messages, en eliminant les tool_responses
    orphelins en tete de fenetre (evite l'erreur 400 OVH quand un
    tool_response n'a pas de tool_call associe dans le contexte).

    Comportement exact :
    1. len(contents) <= keep_recent+1 -> return None (rien a faire).
    2. Trouver l'index du 1er message role=="user". Si absent ->
       warning + return None (degradation gracieuse).
    3. Fenetre = les keep_recent derniers messages. Tant que
       window[0].role == "tool" -> drop window[0].
    4. Resultat = [user_initial] + window, en evitant le doublon par
       INDEX si user_initial est deja dans la fenetre.
    5. Reassigne llm_request.contents = result. return None.

    Note sur le strip-images existant (history_compaction.py) :
    Il remplace des valeurs base64 par des placeholders SANS supprimer
    de messages entiers. L'ordre des messages est donc intact. Les deux
    callbacks peuvent coexister sans conflit d'indices.
    """
    import logging as _logging
    import os
    _logger = _logging.getLogger("th2agent.truncate_history")

    def before_model_callback(*, callback_context, llm_request):
        contents = getattr(llm_request, "contents", None)
        if not contents:
            return None

        total = len(contents)
        if total <= keep_recent + 1:
            return None  # rien a tronquer

        # Trouver le 1er message user
        user_initial_idx = None
        for i, msg in enumerate(contents):
            if getattr(msg, "role", None) == "user":
                user_initial_idx = i
                break

        if user_initial_idx is None:
            _logger.warning(
                "[TRUNCATE_HISTORY] no user message found in %d contents "
                "-- skipping truncation (graceful degradation)",
                total,
            )
            return None

        user_initial = contents[user_initial_idx]

        # Fenetre = les keep_recent derniers messages
        window = list(contents[-keep_recent:])

        # Eliminer les tool_responses orphelins en tete de fenetre
        # Retire les resultats outil orphelins en tete de fenetre. Couvre le
        # format OpenAI (role tool) ET le format Gemini/ADK (function_response
        # dans un message role user), sinon la troncature coupe entre un
        # function_call et son function_response -> Missing corresponding tool call.
        while window and (
            getattr(window[0], "role", None) == "tool"
            or _content_has_function_response(window[0])
        ):
            window.pop(0)

        # Construire le resultat : user_initial + fenetre (sans doublon)
        # Utiliser l'index plutot que l'identite objet (evite les faux
        # positifs / negatifs si les objets ADK overrident __eq__).
        if user_initial_idx >= total - keep_recent:
            result = window
        else:
            result = [user_initial] + window

        _logger.info(
            "[TRUNCATE_HISTORY] truncated %d -> %d messages (keep_recent=%d)",
            total,
            len(result),
            keep_recent,
        )

        # --- TOKEN_DEBUG v2 instrumentation (zero overhead quand desactive) ---
        if os.environ.get("TH2_TOKEN_DEBUG") == "1":
            try:
                import json as _json

                def _part_chars(part):
                    fc = getattr(part, "function_call", None)
                    if fc is not None:
                        size = len(_json.dumps(
                            {
                                "name": getattr(fc, "name", ""),
                                "args": dict(getattr(fc, "args", None) or {}),
                            },
                            default=str,
                            ensure_ascii=False,
                        ))
                        return 0, size, 0, "function_call"
                    fr = getattr(part, "function_response", None)
                    if fr is not None:
                        size = len(_json.dumps(
                            dict(getattr(fr, "response", None) or {}),
                            default=str,
                            ensure_ascii=False,
                        ))
                        return 0, 0, size, "function_response"
                    text = getattr(part, "text", None)
                    if text is not None:
                        return len(text), 0, 0, "text"
                    return len(str(part)), 0, 0, "text"

                per_message = []
                biggest = {"chars": -1, "msg_index": 0, "role": "?", "kind": "text"}

                for msg_idx, msg in enumerate(result):
                    role = getattr(msg, "role", "?")
                    parts = getattr(msg, "parts", None) or []
                    text_c = fc_c = fr_c = 0
                    for part in parts:
                        tc, fcc, frc, kind = _part_chars(part)
                        text_c += tc
                        fc_c += fcc
                        fr_c += frc
                        part_size = tc + fcc + frc
                        if part_size > biggest["chars"]:
                            biggest = {
                                "msg_index": msg_idx,
                                "role": role,
                                "kind": kind,
                                "chars": part_size,
                            }
                    per_message.append({
                        "role": role,
                        "text_chars": text_c,
                        "fc_chars": fc_c,
                        "fr_chars": fr_c,
                        "total_chars": text_c + fc_c + fr_c,
                    })

                grand_total = sum(m["total_chars"] for m in per_message)

                config = getattr(llm_request, "config", None)
                si_chars = -1
                if config is not None:
                    si = getattr(config, "system_instruction", None)
                    if si is not None:
                        if isinstance(si, str):
                            si_chars = len(si)
                        elif hasattr(si, "parts"):
                            si_chars = sum(
                                len(getattr(p, "text", None) or "")
                                for p in (si.parts or [])
                            )
                        else:
                            si_chars = len(str(si))

                tools_chars = -1
                if config is not None:
                    tools = getattr(config, "tools", None)
                    if tools is not None:
                        try:
                            tools_chars = sum(
                                len(_json.dumps(
                                    t.model_dump() if hasattr(t, "model_dump") else dict(t),
                                    default=str,
                                    ensure_ascii=False,
                                ))
                                for t in tools
                            )
                        except Exception:
                            tools_chars = len(str(tools))

                if biggest["chars"] < 0:
                    biggest = {"msg_index": 0, "role": "?", "kind": "text", "chars": 0}

                summary = {
                    "agent": getattr(callback_context, "agent_name", "?"),
                    "n_messages": len(result),
                    "grand_total_chars": grand_total,
                    "system_instruction_chars": si_chars,
                    "tools_chars": tools_chars,
                    "biggest_part": biggest,
                    "per_message": per_message,
                }
                _logger.info("[TOKEN_DEBUG] %s", _json.dumps(summary, ensure_ascii=False))
            except Exception:
                pass
        # --- fin TOKEN_DEBUG v2 ---

        llm_request.contents = result
        return None

    before_model_callback.__name__ = f"truncate_history_keep_{keep_recent}"
    return before_model_callback


# ---------------------------------------------------------------------------
# build_pmi_match_gate_callback — deterministic PMI match gate (matcher)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# build_supplier_mismatch_gate_callback — deterministic supplier mismatch gate
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# _sender_matches_excluded — pure matching logic (testable, no I/O)
# ---------------------------------------------------------------------------




# ---------------------------------------------------------------------------
# build_excluded_supplier_gate_callback — deterministic excluded-supplier gate
# ---------------------------------------------------------------------------




