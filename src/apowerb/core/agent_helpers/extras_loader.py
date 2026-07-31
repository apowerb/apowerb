"""Supplementary tool loaders: skills, SuperAgent recommended tools, BI dashboard.

Extracted from ``agent_utils`` to keep the main builder below the 500-line
threshold.
"""
from __future__ import annotations

import json
import os

from apowerb.configs.th2logger import setup_logging
from apowerb.helpers.encryptor import decrypt_value_in_dict, dict_to_envvar
from apowerb.tools_store.tools_helpers import (
    load_agent_tools_functions,
    tool_config_store,
)


logger = setup_logging(__name__)


def load_agent_skills_toolset(agent_name: str, agent_skills_raw, tools_funcs: list) -> None:
    """Parse ``agent_skills`` and append the resulting skill toolset (if any)."""
    raw = agent_skills_raw or "[]"
    try:
        agent_skill_names = (
            json.loads(raw) if isinstance(raw, str) else (raw or [])
        )
    except (json.JSONDecodeError, TypeError):
        agent_skill_names = []
    if not agent_skill_names:
        return
    from apowerb.skills_store.skills_loader import load_agent_skills

    skill_toolset = load_agent_skills(agent_skill_names)
    if skill_toolset:
        tools_funcs.append(skill_toolset)
        logger.info(
            "[TO_AGENT] Loaded %d skill(s) for %s: %s",
            len(agent_skill_names),
            agent_name,
            agent_skill_names,
        )


def _inject_tool_config_env(new_tool_paths: list[str], owner_id: str) -> None:
    """Inject env vars from owner's tool_configs for each recommended-tool category."""
    if not owner_id:
        return
    categories = {tp.split(".")[0] for tp in new_tool_paths if "." in tp}
    t = tool_config_store.tool_config_table
    for cat in categories:
        q = t.select().where(
            (t.c.tool_category == cat) & (t.c.owner_id == owner_id)
        )
        rows = tool_config_store.get_list_tool_configs(q)
        if rows:
            row = rows[0]._asdict()
            raw_params = row.get("tool_config_params")
            if raw_params:
                params = (
                    json.loads(raw_params) if isinstance(raw_params, str) else raw_params
                )
                params = decrypt_value_in_dict(
                    params, values_to_decrypt=params.keys()
                )
                dict_to_envvar(params)
                logger.info(
                    f"[TO_AGENT] Injected tool_config env vars for category '{cat}' (owner={owner_id})"
                )
        else:
            logger.warning(
                f"[TO_AGENT] No tool_config found for category '{cat}' owner={owner_id}"
            )


def load_superagent_recommended_tools(
    agent_details: dict,
    tools_names: list,
    tools_funcs: list,
    owner_id: str,
) -> None:
    """Resolve SuperAgent template ``recommended_tools`` at runtime."""
    superagent_template_id = agent_details.get("superagent_template_id")
    if not superagent_template_id:
        return
    from apowerb.core.superagents import get_superagent_template

    template = get_superagent_template(superagent_template_id)
    if not template:
        logger.warning(
            f"[TO_AGENT] SuperAgent template '{superagent_template_id}' not found, skipping native tools"
        )
        return

    recommended = template.get("recommended_tools", [])
    new_tool_paths = [t for t in recommended if t not in tools_names]
    if not new_tool_paths:
        return

    logger.info(
        f"[TO_AGENT] SuperAgent template '{superagent_template_id}': resolving {len(new_tool_paths)} native tool(s)"
    )
    native_names, native_funcs = load_agent_tools_functions(
        tools=new_tool_paths, owner_id=owner_id
    )
    tools_names.extend(native_names)
    tools_funcs.extend(native_funcs)
    logger.info(f"[TO_AGENT] Merged native tools: {native_names}")

    # Inject env vars from owner's tool_configs for recommended tool categories
    _inject_tool_config_env(new_tool_paths, agent_details.get("owner_id"))


def inject_bi_dashboard_tools(
    agent_name: str, tools_names: list, tools_funcs: list, owner_id: str
) -> None:
    """Auto-inject BI dashboard tools when ``AGENT_DASHBOARD_ID`` is set."""
    dashboard_id = os.environ.get("AGENT_DASHBOARD_ID", "")
    if not dashboard_id:
        return
    logger.info(
        "[TO_AGENT] Agent %s has dashboard context (%s) — injecting BI tools",
        agent_name,
        dashboard_id,
    )
    bi_tools = [
        "business_intelligence.tool_get_dashboard_data",
        "business_intelligence.tool_create_chart",
        "business_intelligence.tool_add_chart_to_dashboard",
        "business_intelligence.tool_add_kpi_to_dashboard",
        "business_intelligence.tool_update_chart_data",
        "business_intelligence.tool_update_chart",
        "business_intelligence.tool_remove_chart_from_dashboard",
        "business_intelligence.tool_list_dashboards",
    ]
    new_bi = [t for t in bi_tools if t not in tools_names]
    if not new_bi:
        return
    dash_names, dash_funcs = load_agent_tools_functions(
        tools=new_bi, owner_id=owner_id
    )
    tools_names.extend(dash_names)
    tools_funcs.extend(dash_funcs)
