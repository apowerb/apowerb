import json
import os
import signal
import subprocess
from logging import getLogger
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, status
from th2agent.tools_store.tool_manager import get_tools_store
from th2agent.tools_store.tools_helpers import (
    delete_tool_config,
    fetch_tool_configs,
    find_system_tool_config,
    list_all_tool_configs,
    list_user_tool_configs,
    register_tool_config,
    update_tool_config,
)
from th2agent.schema.tool_config_schema import ToolConfigCreateSchema
from th2agent.auth.dependencies import get_current_user
from th2agent.users import schemas as user_schemas
from th2agent.helpers.emails import get_domain_from_email
from th2agent.configs.paths import toolbox_dir

logger = getLogger(__name__)
router = APIRouter()

# Toolbox dir = the deployment root (where the toolbox binary, tools.yaml,
# and PID/log files live). systemd sets WorkingDirectory to that path
# (/home/ubuntu/thaink2/th2agent on the VMs). `Path(__file__).parents[N]` was
# unreliable: N differs between running from src/ and running installed in
# .venv/site-packages. TOOLBOX_DIR can override for exotic setups.
#
# Résolu à l'appel et non plus à l'import : figer le CWD au chargement du
# module rendait le paquet dépendant du répertoire depuis lequel il est
# importé (cf configs/paths.py).
def _toolbox_dir() -> Path:
    return toolbox_dir()


@router.get("/tools", tags=["tools"])
async def list_tools(tools_store=Depends(get_tools_store), _current_user: user_schemas.User = Depends(get_current_user)):
    """Endpoint to list all available tools."""
    return tools_store.get_all_tools()


@router.get("/tools/docs", tags=["tools"])
async def get_tools_docs(tools_store=Depends(get_tools_store), _current_user: user_schemas.User = Depends(get_current_user)):
    """Return documentation for all available tools."""
    return tools_store.get_all_tools_docs()


@router.get("/tools/{tool_name:path}/params", tags=["tools"])
async def get_tool_params(tool_name: str, tools_store=Depends(get_tools_store), _current_user: user_schemas.User = Depends(get_current_user)):
    """Return expected configuration parameters (env vars) for a tool."""
    return tools_store.get_tool_expected_params(tool_name)


@router.get("/tools_config", tags=["tools_config"])
async def list_tool_configs(
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Endpoint to list all tool configs for the current user."""
    return list_user_tool_configs(user_id=current_user.email)


@router.get("/tools_config/{tool_config_id}", tags=["tools_config"])
async def get_tool_configs(
    tool_config_id: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Endpoint to get a specific tool config by ID (scoped to current user)."""
    tools_configs = fetch_tool_configs(
        tool_config_id=tool_config_id,
        owner_id=current_user.email,
    )
    return tools_configs


@router.post("/tools_config", tags=["tools_config"])
async def create_tool_configs(
    tool_config: ToolConfigCreateSchema,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Endpoint to create a new tool config."""
    if tool_config.owner_id == "system":
        # SuperAgent shared tools — check if already exists to avoid duplicates
        existing = find_system_tool_config(tool_name=tool_config.tool_name)
        if existing:
            return existing
        tool_config_with_owner = tool_config
    else:
        organization_d = get_domain_from_email(current_user.email)
        tool_config_with_owner = tool_config.model_copy(
            update={"owner_id": current_user.email, "organization_id": organization_d}
        )
    return register_tool_config(tool_config=tool_config_with_owner)


@router.delete("/tools_config/{tool_config_id}", tags=["tools_config"])
async def delete_tool_configs(
    tool_config_id: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Endpoint to delete a tool config by ID (scoped to current user)."""
    return delete_tool_config(
        tool_config_id=tool_config_id,
        owner_id=current_user.email,
    )


@router.put("/tools_config/{tool_config_id}", tags=["tools_config"])
async def update_tool_configs(
    tool_config_id: str,
    updates: dict,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Update a tool config's name and/or parameters (scoped to current user)."""
    return update_tool_config(
        tool_config_id=tool_config_id,
        updates=updates,
        owner_id=current_user.email,
    )


@router.get("/mcp_configs", tags=["tools"])
async def list_mcp_configs(
    current_user: user_schemas.User = Depends(get_current_user),
):
    """List saved MCP server configurations for the current user."""
    configs = list_user_tool_configs(user_id=current_user.email)
    mcp_configs = [c for c in configs if c.get("tool_category") == "mcp_server"]
    result = []
    for c in mcp_configs:
        params = c.get("tool_config_params", {})
        transport = params.get("transport", "http")
        mcp_type = params.get("mcp_type", "")
        mcp_server = {
            "mcp_config_id": c["tool_config_id"],
            "name": params.get("name", c.get("tool_config_name", "")),
            "transport": transport,
            "toolset": params.get("toolset", ""),
            "mcp_type": mcp_type,
        }
        if mcp_type == "toolbox-db":
            mcp_server["db_config"] = {
                "db_type": params.get("db_type", "postgres"),
                "host": params.get("db_host", ""),
                "port": params.get("db_port", ""),
                "database": params.get("db_database", ""),
                "user": params.get("db_user", ""),
                "sslmode": params.get("db_sslmode", "require"),
            }
            mcp_server["url"] = params.get("url", "")
        if transport == "stdio":
            mcp_server["command"] = params.get("command", "")
            mcp_server["args"] = params.get("args", "").split(" ") if params.get("args") else []
            env = {k[4:]: v for k, v in params.items() if k.startswith("env_")}
            mcp_server["env"] = env
        else:
            mcp_server["url"] = params.get("url", "")
            headers = {k[7:]: v for k, v in params.items() if k.startswith("header_")}
            mcp_server["headers"] = headers
            mcp_params = {k[6:]: v for k, v in params.items() if k.startswith("param_")}
            mcp_server["params"] = mcp_params
        result.append(mcp_server)
    return result


def _build_mcp_tool_params(mcp_data: dict) -> dict:
    """Translate the UI payload into the flat ``tool_config_params`` dict
    that the helper layer encrypts and stores. Shared by POST and PUT.
    """
    transport = mcp_data.get("transport", "http")
    mcp_type = mcp_data.get("mcp_type", "")
    tool_params = {
        "transport": transport,
        "name": mcp_data.get("name", ""),
        "toolset": mcp_data.get("toolset", ""),
        "mcp_type": mcp_type,
    }
    if mcp_type == "toolbox-db":
        db_cfg = mcp_data.get("db_config", {})
        tool_params["url"] = mcp_data.get("url", "http://localhost:5000")
        tool_params["db_type"] = db_cfg.get("db_type", "postgres")
        tool_params["db_host"] = db_cfg.get("host", "")
        tool_params["db_port"] = db_cfg.get("port", "5432")
        tool_params["db_database"] = db_cfg.get("database", "")
        tool_params["db_user"] = db_cfg.get("user", "")
        tool_params["db_password"] = db_cfg.get("password", "")
        tool_params["db_sslmode"] = db_cfg.get("sslmode", "require")
    elif transport == "stdio":
        tool_params["command"] = mcp_data.get("command", "")
        tool_params["args"] = " ".join(mcp_data.get("args", []))
        for k, v in mcp_data.get("env", {}).items():
            tool_params[f"env_{k}"] = v
    else:
        tool_params["url"] = mcp_data.get("url", "")
        for k, v in mcp_data.get("headers", {}).items():
            tool_params[f"header_{k}"] = v
        for k, v in mcp_data.get("params", {}).items():
            tool_params[f"param_{k}"] = v
    return tool_params


def _refresh_toolbox_if_db(mcp_type: str) -> None:
    """Regenerate tools.dynamic.yaml + respawn toolbox when an MCP-DB config
    has just been saved/updated. No-op for non-toolbox-db types.
    """
    if mcp_type != "toolbox-db":
        return
    try:
        _regenerate_toolbox_dynamic_config()
        _reload_toolbox_process()
    except Exception as exc:
        logger.exception("MCP Toolbox reload failed: %s", exc)


@router.post("/mcp_configs", tags=["tools"])
async def save_mcp_config(
    mcp_data: dict,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Save an MCP server configuration as a reusable tool_config."""
    config_name = mcp_data.get("config_name", mcp_data.get("name", "MCP Server"))
    transport = mcp_data.get("transport", "http")
    mcp_type = mcp_data.get("mcp_type", "")
    tool_params = _build_mcp_tool_params(mcp_data)

    tool_config = ToolConfigCreateSchema(
        tool_config_name=config_name,
        tool_name=f"mcp_{transport}",
        tool_config_params=tool_params,
        tool_category="mcp_server",
        tool_config_type="active",
        owner_id=current_user.email,
        organization_id=get_domain_from_email(current_user.email),
    )
    saved = register_tool_config(tool_config=tool_config)
    _refresh_toolbox_if_db(mcp_type)
    return saved


@router.put("/mcp_configs/{mcp_config_id}", tags=["tools"])
async def update_mcp_config(
    mcp_config_id: str,
    mcp_data: dict,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Update an existing MCP config (rebuilds tool_config_params from the
    payload, re-encrypts, and refreshes the toolbox if it is a DB config).
    """
    config_name = mcp_data.get("config_name", mcp_data.get("name", "MCP Server"))
    mcp_type = mcp_data.get("mcp_type", "")
    tool_params = _build_mcp_tool_params(mcp_data)

    updates = {
        "tool_config_name": config_name,
        "tool_config_params": tool_params,
    }
    result = update_tool_config(
        tool_config_id=mcp_config_id,
        updates=updates,
        owner_id=current_user.email,
    )
    _refresh_toolbox_if_db(mcp_type)
    return result


@router.post("/mcp_configs/reload", tags=["tools"])
async def reload_mcp_toolbox(
    _current_user: user_schemas.User = Depends(get_current_user),
):
    """Regenerate tools.dynamic.yaml from active configs and restart the toolbox."""
    try:
        target = _regenerate_toolbox_dynamic_config()
        pid = _reload_toolbox_process()
    except Exception as exc:
        logger.exception("MCP Toolbox reload failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"toolbox reload failed: {exc}",
        )
    return {"status": "ok", "pid": pid, "config": str(target.name)}


# ── MCP Toolbox dynamic config ──────────────────────────────────────────────

# Per-DB-type tool catalogue. Each tuple is (suffix, kind).
# `mssql-list-tables` and similar may vary by toolbox version — `execute-sql`
# is the universally supported one and gives the agent full SQL access.
_TOOLS_BY_DB_TYPE: dict[str, list[tuple[str, str]]] = {
    "postgres": [
        ("execute-sql", "postgres-execute-sql"),
        ("list-tables", "postgres-list-tables"),
        ("list-schemas", "postgres-list-schemas"),
        ("database-overview", "postgres-database-overview"),
    ],
    "mysql": [
        ("execute-sql", "mysql-execute-sql"),
        ("list-tables", "mysql-list-tables"),
    ],
    "mssql": [
        ("execute-sql", "mssql-execute-sql"),
        ("list-tables", "mssql-list-tables"),
    ],
}


def _slug(value: str) -> str:
    return "".join(c if (c.isalnum() or c == "-") else "-" for c in value.lower().replace(" ", "-")).strip("-") or "source"


def _build_source(safe_name: str, params: dict) -> dict:
    """Build a toolbox source mapping from a decrypted tool_config_params dict."""
    db_type = params.get("db_type", "postgres")
    sslmode = params.get("db_sslmode", "require")
    source: dict = {
        "kind": db_type,
        "host": params.get("db_host", ""),
        "port": params.get("db_port", ""),
        "database": params.get("db_database", ""),
        "user": params.get("db_user", ""),
        "password": params.get("db_password", ""),
    }
    if db_type == "postgres" and sslmode and sslmode != "disable":
        source["queryParams"] = {"sslmode": sslmode}
    return source


def _build_tools_for_source(safe_name: str, db_type: str) -> tuple[dict, list[str]]:
    """Return (tools_mapping, ordered list of tool names) for one source."""
    catalogue = _TOOLS_BY_DB_TYPE.get(db_type, [("execute-sql", f"{db_type}-execute-sql")])
    tools: dict = {}
    names: list[str] = []
    for suffix, kind in catalogue:
        tname = f"{safe_name}-{suffix}"
        tools[tname] = {
            "kind": kind,
            "source": safe_name,
            "description": f"{suffix.replace('-', ' ').capitalize()} on '{safe_name}' ({db_type}).",
        }
        names.append(tname)
    return tools, names


def _cascade_update_agent_mcp_servers(updates: dict[str, dict]) -> int:
    """Refresh the per-agent mcp_servers snapshot when the underlying MCP
    config changes.

    When a user attaches an MCP config to an agent, the frontend copies a
    handful of fields (name, toolset, db_database, db_type, ...) into the
    agent's mcp_servers JSON column. That copy is the source of truth at
    runtime — but it goes stale as soon as the user renames the DB or the
    slug is recomputed (typical case: deleting a duplicate config removes
    the disambiguation suffix from the survivor). The toolbox then returns
    ``MCP request failed: toolset does not exist`` and the agent goes
    silent.

    This helper runs after every regen: for every agent that holds a
    mcp_servers entry whose ``mcp_config_id`` is in ``updates``, refresh
    only the non-secret fields (toolset, db_database, db_type, name) and
    write the JSON back. Encrypted ``params`` / ``env`` keys are kept as
    stored — we never decrypt them here.

    ``updates`` maps ``str(tool_config_id)`` to the fresh dict of fields
    (e.g. ``{"toolset": "suiviar-tools", "db_database": "SuiviAR",
    "db_type": "mssql", "name": "SuiviAR"}``).

    Returns the number of agent rows actually changed.
    """
    if not updates:
        return 0
    try:
        from th2agent.agent_store.agent_manager import AgentStore
    except ImportError as exc:
        logger.warning("AgentStore unavailable for cascade: %s", exc)
        return 0

    store = AgentStore()
    table = store.agent_table

    select_q = table.select().where(table.c.mcp_servers.isnot(None))
    changed = 0
    refreshable_keys = ("toolset", "db_database", "db_type", "name")

    with store.engine.begin() as conn:
        rows = list(conn.execute(select_q))
        for row in rows:
            raw = row.mcp_servers
            if not raw:
                continue
            try:
                servers = json.loads(raw) if isinstance(raw, str) else raw
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(servers, list):
                continue

            modified = False
            for srv in servers:
                if not isinstance(srv, dict):
                    continue
                cfg_id = str(srv.get("mcp_config_id") or "")
                if not cfg_id or cfg_id not in updates:
                    continue
                fresh = updates[cfg_id]
                for k in refreshable_keys:
                    if k in fresh and srv.get(k) != fresh[k]:
                        srv[k] = fresh[k]
                        modified = True

            if modified:
                conn.execute(
                    table.update()
                    .where(table.c.agent_id == row.agent_id)
                    .values(mcp_servers=json.dumps(servers))
                )
                changed += 1

    return changed


def _regenerate_toolbox_dynamic_config() -> Path:
    """Aggregate all active mcp_server toolbox-db configs into tools.dynamic.yaml.

    Reads from Postgres (decrypted at the helper layer), so credentials never
    leave the secure path. The output file is gitignored and merged on top of
    tools.yaml by iac/deploy.sh.
    """
    sources: dict = {}
    tools: dict = {}
    toolsets: dict = {}
    # mcp_config_id (str) -> dict of fields to refresh on every agent that
    # has attached this config. See _cascade_update_agent_mcp_servers.
    cascade_updates: dict[str, dict] = {}

    all_configs = list_all_tool_configs()
    for cfg in all_configs:
        if cfg.get("tool_category") != "mcp_server":
            continue
        if cfg.get("tool_config_type") != "active":
            continue
        params = cfg.get("tool_config_params", {}) or {}
        if params.get("mcp_type") != "toolbox-db":
            continue
        if not params.get("db_host"):
            # Skip half-configured rows
            continue
        config_name = params.get("name") or cfg.get("tool_config_name") or "source"
        # Slug source priority: the actual database name (e.g. "PMI") so the
        # agent and the user refer to the DB by its real identity, not the
        # wrapper config name. Fall back to the config name when db_database
        # is not set (older rows).
        slug_source = (params.get("db_database") or "").strip() or config_name
        safe = _slug(slug_source)
        if safe in sources:
            # Same database name twice — disambiguate by tool_config_id
            safe = f"{safe}-{cfg.get('tool_config_id', '')}"
        db_type = params.get("db_type", "postgres")
        sources[safe] = _build_source(safe, params)
        tool_map, names = _build_tools_for_source(safe, db_type)
        tools.update(tool_map)
        expected_toolset = f"{safe}-tools"
        toolsets[expected_toolset] = names

        # Persist the computed toolset name back into the tool_config so that
        # GET /api/mcp_configs and the frontend attach flow propagate it into
        # the agent's mcp_servers entry. mcp_loader then instantiates
        # ToolboxToolset(toolset_name=...) and the agent only sees the tools
        # of its own attached config — no leak across users.
        if params.get("toolset") != expected_toolset:
            try:
                params["toolset"] = expected_toolset
                cfg_id = cfg.get("tool_config_id")
                owner = cfg.get("owner_id")
                if cfg_id and owner:
                    update_tool_config(
                        tool_config_id=cfg_id,
                        updates={"tool_config_params": params},
                        owner_id=owner,
                    )
            except Exception as exc:
                logger.warning(
                    "Could not persist toolset=%s on tool_config %s: %s",
                    expected_toolset, cfg.get("tool_config_id"), exc,
                )

        # Collect what the cascade will need: any agent that has attached
        # this MCP config keeps a snapshot of {toolset, db_database, db_type}
        # in its mcp_servers JSON. When the user renames the DB or the slug
        # is recomputed (e.g. a duplicate is removed and the suffix drops),
        # those snapshots become stale and the toolbox returns
        # ``-32600 toolset does not exist``. We refresh them in one pass.
        cfg_id_str = str(cfg.get("tool_config_id") or "")
        if cfg_id_str:
            cascade_updates[cfg_id_str] = {
                "toolset": expected_toolset,
                "db_database": params.get("db_database", ""),
                "db_type": db_type,
                "name": params.get("name", ""),
            }

    if cascade_updates:
        try:
            n = _cascade_update_agent_mcp_servers(cascade_updates)
            if n:
                logger.info(
                    "Cascade-updated mcp_servers on %d agent(s) "
                    "after MCP-DB config change", n,
                )
        except Exception as exc:
            logger.warning("Cascade update of agent mcp_servers failed: %s", exc)

    payload = {
        "sources": sources,
        "tools": tools,
        "toolsets": toolsets,
    }

    target = _toolbox_dir() / "tools.dynamic.yaml"
    header = (
        "## Auto-generated by th2agent backend.\n"
        "## Edit through the /tool-box UI; do not commit. iac/deploy.sh merges\n"
        "## this file on top of tools.yaml before launching the toolbox.\n\n"
    )
    with open(target, "w") as f:
        f.write(header)
        yaml.safe_dump(payload, f, sort_keys=False, default_flow_style=False, width=200)
    logger.info(
        "tools.dynamic.yaml regenerated: %d sources, %d tools, %d toolsets",
        len(sources), len(tools), len(toolsets),
    )
    return target


def _merge_toolbox_configs() -> Path:
    """Merge tools.yaml (base) and tools.dynamic.yaml (overlay) into tools.merged.yaml."""
    base_path = _toolbox_dir() / "tools.yaml"
    overlay_path = _toolbox_dir() / "tools.dynamic.yaml"
    merged_path = _toolbox_dir() / "tools.merged.yaml"
    base = yaml.safe_load(base_path.read_text()) if base_path.exists() else {}
    base = base or {}
    overlay = yaml.safe_load(overlay_path.read_text()) if overlay_path.exists() else {}
    overlay = overlay or {}
    for key in ("sources", "tools", "toolsets"):
        base.setdefault(key, {})
        base[key].update(overlay.get(key, {}) or {})
    merged_path.write_text(yaml.safe_dump(base, sort_keys=False))
    return merged_path


_TOOLBOX_SYSTEMD_UNIT = "th2agent-toolbox.service"


def _is_toolbox_systemd_managed() -> bool:
    """True when th2agent-toolbox.service is installed and active.

    iac/deploy.sh installs that unit on every deploy; older deployments
    (or local dev without sudo) fall back to the legacy in-line subprocess
    spawn.
    """
    try:
        result = subprocess.run(
            ["systemctl", "is-enabled", _TOOLBOX_SYSTEMD_UNIT],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _reload_toolbox_via_systemd() -> int | None:
    """Restart the toolbox via systemctl (no-password sudoers entry).

    The unit's ExecStart already points at tools.merged.yaml and the
    backend has just rewritten it via _merge_toolbox_configs(), so a
    plain restart is enough — no kill+spawn race, the toolbox keeps
    running in its own systemd cgroup independent of this backend.
    """
    try:
        subprocess.run(
            ["sudo", "-n", "/bin/systemctl", "restart", _TOOLBOX_SYSTEMD_UNIT],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        logger.error("systemctl restart %s failed: %s", _TOOLBOX_SYSTEMD_UNIT, exc)
        return None

    # Read back the new MainPID from systemd for traceability
    try:
        result = subprocess.run(
            ["systemctl", "show", "-p", "MainPID", "--value", _TOOLBOX_SYSTEMD_UNIT],
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
        pid = int(result.stdout.strip() or 0) or None
    except Exception:
        pid = None
    logger.info("toolbox restarted via systemctl (%s, pid=%s)", _TOOLBOX_SYSTEMD_UNIT, pid)
    return pid


def _reload_toolbox_via_subprocess() -> int | None:
    """Legacy path: kill the previous in-line subprocess and respawn.

    Used only when the systemd unit is not installed (older deployments,
    local dev). On systemd-managed hosts ``_reload_toolbox_via_systemd``
    is preferred — see ``_reload_toolbox_process``.
    """
    binary = _toolbox_dir() / "toolbox"
    if not binary.exists():
        logger.warning("toolbox binary not found at %s — skipping reload", binary)
        return None

    environment = os.getenv("ENVIRONMENT", "development")
    pid_file = _toolbox_dir() / f"toolbox-{environment}.pid"
    log_file = _toolbox_dir() / f"toolbox-{environment}.log"
    port = os.getenv("TOOLBOX_PORT", "5000")

    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            os.kill(old_pid, signal.SIGKILL)
            logger.info("killed previous toolbox (pid=%s)", old_pid)
        except (ValueError, ProcessLookupError):
            pass
        except PermissionError as exc:
            logger.warning("cannot kill previous toolbox: %s", exc)

    merged_path = _toolbox_dir() / "tools.merged.yaml"
    log_handle = open(log_file, "ab")
    process = subprocess.Popen(
        [
            str(binary),
            "--tools-file", str(merged_path),
            "--address", "0.0.0.0",
            "--port", str(port),
        ],
        cwd=str(_toolbox_dir()),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    pid_file.write_text(str(process.pid))
    logger.info("toolbox restarted (pid=%s, port=%s)", process.pid, port)
    return process.pid


def _reload_toolbox_process() -> int | None:
    """Restart the toolbox after tools.dynamic.yaml has been regenerated.

    Prefers the systemd-managed unit (stable across backend restarts,
    runs in its own cgroup, auto-restart on failure). Falls back to the
    legacy in-line subprocess only when the unit is not installed.
    """
    # Always re-merge first — both paths read tools.merged.yaml.
    _merge_toolbox_configs()

    if _is_toolbox_systemd_managed():
        return _reload_toolbox_via_systemd()
    return _reload_toolbox_via_subprocess()
