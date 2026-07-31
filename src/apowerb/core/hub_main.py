"""Agent Hub — publish, list, clone agents across users."""

import json
import re
from datetime import datetime

from fastapi import HTTPException

from apowerb.agent_store.hub_manager import HubStore
from apowerb.configs.th2logger import setup_logging
from apowerb.core.agent_main import _parse_string_list, get_agent, register_agent
from apowerb.schema.agent_schema import AgentCreateSchema
from apowerb.schema.hub_schema import HubPublishSchema

logger = setup_logging(__name__)

# DDL déplacé dans helpers/store_migrations.ensure_store_tables(),
# appelé au boot : importer ce module ne doit pas toucher la base.
hub_store = HubStore()

MAX_NESTING_DEPTH = 3


def _filter_native_tools(tools: list | None) -> list:
    """Keep only native tools (category.tool_name format), strip tool_config references."""
    if not tools:
        return []
    return [t for t in tools if isinstance(t, str) and "." in t and not re.match(r"^tool_config\d+$", t)]


def _build_sub_agents_snapshot(sub_agent_ids: list[str], user_id: str, depth: int = 0) -> list[dict]:
    """Recursively build snapshots of sub-agents for hub publishing."""
    if depth >= MAX_NESTING_DEPTH:
        logger.warning(f"Max nesting depth ({MAX_NESTING_DEPTH}) reached, stopping recursion.")
        return []

    snapshots = []
    for agent_id_str in sub_agent_ids:
        # Extract numeric ID
        numeric_id = int(agent_id_str.replace("agent", ""))
        agent = get_agent(numeric_id, user_id=user_id)
        if not agent:
            logger.warning(f"Sub-agent {agent_id_str} not found during publish, skipping.")
            continue

        # Parse sub-agent's own sub_agents
        child_sub_agents = _parse_string_list(agent.get("sub_agents"))
        child_snapshot = []
        if child_sub_agents:
            child_snapshot = _build_sub_agents_snapshot(child_sub_agents, user_id, depth + 1)

        # Parse tools and skills
        agent_tools = _parse_string_list(agent.get("agent_tools"))
        agent_skills = _parse_string_list(agent.get("agent_skills"))

        # Parse boolean fields
        mem = agent.get("memory_enabled", False)
        if isinstance(mem, str):
            mem = mem.lower() == "true"
        art = agent.get("artifacts_enabled", False)
        if isinstance(art, str):
            art = art.lower() == "true"

        # Parse guardrails
        gc = agent.get("guardrails_config")
        if isinstance(gc, str):
            try:
                gc = json.loads(gc)
            except json.JSONDecodeError:
                gc = None

        snapshot = {
            "original_id": agent_id_str,
            "agent_name": agent.get("agent_name", ""),
            "agent_model": agent.get("agent_model", ""),
            "agent_description": agent.get("agent_description", ""),
            "agent_instruction": agent.get("agent_instruction", ""),
            "agent_tools": _filter_native_tools(agent_tools),
            "agent_type": agent.get("agent_type", "base"),
            "sub_agents_snapshot": child_snapshot,
            "memory_enabled": mem,
            "artifacts_enabled": art,
            "guardrails_config": gc,
            "loop_max_iterations": agent.get("loop_max_iterations"),
            "loop_exit_instruction": agent.get("loop_exit_instruction"),
            "agent_skills": agent_skills,
        }
        snapshots.append(snapshot)

    return snapshots


def _clone_sub_agents_from_snapshot(snapshot: list[dict], user_id: str, org_id: str) -> list[str]:
    """Recursively clone sub-agents from a hub snapshot, depth-first."""
    new_agent_ids = []

    for sub in snapshot:
        # Recursively clone nested sub-agents first (depth-first)
        child_ids = None
        child_snapshot = sub.get("sub_agents_snapshot", [])
        if child_snapshot:
            child_ids = _clone_sub_agents_from_snapshot(child_snapshot, user_id, org_id)

        # Parse boolean fields
        mem = sub.get("memory_enabled", False)
        if isinstance(mem, str):
            mem = mem.lower() == "true"
        art = sub.get("artifacts_enabled", False)
        if isinstance(art, str):
            art = art.lower() == "true"

        # Parse guardrails
        gc = sub.get("guardrails_config")
        if isinstance(gc, str):
            try:
                gc = json.loads(gc)
            except json.JSONDecodeError:
                gc = None

        # Parse loop_max_iterations
        loop_max = sub.get("loop_max_iterations")
        if loop_max is not None:
            try:
                loop_max = int(loop_max)
            except (ValueError, TypeError):
                loop_max = None

        base_name = sub.get("agent_name", "sub_agent")
        clone_name = base_name

        # Try to register, handle name collisions
        for attempt in range(5):
            try:
                clone_data = AgentCreateSchema(
                    agent_name=clone_name,
                    agent_model=sub.get("agent_model", ""),
                    agent_model_params=None,
                    agent_description=sub.get("agent_description", ""),
                    agent_instruction=sub.get("agent_instruction", ""),
                    agent_tools=sub.get("agent_tools", []),
                    agent_type=sub.get("agent_type", "base"),
                    sub_agents=child_ids,
                    memory_enabled=mem,
                    artifacts_enabled=art,
                    guardrails_config=gc if isinstance(gc, dict) else None,
                    loop_max_iterations=loop_max,
                    loop_exit_instruction=sub.get("loop_exit_instruction"),
                    agent_skills=sub.get("agent_skills"),
                )
                clone_data_with_owner = clone_data.model_copy(
                    update={"owner_id": user_id, "organization_id": org_id}
                )
                result = register_agent(clone_data_with_owner)
                new_agent_ids.append(result["agent_id"])
                break
            except HTTPException as e:
                if e.status_code == 409 and attempt < 4:
                    clone_name = f"{base_name}_{attempt + 2}"
                else:
                    raise

    return new_agent_ids


def publish_agent(data: HubPublishSchema, user_id: str, org_id: str) -> dict:
    """Publish an agent to the Hub (snapshot without API keys)."""
    numeric_id = int(data.agent_id.replace("agent", ""))
    agent = get_agent(numeric_id, user_id=user_id)
    if not agent:
        return {"error": "Agent not found or not owned by you."}

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Build recursive sub-agents snapshot
    sub_agent_ids = _parse_string_list(agent.get("sub_agents"))
    sub_agents_snapshot = None
    if sub_agent_ids:
        snapshot_list = _build_sub_agents_snapshot(sub_agent_ids, user_id)
        sub_agents_snapshot = json.dumps(snapshot_list) if snapshot_list else None

    # Filter agent tools to keep only native tools
    agent_tools = _parse_string_list(agent.get("agent_tools"))
    native_tools = _filter_native_tools(agent_tools)

    # Parse agent_skills
    agent_skills = _parse_string_list(agent.get("agent_skills"))

    # Never copy agent_model_params (contains API keys)
    insert_q = (
        hub_store.hub_table.insert()
        .values(
            hub_name=data.hub_name,
            hub_description=data.hub_description,
            hub_category=data.hub_category,
            hub_tags=json.dumps(data.hub_tags) if data.hub_tags else "[]",
            agent_name=agent.get("agent_name", ""),
            agent_model=agent.get("agent_model", ""),
            agent_description=agent.get("agent_description", ""),
            agent_instruction=agent.get("agent_instruction", ""),
            agent_tools=json.dumps(native_tools),
            agent_type=agent.get("agent_type", "base"),
            sub_agents=str(agent.get("sub_agents", "[]")),
            sub_agents_snapshot=sub_agents_snapshot,
            memory_enabled=str(agent.get("memory_enabled", False)).lower(),
            artifacts_enabled=str(agent.get("artifacts_enabled", False)).lower(),
            guardrails_config=(
                json.dumps(agent.get("guardrails_config"))
                if agent.get("guardrails_config")
                else None
            ),
            loop_max_iterations=str(agent.get("loop_max_iterations")) if agent.get("loop_max_iterations") else None,
            loop_exit_instruction=agent.get("loop_exit_instruction"),
            agent_skills=json.dumps(agent_skills) if agent_skills else None,
            publisher_id=user_id,
            publisher_org=org_id,
            source_agent_id=data.agent_id,
            clone_count=0,
            published_at=now,
            updated_at=now,
            status="active",
        )
        .returning(hub_store.hub_table.c.hub_id)
    )

    with hub_store.engine.begin() as conn:
        hub_id = conn.execute(insert_q).scalar_one()

    sub_count = len(sub_agent_ids) if sub_agent_ids else 0
    msg = "Agent published to Hub."
    if sub_count > 0:
        msg += f" {sub_count} sub-agent(s) included in snapshot."

    return {
        "hub_id": f"hub{hub_id}",
        "hub_name": data.hub_name,
        "message": msg,
    }


def list_hub_agents() -> list[dict]:
    """List all published agents in the Hub."""
    select_q = hub_store.hub_table.select().where(
        hub_store.hub_table.c.status == "active"
    )
    result = hub_store.get_list(select_q)
    agents = []
    for row in result:
        d = row._asdict()
        d["hub_id"] = f"hub{d['hub_id']}"
        if d.get("hub_tags"):
            try:
                d["hub_tags"] = json.loads(d["hub_tags"])
            except json.JSONDecodeError:
                d["hub_tags"] = []
        if d.get("guardrails_config"):
            try:
                d["guardrails_config"] = json.loads(d["guardrails_config"])
            except json.JSONDecodeError:
                d["guardrails_config"] = None
        d["memory_enabled"] = (d.get("memory_enabled") or "false").lower() == "true"
        d["artifacts_enabled"] = (d.get("artifacts_enabled") or "false").lower() == "true"
        # Parse sub_agents_snapshot
        if d.get("sub_agents_snapshot"):
            try:
                d["sub_agents_snapshot"] = json.loads(d["sub_agents_snapshot"])
            except json.JSONDecodeError:
                d["sub_agents_snapshot"] = None
        # Parse agent_skills
        if d.get("agent_skills"):
            try:
                d["agent_skills"] = json.loads(d["agent_skills"])
            except json.JSONDecodeError:
                d["agent_skills"] = None
        # Parse agent_tools (now stored as JSON)
        if d.get("agent_tools"):
            try:
                parsed_tools = json.loads(d["agent_tools"])
                if isinstance(parsed_tools, list):
                    d["agent_tools"] = parsed_tools
            except json.JSONDecodeError:
                d["agent_tools"] = []
        agents.append(d)
    return agents


def get_hub_agent(hub_id: str) -> dict | None:
    """Get a specific hub agent by ID."""
    numeric_id = int(hub_id.replace("hub", ""))
    select_q = hub_store.hub_table.select().where(
        hub_store.hub_table.c.hub_id == numeric_id
    )
    result = hub_store.get_list(select_q)
    rows = [r._asdict() for r in result]
    if rows:
        d = rows[0]
        d["hub_id"] = f"hub{d['hub_id']}"
        if d.get("hub_tags"):
            try:
                d["hub_tags"] = json.loads(d["hub_tags"])
            except json.JSONDecodeError:
                d["hub_tags"] = []
        if d.get("guardrails_config"):
            try:
                d["guardrails_config"] = json.loads(d["guardrails_config"])
            except json.JSONDecodeError:
                d["guardrails_config"] = None
        d["memory_enabled"] = (d.get("memory_enabled") or "false").lower() == "true"
        d["artifacts_enabled"] = (d.get("artifacts_enabled") or "false").lower() == "true"
        # Parse sub_agents_snapshot
        if d.get("sub_agents_snapshot"):
            try:
                d["sub_agents_snapshot"] = json.loads(d["sub_agents_snapshot"])
            except json.JSONDecodeError:
                d["sub_agents_snapshot"] = None
        # Parse agent_skills
        if d.get("agent_skills"):
            try:
                d["agent_skills"] = json.loads(d["agent_skills"])
            except json.JSONDecodeError:
                d["agent_skills"] = None
        # Parse agent_tools (now stored as JSON)
        if d.get("agent_tools"):
            try:
                parsed_tools = json.loads(d["agent_tools"])
                if isinstance(parsed_tools, list):
                    d["agent_tools"] = parsed_tools
            except json.JSONDecodeError:
                d["agent_tools"] = []
        return d
    return None


def _collect_tools_params(all_tools: list[str]) -> list[dict]:
    """Collect required config params for each tool category.

    Groups tools by category, calls ToolsStore.get_tool_expected_params()
    for each unique category, and returns only categories that have params.
    """
    from apowerb.tools_store.tool_manager import ToolsStore
    tools_store = ToolsStore()

    # Group tools by category
    categories: dict[str, list[str]] = {}
    for t in all_tools:
        if "." in t:
            cat = t.split(".")[0]
            categories.setdefault(cat, []).append(t)

    result = []
    for cat, tools in categories.items():
        params = tools_store.get_tool_expected_params(f"{cat}.dummy")
        if params:  # Only include categories that need config
            result.append({
                "category": cat,
                "tools": tools,
                "required_params": params,
            })
    return result


def _collect_all_tools_from_snapshot(snapshot: list[dict]) -> list[str]:
    """Recursively collect all tool names from a sub-agents snapshot."""
    tools = []
    for sub in snapshot:
        sub_tools = sub.get("agent_tools", [])
        if isinstance(sub_tools, str):
            try:
                sub_tools = json.loads(sub_tools)
            except (json.JSONDecodeError, TypeError):
                sub_tools = []
        if isinstance(sub_tools, list):
            tools.extend(sub_tools)
        # Recurse into nested sub-agents
        nested = sub.get("sub_agents_snapshot", [])
        if nested and isinstance(nested, list):
            tools.extend(_collect_all_tools_from_snapshot(nested))
    return tools


def clone_hub_agent(hub_id: str, user_id: str, org_id: str, clone_name: str | None = None) -> dict:
    """Clone a hub agent into the user's own agent store."""
    hub_agent = get_hub_agent(hub_id)
    if not hub_agent:
        raise HTTPException(status_code=404, detail="Hub agent not found.")

    mem = hub_agent.get("memory_enabled", False)
    if isinstance(mem, str):
        mem = mem.lower() == "true"

    art = hub_agent.get("artifacts_enabled", False)
    if isinstance(art, str):
        art = art.lower() == "true"

    gc = hub_agent.get("guardrails_config")
    if isinstance(gc, str):
        try:
            gc = json.loads(gc)
        except json.JSONDecodeError:
            gc = None

    final_name = clone_name.strip() if clone_name and clone_name.strip() else f"{hub_agent['agent_name']}_clone"

    # Recursively clone sub-agents if snapshot exists
    new_sub_agent_ids = None
    sub_count = 0
    sub_agents_snapshot = hub_agent.get("sub_agents_snapshot")
    if sub_agents_snapshot:
        snapshot_list = sub_agents_snapshot if isinstance(sub_agents_snapshot, list) else []
        if snapshot_list:
            try:
                new_sub_agent_ids = _clone_sub_agents_from_snapshot(snapshot_list, user_id, org_id)
                sub_count = len(new_sub_agent_ids)
                logger.info(f"[CLONE] Cloned {sub_count} sub-agent(s): {new_sub_agent_ids}")
            except Exception as e:
                logger.error(f"[CLONE] Failed to clone sub-agents from snapshot: {e}", exc_info=True)
                new_sub_agent_ids = None
                sub_count = 0

    # Restore native tools from hub (already filtered during publish)
    hub_tools = hub_agent.get("agent_tools", [])
    if isinstance(hub_tools, str):
        try:
            hub_tools = json.loads(hub_tools)
        except (json.JSONDecodeError, TypeError):
            hub_tools = []
    if not isinstance(hub_tools, list):
        hub_tools = []

    # Parse loop config
    loop_max = hub_agent.get("loop_max_iterations")
    if loop_max is not None:
        try:
            loop_max = int(loop_max)
        except (ValueError, TypeError):
            loop_max = None

    # Parse agent_skills
    hub_skills = hub_agent.get("agent_skills")
    if isinstance(hub_skills, str):
        try:
            hub_skills = json.loads(hub_skills)
        except (json.JSONDecodeError, TypeError):
            hub_skills = None
    if not isinstance(hub_skills, list):
        hub_skills = None

    logger.info(f"[CLONE] Creating parent agent '{final_name}' with sub_agents={new_sub_agent_ids}")
    clone_data = AgentCreateSchema(
        agent_name=final_name,
        agent_model=hub_agent.get("agent_model", ""),
        agent_model_params=None,
        agent_description=hub_agent.get("agent_description", ""),
        agent_instruction=hub_agent.get("agent_instruction", ""),
        agent_tools=hub_tools,
        agent_type=hub_agent.get("agent_type", "base"),
        sub_agents=new_sub_agent_ids,
        memory_enabled=mem,
        artifacts_enabled=art,
        guardrails_config=gc if isinstance(gc, dict) else None,
        loop_max_iterations=loop_max,
        loop_exit_instruction=hub_agent.get("loop_exit_instruction"),
        agent_skills=hub_skills,
        hub_origin_id=hub_id,
    )

    # Inject owner_id/organization_id via model_copy (same pattern as agents router)
    clone_data_with_owner = clone_data.model_copy(
        update={"owner_id": user_id, "organization_id": org_id}
    )

    result = register_agent(clone_data_with_owner)

    # Increment clone count
    numeric_hub_id = int(hub_id.replace("hub", ""))
    update_q = (
        hub_store.hub_table.update()
        .where(hub_store.hub_table.c.hub_id == numeric_hub_id)
        .values(clone_count=hub_store.hub_table.c.clone_count + 1)
    )
    with hub_store.engine.begin() as conn:
        conn.execute(update_q)

    # Collect all tools requiring configuration (parent + sub-agents)
    all_tools = list(hub_tools)  # parent tools
    snapshot = hub_agent.get("sub_agents_snapshot")
    if snapshot:
        snap_list = snapshot if isinstance(snapshot, list) else []
        all_tools.extend(_collect_all_tools_from_snapshot(snap_list))
    # Deduplicate
    all_tools = list(dict.fromkeys(all_tools))
    tools_params = _collect_tools_params(all_tools)

    msg = "Agent cloned successfully. Please add your own API key."
    if sub_count > 0:
        msg = f"Agent and {sub_count} sub-agent(s) cloned successfully. Add your API key and use 'Propagate key' to configure all agents."

    return {
        **result,
        "cloned_from": hub_id,
        "sub_agents_cloned": sub_count,
        "sub_agent_ids": new_sub_agent_ids or [],
        "agent_model": hub_agent.get("agent_model", ""),
        "agent_type": hub_agent.get("agent_type", "base"),
        "tools_requiring_config": tools_params,
        "message": msg,
    }


def delete_hub_agent(hub_id: str, user_id: str) -> dict:
    """Remove an agent from the Hub (publisher only)."""
    numeric_id = int(hub_id.replace("hub", ""))
    hub_agent = get_hub_agent(hub_id)
    if not hub_agent:
        raise HTTPException(status_code=404, detail="Hub agent not found.")
    if hub_agent.get("publisher_id") != user_id:
        raise HTTPException(
            status_code=403,
            detail="Only the publisher can remove this agent from the Hub.",
        )

    delete_q = hub_store.hub_table.delete().where(
        hub_store.hub_table.c.hub_id == numeric_id
    )
    with hub_store.engine.begin() as conn:
        conn.execute(delete_q)

    return {"message": "Agent removed from Hub."}
