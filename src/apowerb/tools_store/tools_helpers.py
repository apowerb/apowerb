import inspect
import json
import importlib
import pkgutil
from typing import Any
from datetime import datetime
from logging import getLogger
from sqlalchemy.dialects.postgresql import insert as pg_insert
from apowerb.tools_store.tool_config import ToolConfigStore
from apowerb.helpers.encryptor import (
    encrypt_value_in_dict,
    decrypt_value_in_dict,
    dict_to_envvar,
)
from apowerb.schema.tool_config_schema import ToolConfigCreateSchema
from google.adk.tools.mcp_tool import McpToolset
from apowerb.tools_store.tool_manager import ToolsStore

logger = getLogger(__name__)
# DDL déplacé dans helpers/store_migrations.ensure_store_tables(),
# appelé au boot : importer ce module ne doit pas toucher la base.
tool_config_store = ToolConfigStore()
tool_manager = ToolsStore()


def list_all_tool_configs() -> list[dict]:
    """Get all tool configs from the tool config store."""
    select_query = tool_config_store.tool_config_table.select()
    result = tool_config_store.get_list_tool_configs(select_query)
    tool_configs = [u._asdict() for u in result]

    # Decrypt tool_config_params for each config
    for config in tool_configs:
        # Format tool_config_id as string
        if config.get("tool_config_id"):
            config["tool_config_id"] = f"tool_config{config['tool_config_id']}"

        if config.get("tool_config_params"):
            try:
                # Parse JSON string
                params = json.loads(config["tool_config_params"])
                # Decrypt values
                if params:
                    decrypted_params = decrypt_value_in_dict(
                        params, values_to_decrypt=params.keys()
                    )
                    config["tool_config_params"] = decrypted_params
                else:
                    config["tool_config_params"] = {}
            except (json.JSONDecodeError, Exception) as e:
                print(f"Error decrypting tool_config_params: {e}")
                config["tool_config_params"] = {}
        else:
            config["tool_config_params"] = {}

    return tool_configs


def list_user_tool_configs(user_id: str) -> list[dict]:
    """Get tool configs owned by a specific user, plus shared system tools."""
    t = tool_config_store.tool_config_table
    select_query = t.select().where(
        (t.c.owner_id == user_id) | (t.c.owner_id == "system")
    )
    result = tool_config_store.get_list_tool_configs(select_query)
    tool_configs = [u._asdict() for u in result]

    # Decrypt tool_config_params for each config
    for config in tool_configs:
        # Format tool_config_id as string
        if config.get("tool_config_id"):
            config["tool_config_id"] = f"tool_config{config['tool_config_id']}"

        if config.get("tool_config_params"):
            try:
                # Parse JSON string
                params = json.loads(config["tool_config_params"])
                # Decrypt values
                if params:
                    decrypted_params = decrypt_value_in_dict(
                        params, values_to_decrypt=params.keys()
                    )
                    config["tool_config_params"] = decrypted_params
                else:
                    config["tool_config_params"] = {}
            except (json.JSONDecodeError, Exception) as e:
                logger.error(f"Error decrypting tool_config_params: {e}")
                config["tool_config_params"] = {}
        else:
            config["tool_config_params"] = {}

    return tool_configs


def find_system_tool_config(tool_name: str) -> dict | None:
    """Find an existing system tool config by tool_name. Returns None if not found."""
    t = tool_config_store.tool_config_table
    select_query = t.select().where(
        (t.c.owner_id == "system") & (t.c.tool_name == tool_name)
    )
    result = tool_config_store.get_list_tool_configs(select_query)
    if not result:
        return None
    config = result[0]._asdict()
    if config.get("tool_config_id"):
        config["tool_config_id"] = f"tool_config{config['tool_config_id']}"
    config["tool_config_params"] = {}
    return config


def fetch_tool_configs(
    tool_config_id: str,
    owner_id: str,
    items_to_select: str = "*",
) -> dict:
    """Get a specific tool config by ID, restricted to ``owner_id`` (or ``system``).

    SuperAgent templates share tools owned by ``'system'`` — those remain readable
    by any authenticated user. Any other owner is rejected (returns a
    ``not found`` payload to prevent enumeration).
    """
    tool_config_id = int(tool_config_id.replace("tool_config", ""))  # type: ignore
    t = tool_config_store.tool_config_table
    select_query = t.select().where(
        (t.c.tool_config_id == tool_config_id)
        & ((t.c.owner_id == owner_id) | (t.c.owner_id == "system"))
    )
    result = tool_config_store.get_list_tool_configs(select_query)
    if len(result) == 0:
        return {"status": 404, "message": f"tool config {tool_config_id} not found"}
    tool_configs = [u._asdict() for u in result]
    if tool_configs:
        config = tool_configs[0]
        # Format tool_config_id as string
        if config.get("tool_config_id"):
            config["tool_config_id"] = f"tool_config{config['tool_config_id']}"

        # Decrypt tool_config_params
        if config.get("tool_config_params"):
            try:
                params = json.loads(config["tool_config_params"])
                if params:
                    decrypted_params = decrypt_value_in_dict(
                        params, values_to_decrypt=params.keys()
                    )
                    config["tool_config_params"] = decrypted_params
                else:
                    config["tool_config_params"] = {}
            except (json.JSONDecodeError, Exception) as e:
                print(f"Error decrypting tool_config_params: {e}")
                config["tool_config_params"] = {}
        else:
            config["tool_config_params"] = {}
        return config
    return {}


def register_tool_config(tool_config: ToolConfigCreateSchema) -> dict:
    """Register or update a tool config (upsert on tool_config_name + org + project)."""
    created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    status = "active"

    encrypted_tool_config_model_params = encrypt_value_in_dict(
        tool_config.tool_config_params,  # type: ignore
        values_to_encrypt=tool_config.tool_config_params.keys(),  # type: ignore
    )
    encrypted_params_json = json.dumps(encrypted_tool_config_model_params)

    upsert_query = (
        pg_insert(tool_config_store.tool_config_table)
        .values(
            tool_config_name=tool_config.tool_config_name,
            tool_name=tool_config.tool_name,
            tool_config_params=encrypted_params_json,
            tool_category=getattr(tool_config, "tool_category", None),
            tool_config_type=tool_config.tool_config_type,
            owner_id=tool_config.owner_id,
            organization_id=tool_config.organization_id,
            project_id=tool_config.project_id,
            created_at=created_at,
            updated_at=updated_at,
            status=status,
        )
        .on_conflict_do_update(
            constraint="unique_tool_config_name_per_org_proj",
            set_={
                "tool_name": tool_config.tool_name,
                "tool_config_params": encrypted_params_json,
                "tool_category": getattr(tool_config, "tool_category", None),
                "tool_config_type": tool_config.tool_config_type,
                "updated_at": updated_at,
                "status": status,
            },
        )
        .returning(tool_config_store.tool_config_table.c.tool_config_id)
    )

    with tool_config_store.engine.begin() as conn:
        tool_config_id = conn.execute(upsert_query).scalar_one()
    return {
        "tool_config_id": f"tool_config{tool_config_id}",
        "tool_config_name": tool_config.tool_config_name,
        "message": "Tool Config registered successfully.",
    }


def delete_tool_config(tool_config_id: str, owner_id: str) -> dict:
    """Delete a tool config, restricted to the caller's ``owner_id``.

    ``system``-owned configs cannot be removed by regular users.
    """
    tool_config_id_int = int(tool_config_id.replace("tool_config", ""))  # type: ignore
    return tool_config_store.delete_tool_config(tool_config_id_int, owner_id=owner_id)


def update_tool_config(tool_config_id: str, updates: dict, owner_id: str) -> dict:
    """Update an existing tool config, restricted to the caller's ``owner_id``."""
    tool_config_id_int = int(tool_config_id.replace("tool_config", ""))
    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    values = {"updated_at": updated_at}

    if "tool_config_name" in updates:
        values["tool_config_name"] = updates["tool_config_name"]
    if "tool_name" in updates:
        values["tool_name"] = updates["tool_name"]
    if "tool_config_params" in updates and updates["tool_config_params"] is not None:
        encrypted = encrypt_value_in_dict(
            updates["tool_config_params"],
            values_to_encrypt=updates["tool_config_params"].keys(),
        )
        values["tool_config_params"] = json.dumps(encrypted)

    t = tool_config_store.tool_config_table
    update_query = (
        t.update()
        .where((t.c.tool_config_id == tool_config_id_int) & (t.c.owner_id == owner_id))
        .values(**values)
    )
    with tool_config_store.engine.begin() as conn:
        result = conn.execute(update_query)
        if result.rowcount == 0:
            return {"status": 404, "message": f"Tool config {tool_config_id} not found"}
    return {"status": 200, "message": "Tool config updated successfully", "tool_config_id": f"tool_config{tool_config_id_int}"}


def load_tool_config_params(tool_config_id: str, owner_id: str):
    """Load (decrypted) params for a tool config owned by ``owner_id`` or ``system``."""
    tool_config = fetch_tool_configs(tool_config_id, owner_id=owner_id)
    tool_name = tool_config.get("tool_name")
    tool_config_params = tool_config.get("tool_config_params")

    # Check if tool_config_params is already a dict (from fetch_tool_configs)
    # If it's a string, parse it; if it's already a dict, use it as is
    if tool_config_params:
        if isinstance(tool_config_params, str):
            tool_config_params = json.loads(tool_config_params)  # type: ignore
        # If it's already a dict, fetch_tool_configs already decrypted it

    if tool_config_params is None:
        return None, None

    # Only decrypt if it's not already decrypted (i.e., if it was a string)
    # Since fetch_tool_configs already decrypts, we don't need to decrypt again
    # unless the params came as a string
    if isinstance(tool_config_params, dict) and tool_config_params:
        # Already decrypted by fetch_tool_configs, just return it
        return tool_name, tool_config_params  # type: ignore

    return tool_name, tool_config_params  # type: ignore


# A DB connection config can be saved with two key conventions: the text_to_sql
# tool config uses UPPERCASE DB_* keys (DB_HOST, DB_NAME, ...), while the BI
# "Database" connection picker saves lowercase keys (host, database, schema...).
# text_to_sql / database tools only recognised DB_NAME, so a lowercase config
# made an agent report "no database configured" even though it HAD one.
_DB_KEY_ALIASES = {
    "host": "DB_HOST",
    "port": "DB_PORT",
    "database": "DB_NAME",
    "dbname": "DB_NAME",
    "user": "DB_USER",
    "username": "DB_USER",
    "password": "DB_PASSWORD",
    "schema": "DB_SCHEMA",
    "db_type": "DB_TYPE",
    "type": "DB_TYPE",
    "include_tables": "DB_INCLUDE_TABLES",
}


def normalize_db_params(params):
    """Return ``params`` with canonical DB_* keys filled in from lowercase
    aliases (host->DB_HOST, database->DB_NAME, ...). Explicit uppercase keys win;
    non-DB configs pass through unchanged. Lets text_to_sql / database tools
    accept a connection saved in either key convention."""
    if not params or not isinstance(params, dict):
        return params
    out = dict(params)
    for k, v in params.items():
        canon = _DB_KEY_ALIASES.get(str(k).lower())
        if canon and not out.get(canon):
            out[canon] = v
    return out


def set_tool_config_params_as_envvar(tool_config_id: str, owner_id: str):
    """Inject a tool_config's decrypted params into ``os.environ``.

    ``owner_id`` is required to prevent cross-tenant leakage of OAuth refresh
    tokens or DB credentials.
    """
    tool_name, tool_params = load_tool_config_params(tool_config_id, owner_id=owner_id)
    if tool_params is None:
        return None
    dict_to_envvar(tool_params)
    return tool_name


def _import_from_packs(module_name: str, *, attr: str) -> Any | None:
    """Importe ``module_name`` depuis le premier pack qui le fournit.

    ``attr`` vaut ``"schema_package"`` ou ``"portfolio_package"``. Les packs sont
    parcourus dans l'ordre d'enregistrement — celui du noyau d'abord — donc une
    brique tierce ajoute des outils sans jamais pouvoir masquer ceux du noyau.

    Retourne ``None`` si aucun pack ne le fournit, plutôt que de propager
    l'``ImportError`` : le nom vient de la base de données, un outil retiré ne
    doit pas faire tomber la construction de tous les agents.
    """
    from apowerb.core.extensions.registry import registry as _registry

    for pack in _registry.tool_packs():
        try:
            return importlib.import_module(f"{getattr(pack, attr)}.{module_name}")
        except ImportError:
            continue
    return None


def get_sub_modules():
    """Return the list of tool categories, across every registered tool pack."""
    from apowerb.core.extensions.registry import registry as _registry

    submodules: list[str] = []
    for pack in _registry.tool_packs():
        try:
            paquet = importlib.import_module(pack.schema_package)
        except ImportError:
            continue
        for _, name, _ in pkgutil.iter_modules(paquet.__path__):
            if name not in submodules:
                submodules.append(name)
    return submodules


def get_module_schemas(module: str):
    """Return the list of classes declared in a specific schema module."""
    module_obj = _import_from_packs(module, attr="schema_package")
    if module_obj is None:
        return []
    return [obj for name, obj in inspect.getmembers(module_obj, inspect.isclass)]


def get_tools_schemas():
    """Return a dictionary with categories as keys and lists of tools as values."""
    all_tools_schema = {}
    for sub_module in get_sub_modules():
        all_tools_schema[sub_module] = get_module_schemas(sub_module)
    return all_tools_schema


def get_agent_tools(tools_list):
    if len(tools_list) == 0:
        return []
    actual_tools = []
    for tool_path in tools_list:
        module_name, func_name = tool_path.split(".")
        module = _import_from_packs(module_name, attr="portfolio_package")
        if module is None:
            logger.warning("[TOOLS] module d'outil introuvable dans les packs: %s", module_name)
            continue
        func = getattr(module, func_name)
        actual_tools.append(func)
    return actual_tools


def _resolve_tool_names(raw_tool_name: str | None) -> list[str]:
    """Resolve tool_name which can be a single name or a JSON list of names."""
    if not raw_tool_name:
        return []
    raw_tool_name = raw_tool_name.strip()
    if raw_tool_name.startswith("["):
        try:
            names = json.loads(raw_tool_name)
            if isinstance(names, list):
                return [n for n in names if isinstance(n, str) and n.strip()]
        except (json.JSONDecodeError, TypeError):
            pass
    return [raw_tool_name]


# Legacy tool name aliases — keys (old names) are resolved as their values
# (current names). Used to keep DB-stored ``agent_tools`` and ``tool_configs``
# entries working after a renaming. Each hit emits a WARNING so operations
# can spot agents that still reference outdated names.
_LEGACY_TOOL_RENAMES: dict[str, str] = {
    "onedrive_read.tool_read_file": "onedrive_read.tool_read_onedrive_file",
    "google_drive.tool_read_file": "google_drive.tool_read_drive_file",
}


def load_agent_tools_functions(tools: list[str], owner_id: str):
    """Resolve tool callables for an agent, scoped to ``owner_id``.

    Any ``tool_config{id}`` reference is looked up with the owner filter so an
    agent cannot reach another tenant's secrets, even if it has a foreign
    tool_config id in its ``agent_tools`` list.
    """
    available_tools = tool_manager.get_all_tools()
    tool_funcs = []
    tools_names = []
    for tool in tools:
        # Support both formats:
        # - "tool_config{id}" → load from DB config (may contain multiple tool names)
        # - "category.tool_name" → load directly from portfolio (e.g. SuperAgent tools)
        if tool.startswith("tool_config"):
            raw_tool_name, tool_params = load_tool_config_params(tool, owner_id=owner_id)
            resolved_names = _resolve_tool_names(raw_tool_name)
        elif "." in tool:
            resolved_names = [tool]
            tool_params = {}
        else:
            continue

        # Set env vars once for all tools sharing the same config
        if tool_params:
            dict_to_envvar(tool_params)

        for current_tool_name in resolved_names:
            if current_tool_name in _LEGACY_TOOL_RENAMES:
                new_name = _LEGACY_TOOL_RENAMES[current_tool_name]
                logger.warning(
                    "Legacy tool name %r found in agent_tools — resolving as "
                    "%r. Update the agent or tool_config to the new name.",
                    current_tool_name,
                    new_name,
                )
                current_tool_name = new_name

            # Skip duplicates — multiple ``tool_config{id}`` entries on the
            # same agent may each resolve to overlapping tool sets (e.g.
            # several Outlook configs all expose
            # ``outlook_mail.tool_download_attachment``). Vertex AI rejects
            # the request with ``Duplicate function declaration found`` if
            # the same tool name appears twice in the function manifest.
            if current_tool_name in tools_names:
                continue

            target_category = [
                category
                for category, tools_list in available_tools.items()
                for tn in tools_list
                if tn == current_tool_name
            ]
            module_to_load = target_category[0] if target_category else None
            if module_to_load is None or module_to_load == "overlay":
                # Overlay-registered tool (scei_*): resolve from the extension
                # registry instead of the hardcoded portfolio path.
                from apowerb.core.extensions.registry import registry as _ext_registry
                _ovl = _ext_registry.overlay_tools()
                _tool_attr = _ovl.get(current_tool_name) or _ovl.get(current_tool_name.split(".")[-1])
                if _tool_attr is None:
                    continue
                tool_funcs.append(_tool_attr)
                tools_names.append(current_tool_name)
                continue

            tool_module = _import_from_packs(module_to_load, attr="portfolio_package")
            if tool_module is None:
                logger.warning(
                    "[TOOLS] module d'outil introuvable dans les packs: %s", module_to_load
                )
                continue
            tool_func = current_tool_name.split(".")[-1]
            tool_attr = getattr(tool_module, tool_func)
            # If it's a factory function (not a McpToolset instance), call it
            if callable(tool_attr) and not isinstance(tool_attr, McpToolset):
                # For MCP factories, call to get fresh instance
                if tool_func.startswith("mcp_tool_set_"):
                    tool_attr = tool_attr()
            tool_funcs.append(tool_attr)
            tools_names.append(current_tool_name)

    return tools_names, tool_funcs
