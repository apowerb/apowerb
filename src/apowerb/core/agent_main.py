import ast
import json
import os
from datetime import datetime
from typing import Dict

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from apowerb.configs.paths import agents_pool_dir
from apowerb.schema.agent_schema import AgentCreateSchema
from apowerb.agent_store.agent_manager import AgentStore
from apowerb.core.adk_agent_builder import create_agent_module, delete_agent_module
from apowerb.core.agent_helpers.default_llm import (
    is_default_llm_model,
    mask_model_api_key,
    strip_default_llm_params,
    unmask_model_api_key,
)
from apowerb.core.superagents import compute_template_hash
from apowerb.helpers.encryptor import encrypt_value_in_dict, decrypt_value_in_dict


# DDL déplacé dans helpers/store_migrations.ensure_store_tables(),
# appelé au boot : importer ce module ne doit pas toucher la base.
agent_store = AgentStore()


def _parse_string_list(raw) -> list:
    """Parse a stringified list from DB back into a Python list.

    Handles both JSON format '["a", "b"]' and legacy Python repr "['a', 'b']".
    Returns an empty list for None, empty strings, or invalid data.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if not isinstance(raw, str) or not raw.strip() or raw.strip() in ("None", "[]"):
        return []
    # Try JSON first (new format)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass
    # Fallback to ast.literal_eval for legacy Python repr format
    try:
        parsed = ast.literal_eval(raw)
        if isinstance(parsed, list):
            return parsed
    except (ValueError, SyntaxError):
        pass
    return []


def _encrypt_mcp_servers(mcp_servers) -> str | None:
    """Encrypt params in each MCP server config and return JSON string for DB storage."""
    if not mcp_servers:
        return None
    encrypted = []
    for srv in mcp_servers:
        srv_dict = srv.model_dump() if hasattr(srv, "model_dump") else dict(srv)
        # Encrypt HTTP params
        if srv_dict.get("params"):
            srv_dict["params"] = encrypt_value_in_dict(
                srv_dict["params"],
                values_to_encrypt=srv_dict["params"].keys(),
            )
        # Encrypt stdio env vars
        if srv_dict.get("env"):
            srv_dict["env"] = encrypt_value_in_dict(
                srv_dict["env"],
                values_to_encrypt=srv_dict["env"].keys(),
            )
        encrypted.append(srv_dict)
    return json.dumps(encrypted)


def register_agent(agent: AgentCreateSchema) -> dict:
    """Register a new agent in the agent store."""
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    update_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status = "active"

    # Agent en modele thaink2 par defaut : rien a persister cote credentials.
    # Ce qui n'est pas en base ne peut pas fuir, ni etre detourne.
    model_params_to_store = strip_default_llm_params(
        agent.agent_model, agent.agent_model_params
    )
    encrypted_agent_model_params = encrypt_value_in_dict(
        model_params_to_store,  # type: ignore
        values_to_encrypt=["model_api_key"],
    )

    mcp_servers_to_store = _encrypt_mcp_servers(agent.mcp_servers)

    # Snapshot the template's current hash so the UI can later detect drift
    # ("template updated, click to sync") between this agent and the template
    # it was created from. None if no template (free-form agent).
    template_version_hash = (
        compute_template_hash(agent.superagent_template_id)
        if agent.superagent_template_id
        else None
    )

    insert_query = (
        agent_store.agent_table.insert()
        .values(
            agent_name=agent.agent_name,
            agent_model=agent.agent_model,
            agent_model_params=json.dumps(encrypted_agent_model_params),
            agent_description=agent.agent_description,
            agent_instruction=agent.agent_instruction,
            agent_tools=json.dumps(agent.agent_tools or []),
            sub_agents=json.dumps(agent.sub_agents or []),
            # v2 sub-agent pipeline fields (PR #174 + #176).
            # Persisting these is what makes scei_ar_assistant_v2
            # actually function — without them, ADK output_key,
            # after_agent_callback validation, and skip-cascade
            # short-circuit all stay armed=False at runtime.
            output_key=agent.output_key,
            output_schema_name=agent.output_schema_name,
            skip_when_upstream=agent.skip_when_upstream,
            agent_type=agent.agent_type,
            code_executor=agent.code_executor,
            input_schema=json.dumps(agent.input_schema),
            output_schema=json.dumps(agent.output_schema),
            owner_id=agent.owner_id,
            organization_id=agent.organization_id,
            project_id=agent.project_id,
            created_at=created_at,
            updated_at=update_at,
            status=status,
            guardrails_config=json.dumps(agent.guardrails_config) if agent.guardrails_config else None,
            memory_enabled=str(agent.memory_enabled).lower() if agent.memory_enabled else "false",
            artifacts_enabled=str(agent.artifacts_enabled).lower() if agent.artifacts_enabled else "false",
            superagent_template_id=agent.superagent_template_id,
            superagent_template_version_hash=template_version_hash,
            loop_max_iterations=str(agent.loop_max_iterations) if agent.loop_max_iterations else None,
            loop_exit_instruction=agent.loop_exit_instruction,
            mcp_servers=mcp_servers_to_store,
            agent_skills=json.dumps(agent.agent_skills) if agent.agent_skills else None,
            hub_origin_id=agent.hub_origin_id,
            tags=json.dumps(agent.tags) if agent.tags else None,
        )
        .returning(agent_store.agent_table.c.agent_id)
    )

    try:
        with agent_store.engine.begin() as conn:
            agent_id = conn.execute(insert_query).scalar_one()
    except IntegrityError:
        raise HTTPException(
            status_code=409,
            detail=f"An agent named '{agent.agent_name}' already exists in this organization.",
        )

    agent_id = str(agent_id)
    create_agent_module(
        agent_name=agent_id,
        description=agent.agent_description,
        instruction=agent.agent_instruction,
        tools=agent.agent_tools,
        model=agent.agent_model,
    )
    return {
        "agent_id": f"agent{agent_id}",
        "agent_name": agent.agent_name,
        "agent_model": agent.agent_model,
        "agent_description": agent.agent_description,
        "owner_id": agent.owner_id,
        "organization_id": agent.organization_id,
        "created_at": created_at,
        "message": "Agent registered successfully.",
    }


def get_agent_template_status(agent_id: int, user_id: str) -> dict:
    """Compare an agent's stored template snapshot against the live template.

    Returns the diff report from ``diff_agent_against_template``, or
    ``{"is_in_sync": True, "template_id": None, ...}`` if the agent has no
    superagent template attached. ``HTTPException(404)`` if the agent is
    not found / not owned by ``user_id``.
    """
    from apowerb.core.superagents import diff_agent_against_template

    agent = get_agent(agent_id, user_id=user_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")

    template_id = agent.get("superagent_template_id")
    if not template_id:
        return {
            "agent_id": agent_id,
            "template_id": None,
            "is_in_sync": True,
            "stored_hash": None,
            "current_hash": None,
            "drift_fields": [],
        }

    # Tools / tags arrive from get_agent as already-parsed lists; instructions
    # as string. diff_agent_against_template compares them positionally
    # against the template, so passing the agent dict directly is sufficient.
    report = diff_agent_against_template(agent, template_id)
    report["agent_id"] = agent_id
    return report


def resync_agent_to_template(agent_id: int, user_id: str) -> dict:
    """Overwrite the agent's hash-relevant fields with the live template.

    Touches only ``_TEMPLATE_RESYNC_FIELDS`` (instruction, tools, tags) —
    user-owned knobs (model, model_params, mcp_servers, guardrails,
    artifacts/memory toggles, agent_skills) are left untouched. The new
    template hash is also written so the UI banner clears.

    Returns the post-resync ``get_agent_template_status`` report.
    """
    from apowerb.core.superagents import (
        _TEMPLATE_RESYNC_FIELDS,
        compute_template_hash,
        get_superagent_template,
    )

    agent = get_agent(agent_id, user_id=user_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Agent not found.")

    template_id = agent.get("superagent_template_id")
    if not template_id:
        raise HTTPException(
            status_code=400,
            detail=(
                "Agent has no superagent_template_id — there is nothing to "
                "resync from."
            ),
        )

    template = get_superagent_template(template_id, user=None)
    if template is None:
        raise HTTPException(
            status_code=404,
            detail=f"Source template {template_id!r} no longer exists in the registry.",
        )

    new_hash = compute_template_hash(template_id)
    update_values: dict = {
        "superagent_template_version_hash": new_hash,
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    for field in _TEMPLATE_RESYNC_FIELDS:
        value = template.get(field)
        if field == "agent_tools":
            # Preserve the user's ``tool_config*`` attachments — those
            # carry the agent's credentials (DB connections, OAuth, ...)
            # and are not part of the template (which only declares the
            # native ``category.tool_name`` entries it depends on).
            # Wiping them out on resync left the agent with no DB tools
            # at all (live regression: agent6 lost tool_run_sql_pmi /
            # tool_run_sql_suiviar, breaking SuiviAR INSERTs).
            existing = _parse_string_list(agent.get("agent_tools"))
            user_configs = [t for t in existing if t.startswith("tool_config")]
            template_natives = list(value or [])
            # Configs first → priority in load_agent_tools_functions
            # (matches the order used by AgentModal when wiring a new
            # agent from a template).
            update_values[field] = json.dumps(user_configs + template_natives)
        elif field == "tags":
            update_values[field] = json.dumps(value or [])
        else:
            update_values[field] = value

    upd = (
        agent_store.agent_table.update()
        .where(
            agent_store.agent_table.c.agent_id == agent_id,
            agent_store.agent_table.c.owner_id == user_id,
        )
        .values(**update_values)
    )
    with agent_store.engine.begin() as conn:
        conn.execute(upd)

    # Re-read the agent module on disk so subsequent /run_sse calls pick up
    # the new instruction / tools without a service restart.
    create_agent_module(
        agent_name=str(agent_id),
        description=agent.get("agent_description"),
        instruction=template.get("agent_instruction"),
        tools=template.get("agent_tools"),
        model=agent.get("agent_model"),
    )

    return get_agent_template_status(agent_id, user_id=user_id)


def fetch_agents(user_id: str | None = None) -> list[Dict]:
    """Fetch agents from the agent store.

    When *user_id* is a string, returns agents owned by that user (the
    behaviour every API caller relies on).
    When *user_id* is ``None``, returns ALL agents regardless of owner —
    this is the **admin / CLI path**, gated by host access. Do NOT call
    with ``None`` from a router or any user-facing surface; it would
    leak agents across tenants.
    """

    select_query = agent_store.agent_table.select()
    if user_id is not None:
        select_query = select_query.where(
            agent_store.agent_table.c.owner_id == user_id
        )
    result = agent_store.get_list_agents(select_query)
    agents = [u._asdict() for u in result]

    # Check integrity and deserialize for each agent
    for agent in agents:
        agent_id = agent.get("agent_id")
        if agent_id:
            folder_name = f"agent{agent_id}"
            agent_path = os.path.join(str(agents_pool_dir()), folder_name, "agent.py")
            if not os.path.exists(agent_path):
                agent["integrity_errors"] = ["Missing agent.py file on server"]
            else:
                agent["integrity_errors"] = []

        # Parse stringified lists into proper JSON arrays
        agent["agent_tools"] = _parse_string_list(agent.get("agent_tools"))
        agent["sub_agents"] = _parse_string_list(agent.get("sub_agents"))
        agent["tags"] = _parse_string_list(agent.get("tags"))
        agent["agent_skills"] = _parse_string_list(agent.get("agent_skills"))

        # Decrypt mcp_servers params/env to prevent double-encryption on re-save
        if agent.get("mcp_servers"):
            try:
                mcp_list = json.loads(agent["mcp_servers"])
                for srv in mcp_list:
                    if srv.get("params"):
                        srv["params"] = decrypt_value_in_dict(
                            srv["params"],
                            values_to_decrypt=srv["params"].keys(),
                        )
                    if srv.get("env"):
                        srv["env"] = decrypt_value_in_dict(
                            srv["env"],
                            values_to_decrypt=srv["env"].keys(),
                        )
                agent["mcp_servers"] = mcp_list
            except (json.JSONDecodeError, Exception):
                agent["mcp_servers"] = None

    return agents


def get_agent(agent_id: int, user_id: str, reveal_secrets: bool = False) -> dict:
    """Get a specific agent by name from the agent store.

    La cle API est **masquee** par defaut : l'ancien comportement renvoyait
    la valeur en clair « for frontend convenience », donc n'importe quel
    porteur du cookie pouvait la relire dans l'onglet reseau. Le front
    recoit ``__unchanged__`` et nous le renvoie tel quel au PUT, ou
    ``unmask_model_api_key`` recolle la vraie valeur.

    ``reveal_secrets=True`` est reserve aux appels **internes** qui doivent
    reecrire la cle ailleurs (propagation aux sous-agents). Ne jamais
    l'exposer a un routeur.
    """
    from apowerb.helpers.encryptor import decrypt_value_in_dict

    select_query = agent_store.agent_table.select().where(
        agent_store.agent_table.c.agent_id == agent_id,
        agent_store.agent_table.c.owner_id == user_id,
    )
    result = agent_store.get_list_agents(select_query)
    agents = [u._asdict() for u in result]
    if agents:
        agent = agents[0]
        # Decrypt agent_model_params to extract model_api_key
        if agent.get("agent_model_params"):
            try:
                model_params = json.loads(agent["agent_model_params"])
                decrypted_params = decrypt_value_in_dict(
                    model_params, values_to_decrypt=["model_api_key"]
                )
                if not reveal_secrets:
                    decrypted_params = mask_model_api_key(decrypted_params)
                # Add model_api_key as a top-level field for frontend convenience
                agent["model_api_key"] = decrypted_params.get("model_api_key", "")
                # Keep the original params as well
                agent["agent_model_params"] = decrypted_params
            except (json.JSONDecodeError, Exception) as e:
                print(f"Error decrypting agent_model_params: {e}")
                agent["model_api_key"] = ""
                agent["agent_model_params"] = {}
        else:
            agent["model_api_key"] = ""
            agent["agent_model_params"] = {}

        if agent.get("guardrails_config"):
            try:
                agent["guardrails_config"] = json.loads(agent["guardrails_config"])
            except json.JSONDecodeError:
                agent["guardrails_config"] = None

        # Parse stringified lists into proper JSON arrays
        agent["agent_tools"] = _parse_string_list(agent.get("agent_tools"))
        agent["sub_agents"] = _parse_string_list(agent.get("sub_agents"))
        agent["tags"] = _parse_string_list(agent.get("tags"))
        agent["agent_skills"] = _parse_string_list(agent.get("agent_skills"))

        agent["memory_enabled"] = (agent.get("memory_enabled") or "false").lower() == "true"
        agent["artifacts_enabled"] = (agent.get("artifacts_enabled") or "false").lower() == "true"

        if agent.get("mcp_servers"):
            try:
                mcp_list = json.loads(agent["mcp_servers"])
                for srv in mcp_list:
                    if srv.get("params"):
                        srv["params"] = decrypt_value_in_dict(
                            srv["params"],
                            values_to_decrypt=srv["params"].keys(),
                        )
                    if srv.get("env"):
                        srv["env"] = decrypt_value_in_dict(
                            srv["env"],
                            values_to_decrypt=srv["env"].keys(),
                        )
                agent["mcp_servers"] = mcp_list
            except json.JSONDecodeError:
                agent["mcp_servers"] = None

        return agent
    return {}


def get_agent_by_id(agent_id: str, user_id: str) -> dict | None:
    """
    Get agent information by agent_id.
    """
    # Extract numeric ID from agent_id (handle both "agent95" and "95")
    if agent_id.startswith("agent"):
        numeric_id = int(agent_id.replace("agent", ""))
    else:
        numeric_id = int(agent_id)
    
    # Query for agent by ID and owner
    select_query = agent_store.agent_table.select().where(
        agent_store.agent_table.c.agent_id == numeric_id,
        agent_store.agent_table.c.owner_id == user_id,
    )
    result = agent_store.get_list_agents(select_query)
    agents = [u._asdict() for u in result]
    
    if not agents:
        return None
    
    agent = agents[0]
    
    # Return formatted agent info (similar to get_agent but simpler)
    return {
        "id": agent["agent_id"],
        "agent_id": f"agent{agent['agent_id']}",
        "agent_name": agent["agent_name"],
        "agent_model": agent["agent_model"],
        "agent_description": agent["agent_description"],
        "agent_instruction": agent["agent_instruction"],
        "owner_id": agent["owner_id"],
        "organization_id": agent["organization_id"],
        "created_at": agent["created_at"],
        "updated_at": agent["updated_at"],
        "status": agent["status"],
    }


def propagate_api_key(
    agent_id: int,
    user_id: str,
    model_api_key: str,
    model: str | None = None,
    model_api_base: str | None = None,
) -> dict:
    """Propagate the parent's API key (and optionally model) to all sub-agents recursively."""
    # reveal_secrets : on REECRIT la cle des sous-agents, un masque propage
    # remplacerait leur cle par la chaine sentinelle.
    agent = get_agent(agent_id, user_id=user_id, reveal_secrets=True)
    if not agent:
        return {"propagated_to": [], "count": 0}

    # Use the parent's model as the source of truth for propagation
    effective_model = model or agent.get("agent_model", "")

    sub_agent_ids = _parse_string_list(agent.get("sub_agents"))
    if not sub_agent_ids:
        return {"propagated_to": [], "count": 0}

    propagated = []

    for sub_id_str in sub_agent_ids:
        numeric_id = int(sub_id_str.replace("agent", ""))
        sub_agent = get_agent(numeric_id, user_id=user_id, reveal_secrets=True)
        if not sub_agent:
            continue

        # Build the updated model_params with the parent's key
        current_params = sub_agent.get("agent_model_params") or {}
        if isinstance(current_params, str):
            try:
                current_params = json.loads(current_params)
            except json.JSONDecodeError:
                current_params = {}
        updated_params = {**current_params, "model_api_key": model_api_key}
        if model_api_base:
            updated_params["model_api_base"] = model_api_base

        # Build a full AgentCreateSchema from the sub-agent's current data to call update_agent
        sub_tools = _parse_string_list(sub_agent.get("agent_tools"))
        sub_sub_agents = _parse_string_list(sub_agent.get("sub_agents"))
        sub_tags = _parse_string_list(sub_agent.get("tags"))
        sub_skills = _parse_string_list(sub_agent.get("agent_skills"))

        mem = sub_agent.get("memory_enabled", False)
        if isinstance(mem, str):
            mem = mem.lower() == "true"
        art = sub_agent.get("artifacts_enabled", False)
        if isinstance(art, str):
            art = art.lower() == "true"

        gc = sub_agent.get("guardrails_config")
        if isinstance(gc, str):
            try:
                gc = json.loads(gc)
            except json.JSONDecodeError:
                gc = None

        loop_max = sub_agent.get("loop_max_iterations")
        if loop_max is not None:
            try:
                loop_max = int(loop_max)
            except (ValueError, TypeError):
                loop_max = None

        # Handle MCP servers - need to convert back to McpServerConfig objects
        mcp_servers = None
        raw_mcp = sub_agent.get("mcp_servers")
        if raw_mcp:
            if isinstance(raw_mcp, str):
                try:
                    raw_mcp = json.loads(raw_mcp)
                except json.JSONDecodeError:
                    raw_mcp = None
            if isinstance(raw_mcp, list):
                from apowerb.schema.agent_schema import McpServerConfig
                mcp_servers = [McpServerConfig(**srv) for srv in raw_mcp]

        # Handle input_schema / output_schema - already parsed dicts from get_agent()
        input_sch = sub_agent.get("input_schema")
        if isinstance(input_sch, str):
            try:
                input_sch = json.loads(input_sch)
            except json.JSONDecodeError:
                input_sch = None

        output_sch = sub_agent.get("output_schema")
        if isinstance(output_sch, str):
            try:
                output_sch = json.loads(output_sch)
            except json.JSONDecodeError:
                output_sch = None

        update_schema = AgentCreateSchema(
            agent_name=sub_agent.get("agent_name", ""),
            agent_model=effective_model,
            agent_model_params=updated_params,
            agent_description=sub_agent.get("agent_description", ""),
            agent_instruction=sub_agent.get("agent_instruction", ""),
            agent_tools=sub_tools,
            agent_type=sub_agent.get("agent_type", "base"),
            sub_agents=sub_sub_agents if sub_sub_agents else None,
            code_executor=sub_agent.get("code_executor"),
            input_schema=input_sch,
            output_schema=output_sch,
            memory_enabled=mem,
            artifacts_enabled=art,
            guardrails_config=gc if isinstance(gc, dict) else None,
            superagent_template_id=sub_agent.get("superagent_template_id"),
            loop_max_iterations=loop_max,
            loop_exit_instruction=sub_agent.get("loop_exit_instruction"),
            mcp_servers=mcp_servers,
            agent_skills=sub_skills if sub_skills else None,
            tags=sub_tags if sub_tags else None,
        )

        # Inject owner_id/organization_id/project_id via model_copy (same pattern as agents router)
        update_schema_with_owner = update_schema.model_copy(
            update={
                "owner_id": user_id,
                "organization_id": sub_agent.get("organization_id", ""),
                "project_id": sub_agent.get("project_id", "thaink2"),
            }
        )

        update_agent(numeric_id, update_schema_with_owner, user_id=user_id)
        propagated.append(sub_id_str)

        # Recurse into this sub-agent's children
        if sub_sub_agents:
            child_result = propagate_api_key(numeric_id, user_id, model_api_key, model=effective_model, model_api_base=model_api_base)
            propagated.extend(child_result.get("propagated_to", []))

    return {"propagated_to": propagated, "count": len(propagated)}


def update_agent(agent_id: int, agent: AgentCreateSchema, user_id: str) -> dict:
    """Update an existing agent in the agent store."""
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Le front ne recoit plus la cle en clair mais un masque ; s'il nous le
    # renvoie tel quel, on recolle la valeur stockee. Sans ce recollage, un
    # simple renommage d'agent effacerait sa cle API.
    existing = get_agent(agent_id, user_id=user_id, reveal_secrets=True)
    model_params_to_store = unmask_model_api_key(
        agent.agent_model_params, existing.get("agent_model_params")
    )
    model_params_to_store = strip_default_llm_params(
        agent.agent_model, model_params_to_store
    )
    encrypted_agent_model_params = encrypt_value_in_dict(
        model_params_to_store,  # type: ignore
        values_to_encrypt=["model_api_key"],
    )

    mcp_servers_to_store = _encrypt_mcp_servers(agent.mcp_servers)

    update_query = (
        agent_store.agent_table.update()
        .where(
            agent_store.agent_table.c.agent_id == agent_id,
            agent_store.agent_table.c.owner_id == user_id,
        )
        .values(
            agent_name=agent.agent_name,
            agent_model=agent.agent_model,
            agent_model_params=json.dumps(encrypted_agent_model_params),
            agent_description=agent.agent_description,
            agent_instruction=agent.agent_instruction,
            agent_tools=json.dumps(agent.agent_tools or []),
            sub_agents=json.dumps(agent.sub_agents or []),
            # v2 sub-agent pipeline fields (PR #174 + #176).
            # Persisting these is what makes scei_ar_assistant_v2
            # actually function — without them, ADK output_key,
            # after_agent_callback validation, and skip-cascade
            # short-circuit all stay armed=False at runtime.
            output_key=agent.output_key,
            output_schema_name=agent.output_schema_name,
            skip_when_upstream=agent.skip_when_upstream,
            agent_type=agent.agent_type,
            code_executor=agent.code_executor,
            input_schema=json.dumps(agent.input_schema),
            output_schema=json.dumps(agent.output_schema),
            organization_id=agent.organization_id,
            project_id=agent.project_id,
            owner_id=agent.owner_id,
            updated_at=updated_at,
            guardrails_config=json.dumps(agent.guardrails_config) if agent.guardrails_config else None,
            memory_enabled=str(agent.memory_enabled).lower() if agent.memory_enabled else "false",
            artifacts_enabled=str(agent.artifacts_enabled).lower() if agent.artifacts_enabled else "false",
            superagent_template_id=agent.superagent_template_id,
            loop_max_iterations=str(agent.loop_max_iterations) if agent.loop_max_iterations else None,
            loop_exit_instruction=agent.loop_exit_instruction,
            mcp_servers=mcp_servers_to_store,
            agent_skills=json.dumps(agent.agent_skills) if agent.agent_skills else None,
            tags=json.dumps(agent.tags) if agent.tags else None,
        )
    )

    with agent_store.engine.begin() as conn:
        result = conn.execute(update_query)
        if result.rowcount == 0:
            raise HTTPException(
                status_code=403,
                detail="Agent not found or you do not have permission to update it.",
            )

    # Update the agent module file
    create_agent_module(
        agent_name=str(agent_id),
        description=agent.agent_description,
        instruction=agent.agent_instruction,
        tools=agent.agent_tools,
        model=agent.agent_model,
    )

    result = {
        "agent_id": f"agent{agent_id}",
        "agent_name": agent.agent_name,
        "message": "Agent updated successfully.",
    }

    # Propagate API key (and model) to sub-agents if requested.
    # On propage les params RECOLLES, pas ceux recus : le front nous renvoie
    # le masque quand l'utilisateur n'a pas touche au champ, et propager
    # `__unchanged__` ecraserait la cle de chaque sous-agent par une chaine
    # inutilisable.
    if getattr(agent, "propagate_api_key", False):
        params = model_params_to_store if isinstance(model_params_to_store, dict) else {}
        api_key = params.get("model_api_key")
        # Modele mutualise : aucune cle a propager (elle vit dans l'env), mais
        # le MODELE doit descendre pour que les sous-agents basculent aussi.
        if api_key or is_default_llm_model(agent.agent_model):
            prop_result = propagate_api_key(
                agent_id,
                user_id,
                api_key or "",
                model=agent.agent_model,
                model_api_base=params.get("model_api_base"),
            )
            if prop_result["count"] > 0:
                result["propagated_to"] = prop_result["propagated_to"]
                result["message"] += f" API key and model propagated to {prop_result['count']} sub-agent(s)."

    return result


def delete_agent(agent_id: str, user_id: str) -> None:
    """Delete an agent by ID from the agent store.

    The filesystem module is only removed when the DB row actually matched
    (id + owner_id) and was deleted. It used to run unconditionally: any
    authenticated user could DELETE /agents/{any_id} and, even though the
    owner_id filter above correctly left the DB row untouched for an agent
    they don't own, delete_agent_module() still ran on that same id and
    shutil.rmtree()'d the target agent's ADK folder — an unauthenticated-by-
    row-match, IDOR-driven destructive filesystem delete.
    """
    delete_query = agent_store.agent_table.delete().where(
        agent_store.agent_table.c.agent_id == agent_id,
        agent_store.agent_table.c.owner_id == user_id,
    )
    with agent_store.engine.connect() as conn:
        result = conn.execute(delete_query)
        conn.commit()
        deleted = result.rowcount > 0
    if deleted:
        delete_agent_module(
            agent_name=agent_id,
        )


def get_agent_folder_name(agent_name: str) -> str:
    """
    Get the directory name for an agent given its display name.
    If the agent name corresponds to an existing agent folder (agent{ID}), returns it.
    Otherwise looks up the agent by name in the DB and returns agent{ID}.
    Returns the original name if not found (fallback).
    """
    # If it already looks like an ID (agent123) and we might want to check existence,
    # but for now let's assume if it is agent+digits it is likely good or we check DB.
    # Actually, the user passes "david_agent1".

    # Try to find by name
    select_query = agent_store.agent_table.select().where(
        agent_store.agent_table.c.agent_name == agent_name
    )
    result = agent_store.get_list_agents(select_query)
    agents = [u._asdict() for u in result]

    if agents:
        agent_id = agents[0]["agent_id"]
        return f"agent{agent_id}"

    return agent_name