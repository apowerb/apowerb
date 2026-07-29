"""MCP server toolset loading for ``to_agent``.

Extracted from ``agent_utils`` to keep the main builder below the 500-line
threshold.
"""
from __future__ import annotations

import json
import os

from th2agent.configs.th2logger import setup_logging
from th2agent.helpers.encryptor import decrypt_value_in_dict


logger = setup_logging(__name__)


def _patch_toolbox_tool_get_declaration() -> None:
    """Workaround for toolbox-adk: ``ToolboxTool`` does not implement
    ``_get_declaration``. It inherits the BaseTool default which returns
    ``None``, so ``LlmRequest.append_tools`` never adds the tool to
    ``tools_dict``. Symptom in the agent transcript:

        Tool 'pmi-tool_config11-list-tables' not found.
        Available tools: <only the natively-registered ones>

    The Toolset's tools never reach the dispatcher even though they are
    advertised in the system-prompt preamble (so the LLM happily emits
    function calls for them). We rebuild a ``FunctionDeclaration`` from
    the underlying ``core_tool._params`` and attach it to ``ToolboxTool``
    once at import time.
    """
    try:
        from toolbox_adk.tool import ToolboxTool  # third-party MCP-Toolbox ADK adapter
        from google.genai import types
    except ImportError:
        return  # toolbox-adk not installed → nothing to patch
    if getattr(ToolboxTool, "_get_declaration_patched", False):
        return

    _GENAI_TYPE_MAP = {
        "string": types.Type.STRING,
        "integer": types.Type.INTEGER,
        "number": types.Type.NUMBER,
        "boolean": types.Type.BOOLEAN,
        "array": types.Type.ARRAY,
        "object": types.Type.OBJECT,
    }

    def _get_declaration(self):
        params = getattr(self._core_tool, "_params", ()) or ()
        properties: dict = {}
        required: list[str] = []
        for p in params:
            ptype = _GENAI_TYPE_MAP.get(
                (getattr(p, "type", "") or "string").lower(),
                types.Type.STRING,
            )
            properties[p.name] = types.Schema(
                type=ptype,
                description=(getattr(p, "description", "") or ""),
            )
            if getattr(p, "required", False):
                required.append(p.name)
        return types.FunctionDeclaration(
            name=self.name,
            description=(self.description or ""),
            parameters=types.Schema(
                type=types.Type.OBJECT,
                properties=properties,
                required=required or None,
            ),
        )

    ToolboxTool._get_declaration = _get_declaration
    ToolboxTool._get_declaration_patched = True
    logger.info("Applied ToolboxTool._get_declaration monkey-patch")


_patch_toolbox_tool_get_declaration()


def _parse_mcp_servers_config(mcp_servers_raw):
    """Normalise the raw value (DB column may be a JSON string or a list) to
    a Python list of dicts. Returns [] for empty/invalid input.
    """
    if not mcp_servers_raw:
        return []
    try:
        servers = (
            json.loads(mcp_servers_raw)
            if isinstance(mcp_servers_raw, str)
            else mcp_servers_raw
        )
    except (json.JSONDecodeError, TypeError):
        return []
    if not isinstance(servers, list):
        return []
    return servers


def _slug(value: str) -> str:
    """Mirror the slug logic used by routers.tools._slug() so the preamble
    references the exact same tool names that the toolbox exposes.
    """
    s = "".join(
        c if (c.isalnum() or c == "-") else "-"
        for c in (value or "").lower().replace(" ", "-")
    ).strip("-")
    return s or "source"


def build_mcp_instruction_preamble(mcp_servers_raw) -> str:
    """Generate a human-readable preamble describing the MCP servers attached
    to this agent, suitable to prepend to the agent's instruction.

    Returns an empty string when no MCP is attached. The preamble is
    re-generated on every ``to_agent`` call, so it always reflects the
    current set of attached servers — no hardcoded names or per-tenant
    strings.
    """
    servers = _parse_mcp_servers_config(mcp_servers_raw)
    if not servers:
        return ""

    db_lines: list[str] = []
    other_lines: list[str] = []
    for cfg in servers:
        name = (cfg.get("name") or "mcp_server").strip()
        mcp_type = (cfg.get("mcp_type") or "").lower()
        transport = (cfg.get("transport") or "http").lower()

        if mcp_type == "toolbox-db":
            db_type = (cfg.get("db_type") or "").strip() or "database"
            db_kind_label = {
                "postgres": "PostgreSQL",
                "mysql": "MySQL",
                "mssql": "SQL Server",
            }.get(db_type, db_type)
            db_database = (cfg.get("db_database") or "").strip()
            display_name = db_database or name
            # Tool names are scoped per config via the toolset name written
            # back by routers.tools._regenerate_toolbox_dynamic_config. We
            # derive the slug from cfg.toolset (stripping the "-tools"
            # suffix) so the agent uses the EXACT names the toolbox exposes
            # for this user's config — even when two configs target the
            # same DB and one was disambiguated with a -tool_configN suffix.
            toolset_name = (cfg.get("toolset") or "").strip()
            if toolset_name.endswith("-tools"):
                slug = toolset_name[: -len("-tools")]
            else:
                slug = _slug(db_database or name)
            list_tool = f"{slug}-list-tables"
            exec_tool = f"{slug}-execute-sql"
            db_lines.append(
                f"- **{display_name}** ({db_kind_label}). "
                f"Tools: `{list_tool}` (list tables) and `{exec_tool}` "
                f"(run SQL). Use these EXACT names — do not invent names "
                f"like `{db_type}-list-tables`. Call them whenever the "
                f"user asks for data lookups, schema introspection, or "
                f"SQL queries against the **{display_name}** database. "
                f"When you describe your data sources to the user, refer "
                f"to this database as **{display_name}**, never as "
                f"\"toolbox\" or \"MCP\"."
            )
            continue

        if transport == "stdio":
            label = "Local MCP (stdio)"
            usage = (
                "Use it for the capabilities advertised by its tools "
                "(see the function spec)."
            )
        elif transport == "sse":
            label = "Remote MCP (SSE)"
            usage = "Use it for the capabilities advertised by its tools."
        else:
            label = "Remote MCP (HTTP)"
            usage = "Use it for the capabilities advertised by its tools."
        other_lines.append(f"- {name} ({label}). {usage}")

    lines: list[str] = []
    if db_lines:
        lines.append("You have direct access to the following databases:")
        lines.extend(db_lines)
    if other_lines:
        if lines:
            lines.append("")
        lines.append("You also have the following MCP toolsets:")
        lines.extend(other_lines)

    lines.append(
        "Mention these capabilities explicitly when the user asks what data "
        "sources or capabilities you have."
    )
    return "\n".join(lines)


def load_mcp_servers(mcp_servers_raw, tools_funcs: list) -> None:
    """Parse the agent's ``mcp_servers`` config and append McpToolsets to ``tools_funcs``.

    Mutates ``tools_funcs`` in-place to match the legacy behaviour of
    ``to_agent``.
    """
    if not mcp_servers_raw:
        return

    try:
        mcp_servers = (
            json.loads(mcp_servers_raw)
            if isinstance(mcp_servers_raw, str)
            else mcp_servers_raw
        )
        if not (mcp_servers and isinstance(mcp_servers, list)):
            return
        from google.adk.tools.mcp_tool import McpToolset
        from google.adk.tools.mcp_tool.mcp_session_manager import (
            StreamableHTTPServerParams,
            SseConnectionParams,
        )
        from mcp.client.stdio import StdioServerParameters
        from urllib.parse import urlencode, urlparse

        for mcp_cfg in mcp_servers:
            try:
                mcp_name = mcp_cfg.get("name", "mcp_server")
                transport = mcp_cfg.get("transport", "http")
                mcp_type = (mcp_cfg.get("mcp_type") or "").lower()

                # MCP Toolbox for Databases — needs ToolboxToolset, not the
                # generic McpToolset. The toolbox uses HTTP at the wire level
                # but exposes /mcp JSON-RPC + /api/tool/... rather than the
                # standard MCP SSE/Streamable spec.
                if mcp_type == "toolbox-db":
                    mcp_url = mcp_cfg.get("url", "").strip() or "http://localhost:5000"
                    toolset_name = mcp_cfg.get("toolset", "") or ""
                    try:
                        from google.adk.tools.toolbox_toolset import ToolboxToolset
                    except ImportError as exc:
                        logger.error(
                            f"[TO_AGENT] ToolboxToolset unavailable for '{mcp_name}': {exc}"
                        )
                        continue
                    kwargs = {"server_url": mcp_url}
                    if toolset_name:
                        kwargs["toolset_name"] = toolset_name
                    tools_funcs.append(ToolboxToolset(**kwargs))
                    logger.info(
                        f"[TO_AGENT] Added MCP toolbox-db '{mcp_name}' "
                        f"({mcp_url}, toolset={toolset_name or 'all'})"
                    )
                    continue

                if transport == "stdio":
                    # Stdio transport: local command (e.g. npx, uvx, python)
                    mcp_command = mcp_cfg.get("command", "")
                    mcp_args = mcp_cfg.get("args", [])
                    mcp_env = mcp_cfg.get("env", {})
                    # Windows fix: npx/npm need .cmd extension
                    if os.name == "nt" and mcp_command in ("npx", "npm", "yarn", "pnpm"):
                        mcp_command = f"{mcp_command}.cmd"
                    if not mcp_command:
                        logger.warning(
                            f"[TO_AGENT] MCP server '{mcp_name}': no command specified, skipping"
                        )
                        continue
                    # Decrypt env values
                    if mcp_env:
                        mcp_env = decrypt_value_in_dict(
                            mcp_env, values_to_decrypt=mcp_env.keys()
                        )
                    mcp_toolset = McpToolset(
                        connection_params=StdioServerParameters(
                            command=mcp_command,
                            args=mcp_args,
                            env={**os.environ, **mcp_env},
                        ),
                    )
                    tools_funcs.append(mcp_toolset)
                    logger.info(
                        f"[TO_AGENT] Added MCP server '{mcp_name}' (stdio: {mcp_command})"
                    )
                elif transport == "sse":
                    # SSE transport: Server-Sent Events (legacy MCP protocol)
                    mcp_url = mcp_cfg.get("url", "").strip()
                    mcp_headers = mcp_cfg.get("headers", {})
                    mcp_params = mcp_cfg.get("params", {})
                    if not mcp_url:
                        logger.warning(
                            f"[TO_AGENT] MCP server '{mcp_name}': no URL specified, skipping"
                        )
                        continue
                    if mcp_params:
                        mcp_params = decrypt_value_in_dict(
                            mcp_params, values_to_decrypt=mcp_params.keys()
                        )
                    if mcp_params:
                        separator = "&" if "?" in mcp_url else "?"
                        query_string = urlencode(mcp_params)
                        mcp_url = f"{mcp_url}{separator}{query_string}"
                    logger.info(
                        f"[TO_AGENT] MCP SSE DEBUG: url={mcp_url!r}, headers={mcp_headers!r}"
                    )
                    mcp_toolset = McpToolset(
                        connection_params=SseConnectionParams(
                            url=mcp_url,
                            headers=mcp_headers if mcp_headers else None,
                        ),
                    )
                    tools_funcs.append(mcp_toolset)
                    _parsed_url = urlparse(mcp_url)
                    logger.info(
                        f"[TO_AGENT] Added MCP server '{mcp_name}' (sse: {_parsed_url.scheme}://{_parsed_url.netloc})"
                    )
                else:
                    # HTTP transport (default)
                    mcp_url = mcp_cfg.get("url", "").strip()
                    mcp_headers = mcp_cfg.get("headers", {})
                    mcp_params = mcp_cfg.get("params", {})
                    if not mcp_url:
                        logger.warning(
                            f"[TO_AGENT] MCP server '{mcp_name}': no URL specified, skipping"
                        )
                        continue
                    if mcp_params:
                        mcp_params = decrypt_value_in_dict(
                            mcp_params, values_to_decrypt=mcp_params.keys()
                        )
                    if mcp_params:
                        separator = "&" if "?" in mcp_url else "?"
                        query_string = urlencode(mcp_params)
                        mcp_url = f"{mcp_url}{separator}{query_string}"
                    mcp_toolset = McpToolset(
                        connection_params=StreamableHTTPServerParams(
                            url=mcp_url,
                            headers=mcp_headers,
                        ),
                    )
                    tools_funcs.append(mcp_toolset)
                    _parsed_url = urlparse(mcp_url)
                    logger.info(
                        f"[TO_AGENT] Added MCP server '{mcp_name}' ({_parsed_url.scheme}://{_parsed_url.netloc})"
                    )
            except Exception as e:
                logger.error(
                    f"[TO_AGENT] Failed to load MCP server '{mcp_cfg.get('name', '?')}': {e}"
                )
    except (json.JSONDecodeError, TypeError) as e:
        logger.error(f"[TO_AGENT] Error parsing MCP servers config: {e}")
