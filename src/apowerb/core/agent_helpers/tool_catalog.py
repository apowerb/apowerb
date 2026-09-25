"""The tool catalogue as an agent sees it: what exists, and what it can add in one click.

An agent only runs the tools of its config (``agent_tools``) and of its
SuperAgent template. Connecting an integration brings credentials, not
tools: an agent asked to "sort my e-mails" without a mail tool could not do
it, even with Gmail connected. ``find_tools`` lets the agent look up the tool
it lacks; ``propose_agent_upgrade`` shows the card; on approval the UI calls
``POST /api/agents/{id}/tools``, which adds the tool through the PATCH path.

A tool can be added in one click when it needs nothing the user would have
to type: no user-fillable parameter (see ``get_tool_expected_params``). Its
integration, if any, is connected from the chat on the first call
(``INTEGRATION_MISSING`` -> ``request_integration``). Tools that expect a
parameter (a database, an API key) still need a Tool Config.
"""

from __future__ import annotations

import unicodedata
from functools import lru_cache

from apowerb.tools_store.tool_manager import _category_requires_oauth, get_tools_store

# Client-specific tools registered by an overlay are not offered by name.
_EXCLUDED_CATEGORIES = frozenset({"overlay"})
_MAX_DESCRIPTION = 200


def _fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


@lru_cache(maxsize=1)
def catalog_entries() -> dict[str, dict]:
    """Every catalogue tool by dotted name, with what it needs to run.

    Cached: the catalogue is the installed code, it does not change while the
    process runs. Read-only for callers.
    """
    store = get_tools_store()
    docs = store.get_all_tools_docs()
    entries: dict[str, dict] = {}
    for category, names in store.get_all_tools().items():
        if category in _EXCLUDED_CATEGORIES:
            continue
        described = {
            t.get("name"): t.get("description", "")
            for t in (docs.get(category) or {}).get("tools", [])
            if isinstance(t, dict)
        }
        for name in names:
            entries[name] = {
                "tool_name": name,
                "category": category,
                "description": (described.get(name) or "")[
                    :_MAX_DESCRIPTION
                ],
                "needs_integration": _category_requires_oauth(category),
                "needs_tool_config": bool(store.get_tool_expected_params(name)),
            }
    return entries


def addable_tool(tool_name: str | None) -> tuple[bool, str]:
    """Whether ``tool_name`` can be added to an agent without a Tool Config."""
    name = (tool_name or "").strip()
    entry = catalog_entries().get(name)
    if entry is None:
        return False, (
            f"Unknown tool: {name!r}. Call find_tools and use the exact "
            "tool_name it returns."
        )
    if entry["needs_tool_config"]:
        return False, (
            f"{name} needs a Tool Config (credentials or parameters): it cannot "
            "be added from the chat. Ask the user to configure it in the Tool Box."
        )
    return True, ""


def search_tools(query: str, limit: int = 8) -> list[dict]:
    """Catalogue tools matching ``query``, best first."""
    terms = [t for t in _fold(query).replace("_", " ").split() if len(t) >= 3]
    if not terms:
        return []
    scored = []
    for entry in catalog_entries().values():
        name = _fold(entry["tool_name"])
        description = _fold(entry["description"])
        score = sum(
            (3 if term in name else 0) + (1 if term in description else 0)
            for term in terms
        )
        if score:
            scored.append((score, entry["tool_name"], entry))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [entry for _, _, entry in scored[:limit]]


def find_tools(query: str) -> dict:
    """Look up the catalogue for a tool this agent does not have yet.

    Call it when the user asks for something none of your tools can do (read
    their e-mails, list Drive files, open GitHub issues...). Then call
    ``propose_agent_upgrade`` with the exact ``tool_name`` of the tool to add:
    the user approves the card and the tool is added to you.

    Args:
        query: A few English keywords for the capability, e.g. "gmail list
            emails", "outlook search emails", "google drive files".

    Returns:
        dict with ``tools``: each has ``tool_name``, ``description``,
        ``needs_integration`` (the user connects the account on first use)
        and ``addable`` (False: it needs a Tool Config, only the user can set
        it up in the Tool Box).
    """
    tools = [
        {
            "tool_name": e["tool_name"],
            "description": e["description"],
            "needs_integration": e["needs_integration"],
            "addable": not e["needs_tool_config"],
        }
        for e in search_tools(query)
    ]
    if not tools:
        return {
            "status": "not_found",
            "tools": [],
            "message": "No tool matches. Try other English keywords.",
        }
    return {"status": "success", "tools": tools}
