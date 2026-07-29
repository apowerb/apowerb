"""Guardrail callback factories for agent input/output control."""

import re
from logging import getLogger
from typing import Optional
from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmResponse, LlmRequest
from google.genai import types
from th2agent.configs.paths import uploads_dir

logger = getLogger(__name__)


def create_before_model_callback(config: dict):
    """Create a before_model_callback that validates user input.

    Checks:
    - blocked_terms: list of terms that are not allowed in user messages
    - max_input_length: maximum character length for user messages
    """
    blocked_terms = [t.strip().lower() for t in config.get("blocked_terms", []) if t.strip()]
    max_input_length = config.get("max_input_length")

    if not blocked_terms and not max_input_length:
        return None

    def before_model_callback(
        *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> Optional[LlmResponse]:
        # Get the last user message text
        last_user_text = ""
        if llm_request.contents:
            for content in reversed(llm_request.contents):
                if content.role == "user" and content.parts:
                    for part in content.parts:
                        if part.text:
                            last_user_text = part.text
                            break
                    if last_user_text:
                        break

        if not last_user_text:
            return None

        # Check blocked terms
        if blocked_terms:
            lower_text = last_user_text.lower()
            for term in blocked_terms:
                if term in lower_text:
                    return LlmResponse(
                        content=types.Content(
                            role="model",
                            parts=[types.Part(text=f"I cannot process this request. The term '{term}' is not allowed by the guardrail policy.")],
                        )
                    )

        # Check max input length
        if max_input_length and len(last_user_text) > int(max_input_length):
            return LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=f"Input too long. Maximum allowed length is {max_input_length} characters.")],
                )
            )

        return None

    return before_model_callback


def create_after_model_callback(config: dict):
    """Create an after_model_callback that validates LLM output.

    Checks:
    - blocked_terms: list of terms that should be filtered from output
    - max_output_length: maximum character length for model responses
    """
    blocked_terms = [t.strip().lower() for t in config.get("blocked_terms", []) if t.strip()]
    max_output_length = config.get("max_output_length")

    if not blocked_terms and not max_output_length:
        return None

    def after_model_callback(
        *, callback_context: CallbackContext, llm_response: LlmResponse
    ) -> Optional[LlmResponse]:
        if not llm_response.content or not llm_response.content.parts:
            return None

        response_text = ""
        for part in llm_response.content.parts:
            if part.text:
                response_text += part.text

        if not response_text:
            return None

        # Check blocked terms in output
        if blocked_terms:
            lower_text = response_text.lower()
            for term in blocked_terms:
                if term in lower_text:
                    return LlmResponse(
                        content=types.Content(
                            role="model",
                            parts=[types.Part(text="[Response filtered by guardrail policy: output contained blocked content.]")],
                        )
                    )

        # Check max output length
        if max_output_length and len(response_text) > int(max_output_length):
            truncated = response_text[: int(max_output_length)]
            return LlmResponse(
                content=types.Content(
                    role="model",
                    parts=[types.Part(text=truncated + "\n\n[Response truncated by guardrail policy.]")],
                )
            )

        return None

    return after_model_callback


def create_before_tool_callback(config: dict):
    """Create a before_tool_callback that blocks specified tools.

    Checks:
    - blocked_tools: list of tool names that are not allowed to execute

    ADK invokes agent-level (canonical) tool callbacks as
    ``callback(tool=..., args=..., tool_context=...)`` (cf
    ``google.adk.flows.llm_flows.functions``). The kwarg is ``args``
    — NOT ``tool_args`` (that name belongs to the plugin-manager
    call site, which this callback never goes through). The
    2026-05-19 fix wrongly used ``tool_args`` and crashed every real
    tool call with ``unexpected keyword argument 'args'``.
    """
    blocked_tools = [t.strip().lower() for t in config.get("blocked_tools", []) if t.strip()]

    if not blocked_tools:
        return None

    def before_tool_callback(
        *, tool, args: dict, tool_context
    ) -> Optional[dict]:
        # ``tool`` is the BaseTool instance; ``tool.name`` is the
        # registered name. Fall back to ``str(tool)`` defensively.
        tool_name = getattr(tool, "name", None) or str(tool)
        if tool_name.lower() in blocked_tools:
            return {"error": f"Tool '{tool_name}' is blocked by guardrail policy."}
        return None

    return before_tool_callback


def _sanitize_session_id(sid: str) -> str | None:
    """Only allow safe session_id patterns."""
    if re.match(r"^session_\d+$", sid):
        return sid
    logger.warning("[RAG_CB] Rejected unsafe session_id: %r", sid)
    return None


def create_rag_before_model_callback(agent_name: str):
    """Create a before_model_callback that reads the knowledge map for RAG context.

    Non-blocking version: reads .knowledge_map.json (written by /api/rag/* endpoints)
    instead of synchronously indexing files during the SSE stream.

    The callback dynamically resolves the session_id from the CallbackContext
    at execution time so that per-session knowledge bases are used when
    available, with a fallback to the per-agent knowledge map.
    """
    import os
    import json
    from logging import getLogger
    _logger = getLogger(f"th2agent.rag_callback.{agent_name}")

    def _resolve_session_id(callback_context: CallbackContext) -> Optional[str]:
        """Try to extract the session id from the callback context."""
        # Method 1: direct attribute
        sid = getattr(callback_context, "session_id", None)
        if sid:
            return _sanitize_session_id(sid)
        # Method 2: state dict
        state = getattr(callback_context, "state", None)
        if isinstance(state, dict) and state.get("session_id"):
            return _sanitize_session_id(state["session_id"])
        # Method 3: invocation context -> session object
        try:
            inv_ctx = getattr(callback_context, "_invocation_context", None)
            if inv_ctx is not None:
                session = getattr(inv_ctx, "session", None)
                if session is not None:
                    sid = getattr(session, "id", None)
                    if sid:
                        return _sanitize_session_id(sid)
        except Exception:
            pass
        return None

    def before_model_callback(
        *, callback_context: CallbackContext, llm_request: LlmRequest
    ) -> Optional[LlmResponse]:
        session_id = _resolve_session_id(callback_context)
        scope_id = session_id if session_id else agent_name

        # Try per-session path first, fall back to per-agent if not found
        map_path = str(uploads_dir() / scope_id / ".knowledge_map.json")
        if not os.path.exists(map_path) and scope_id != agent_name:
            # Fallback to agent-level knowledge map
            _logger.debug("[RAG_CB] No session knowledge map at %s, falling back to agent scope", map_path)
            map_path = str(uploads_dir() / agent_name / ".knowledge_map.json")

        if not os.path.exists(map_path):
            _logger.debug("[RAG_CB] No knowledge map: %s", map_path)
            return None

        try:
            with open(map_path, "r") as f:
                knowledge_map = json.load(f)
        except Exception as e:
            _logger.warning("[RAG_CB] Failed to read knowledge map: %s", e)
            return None

        indexed = {
            s["name"]: s["knowledge_id"]
            for s in knowledge_map.get("sources", [])
            if s.get("status") == "complete"
        }
        failed = {
            s["name"]: "indexation failed"
            for s in knowledge_map.get("sources", [])
            if s.get("status") == "error"
        }

        if indexed or failed:
            _inject_context(indexed, failed, llm_request)

        return None

    return before_model_callback


def _inject_context(
    indexed_files: dict, failed_files: dict, llm_request: LlmRequest
) -> None:
    """Insert a [CONTEXT] content block at the start of llm_request.contents."""
    context_lines = []

    if indexed_files:
        context_lines.append("[CONTEXT] Automatically indexed documents:")
        for fname, kid in indexed_files.items():
            context_lines.append(f"  - '{fname}' -> knowledge_id={kid}")
        context_lines.append(
            "Use tool_search_knowledge(knowledge_id=..., query=...) "
            "to answer questions about these documents."
        )

    if failed_files:
        context_lines.append("[WARNING] The following files could NOT be indexed automatically:")
        for fname, err in failed_files.items():
            context_lines.append(f"  - '{fname}': {err}")
        context_lines.append(
            "You can retry manually with tool_create_knowledge(name=filename, "
            "description=..., files=['uploads/<agent_name>/<filename>']). "
            "Otherwise, inform the user that indexing failed and suggest re-uploading."
        )

    if not context_lines:
        return

    context_text = "\n".join(context_lines)
    context_content = types.Content(
        role="user",
        parts=[types.Part(text=context_text)],
    )
    llm_request.contents.insert(0, context_content)
