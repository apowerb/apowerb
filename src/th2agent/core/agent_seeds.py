"""Agent seed export/import system.

Exports agent and tool_config definitions to YAML files stored on S3,
organised by owner_id.  Imports them back into any database **preserving
original IDs** so that all references (agent_tools, sub_agents) remain
valid across DB migrations.  Sensitive data (API keys, passwords) is
redacted on export.
"""

import ast
import json
import re
from datetime import datetime
from logging import getLogger
from typing import Any

import yaml
from sqlalchemy import text

from th2agent.agent_store.agent_manager import AgentStore
from th2agent.core.agent_main import fetch_agents
from th2agent.core.adk_agent_builder import create_agent_module
from th2agent.helpers.encryptor import encrypt_value_in_dict
from th2agent.storage.s3 import (
    download_file_from_s3,
    list_files_in_s3,
    upload_bytes_to_s3,
)
from th2agent.tools_store.tool_config import ToolConfigStore
from th2agent.tools_store.tools_helpers import fetch_tool_configs

logger = getLogger(__name__)

REDACTED = "__REDACTED__"
SEEDS_S3_PREFIX = "seeds/"

_SENSITIVE_RE = re.compile(
    r"(password|secret|token|key|api_key|credential|auth)", re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_sensitive(key: str) -> bool:
    return bool(_SENSITIVE_RE.search(key))


def _redact_params(params: dict | None) -> dict:
    """Replace sensitive values with __REDACTED__."""
    if not params:
        return {}
    return {k: REDACTED if _is_sensitive(k) else v for k, v in params.items()}


def _parse_string_list(value: str | list | None) -> list:
    """Parse agent_tools/sub_agents stored as ``str(list)`` in the DB."""
    if not value:
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = ast.literal_eval(value)
        return parsed if isinstance(parsed, list) else []
    except (ValueError, SyntaxError):
        return []


def _safe_filename(name: str) -> str:
    return re.sub(r"[^\w\-.]", "_", name).strip("_").lower()


def _parse_json_field(value: str | dict | list | None):
    """Parse a JSON string field, returning None if empty/invalid."""
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return None


def _s3_write_yaml(s3_key: str, data: dict) -> str:
    """Serialize *data* to YAML and upload to S3. Returns the S3 key."""
    content = yaml.dump(data, default_flow_style=False, allow_unicode=True, sort_keys=False)
    upload_bytes_to_s3(content.encode("utf-8"), s3_key, content_type="text/yaml")
    return s3_key


def _s3_read_yaml(s3_key: str) -> dict | None:
    """Download a YAML file from S3 and parse it."""
    raw = download_file_from_s3(s3_key)
    return yaml.safe_load(raw.decode("utf-8"))


def _s3_list_owner_dirs(prefix: str) -> list[str]:
    """List unique 'owner_id/' sub-prefixes under *prefix* on S3."""
    keys = list_files_in_s3(prefix)
    owners: set[str] = set()
    for key in keys:
        relative = key[len(prefix):]
        parts = relative.split("/")
        if len(parts) >= 2:
            owners.add(parts[0])
    return sorted(owners)


def _s3_list_yamls(prefix: str) -> list[str]:
    """List all .yaml S3 keys under *prefix*."""
    return [k for k in list_files_in_s3(prefix) if k.endswith(".yaml")]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def export_agents(
    owner_id: str,
    s3_prefix: str = SEEDS_S3_PREFIX,
) -> list[str]:
    """Export all agents + their tool_configs for *owner_id* to YAML on S3.

    Preserves original ``agent_id`` and ``tool_config_id`` so that
    ``agent_tools`` and ``sub_agents`` references remain valid on import.

    Returns the list of written S3 keys.
    """
    agents_prefix = f"{s3_prefix}agents/{owner_id}/"
    configs_prefix = f"{s3_prefix}tool_configs/{owner_id}/"

    agent_store = AgentStore()
    agent_store.create_table()
    agents = fetch_agents(owner_id)

    written: list[str] = []
    exported_tc_ids: set[int] = set()

    for agent in agents:
        agent_id = agent["agent_id"]

        agent_tools = _parse_string_list(agent.get("agent_tools"))
        sub_agents = _parse_string_list(agent.get("sub_agents"))

        for ref in agent_tools:
            if ref.startswith("tool_config"):
                try:
                    tc_num = int(ref.replace("tool_config", ""))
                    exported_tc_ids.add(tc_num)
                except ValueError:
                    pass

        mcp_servers = _parse_json_field(agent.get("mcp_servers"))
        if mcp_servers:
            for srv in mcp_servers:
                if srv.get("params"):
                    srv["params"] = _redact_params(srv["params"])
                if srv.get("env"):
                    srv["env"] = _redact_params(srv["env"])

        seed: dict[str, Any] = {
            "agent_id": agent_id,
            "agent_name": agent["agent_name"],
            "agent_model": agent["agent_model"],
            "agent_type": agent.get("agent_type", "base"),
            "agent_description": agent.get("agent_description", ""),
            "agent_instruction": agent.get("agent_instruction", ""),
            "agent_tools": agent_tools,
            "sub_agents": sub_agents,
            "owner_id": owner_id,
            "organization_id": agent.get("organization_id", "thaink2"),
            "project_id": agent.get("project_id", "thaink2"),
            "memory_enabled": (agent.get("memory_enabled") or "false").lower() == "true"
                if isinstance(agent.get("memory_enabled"), str)
                else bool(agent.get("memory_enabled")),
            "artifacts_enabled": (agent.get("artifacts_enabled") or "false").lower() == "true"
                if isinstance(agent.get("artifacts_enabled"), str)
                else bool(agent.get("artifacts_enabled")),
        }

        tags = _parse_json_field(agent.get("tags"))
        if tags:
            seed["tags"] = tags
        guardrails = _parse_json_field(agent.get("guardrails_config"))
        if guardrails:
            seed["guardrails_config"] = guardrails
        input_schema = _parse_json_field(agent.get("input_schema"))
        if input_schema:
            seed["input_schema"] = input_schema
        output_schema = _parse_json_field(agent.get("output_schema"))
        if output_schema:
            seed["output_schema"] = output_schema
        if agent.get("code_executor"):
            seed["code_executor"] = agent["code_executor"]
        if agent.get("superagent_template_id"):
            seed["superagent_template_id"] = agent["superagent_template_id"]
        if agent.get("loop_max_iterations"):
            try:
                seed["loop_max_iterations"] = int(agent["loop_max_iterations"])
            except (ValueError, TypeError):
                pass
        if agent.get("loop_exit_instruction"):
            seed["loop_exit_instruction"] = agent["loop_exit_instruction"]
        if mcp_servers:
            seed["mcp_servers"] = mcp_servers

        filename = f"agent{agent_id}_{_safe_filename(agent['agent_name'])}.yaml"
        s3_key = f"{agents_prefix}{filename}"
        _s3_write_yaml(s3_key, seed)
        written.append(s3_key)
        logger.info("Exported agent%d '%s' → s3://%s", agent_id, agent["agent_name"], s3_key)

    # Export referenced tool_configs
    if exported_tc_ids:
        for tc_id in sorted(exported_tc_ids):
            tc = fetch_tool_configs(f"tool_config{tc_id}", owner_id=owner_id)
            if not tc or not tc.get("tool_config_name"):
                continue
            tc_seed: dict[str, Any] = {
                "tool_config_id": tc_id,
                "tool_config_name": tc["tool_config_name"],
                "tool_name": tc.get("tool_name", ""),
                "tool_category": tc.get("tool_category", ""),
                "tool_config_params": _redact_params(tc.get("tool_config_params")),
                "tool_config_type": tc.get("tool_config_type", "active"),
                "owner_id": tc.get("owner_id", owner_id),
                "organization_id": tc.get("organization_id", "thaink2"),
                "project_id": tc.get("project_id", "thaink2"),
            }
            tc_filename = f"tool_config{tc_id}_{_safe_filename(tc['tool_config_name'])}.yaml"
            s3_key = f"{configs_prefix}{tc_filename}"
            _s3_write_yaml(s3_key, tc_seed)
            written.append(s3_key)
            logger.info("Exported tool_config%d '%s' → s3://%s", tc_id, tc["tool_config_name"], s3_key)

    return written


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def _agent_id_exists(agent_id: int, agent_store: AgentStore) -> bool:
    t = agent_store.agent_table
    q = t.select().where(t.c.agent_id == agent_id)
    return bool(agent_store.get_list_agents(q))


def _tool_config_id_exists(tc_id: int, tc_store: ToolConfigStore) -> bool:
    t = tc_store.tool_config_table
    q = t.select().where(t.c.tool_config_id == tc_id)
    return bool(tc_store.get_list_tool_configs(q))


def _insert_tool_config_with_id(seed: dict[str, Any], tc_store: ToolConfigStore) -> None:
    """Insert a tool_config row with a specific tool_config_id."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    params = seed.get("tool_config_params", {})
    params = {k: v for k, v in params.items() if v != REDACTED}
    if params:
        params = encrypt_value_in_dict(params, values_to_encrypt=params.keys())
    params_json = json.dumps(params)

    with tc_store.engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO tool_configs "
            "(tool_config_id, tool_config_name, tool_name, tool_config_params, "
            " tool_category, tool_config_type, owner_id, organization_id, project_id, "
            " created_at, updated_at, status) "
            "VALUES (:id, :name, :tool_name, :params, :category, :tc_type, "
            "        :owner, :org, :proj, :created, :updated, :status)"
        ), {
            "id": seed["tool_config_id"],
            "name": seed["tool_config_name"],
            "tool_name": seed.get("tool_name", ""),
            "params": params_json,
            "category": seed.get("tool_category", ""),
            "tc_type": seed.get("tool_config_type", "active"),
            "owner": seed["owner_id"],
            "org": seed.get("organization_id", "thaink2"),
            "proj": seed.get("project_id", "thaink2"),
            "created": now,
            "updated": now,
            "status": "active",
        })


def _insert_agent_with_id(seed: dict[str, Any], agent_store: AgentStore) -> None:
    """Insert an agent row with a specific agent_id, then generate the module file."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    model_params = seed.get("agent_model_params") or {}
    encrypted_model_params = encrypt_value_in_dict(
        model_params, values_to_encrypt=["model_api_key"]
    )

    mcp_servers = seed.get("mcp_servers")
    mcp_json = None
    if mcp_servers:
        encrypted_mcp = []
        for srv in mcp_servers:
            s = dict(srv)
            if s.get("params"):
                clean = {k: v for k, v in s["params"].items() if v != REDACTED}
                s["params"] = encrypt_value_in_dict(clean, values_to_encrypt=clean.keys()) if clean else {}
            if s.get("env"):
                clean = {k: v for k, v in s["env"].items() if v != REDACTED}
                s["env"] = encrypt_value_in_dict(clean, values_to_encrypt=clean.keys()) if clean else {}
            encrypted_mcp.append(s)
        mcp_json = json.dumps(encrypted_mcp)

    agent_tools = seed.get("agent_tools", [])
    sub_agents = seed.get("sub_agents", [])

    with agent_store.engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO th2agents_store "
            "(agent_id, agent_name, agent_model, agent_model_params, "
            " agent_description, agent_instruction, agent_tools, sub_agents, "
            " agent_type, code_executor, input_schema, output_schema, "
            " owner_id, organization_id, project_id, "
            " created_at, updated_at, status, "
            " guardrails_config, memory_enabled, artifacts_enabled, "
            " superagent_template_id, loop_max_iterations, loop_exit_instruction, "
            " mcp_servers, tags) "
            "VALUES (:agent_id, :name, :model, :model_params, "
            "        :desc, :instruction, :tools, :sub_agents, "
            "        :atype, :code_exec, :input_s, :output_s, "
            "        :owner, :org, :proj, "
            "        :created, :updated, :status, "
            "        :guardrails, :mem, :artifacts, "
            "        :superagent, :loop_max, :loop_exit, "
            "        :mcp, :tags)"
        ), {
            "agent_id": seed["agent_id"],
            "name": seed["agent_name"],
            "model": seed["agent_model"],
            "model_params": json.dumps(encrypted_model_params),
            "desc": seed.get("agent_description", ""),
            "instruction": seed.get("agent_instruction", ""),
            "tools": str(agent_tools),
            "sub_agents": str(sub_agents),
            "atype": seed.get("agent_type", "base"),
            "code_exec": seed.get("code_executor"),
            "input_s": json.dumps(seed.get("input_schema")),
            "output_s": json.dumps(seed.get("output_schema")),
            "owner": seed["owner_id"],
            "org": seed.get("organization_id", "thaink2"),
            "proj": seed.get("project_id", "thaink2"),
            "created": now,
            "updated": now,
            "status": "active",
            "guardrails": json.dumps(seed["guardrails_config"]) if seed.get("guardrails_config") else None,
            "mem": str(seed.get("memory_enabled", False)).lower(),
            "artifacts": str(seed.get("artifacts_enabled", False)).lower(),
            "superagent": seed.get("superagent_template_id"),
            "loop_max": str(seed["loop_max_iterations"]) if seed.get("loop_max_iterations") else None,
            "loop_exit": seed.get("loop_exit_instruction"),
            "mcp": mcp_json,
            "tags": json.dumps(seed["tags"]) if seed.get("tags") else None,
        })

    create_agent_module(
        agent_name=str(seed["agent_id"]),
        description=seed.get("agent_description", ""),
        instruction=seed.get("agent_instruction", ""),
        tools=agent_tools,
        model=seed["agent_model"],
    )


def _reset_sequences(agent_store: AgentStore, tc_store: ToolConfigStore) -> None:
    """Reset PostgreSQL auto-increment sequences to max(id) + 1."""
    with agent_store.engine.begin() as conn:
        conn.execute(text(
            "SELECT setval("
            "  pg_get_serial_sequence('th2agents_store', 'agent_id'), "
            "  COALESCE((SELECT MAX(agent_id) FROM th2agents_store), 0) + 1, "
            "  false"
            ")"
        ))
    with tc_store.engine.begin() as conn:
        conn.execute(text(
            "SELECT setval("
            "  pg_get_serial_sequence('tool_configs', 'tool_config_id'), "
            "  COALESCE((SELECT MAX(tool_config_id) FROM tool_configs), 0) + 1, "
            "  false"
            ")"
        ))
    logger.info("Reset auto-increment sequences")


def import_agents(
    s3_prefix: str = SEEDS_S3_PREFIX,
    dry_run: bool = False,
) -> dict[str, list[str]]:
    """Import agents and tool_configs from YAML seed files on S3.

    Preserves original IDs. Skips entries whose ID already exists.

    Returns ``{"created": [...], "skipped": [...], "errors": [...]}``.
    """
    agents_prefix = f"{s3_prefix}agents/"
    configs_prefix = f"{s3_prefix}tool_configs/"

    # Check if any seed files exist on S3
    agent_keys = _s3_list_yamls(agents_prefix)
    if not agent_keys:
        logger.warning("No seed files found on S3 under %s", agents_prefix)
        return {"created": [], "skipped": [], "errors": []}

    agent_store = AgentStore()
    agent_store.create_table()
    tc_store = ToolConfigStore()

    created: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    did_insert = False

    # -- Phase 1: import tool_configs (must exist before agents) -----------
    config_keys = _s3_list_yamls(configs_prefix)
    for s3_key in sorted(config_keys):
        # Extract owner_id from key: seeds/tool_configs/{owner_id}/file.yaml
        parts = s3_key[len(configs_prefix):].split("/")
        if len(parts) < 2:
            continue
        owner_id = parts[0]
        label = f"tool_config:{owner_id}/{parts[-1]}"
        try:
            seed = _s3_read_yaml(s3_key)
            if not seed or "tool_config_id" not in seed:
                errors.append(f"{label}: missing tool_config_id")
                continue

            if seed.get("owner_id") and seed["owner_id"] != owner_id:
                errors.append(f"{label}: owner_id mismatch")
                continue

            tc_id = seed["tool_config_id"]
            if _tool_config_id_exists(tc_id, tc_store):
                skipped.append(label)
                continue

            if dry_run:
                created.append(label)
                continue

            _insert_tool_config_with_id(seed, tc_store)
            created.append(label)
            did_insert = True
            logger.info("Created tool_config%d '%s'", tc_id, seed.get("tool_config_name"))
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            logger.exception("Failed to import %s", label)

    # -- Phase 2: import agents --------------------------------------------
    for s3_key in sorted(agent_keys):
        parts = s3_key[len(agents_prefix):].split("/")
        if len(parts) < 2:
            continue
        owner_id = parts[0]
        label = f"agent:{owner_id}/{parts[-1]}"
        try:
            seed = _s3_read_yaml(s3_key)
            if not seed or "agent_id" not in seed:
                errors.append(f"{label}: missing agent_id")
                continue

            if seed.get("owner_id") and seed["owner_id"] != owner_id:
                errors.append(f"{label}: owner_id mismatch")
                continue

            agent_id = seed["agent_id"]
            if _agent_id_exists(agent_id, agent_store):
                skipped.append(label)
                continue

            if dry_run:
                created.append(label)
                continue

            _insert_agent_with_id(seed, agent_store)
            created.append(label)
            did_insert = True
            logger.info("Created agent%d '%s' for %s", agent_id, seed["agent_name"], owner_id)
        except Exception as exc:
            errors.append(f"{label}: {exc}")
            logger.exception("Failed to import %s", label)

    # -- Phase 3: fix sequences --------------------------------------------
    if did_insert and not dry_run:
        try:
            _reset_sequences(agent_store, tc_store)
        except Exception as exc:
            logger.warning("Failed to reset sequences (non-fatal): %s", exc)

    return {"created": created, "skipped": skipped, "errors": errors}


# ---------------------------------------------------------------------------
# Auto-seed (called at startup)
# ---------------------------------------------------------------------------

def ensure_seed_agents() -> None:
    """Import seed agents from S3 if any seed files exist.

    Safe to call on every startup -- existing agents/configs are skipped.
    """
    try:
        agent_keys = _s3_list_yamls(f"{SEEDS_S3_PREFIX}agents/")
    except Exception as exc:
        logger.debug("Could not check S3 for seeds (skipping): %s", exc)
        return

    if not agent_keys:
        return

    logger.info("Seed files found on S3, importing agents...")
    result = import_agents(SEEDS_S3_PREFIX)

    if result["created"]:
        logger.info("Seed imports created: %s", result["created"])
    if result["skipped"]:
        logger.debug("Seed imports skipped (already exist): %s", result["skipped"])
    if result["errors"]:
        logger.error("Seed import errors: %s", result["errors"])
