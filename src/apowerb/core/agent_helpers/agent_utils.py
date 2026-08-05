"""Agent utilities: details, model params, notifications, and the main ``to_agent`` builder."""
from __future__ import annotations

import json
import os

from google.adk.agents import LlmAgent, SequentialAgent, ParallelAgent, LoopAgent
from google.adk.tools import load_memory

from apowerb.configs.th2logger import setup_logging
from apowerb.agent_store.agent_manager import AgentStore
from apowerb.core.agent_helpers.default_llm import (
    is_default_llm_model,
    resolve_model_credentials,
)
from apowerb.helpers.encryptor import decrypt_value_in_dict, dict_to_envvar
from apowerb.tools_store.tools_helpers import load_agent_tools_functions
from apowerb.core.guardrails import (
    create_before_model_callback,
    create_after_model_callback,
    create_before_tool_callback,
    create_rag_before_model_callback,
)
from apowerb.core.history_compaction import (
    create_strip_large_payloads_callback,
)
from apowerb.core.agent_helpers.callbacks import (
    create_truncate_history_callback,
)
from apowerb.core.agent_helpers.text_utils import _escape_template_vars
from apowerb.core.agent_helpers.mcp_loader import (
    build_mcp_instruction_preamble,
    load_mcp_servers,
)
from apowerb.core.agent_helpers.llm_model_builder import (
    build_litellm_model,
    apply_output_schema,
)
from apowerb.core.agent_helpers.tools_binder import (
    is_rag_template,
    bind_read_uploaded_file,
    bind_create_downloadable_file,
    bind_pdf_to_images,
    bind_pdf_first_page,
    bind_get_backlog_status,
    dedupe_tools_by_name,
    rebind_upload_file,
    rebind_visualization,
    rebind_text_to_sql,
    rebind_database,
)
from apowerb.core.agent_helpers.extras_loader import (
    load_agent_skills_toolset,
    load_superagent_recommended_tools,
    inject_bi_dashboard_tools,
)
from apowerb.core.agent_helpers.chat_action_tools import (
    INTERACTIVE_UI_INSTRUCTION,
    confirm_destructive,
    embed_chart,
    propose_agent_upgrade,
    propose_artifact_edit,
    request_file_from_user,
    request_location,
    request_payment,
    request_user_input,
    schedule_followup,
)


logger = setup_logging(__name__)
agent_store = AgentStore()

# ---------------------------------------------------------------------------
# Chat action-card tool suppression for structured-output (pipeline) agents
# ---------------------------------------------------------------------------

_CHAT_ACTION_CARD_TOOL_NAMES: frozenset[str] = frozenset({
    "request_user_input",
    "confirm_destructive",
    "request_payment",
    "schedule_followup",
    "propose_artifact_edit",
})


def _should_inject_chat_action_tools(agent_details: dict) -> bool:
    """Return True iff this agent should receive chat action-card tools.

    Agents with a non-empty ``output_schema_name`` are fully-automatic pipeline
    agents (webhook, no human in the loop). They must NOT receive
    ``request_user_input`` and friends, nor the ``INTERACTIVE_UI_INSTRUCTION``
    that pushes the LLM to call them -- doing so caused the SCEI intake (Qwen)
    to emit ``request_user_input`` instead of classifying/extracting, blocking
    the whole pipeline (incident 2026-05-22).

    Chat agents (``output_schema_name`` None or absent) keep all tools.
    """
    return not bool(agent_details.get("output_schema_name"))


def _should_inject_artifact_tool(agent_details: dict) -> bool:
    """Return True iff this agent should receive the artifact-saving tool.

    The agent editor exposes a "Code Artifacts" switch stored as
    ``artifacts_enabled``. That column was written, read back, cloned and
    seeded -- but nothing ever turned it into a tool, so an agent with the
    switch on behaved exactly like one with it off. Measured on 2026-08-05:
    0 of 184 agents carried the tool, and a live demo failed because it had to
    be added by hand.

    The value arrives either as a bool (API schema) or as the strings
    "true"/"false" (the DB column is VARCHAR), so both shapes are honoured.

    Structured-output agents are excluded for the same reason as the chat
    action-card tools above: they must emit their JSON, not a tool call.
    """
    if not _should_inject_chat_action_tools(agent_details):
        return False

    value = agent_details.get("artifacts_enabled")
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return bool(value)



def get_agent_details(agent_id: int, items_to_select: str = "*") -> dict:
    """Get a specific agent by name from the agent store."""
    select_query = agent_store.agent_table.select().where(
        agent_store.agent_table.c.agent_id == agent_id
    )
    result = agent_store.get_list_agents(select_query)
    agents = [u._asdict() for u in result]
    if agents:
        return agents[0]
    return {}


def load_agent_model_params(agent_id: int) -> tuple[str, dict]:
    """(modele, params) effectifs de l'agent, cle dechiffree.

    Point de passage commun des appels LLM hors ADK (titre de session,
    text-to-sql, brouillons de prospection) : c'est ici qu'on substitue les
    credentials du modele thaink2 par defaut, sinon ces chemins tomberaient
    sur une cle absente pour un agent « default ».
    """
    agent_details = get_agent_details(agent_id=agent_id)
    agent_model = agent_details.get("agent_model")
    agent_model_params = agent_details.get("agent_model_params")
    if agent_model_params:
        agent_model_params = json.loads(agent_model_params)  # type: ignore
        agent_model_params = decrypt_value_in_dict(
            agent_model_params, values_to_decrypt=["model_api_key"]
        )
    else:
        agent_model_params = {}
    return resolve_model_credentials(agent_model, agent_model_params)


def set_model_params_as_envvar(agent_name: str):
    agent_id = int(agent_name.replace("agent", ""))
    agent_model_name, model_params = load_agent_model_params(agent_id)
    model_provider = agent_model_name.split("/")[0]
    if model_params.get("model_api_key") is None:
        return "nothing to declare as a environnemnt variable"
    env_dict = {}
    if model_provider in ["anthropic", "claude"]:
        env_dict = {"ANTHROPIC_API_KEY": model_params.get("model_api_key")}
    elif model_provider in ["ovhcloud"]:
        env_dict = {"OVHCLOUD_API_KEY": model_params.get("model_api_key")}
    elif model_provider in ["mistral"]:
        api_base = (
            model_params.get("model_api_base")
            or "https://mistral-small-3-2-24b-instruct-2506.endpoints.kepler.ai.cloud.ovh.net/api/openai_compat/v1"
        )
        env_dict = {
            "MISTRAL_API_KEY": model_params.get("model_api_key"),
            "MISTRAL_API_BASE": api_base,
        }
    dict_to_envvar(env_dict)
    return {"status": "success"}


def notify_user(message: str = "Task completed", title: str = "") -> dict:
    """Send a browser notification to the user. Call this tool when you have completed a long-running task
    and want to alert the user who may be doing something else while waiting.

    The notification will appear as a browser notification on the user's device.

    Args:
        message: The notification message to display to the user.
        title: Optional title for the notification. If empty, defaults to the agent name.

    Returns:
        dict confirming the notification was sent.
    """
    return {
        "status": "success",
        "message": message,
        "title": title,
        "_notification": True,
    }


VALID_INTEGRATION_PROVIDERS = {
    "google_drive", "google_gmail", "google_calendar", "google_sheets", "google_docs",
    "microsoft_outlook", "microsoft_teams", "microsoft_onedrive", "microsoft_sharepoint",
    "github", "odoo",
}


def request_integration(provider: str, reason: str = "") -> dict:
    """Propose a connection button to the user for a missing or expired integration.

    **WHEN TO CALL THIS TOOL** — only when a previous tool result carried
    ``"status": "integration_status"`` with a ``"code"`` of either:
      - ``INTEGRATION_MISSING`` — the user has never connected.
      - ``INTEGRATION_EXPIRED`` — the user must reconnect.

    **WHEN NOT TO CALL THIS TOOL**:
      - On ``INTEGRATION_ERROR`` (transient API failure, scope mismatch,
        server-side outage). Reconnecting will not help — just report the
        error to the user.
      - On any other tool error (timeouts, validation, network…). These
        have ``"status": "error"`` without an ``integration_status`` flag.
      - "Just to be safe" before trying a tool — let the tool fail first
        and react to its structured status; otherwise you will spam the
        user with reconnect prompts when their integration was fine.

    Args:
        provider: The integration provider key. Must be one of: google_drive, google_gmail,
                  google_calendar, google_sheets, google_docs, microsoft_outlook,
                  microsoft_teams, microsoft_onedrive, microsoft_sharepoint, github, odoo.
        reason: Why this integration is needed (displayed to the user in the connect card).

    Returns:
        dict with integration request details for the frontend to render a connect button.
    """
    if provider not in VALID_INTEGRATION_PROVIDERS:
        return {
            "status": "error",
            "message": f"Unknown provider: {provider}. Valid: {', '.join(sorted(VALID_INTEGRATION_PROVIDERS))}",
        }
    return {
        "status": "integration_required",
        "provider": provider,
        "reason": reason,
        "_integration_request": True,
    }


# Mapping: tool module prefix → (integration provider in DB, env var name)
_GOOGLE_TOOL_PROVIDER_MAP = {
    "google_gmail":    ("google_gmail",    "GOOGLE_GMAIL_REFRESH_TOKEN"),
    "google_drive":    ("google_drive",    "GOOGLE_DRIVE_REFRESH_TOKEN"),
    "google_calendar": ("google_calendar", "GOOGLE_CALENDAR_REFRESH_TOKEN"),
    "google_sheets":   ("google_sheets",   "GOOGLE_SHEETS_REFRESH_TOKEN"),
    "google_docs":     ("google_docs",     "GOOGLE_DOCS_REFRESH_TOKEN"),
}


def _inject_google_integration_tokens(tools_names: list[str], owner_id: str | None) -> None:
    """Inject Google refresh tokens from the integrations table into env vars.

    Uses the same ``fetch_integration_configs()`` helper as Outlook tools —
    queries via the async ORM engine so schema resolution is automatic.
    """
    if not owner_id:
        logger.warning("[TO_AGENT] Google token injection skipped: no owner_id")
        return

    # Detect which Google services are needed based on loaded tools
    needed_services: set[str] = set()
    for tool_name in tools_names:
        for prefix in _GOOGLE_TOOL_PROVIDER_MAP:
            if tool_name.startswith(prefix + ".") or tool_name.startswith(prefix + "_"):
                needed_services.add(prefix)
                break

    if not needed_services:
        return

    from apowerb.integrations.helpers import fetch_integration_configs

    for service_prefix in needed_services:
        provider, env_var = _GOOGLE_TOOL_PROVIDER_MAP[service_prefix]
        try:
            configs = fetch_integration_configs(provider)
            refresh_token = configs.get("refresh_token")
            if refresh_token:
                os.environ[env_var] = refresh_token
                logger.info(f"[TO_AGENT] Injected {env_var} from integration '{provider}' for owner={owner_id}")
            else:
                logger.warning(f"[TO_AGENT] {provider} integration found but refresh_token is empty for owner={owner_id}")
        except Exception as e:
            logger.warning(f"[TO_AGENT] Could not load {provider} integration for owner={owner_id}: {e}")




def _lookup_output_schema(name: str):
    """Resolve a Pydantic schema class by name from the extension registry
    (populated by client overlays, e.g. SCEI). Returns None if unknown."""
    return _ext_registry.schemas().get(name)














# Extension registry consumed below (the SCEI overlay's init_overlay
# registers the gate appliers + tool rebinders; the core only consumes).
from apowerb.core.extensions.registry import registry as _ext_registry  # noqa: E402


def to_agent(agent_name: str) -> LlmAgent:
    """Convert the schema to an Agent instance."""
    logger.info(f"[TO_AGENT] Loading agent: {agent_name}")

    # Parse tools from string to list of tool paths, then load actual functions
    agent_id = int(agent_name.replace("agent", ""))
    logger.info(f"[TO_AGENT] Fetching agent details for ID: {agent_id}")
    agent_details = get_agent_details(agent_id=agent_id)

    os.environ["ROOT_AGENT_ID"] = str(agent_id)  # Set AGENT_ID for tools that need it
    owner_id = agent_details.get("owner_id") or "unknown_user"
    os.environ["AGENT_OWNER"] = owner_id  # Set AGENT_OWNER for tools that need it
    os.environ["AGENT_ORGANIZATION_ID"] = agent_details.get(
        "organization_id", "default"
    )  # Set AGENT_ORGANIZATION_ID for BI tools
    os.environ["AGENT_PROJECT_ID"] = agent_details.get(
        "project_id", "thaink2"
    )  # Set AGENT_PROJECT_ID for BI tools

    tools_ids_raw = agent_details.get("agent_tools")
    logger.info(f"[TO_AGENT] Raw agent_tools from DB: {tools_ids_raw}")
    from apowerb.core.agent_main import _parse_string_list
    tools_ids = _parse_string_list(tools_ids_raw)
    logger.info(f"[TO_AGENT] Parsed agent_tools: {tools_ids}")

    if not tools_ids:
        logger.info(f"[TO_AGENT] No tools configured for {agent_name}")

    logger.info(f"[TO_AGENT] Loading {len(tools_ids)} tool(s) for {agent_name}")
    tools_names, tools_funcs = load_agent_tools_functions(
        tools=tools_ids, owner_id=owner_id
    )
    logger.info(f"[TO_AGENT] Loaded tools: {tools_names}")

    # Feature: MCP Servers — dynamically instantiate McpToolset from agent config
    load_mcp_servers(agent_details.get("mcp_servers"), tools_funcs)

    # ── Skills ──────────────────────────────────────────────────────────
    load_agent_skills_toolset(agent_name, agent_details.get("agent_skills"), tools_funcs)

    # Google integration tokens are now resolved lazily by google_auth
    # against the request-bound invoker — no longer pre-injected here.
    # See apowerb.tools_store.portfolio.google_auth._ensure_integration_tokens
    # and apowerb.core.invocation_context.

    # Feature: SuperAgent template — resolve recommended_tools at runtime
    load_superagent_recommended_tools(agent_details, tools_names, tools_funcs, owner_id)
    superagent_template_id = agent_details.get("superagent_template_id")

    # Auto-inject BI dashboard tools when AGENT_DASHBOARD_ID is set
    inject_bi_dashboard_tools(agent_name, tools_names, tools_funcs, owner_id)

    # load sub agents
    # Save parent env vars before recursive to_agent() calls overwrite them.
    # Sub-agent loading calls to_agent() recursively which overwrites global
    # os.environ with the sub-agent's owner credentials.
    _ENVVARS_TO_SAVE = [
        "ROOT_AGENT_ID",
        "AGENT_OWNER",
        "AGENT_ORGANIZATION_ID",
        "AGENT_PROJECT_ID",
        # Model API keys (overwritten by set_model_params_as_envvar in recursion)
        "ANTHROPIC_API_KEY",
        "OVHCLOUD_API_KEY",
        "MISTRAL_API_KEY",
        "MISTRAL_API_BASE",
        # Outlook/Microsoft token (overwritten by SuperAgent dict_to_envvar)
        "OUTLOOK_REFRESH_TOKEN",
        # DB vars — prevent sub-agent loading from corrupting parent's DB config
        "DB_HOST", "DB_PORT", "DB_NAME", "DB_USER", "DB_PASSWORD",
        "DB_TYPE", "DB_SCHEMA", "DB_INCLUDE_TABLES",
    ] + [env_var for _, env_var in _GOOGLE_TOOL_PROVIDER_MAP.values()]
    _saved_env = {k: os.environ.get(k) for k in _ENVVARS_TO_SAVE}

    sub_agents_raw = agent_details.get("sub_agents")
    sub_agents_list = _parse_string_list(sub_agents_raw)
    sub_agents = []
    if sub_agents_list:
        logger.info(f"[TO_AGENT] Loading {len(sub_agents_list)} sub-agent(s)")
        for sub_agent_str in sub_agents_list:
            adk_sub_agent = to_agent(agent_name=sub_agent_str)
            sub_agents.append(adk_sub_agent)

    # Restore parent agent's env vars after sub-agent loading
    for _key, _val in _saved_env.items():
        if _val is not None:
            os.environ[_key] = _val
        else:
            os.environ.pop(_key, None)
    logger.info(f"[TO_AGENT] Restored env vars for parent agent {agent_name} (owner={_saved_env.get('AGENT_OWNER')})")

    # load and set agent parameters as env variables
    logger.info(f"[TO_AGENT] Setting model params as env vars for {agent_name}")
    set_model_params_as_envvar(agent_name=agent_name)

    # Extract temperature from agent_model_params (if configured)
    _temperature = None
    _raw_model_params = agent_details.get("agent_model_params")
    if _raw_model_params:
        try:
            _parsed_params = (
                json.loads(_raw_model_params)
                if isinstance(_raw_model_params, str)
                else _raw_model_params
            )
            if "temperature" in _parsed_params:
                _temperature = float(_parsed_params["temperature"])
                logger.info(f"[TO_AGENT] Temperature configured: {_temperature}")
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    agent_type = agent_details.get("agent_type", "base").lower()
    logger.info(f"[TO_AGENT] Creating agent with type: {agent_type}")

    if agent_type == "sequential":
        agent = SequentialAgent(
            name=agent_name,
            description=agent_details["agent_description"],
            sub_agents=sub_agents,
        )
    elif agent_type == "parallel":
        agent = ParallelAgent(
            name=agent_name,
            description=agent_details["agent_description"],
            sub_agents=sub_agents,
        )
    elif agent_type == "loop":
        # Loop agent: wraps sub-agents using ADK LoopAgent.
        #
        # Both fixed and conditional modes use LoopAgent with max_iterations.
        # - Fixed mode (no exit instruction): runs exactly loop_max_iterations times.
        # - Conditional mode (has exit instruction): JS workflowRunner evaluates the
        #   LLM exit condition between iterations on the workflow path and stops early.
        #   IMPORTANT: the evaluator calls a BASE sub-agent for the OUI/NON question,
        #   NOT this LoopAgent, so evaluation never triggers extra tool calls.
        #   On the chat path, max_iterations is the hard cap (same value as fixed).
        HARD_LIMIT = 100
        loop_max_iter = agent_details.get("loop_max_iterations")
        loop_exit_instr = agent_details.get("loop_exit_instruction")

        # Same default (3) for both modes — users set this explicitly.
        max_iter = min(int(loop_max_iter) if loop_max_iter else 3, HARD_LIMIT)

        if loop_exit_instr and str(loop_exit_instr).strip():
            logger.info(
                f"[TO_AGENT] Loop agent (conditional mode) max_iterations={max_iter}"
            )
        else:
            logger.info(
                f"[TO_AGENT] Loop agent (fixed mode) max_iterations={max_iter}"
            )

        agent = LoopAgent(
            name=agent_name,
            description=agent_details["agent_description"],
            sub_agents=sub_agents,
            max_iterations=max_iter,
        )
    else:
        # Default to LlmAgent (Base / Router)
        instruction = agent_details.get("agent_instruction", "")

        # Feature: Router - auto-generate routing instructions
        if agent_type == "router" and sub_agents:
            routing_lines = [
                "\n\nYou are a routing agent. Based on the user's request, transfer to the most appropriate sub-agent.",
                "Available sub-agents:",
            ]
            for sa in sub_agents:
                routing_lines.append(f"- {sa.name}: {sa.description}")
            routing_lines.append(
                "\nAnalyze the user's request and transfer to the best matching sub-agent."
            )
            instruction += "\n".join(routing_lines)

        # Feature: Memory - add load_memory tool
        memory_enabled = agent_details.get("memory_enabled") or "false"
        if isinstance(memory_enabled, str):
            memory_enabled = memory_enabled.lower() == "true"
        if memory_enabled:
            tools_funcs.append(load_memory)
            logger.info(
                f"[TO_AGENT] Memory enabled for {agent_name}, added load_memory tool"
            )

        # Auto-add file reading tool — skip for RAG agents (they use the RAG API pipeline)
        _is_rag_template = is_rag_template(superagent_template_id)
        tools_funcs = bind_read_uploaded_file(agent_name, tools_funcs, _is_rag_template)

        # Auto-add create_downloadable_file so the agent can generate files for download
        bind_create_downloadable_file(agent_name, tools_funcs)

        # Auto-add pdf_to_images so the agent can render uploaded PDFs to
        # base64 PNGs for vision-LLM analysis (scanned ARs, complex layouts...)
        tools_funcs = bind_pdf_to_images(agent_name, tools_funcs)
        tools_funcs = bind_pdf_first_page(agent_name, tools_funcs)

        # Bind tool_get_webhook_backlog_status to the integer agent id so
        # the agent (when invoked by a webhook) can report how many
        # notifications are still queued behind the row it just handled.
        # The bind is a no-op when the agent did not opt into the tool.
        tools_funcs = bind_get_backlog_status(agent_name, tools_funcs)

        # Auto-rebind tool_upload_file to the agent folder (replaces unbound version)
        tools_funcs = rebind_upload_file(agent_name, tools_funcs)

        # Auto-rebind visualization tools to the agent's upload folder
        tools_funcs = rebind_visualization(agent_name, tools_funcs)

        # Auto-rebind text-to-SQL tools to the agent's LLM config
        tools_funcs = rebind_text_to_sql(
            agent_name, tools_ids, tools_funcs, owner_id=owner_id
        )

        # Auto-rebind database tools to agent-specific DB credentials
        tools_funcs = rebind_database(
            agent_name, tools_ids, tools_funcs, owner_id=owner_id
        )

        # Rebind client-overlay tools (e.g. SCEI persist/mail) to bound
        # MSSQL db_params via the extension registry — same per-request
        # rebinders, applied in registration order (persist, mail).
        for _tool_name, _rebinder in _ext_registry.tool_rebinders().items():
            tools_funcs = _rebinder(agent_name, tools_ids, tools_funcs, owner_id)

        # Auto-add shared runtime tools. Vertex AI / Gemini rejects duplicate
        # function declarations, so we dedup by function __name__ against any
        # tool that was already loaded from the agent's own config.
        from apowerb.tools_store.portfolio.business_intelligence import (
            tool_get_dashboard_data,
        )

        _existing_names = {
            getattr(fn, "__name__", "") for fn in tools_funcs
        }

        def _add_auto_tool(fn):
            name = getattr(fn, "__name__", "")
            if name and name in _existing_names:
                logger.debug(
                    f"[TO_AGENT] skipping auto-add of {name!r} for {agent_name} "
                    f"(already present in agent_tools)"
                )
                return
            tools_funcs.append(fn)
            if name:
                _existing_names.add(name)
                logger.info(f"[TO_AGENT] {name} tool added for {agent_name}")

        _add_auto_tool(notify_user)
        _add_auto_tool(request_integration)
        # Dashboard reader: lets every agent answer questions about the BI
        # dashboard it is linked to (mini chat sets AGENT_DASHBOARD_ID).
        _add_auto_tool(tool_get_dashboard_data)
        # Chat action-card tools -- only for chat agents (output_schema_name absent).
        # Structured-output pipeline agents (SCEI intake/matcher/recorder/notifier)
        # must not receive these tools: they cause the LLM to emit
        # request_user_input instead of producing the expected JSON output.
        if _should_inject_chat_action_tools(agent_details):
            _add_auto_tool(request_user_input)
            _add_auto_tool(confirm_destructive)
            _add_auto_tool(request_payment)
            _add_auto_tool(schedule_followup)
            _add_auto_tool(propose_artifact_edit)
            logger.info(
                "[TO_AGENT] %s: chat agent -> action-card tools added",
                agent_name,
            )
        else:
            logger.info(
                "[TO_AGENT] %s: structured-output agent -> "
                "chat action-card tools + interactive instruction suppressed",
                agent_name,
            )
        # The "Code Artifacts" switch, finally wired: with it on, the agent
        # gets the saving tool without anyone having to add it by hand.
        if _should_inject_artifact_tool(agent_details):
            from apowerb.tools_store.portfolio.artifacts import (
                tool_save_code_artifact,
            )

            _add_auto_tool(tool_save_code_artifact)

        _add_auto_tool(request_file_from_user)
        _add_auto_tool(propose_agent_upgrade)
        _add_auto_tool(embed_chart)
        _add_auto_tool(request_location)

        tools_funcs = dedupe_tools_by_name(tools_funcs, agent_name=agent_name)

        # Feature: Guardrails - create callbacks from config
        before_model_cb = None
        after_model_cb = None
        before_tool_cb = None
        guardrails_config_str = agent_details.get("guardrails_config")
        if guardrails_config_str and guardrails_config_str not in ("null", "None", ""):
            try:
                guardrails_config = (
                    json.loads(guardrails_config_str)
                    if isinstance(guardrails_config_str, str)
                    else guardrails_config_str
                )
                before_model_cb = create_before_model_callback(guardrails_config)
                after_model_cb = create_after_model_callback(guardrails_config)
                before_tool_cb = create_before_tool_callback(guardrails_config)
                logger.info(f"[TO_AGENT] Guardrails configured for {agent_name}")
            except (json.JSONDecodeError, Exception) as e:
                logger.warning(
                    f"[TO_AGENT] Failed to parse guardrails config for {agent_name}: {e}"
                )

        # Feature: RAG auto-indexing callback — if template has "rag" tag
        if superagent_template_id:
            from apowerb.core.superagents import get_superagent_template as _get_tpl

            _tpl = _get_tpl(superagent_template_id)
            if _tpl and "rag" in _tpl.get("tags", []):
                rag_before_cb = create_rag_before_model_callback(agent_name)
                if before_model_cb:
                    existing_cb = before_model_cb

                    def chained_before_model_cb(
                        *, callback_context, llm_request,
                        _rag=rag_before_cb, _existing=existing_cb
                    ):
                        rag_result = _rag(callback_context=callback_context, llm_request=llm_request)
                        if rag_result is not None:
                            return rag_result
                        return _existing(callback_context=callback_context, llm_request=llm_request)

                    before_model_cb = chained_before_model_cb
                else:
                    before_model_cb = rag_before_cb
                logger.info(
                    f"[TO_AGENT] RAG auto-indexing callback attached for {agent_name}"
                )

        # Feature: history compaction — strip large base64 payloads (e.g.
        # ``tool_pdf_to_images`` outputs) from older turns so the LLM
        # doesn't pay for re-sending the same image on every turn.
        # Always attached: cheap no-op when no base64 in history (overhead
        # linear in history length). Required for SequentialAgent pipelines
        # where downstream sub-agents inherit history blobs from upstream
        # sub-agents that called pdf_to_images — they don't have the tool
        # themselves, so a tool-presence check would skip the strip and
        # let Gemini exceed its context window on the next sub-agent turn.
        # Levier 1 : troncature historique (adapte au contexte 32k OVHcloud)
        # Chaine AVANT le strip-images : la troncature reduit le nombre de
        # messages, le strip nettoie ensuite les base64 dans les messages restants.
        truncate_cb = create_truncate_history_callback(keep_recent=14)
        if before_model_cb:
            _existing_for_truncate = before_model_cb

            def chained_with_truncate(
                *, callback_context, llm_request,
                _trunc=truncate_cb, _ex=_existing_for_truncate
            ):
                _trunc(callback_context=callback_context, llm_request=llm_request)
                return _ex(callback_context=callback_context, llm_request=llm_request)

            before_model_cb = chained_with_truncate
        else:
            def _truncate_only(*, callback_context, llm_request, _trunc=truncate_cb):
                return _trunc(callback_context=callback_context, llm_request=llm_request)
            before_model_cb = _truncate_only

        strip_cb = create_strip_large_payloads_callback(agent_name)
        if before_model_cb:
            existing_cb2 = before_model_cb

            def chained_with_strip(
                *, callback_context, llm_request,
                _strip=strip_cb, _existing=existing_cb2
            ):
                # Truncate EN PREMIER (levier principal) : reduit l'historique.
                # Strip ensuite : nettoie les base64 dans les messages restants.
                # Aucun des deux ne court-circuite l'autre (les deux retournent None).
                _existing(callback_context=callback_context, llm_request=llm_request)
                _strip(callback_context=callback_context, llm_request=llm_request)
                return None

            before_model_cb = chained_with_strip
        else:
            before_model_cb = strip_cb
        logger.info(
            f"[TO_AGENT] history compaction (strip large payloads) attached for {agent_name}"
        )

        # Build LiteLlm model — pass temperature as kwarg if configured
        model = build_litellm_model(agent_details, _temperature)

        # Make the agent explicitly aware of attached MCP toolsets, so it
        # mentions them when asked and uses their tools proactively. The
        # preamble is re-built from the agent's mcp_servers on every load,
        # so adding/removing an MCP from the UI is immediately reflected.
        mcp_preamble = build_mcp_instruction_preamble(
            agent_details.get("mcp_servers")
        )
        if mcp_preamble:
            instruction = (instruction or "") + "\n\n" + mcp_preamble

        # Teach chat agents to use interactive chips. Pipeline agents with
        # output_schema_name skip this instruction -- injecting it caused Qwen
        # to call request_user_input instead of emitting the structured output.
        if _should_inject_chat_action_tools(agent_details):
            instruction = (instruction or "") + INTERACTIVE_UI_INSTRUCTION

        # Feature: Output Schema - inject output format instruction
        instruction = apply_output_schema(
            instruction, agent_details.get("output_schema"), agent_name
        )

        instruction = _escape_template_vars(instruction)
        async def _on_tool_error(tool, args, tool_context, error):
            """Safety-net callback: return error dict instead of crashing the SSE stream."""
            tool_name = getattr(tool, "name", str(tool))
            logger.error("[TOOL_ERROR] %s raised %s: %s", tool_name, type(error).__name__, error)
            return {
                "success": False,
                "error": f"Tool '{tool_name}' failed: {type(error).__name__}: {error}",
            }

        def _sanitize_tool_response(tool, args, tool_context, tool_response):
            """Coerce numpy scalars / non-JSON values before ADK persists the
            event. Without this, a stray ``numpy.bool_`` in any tool response
            makes ``event.model_dump_json`` raise ``PydanticSerializationError``
            and tears down the SSE stream — users read that as the agent
            silently ignoring their file.
            """
            from apowerb.helpers.jsonify import to_jsonable
            if isinstance(tool_response, dict):
                return to_jsonable(tool_response)
            return None

        agent_kwargs = dict(
            name=agent_name,
            model=model,
            description=agent_details["agent_description"],
            instruction=instruction,
            code_executor=agent_details.get("code_executor", None),
            sub_agents=sub_agents,
            tools=tools_funcs,  # type: ignore
            on_tool_error_callback=_on_tool_error,
            after_tool_callback=_sanitize_tool_response,
        )
        # ADK output_key — when set, the agent's final text is written
        # to session.state[output_key] so a downstream sub-agent in a
        # SequentialAgent can reference it via {output_key} in its prompt.
        # See [[project_th2agent_pr173]] for the brace-resolution gotcha
        # and the SCEI sub-agent design.
        _output_key = agent_details.get("output_key")
        if _output_key:
            agent_kwargs["output_key"] = _output_key

        # When output_schema_name is also set, wire an after_agent_callback
        # that parses the agent's final text as JSON, validates it against
        # the registered Pydantic schema, and rewrites state[output_key] as a
        # canonical JSON string. Without this wiring, ADK only writes the raw
        # final text into state — downstream sub-agents receive prose +
        # markdown fences, not parsable JSON. (Caught by critic 2026-05-18.)
        # Phase 2a — skip path: when this sub-agent declares
        # `skip_when_upstream=<state_key>`, wire a before_model_callback
        # that short-circuits the LLM call if the upstream sub-agent
        # wrote `status: skip` (or a `__skipped_upstream__` cascade
        # sentinel). Saves input tokens on filtered ARs.
        _skip_when_upstream = agent_details.get("skip_when_upstream")
        if _skip_when_upstream and _output_key:
            from apowerb.core.agent_helpers.callbacks import (
                build_skip_short_circuit_callback,
            )
            _skip_cb = build_skip_short_circuit_callback(
                upstream_key=_skip_when_upstream,
                downstream_output_key=_output_key,
            )
            # If the template also has a custom before_model_callback,
            # chain them: skip check first, then custom only if no SC.
            _existing_before = before_model_cb
            if _existing_before is None:
                before_model_cb = _skip_cb
            else:
                import inspect as _inspect
                async def _chained(callback_context, llm_request=None):
                    r = await _skip_cb(
                        callback_context=callback_context,
                        llm_request=llm_request,
                    )
                    if r is not None:
                        return r
                    if _inspect.iscoroutinefunction(_existing_before):
                        return await _existing_before(
                            callback_context=callback_context,
                            llm_request=llm_request,
                        )
                    return _existing_before(
                        callback_context=callback_context,
                        llm_request=llm_request,
                    )
                before_model_cb = _chained
            logger.info(
                f"[TO_AGENT] Wired skip_short_circuit for "
                f"{agent_name}: upstream=state[{_skip_when_upstream!r}]"
            )

        _output_schema_name = agent_details.get("output_schema_name")
        if _output_schema_name and _output_key:
            schema_class = _lookup_output_schema(_output_schema_name)
            if schema_class is not None:
                from apowerb.core.agent_helpers.callbacks import (
                    build_validating_state_writer,
                )
                # Levier C: pass the agent's own model so the writer can make
                # one JSON-repair round-trip on its failure path before the
                # sentinel. Auth is propagated EXPLICITLY (decrypted api key +
                # api base), mirroring build_litellm_model, so the repair call
                # does not depend on OVHCLOUD_API_KEY being in the global env
                # (fragile, overwritten in recursive runs).
                _repair_model = agent_details.get("agent_model")
                _repair_api_key = None
                _repair_api_base = None
                _repair_params = agent_details.get("agent_model_params") or {}
                if isinstance(_repair_params, str):
                    try:
                        _repair_params = json.loads(_repair_params)
                    except (TypeError, json.JSONDecodeError):
                        _repair_params = {}
                if isinstance(_repair_params, dict):
                    _repair_params = decrypt_value_in_dict(
                        _repair_params, values_to_decrypt=["model_api_key"]
                    )
                    _repair_api_key = _repair_params.get("model_api_key")
                    _repair_api_base = _repair_params.get("model_api_base")
                agent_kwargs["after_agent_callback"] = (
                    build_validating_state_writer(
                        schema_class,
                        _output_key,
                        repair_model=_repair_model,
                        repair_api_key=_repair_api_key,
                        repair_api_base=_repair_api_base,
                    )
                )
                logger.info(
                    f"[TO_AGENT] Wired after_agent_callback for "
                    f"{agent_name}: {_output_schema_name} -> "
                    f"state[{_output_key!r}] (repair_model={_repair_model!r}, "
                    f"api_base={'set' if _repair_api_base else 'unset'}, "
                    f"api_key={'set' if _repair_api_key else 'unset'})"
                )
            else:
                logger.warning(
                    f"[TO_AGENT] output_schema_name={_output_schema_name!r} "
                    f"not in any schema registry — callback NOT wired"
                )
        # Deterministic gates (PDF / excluded-supplier / AR / PMI-match /
        # supplier-mismatch) registered on the extension registry (core shim
        # until Phase C). Applied in REGISTRATION ORDER -> reproduces the
        # prod-critical composition (final before_agent_callback = [ar,
        # excluded, pdf]). Do NOT reorder the registration above.
        for _gate_name, _gate_applier in _ext_registry.gate_appliers():
            if _gate_applier(agent_details, _output_key, agent_kwargs):
                logger.info(
                    f"[TO_AGENT] Wired {_gate_name} for "
                    f"{agent_name} -> state[{_output_key!r}]"
                )

        # Usage accounting — TOUJOURS attaché (même sans guardrails), au
        # meme titre que truncate_cb. Chaine avec l'eventuel after_model_cb
        # guardrails existant : le recorder s'execute D'ABORD (ne retourne
        # jamais de valeur), puis l'existant ; la valeur retournee est celle
        # de l'existant.
        from apowerb.core.agent_helpers.callback_chain import (
            chain_after_model_callbacks,
        )
        from apowerb.core.extensions.registry import registry as _registry
        # Record the agent's BUSINESS name ("Analyste AR"), not the ADK
        # appName ("agent1147") this function receives -- the usage
        # dashboard is read by humans. Falls back to the appName when the
        # store row has no name.
        # Modele mutualise : on enregistre le modele REELLEMENT appele
        # (gemini/...), pas la sentinelle `thaink2/default` -- sinon le cout
        # par modele du dashboard perdrait sa cle de jointure avec la grille
        # tarifaire. Le flag, lui, porte l'information de facturation.
        _declared_model = agent_details.get("agent_model")
        _uses_default_llm = is_default_llm_model(_declared_model)
        _recorded_model = (
            resolve_model_credentials(_declared_model, {})[0]
            if _uses_default_llm
            else _declared_model
        )
        # Le noyau resout QUEL modele a reellement repondu — c'est sa
        # connaissance. Ce qu'on fait de l'information (la comptabiliser, la
        # facturer) appartient a une brique. Sans observateur enregistre, rien
        # n'est chaine et l'agent tourne a l'identique.
        for _fabrique in _registry.model_observers():
            _observateur = _fabrique(
                agent_id=agent_id,
                agent_name=agent_details.get("agent_name") or agent_name,
                owner_id=owner_id,
                model_name=_recorded_model,
                billed_to_thaink2=_uses_default_llm,
            )
            if _observateur is not None:
                after_model_cb = chain_after_model_callbacks(_observateur, after_model_cb)

        if before_model_cb:
            agent_kwargs["before_model_callback"] = before_model_cb
        if after_model_cb:
            agent_kwargs["after_model_callback"] = after_model_cb
        if before_tool_cb:
            agent_kwargs["before_tool_callback"] = before_tool_cb

        agent = LlmAgent(**agent_kwargs)

    logger.info(
        f"[TO_AGENT] Successfully created agent: {agent_name} ({type(agent).__name__})"
    )
    return agent
