import hashlib
import json
import re
import time
from logging import getLogger
from pathlib import Path
from typing import Any, Optional

from google.adk.tools.tool_context import ToolContext

from apowerb.configs.paths import uploads_dir

logger = getLogger(__name__)

_MEMORY_FILENAME = ".agent_memory.json"
_MAX_MEMORY_ITEMS = 1000


_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9_-]+$")

_UNKNOWN_CALLER = (
    "memory is unavailable: the calling agent and user could not be identified"
)


def _scoped_folder(folder_name: str, tool_context: Optional[ToolContext]) -> Optional[str]:
    """Where this caller's memory lives, or ``None`` when the caller is unknown.

    Scope = the agent AND the user. The agent is the explicit ``folder_name``
    of a tool bound with ``make_memory_tools``, else the root agent of the ADK
    session; the user is the caller ADK resolved for this run, hashed so an
    identifier can never shape a path.

    No process environment variable is read. ``AGENT_FOLDER`` is global to the
    process and nothing set it per run: every agent and every user of the
    instance ended up in the same ``uploads/default`` file (apowerb/roadmap#84). An
    unknown caller is refused rather than sent to a shared default.
    """
    agent = folder_name
    if not agent and tool_context is not None:
        agent = getattr(getattr(tool_context, "session", None), "app_name", "") or ""
    if not agent or not _SAFE_SEGMENT.match(agent):
        return None
    if tool_context is None:
        # Explicitly bound tool called outside an ADK run: agent scope only.
        return agent
    user = getattr(tool_context, "user_id", "") or ""
    if not user:
        return None
    user_key = hashlib.sha256(user.encode("utf-8")).hexdigest()[:16]
    return f"{agent}/memory/{user_key}"


def _memory_path(folder_name: str) -> Path:
    """Return the memory file path for a given agent folder."""
    p = uploads_dir() / folder_name
    p.mkdir(parents=True, exist_ok=True)
    return p / _MEMORY_FILENAME


def _load_memory(folder_name: str) -> dict:
    """Load memory store from disk. Returns empty structure on first use."""
    path = _memory_path(folder_name)
    if not path.exists():
        return {"text_memories": [], "tool_usages": []}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        data.setdefault("text_memories", [])
        data.setdefault("tool_usages", [])
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"[MEMORY] Could not load memory file {path}: {e}. Starting fresh.")
        return {"text_memories": [], "tool_usages": []}


def _save_memory(folder_name: str, data: dict) -> None:
    """Persist memory store to disk (atomic write via temp file)."""
    path = _memory_path(folder_name)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)  # atomic on POSIX


def _trim_to_limit(items: list, limit: int = _MAX_MEMORY_ITEMS) -> list:
    """Keep only the most recent `limit` items."""
    if len(items) > limit:
        return items[-limit:]
    return items


def _make_save_memory(folder_name: str = ""):
    """
    Factory: returns tool_save_memory, scoped per agent and per user at call time
    (see ``_scoped_folder``).
    """
    def tool_save_memory(
        content: str, tag: str = "", tool_context: Optional[ToolContext] = None
    ) -> dict:
        """
        Saves a piece of text to the agent's persistent memory.

        Returns:
            dict with success status and current memory count.
        """
        if not content or not content.strip():
            return {"success": False, "error": "content cannot be empty"}

        folder = _scoped_folder(folder_name, tool_context)
        if folder is None:
            return {"success": False, "error": _UNKNOWN_CALLER}
        store = _load_memory(folder)

        item = {
            "content": content.strip(),
            "tag": tag.strip(),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        store["text_memories"].append(item)
        store["text_memories"] = _trim_to_limit(store["text_memories"])
        _save_memory(folder, store)

        logger.info(f"[MEMORY] Saved text memory (tag={tag!r}) → uploads/{folder}/{_MEMORY_FILENAME}")
        return {
            "success": True,
            "tag": tag,
            "folder": folder,
            "memory_count": len(store["text_memories"]),
            "message": f"Memory saved to uploads/{folder}/. Total text memories: {len(store['text_memories'])}",
        }

    return tool_save_memory


def _make_search_memory(folder_name: str = ""):
    """Factory: returns tool_search_memory bound to an agent folder."""

    def tool_search_memory(
        query: str,
        tag: str = "",
        max_results: int = 10,
        tool_context: Optional[ToolContext] = None,
    ) -> dict:
        """
        Searches the agent's persistent memory for relevant saved information.

        Use this at the start of a conversation or when you need to recall
        past learnings, business rules, or user preferences. Results are
        ranked by simple keyword relevance.

        Returns:
            dict with matching memory items sorted by relevance.
        """
        if not query.strip():
            return {"success": False, "error": "query cannot be empty"}

        folder = _scoped_folder(folder_name, tool_context)
        if folder is None:
            return {"success": False, "error": _UNKNOWN_CALLER}
        store = _load_memory(folder)

        candidates = store["text_memories"]
        if tag:
            candidates = [m for m in candidates if m.get("tag", "") == tag]

        query_words = query.lower().split()

        def score(item: dict) -> int:
            text = item.get("content", "").lower()
            return sum(1 for w in query_words if w in text)

        scored = [(score(m), m) for m in candidates]
        scored = [(s, m) for s, m in scored if s > 0]
        scored.sort(key=lambda x: x[0], reverse=True)

        results = [m for _, m in scored[:max_results]]

        logger.info(f"[MEMORY] Search query={query!r} tag={tag!r} folder={folder!r} → {len(results)} results")
        return {
            "success": True,
            "query": query,
            "folder": folder,
            "result_count": len(results),
            "total_memories": len(candidates),
            "results": results,
        }

    return tool_search_memory


def _make_save_question_tool_usage(folder_name: str = ""):
    """Factory: returns tool_save_question_tool_usage bound to an agent folder."""

    def tool_save_question_tool_usage(
        question: str,
        tool_name: str,
        tool_args: dict[str, Any],
        result_summary: str = "",
        tool_context: Optional[ToolContext] = None,
    ) -> dict:
        """
        Saves a successful (question → tool_name + args) pair to memory so the
        agent can recall correct tool usage patterns in future conversations.

        Returns:
            dict with success status and saved pattern count.
        """
        if not question.strip() or not tool_name.strip():
            return {"success": False, "error": "question and tool_name are required"}

        folder = _scoped_folder(folder_name, tool_context)
        if folder is None:
            return {"success": False, "error": _UNKNOWN_CALLER}
        store = _load_memory(folder)

        item: dict[str, Any] = {
            "question": question.strip(),
            "tool_name": tool_name.strip(),
            "tool_args": tool_args,
            "result_summary": result_summary.strip(),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        store["tool_usages"].append(item)
        store["tool_usages"] = _trim_to_limit(store["tool_usages"])
        _save_memory(folder, store)

        logger.info(
            f"[MEMORY] Saved tool usage: question={question[:60]!r}, tool={tool_name}, folder={folder!r}"
        )
        return {
            "success": True,
            "tool_name": tool_name,
            "folder": folder,
            "usage_count": len(store["tool_usages"]),
            "message": f"Tool usage pattern saved to uploads/{folder}/. Total patterns: {len(store['tool_usages'])}",
        }

    return tool_save_question_tool_usage


def _make_search_tool_usages(folder_name: str = ""):
    """Factory: returns tool_search_saved_tool_usages bound to an agent folder."""

    def tool_search_saved_tool_usages(
        question: str,
        tool_name: str = "",
        max_results: int = 5,
        tool_context: Optional[ToolContext] = None,
    ) -> dict:
        """
        Retrieves previously saved (question → tool args) patterns that are
        similar to the current question.

        Returns:
            dict with matching patterns including tool_name and tool_args.
        """
        if not question.strip():
            return {"success": False, "error": "question cannot be empty"}

        folder = _scoped_folder(folder_name, tool_context)
        if folder is None:
            return {"success": False, "error": _UNKNOWN_CALLER}
        store = _load_memory(folder)

        candidates = store["tool_usages"]
        if tool_name:
            candidates = [u for u in candidates if u.get("tool_name") == tool_name]

        query_words = question.lower().split()

        def score(item: dict) -> int:
            text = item.get("question", "").lower()
            return sum(1 for w in query_words if w in text)

        scored = [(score(u), u) for u in candidates]
        scored = [(s, u) for s, u in scored if s > 0]
        scored.sort(key=lambda x: x[0], reverse=True)

        results = [u for _, u in scored[:max_results]]

        logger.info(
            f"[MEMORY] Tool usage search: {question[:60]!r} folder={folder!r} → {len(results)} patterns found"
        )
        return {
            "success": True,
            "query": question,
            "folder": folder,
            "result_count": len(results),
            "results": results,
        }

    return tool_search_saved_tool_usages


# ---------------------------------------------------------------------------
# Module-level tools, the ones the catalogue serves: scoped per agent and per
# user from the ADK tool_context injected at call time.
# ---------------------------------------------------------------------------
tool_save_memory = _make_save_memory()
tool_search_memory = _make_search_memory()
tool_save_question_tool_usage = _make_save_question_tool_usage()
tool_search_saved_tool_usages = _make_search_tool_usages()


def make_memory_tools(folder_name: str) -> list:
    """
    Returns all 4 memory tools bound to the agent's upload folder.

    PREFERRED usage — call this in agent.py and pass the agent's folder name:

        from apowerb.tools_store.portfolio.memory import make_memory_tools
        tools_funcs.extend(make_memory_tools("agent229"))

    This guarantees memory is saved to uploads/agent229/.agent_memory.json
    regardless of environment variables.
    """
    if not folder_name:
        raise ValueError("folder_name is required for make_memory_tools(). Pass the agent's folder name.")
    return [
        _make_save_memory(folder_name),
        _make_search_memory(folder_name),
        _make_save_question_tool_usage(folder_name),
        _make_search_tool_usages(folder_name),
    ]