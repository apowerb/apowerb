"""Agent-bound tool rebinding helpers used by ``to_agent``.

These helpers mutate or rebuild ``tools_funcs`` so that generic placeholder
tools get replaced with agent-folder-bound or config-bound versions.
"""
from __future__ import annotations

from th2agent.configs.th2logger import setup_logging
from th2agent.core.agent_helpers.tool_factories import (
    _TEXT_TO_SQL_TOOL_NAMES,
    _make_create_downloadable_file,
    _make_upload_file,
    _make_pdf_to_images,
    _make_read_uploaded_file,
    _make_get_backlog_status,
    _resolve_text_to_sql_tools,
)
from th2agent.core.db.write_config import select_write_config as _select_mssql_write_config


logger = setup_logging(__name__)


_DATABASE_TOOL_NAMES = {"tool_run_sql", "tool_db"}


def dedupe_tools_by_name(tools_funcs: list, agent_name: str = "?") -> list:
    """Drop tools sharing a Python ``__name__`` to avoid Vertex AI 400.

    Multiple portfolio modules expose tools with the same Python name
    (e.g. ``tool_read_file`` lives in ``basic``, ``onedrive_read`` AND
    ``google_drive``). When two are pushed into ADK's ``function_declarations``
    Gemini rejects the whole call with ``Duplicate function declaration``.

    Keep the first occurrence — list order reflects priority (agent_tools first,
    then MCP, skills, recommended tools, bindings, auto-tools).
    Each dropped duplicate is logged with its source module so the conflict
    surfaces in operations.
    """
    seen: set[str] = set()
    deduped: list = []
    for fn in tools_funcs:
        name = getattr(fn, "__name__", "")
        if name and name in seen:
            kept_module = next(
                (
                    getattr(g, "__module__", "?")
                    for g in deduped
                    if getattr(g, "__name__", "") == name
                ),
                "?",
            )
            logger.warning(
                "[TO_AGENT] %s: dropping duplicate tool %r from %s "
                "(kept %s). Rename the conflicting function or remove "
                "one source from agent_tools.",
                agent_name,
                name,
                getattr(fn, "__module__", "?"),
                kept_module,
            )
            continue
        deduped.append(fn)
        if name:
            seen.add(name)
    return deduped


def is_rag_template(superagent_template_id) -> bool:
    """Return True if the SuperAgent template has the 'rag' tag."""
    if not superagent_template_id:
        return False
    from th2agent.core.superagents import (
        get_superagent_template as _get_tpl_check,
    )

    _tpl_check = _get_tpl_check(superagent_template_id)
    return bool(_tpl_check and "rag" in _tpl_check.get("tags", []))


def bind_read_uploaded_file(agent_name: str, tools_funcs: list, is_rag: bool) -> list:
    """Attach or replace ``tool_read_uploaded_file`` for the given agent.

    RAG agents get the placeholder stripped instead (they use the RAG API).
    Returns the (possibly rebuilt) ``tools_funcs`` list.
    """
    if is_rag:
        tools_funcs = [
            fn
            for fn in tools_funcs
            if getattr(fn, "__name__", "") != "tool_read_uploaded_file"
        ]
        logger.info(
            f"[TO_AGENT] RAG agent — read_uploaded_file excluded for {agent_name}"
        )
        return tools_funcs

    from th2agent.tools_store.portfolio.basic import (
        tool_read_uploaded_file as _placeholder,
    )

    replaced = False
    for i, fn in enumerate(tools_funcs):
        if (
            fn is _placeholder
            or getattr(fn, "__name__", "") == "tool_read_uploaded_file"
        ):
            tools_funcs[i] = _make_read_uploaded_file(agent_name)
            replaced = True
            break
    if not replaced:
        tools_funcs.append(_make_read_uploaded_file(agent_name))
    logger.info(f"[TO_AGENT] read_uploaded_file bound to folder {agent_name}")
    return tools_funcs


def bind_create_downloadable_file(agent_name: str, tools_funcs: list) -> None:
    """Append ``create_downloadable_file`` tool bound to the agent folder."""
    tools_funcs.append(_make_create_downloadable_file(agent_name))
    logger.info(f"[TO_AGENT] create_downloadable_file bound to folder {agent_name}")


def bind_pdf_first_page(agent_name: str, tools_funcs: list) -> list:
    """Replace the ``tool_pdf_first_page`` placeholder with a folder-bound
    closure. Replace-only (no auto-append): unlike pdf_to_images, only agents
    that explicitly declare this tool (e.g. the SCEI intake) receive it.
    """
    from th2agent.tools_store.portfolio.basic import (
        tool_pdf_first_page as _placeholder,
    )
    from th2agent.core.agent_helpers.pdf_to_images_tool import (
        _make_pdf_first_page,
    )

    for i, fn in enumerate(tools_funcs):
        if (
            fn is _placeholder
            or getattr(fn, "__name__", "") == "tool_pdf_first_page"
        ):
            tools_funcs[i] = _make_pdf_first_page(agent_name)
            logger.info(f"[TO_AGENT] pdf_first_page bound to folder {agent_name}")
            break
    return tools_funcs


def bind_pdf_to_images(agent_name: str, tools_funcs: list) -> list:
    """Attach or replace ``tool_pdf_to_images`` for the given agent.

    Mirrors the pattern used by ``bind_read_uploaded_file``: the placeholder
    declared in ``tools_store.portfolio.basic`` is swapped with the
    folder-bound closure produced by ``_make_pdf_to_images``.
    """
    from th2agent.tools_store.portfolio.basic import (
        tool_pdf_to_images as _placeholder,
    )

    replaced = False
    for i, fn in enumerate(tools_funcs):
        if (
            fn is _placeholder
            or getattr(fn, "__name__", "") == "tool_pdf_to_images"
        ):
            tools_funcs[i] = _make_pdf_to_images(agent_name)
            replaced = True
            break
    if not replaced:
        tools_funcs.append(_make_pdf_to_images(agent_name))
    logger.info(f"[TO_AGENT] pdf_to_images bound to folder {agent_name}")
    return tools_funcs


def bind_get_backlog_status(agent_name: str, tools_funcs: list) -> list:
    """Attach or replace ``tool_get_webhook_backlog_status`` for the agent.

    The factory wants the integer agent id so it can scope the
    ``webhook_logs`` query. We derive it from ``agent_name`` (canonical
    form ``agent<id>``) — if the name doesn't match that shape we skip
    the bind: leaking another agent's queue would be worse than the
    agent simply not having the tool.
    """
    from th2agent.tools_store.portfolio.basic import (
        tool_get_webhook_backlog_status as _placeholder,
    )

    if not agent_name.startswith("agent"):
        logger.warning(
            "[TO_AGENT] get_backlog_status: agent_name=%r does not match "
            "'agent<id>'; skipping bind to avoid cross-agent leaks.",
            agent_name,
        )
        return tools_funcs
    try:
        agent_id = int(agent_name.removeprefix("agent"))
    except ValueError:
        logger.warning(
            "[TO_AGENT] get_backlog_status: cannot parse id from %r; skipping bind.",
            agent_name,
        )
        return tools_funcs

    replaced = False
    for i, fn in enumerate(tools_funcs):
        if (
            fn is _placeholder
            or getattr(fn, "__name__", "")
            in ("tool_get_webhook_backlog_status", "get_webhook_backlog_status")
        ):
            tools_funcs[i] = _make_get_backlog_status(agent_id)
            replaced = True
            break
    if not replaced:
        # Only append if the placeholder was declared on the agent —
        # otherwise the agent didn't ask for the tool and we should
        # respect that.
        return tools_funcs
    logger.info(
        "[TO_AGENT] get_webhook_backlog_status bound to agent_id=%s",
        agent_id,
    )
    return tools_funcs


def rebind_upload_file(agent_name: str, tools_funcs: list) -> list:
    """If ``tool_upload_file`` placeholder is present, rebind it to the agent folder."""
    if any(getattr(fn, "__name__", "") == "tool_upload_file" for fn in tools_funcs):
        tools_funcs = [
            fn
            for fn in tools_funcs
            if getattr(fn, "__name__", "") != "tool_upload_file"
        ]
        tools_funcs.append(_make_upload_file(agent_name))
        logger.info(f"[TO_AGENT] tool_upload_file rebound to folder {agent_name}")
    return tools_funcs


def rebind_visualization(agent_name: str, tools_funcs: list) -> list:
    """Rebind unbound visualization tools to the agent's upload folder."""
    has_viz_tool = any(
        getattr(fn, "__name__", "") == "tool_visualize_data" for fn in tools_funcs
    )
    if has_viz_tool:
        from th2agent.tools_store.portfolio.visualization import (
            make_visualization_tools,
        )

        tools_funcs = [
            fn
            for fn in tools_funcs
            if getattr(fn, "__name__", "")
            not in ("tool_visualize_data", "tool_export_chart_from_csv")
        ]
        tools_funcs.extend(make_visualization_tools(agent_name))
        logger.info(f"[TO_AGENT] visualization tools bound to folder {agent_name}")
    return tools_funcs


def rebind_text_to_sql(
    agent_name: str, tools_ids: list, tools_funcs: list, owner_id: str
) -> list:
    """Delegate to ``_resolve_text_to_sql_tools`` when SQL placeholders exist."""
    if any(
        getattr(fn, "__name__", "") in _TEXT_TO_SQL_TOOL_NAMES for fn in tools_funcs
    ):
        tools_funcs = _resolve_text_to_sql_tools(
            agent_name, tools_ids, tools_funcs, owner_id=owner_id
        )
    return tools_funcs


def rebind_database(
    agent_name: str, tools_ids: list, tools_funcs: list, owner_id: str
) -> list:
    """Rebind ``tool_run_sql``/``tool_db`` to agent-specific DB credentials.

    Supports **multiple** DB configs on the same agent: when N>=2 ``tool_config``
    entries each carry their own ``DB_NAME``, this function emits one pair of
    tools per config — ``tool_run_sql_<slug>`` / ``tool_db_<slug>`` — keyed by
    a slug derived from ``DB_NAME``. With a single config, the canonical names
    ``tool_run_sql`` / ``tool_db`` are preserved for backward compatibility.
    """
    has_db_tool = any(
        getattr(fn, "__name__", "") in _DATABASE_TOOL_NAMES for fn in tools_funcs
    )
    if not has_db_tool:
        return tools_funcs

    from th2agent.tools_store.portfolio.database import (
        _slugify_db_name,
        make_database_tools,
    )
    from th2agent.tools_store.tools_helpers import (
        load_tool_config_params,
        normalize_db_params,
    )

    all_db_params: list[dict] = []
    for tid in tools_ids:
        try:
            _, tparams = load_tool_config_params(tid, owner_id=owner_id)
            tparams = normalize_db_params(tparams)  # accept lowercase host/database/...
            if tparams and tparams.get("DB_NAME"):
                all_db_params.append(tparams)
        except Exception:
            pass

    tools_funcs = [
        fn
        for fn in tools_funcs
        if getattr(fn, "__name__", "") not in _DATABASE_TOOL_NAMES
    ]

    if len(all_db_params) <= 1:
        single = all_db_params[0] if all_db_params else None
        tools_funcs.extend(make_database_tools(agent_name, db_params=single))
        logger.info(
            "[TO_AGENT] database tools rebound for %s (%s)",
            agent_name,
            f"DB={single['DB_NAME']!r}"
            if single
            else "no DB configured — using env vars",
        )
        return tools_funcs

    seen_slugs: set[str] = set()
    for params in all_db_params:
        slug_base = _slugify_db_name(params["DB_NAME"])
        slug = slug_base
        i = 2
        while slug in seen_slugs:
            slug = f"{slug_base}_{i}"
            i += 1
        seen_slugs.add(slug)
        tools_funcs.extend(
            make_database_tools(agent_name, db_params=params, name_suffix=slug)
        )
    logger.info(
        "[TO_AGENT] %d database configs rebound for %s: %s",
        len(all_db_params),
        agent_name,
        [p["DB_NAME"] for p in all_db_params],
    )
    return tools_funcs




