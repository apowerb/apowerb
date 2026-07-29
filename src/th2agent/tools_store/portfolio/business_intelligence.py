"""Business Intelligence tools for ADK agents.

Provides 7 tools that let agents create and manage dashboards and charts
via the existing ChartService / DashboardService layer, persisted in the
``business_intelligence`` DB table through DatabaseChartStore and
DatabaseDashboardStore.

All tools are **synchronous** (ADK requirement) but call async services
internally via ``_run_async``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import re
from logging import getLogger
from typing import Any

logger = getLogger(__name__)


# ---------------------------------------------------------------------------
# Chart reference resolution — accept UUID directly or resolve a name to UUID.
# Anti-récidive du bug 2026-05-12 où l'agent passait le  au lieu de
# l'UUID retourné par tool_create_chart, faisant 404 le rendu du chart côté UI.
# ---------------------------------------------------------------------------

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


# A query 'looks like SQL' if it starts with a SQL verb followed by any
# whitespace/boundary. The old check required a trailing SPACE
# ("select "), so a model emitting "SELECT\n  ..." (newline, very common)
# skipped DB-connection auto-detection and produced a source-less chart.
_SQL_START_RE = re.compile(r"\s*(select|with|show|describe)\b", re.IGNORECASE)


def _looks_like_sql(query: str) -> bool:
    return bool(query) and _SQL_START_RE.match(query) is not None


async def _resolve_chart_ref(chart_ref: str, store) -> tuple[str | None, str | None]:
    """Resolve a chart reference to a chart UUID.

    Accepts either a UUID (returned as-is) or a chart name (looked up in
    the owner-scoped chart store). On ambiguity (multiple charts with the
    same name) or absence, returns an error message that the agent can
    surface to the user.

    store is expected to expose list_all() returning charts already
    scoped to the owner (see DatabaseChartStore / InMemoryChartStore).

    Returns
    -------
    tuple[str | None, str | None]
        (resolved_uuid, None) on success, (None, error_message) otherwise.
    """
    if _UUID_RE.match(chart_ref):
        return chart_ref, None
    all_charts = await store.list_all()
    matches = [c for c in all_charts if c.name == chart_ref]
    if not matches:
        return None, (
            f"No chart named '{chart_ref}' found. Pass the UUID returned by "
            f"tool_create_chart (field 'chart_id'), not the chart name."
        )
    if len(matches) > 1:
        ids = [c.id for c in matches]
        return None, (
            f"Ambiguous name '{chart_ref}' — {len(matches)} charts match. "
            f"Pass one of these UUIDs instead: {ids}"
        )
    logger.warning(
        "[BI] _resolve_chart_ref: name %r resolved to UUID %s (agent should pass the UUID directly)",
        chart_ref, matches[0].id,
    )
    return matches[0].id, None


# ---------------------------------------------------------------------------
# Async-from-sync bridge (FastAPI/ADK already runs an event loop)
# ---------------------------------------------------------------------------


def _run_async(coro):
    """Run an async coroutine from sync context (ADK tools are sync)."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        result = None
        exception = None

        def _run():
            nonlocal result, exception
            try:
                result = asyncio.run(coro)
            except Exception as e:
                exception = e

        t = threading.Thread(target=_run)
        t.start()
        t.join(timeout=30)
        if t.is_alive():
            raise TimeoutError(
                "_run_async: coroutine did not complete within 30 seconds"
            )
        if exception:
            raise exception
        return result
    else:
        return asyncio.run(coro)


# ---------------------------------------------------------------------------
# DB session helper
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _get_session():
    """Create a standalone async DB session for use inside tools.

    We cannot reuse the shared ``sessionmanager`` because its connection pool
    is bound to the main FastAPI event loop, while BI tools run in a
    separate thread with their own event loop (via ``_run_async``).
    Creating a fresh engine per call is slightly more expensive but avoids
    the "Future attached to a different loop" error.
    """
    from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
    from sqlalchemy.orm import sessionmaker as sa_sessionmaker

    from th2agent.helpers.database_connection import DBConfig
    from th2agent.configs.settings import get_settings

    settings = get_settings()
    db_url = DBConfig().get_db_url()

    engine = create_async_engine(
        db_url,
        connect_args={"server_settings": {"jit": "off"}},
        pool_pre_ping=True,
    )
    SessionLocal = sa_sessionmaker(
        autocommit=False, bind=engine, class_=AsyncSession
    )
    session = SessionLocal()
    try:
        yield session
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
        await engine.dispose()


# ---------------------------------------------------------------------------
# Internal async implementations
# ---------------------------------------------------------------------------


async def _async_create_chart(
    name: str,
    title: str,
    chart_type: str,
    query: str,
    organization_id: str,
    project_id: str,
    description: str,
    refresh_interval: int,
    time_field: str,
    group_by: str,
    aggregation: str,
    connection_config_id: str,
    config: str,
    owner_email: str,
) -> dict:
    from th2agent.bi.charts.core import ChartType, DataSource, AggregationFunc
    from th2agent.bi.charts.schemas import ChartCreateRequest, DataSourceSchema
    from th2agent.bi.charts.service import ChartConflictError, ChartService
    from th2agent.bi.db_stores import DatabaseChartStore

    # Parse config JSON
    config_dict: dict[str, Any] = {}
    if config:
        try:
            config_dict = json.loads(config)
        except json.JSONDecodeError:
            return {"success": False, "error": f"Invalid JSON in config: {config}"}

    # Validate chart_type
    try:
        ct = ChartType(chart_type.lower())
    except ValueError:
        valid = [t.value for t in ChartType]
        return {"success": False, "error": f"Invalid chart_type '{chart_type}'. Valid: {valid}"}

    # Build aggregation
    agg = None
    if aggregation:
        try:
            agg = AggregationFunc(aggregation.lower())
        except ValueError:
            valid = [a.value for a in AggregationFunc]
            return {"success": False, "error": f"Invalid aggregation '{aggregation}'. Valid: {valid}"}

    # Build group_by list
    group_by_list = [g.strip() for g in group_by.split(",") if g.strip()] if group_by else []

    # Owner used to load the DB connection config when validating the query
    # below. Defaults to the chart owner; overridden to the agent's owner when
    # we auto-detect the connection config (that is who the config is scoped to).
    _probe_owner = owner_email

    # Auto-detect connection_config_id for SQL queries.
    # When the query looks like SQL (SELECT, WITH, etc.) and no explicit
    # connection_config_id was given, try to find the agent's DB tool config.
    if not connection_config_id and query:
        if _looks_like_sql(query):
            import os
            agent_id = os.getenv("ROOT_AGENT_ID", "")
            if agent_id:
                try:
                    from th2agent.core.agent_helpers import get_agent_details
                    details = get_agent_details(int(agent_id))
                    tools_raw = details.get("agent_tools", "[]")
                    tools_list = json.loads(tools_raw) if isinstance(tools_raw, str) else (tools_raw or [])
                    # Scope the tool_config lookup to the agent's owner so we
                    # never pick up a foreign tenant's DB config.
                    agent_owner = details.get("owner_id") or os.getenv("AGENT_OWNER", "")
                    for t in tools_list:
                        if isinstance(t, str) and t.startswith("tool_config"):
                            from th2agent.tools_store.tools_helpers import load_tool_config_params
                            tname, _ = load_tool_config_params(t, owner_id=agent_owner)
                            if tname and "database" in str(tname).lower():
                                connection_config_id = t
                                _probe_owner = agent_owner or owner_email
                                logger.info(f"[BI] Auto-detected connection_config_id={t} for SQL query")
                                break
                except Exception as e:
                    logger.warning(f"[BI] Could not auto-detect connection_config_id: {e}")

    # Validate a SQL query BEFORE persisting. A chart whose query errors only
    # surfaces later as an opaque HTTP 502 at render time (and the bad chart
    # lingers in the dashboard). Probe it now with a LIMIT 1 and refuse to
    # create the chart on a genuine SQL error, returning the DB message so the
    # agent corrects the query (via tool_text_to_sql) instead of saving garbage.
    # Fails OPEN on infra/credential/owner errors so a legitimate chart is never
    # blocked by a transient connection issue.
    if query and _looks_like_sql(query) and connection_config_id and _probe_owner:
        from th2agent.bi.charts.core import DataSource
        from th2agent.bi.data.db_executor import DatabaseQueryExecutor
        try:
            probe_source = DataSource(
                query=query, connection_config_id=connection_config_id, limit=1,
            )
            await DatabaseQueryExecutor(connection_config_id, owner_id=_probe_owner).run(
                probe_source
            )
        except Exception as exc:
            low = str(exc).lower()
            sql_error = any(s in low for s in (
                "query error", "does not exist", "no such", "syntax",
                "undefined", "unknown column", "unknown table", "invalid",
                "permitted",  # validate_query: only SELECT permitted
            ))
            if sql_error:
                logger.warning("[BI] chart query failed validation, not creating: %s", exc)
                return {
                    "success": False,
                    "error": (
                        f"The chart query failed to execute, so the chart was NOT created: {exc}. "
                        "Do not hand-write SQL against an assumed schema: call tool_text_to_sql "
                        "with the user's question to get a query grounded in the real database "
                        "schema, then pass its sql_query to tool_create_chart."
                    ),
                }
            logger.warning("[BI] chart query validation skipped (non-SQL error): %s", exc)

    source_schema = DataSourceSchema(
        query=query,
        aggregation=agg,
        group_by=group_by_list,
        time_field=time_field or None,
        connection_config_id=connection_config_id or None,
    )

    async def _create_with_name(chart_name: str):
        # Each attempt gets its OWN session: a UniqueViolation poisons the
        # session (PendingRollbackError), so a retry must not reuse it.
        async with _get_session() as db:
            store = DatabaseChartStore(db, owner=owner_email)
            svc = ChartService(store, db)
            req = ChartCreateRequest(
                name=chart_name,
                title=title,
                chart_type=ct,
                source=source_schema,
                description=description or None,
                refresh_interval=refresh_interval if refresh_interval > 0 else None,
                organization_id=organization_id,
                project_id=project_id,
                config=config_dict,
            )
            return await svc.create(req, created_by=owner_email)

    try:
        chart = await _create_with_name(name)
    except ChartConflictError:
        # The agent regenerates the same machine `name` for a similar request
        # (the template tells it to recreate a chart every time), which collides
        # with the (name, type, org, project) unique constraint and used to
        # crash chart creation. Retry once with a unique name suffix so
        # re-asking a chart simply makes a new one.
        import uuid
        chart = await _create_with_name(f"{name}-{uuid.uuid4().hex[:6]}")
    return {
        "success": True,
        "chart_id": chart.id,
        "name": chart.name,
        "title": chart.title,
        "chart_type": chart.chart_type.value,
        "message": f"Chart '{chart.title}' created successfully.",
    }


async def _async_create_dashboard(
    title: str,
    description: str,
    slug: str,
    owner_email: str,
) -> dict:
    import os

    from th2agent.bi.dashboards.schema import DashboardCreateRequest
    from th2agent.bi.dashboards.service import DashboardService
    from th2agent.bi.db_stores import DatabaseDashboardStore

    agent_id_str = os.getenv("ROOT_AGENT_ID", "")
    agent_id = int(agent_id_str) if agent_id_str else None

    async with _get_session() as db:
        store = DatabaseDashboardStore(db, owner=owner_email)
        svc = DashboardService(store)

        req = DashboardCreateRequest(
            title=title,
            description=description or None,
            slug=slug or None,
            agent_id=agent_id,
        )

        dashboard = await svc.create(req, created_by=owner_email)
        return {
            "success": True,
            "dashboard_id": dashboard.id,
            "title": dashboard.title,
            "slug": dashboard.slug,
            "status": dashboard.status.value,
            "message": f"Dashboard '{dashboard.title}' created successfully.",
        }


async def _async_add_chart_to_dashboard(
    dashboard_id: str,
    chart_id: str,
    row: int,
    col: int,
    width: int,
    height: int,
    title_override: str,
    owner_email: str,
) -> dict:
    from th2agent.bi.dashboards.core import DashboardComponent, GridPosition
    from th2agent.bi.dashboards.schema import AddComponentRequest, ComponentCreateSchema, ChartWidgetSchema, GridPositionSchema
    from th2agent.bi.dashboards.service import DashboardService
    from th2agent.bi.db_stores import DatabaseDashboardStore

    async with _get_session() as db:
        store = DatabaseDashboardStore(db, owner=owner_email)
        svc = DashboardService(store)
        # Resolve chart_id: accept UUID directly or look up by name
        from th2agent.bi.db_stores import DatabaseChartStore
        chart_store = DatabaseChartStore(db, owner=owner_email)
        resolved, err = await _resolve_chart_ref(chart_id, chart_store)
        if err:
            return {"success": False, "error": err}
        chart_id = resolved

        component_schema = ComponentCreateSchema(
            component_type="chart",
            position=GridPositionSchema(row=row, col=col, width=width, height=height),
            chart=ChartWidgetSchema(
                chart_id=chart_id,
                title_override=title_override or None,
            ),
        )
        req = AddComponentRequest(component=component_schema)

        dashboard = await svc.add_component(dashboard_id, req)
        return {
            "success": True,
            "dashboard_id": dashboard.id,
            "component_count": len(dashboard.components),
            "message": f"Chart '{chart_id}' added to dashboard '{dashboard.title}'.",
        }


async def _async_add_kpi_to_dashboard(
    dashboard_id: str,
    label: str,
    value: str,
    unit: str,
    description: str,
    trend_value: float,
    trend_direction: str,
    trend_sentiment: str,
    row: int,
    col: int,
    width: int,
    height: int,
    owner_email: str,
) -> dict:
    from th2agent.bi.dashboards.core import TrendDirection, TrendSentiment
    from th2agent.bi.dashboards.schema import (
        AddComponentRequest,
        ComponentCreateSchema,
        GridPositionSchema,
        KeyValueCreateSchema,
        TrendSchema,
    )
    from th2agent.bi.dashboards.service import DashboardService
    from th2agent.bi.db_stores import DatabaseDashboardStore

    # Validate trend direction/sentiment
    try:
        td = TrendDirection(trend_direction.lower())
    except ValueError:
        valid = [d.value for d in TrendDirection]
        return {"success": False, "error": f"Invalid trend_direction '{trend_direction}'. Valid: {valid}"}

    try:
        ts = TrendSentiment(trend_sentiment.lower())
    except ValueError:
        valid = [s.value for s in TrendSentiment]
        return {"success": False, "error": f"Invalid trend_sentiment '{trend_sentiment}'. Valid: {valid}"}

    # Parse value — try numeric first
    parsed_value: int | float | str = value
    try:
        parsed_value = float(value)
        if parsed_value == int(parsed_value):
            parsed_value = int(parsed_value)
    except (ValueError, OverflowError):
        pass

    trend = None
    if trend_value != 0.0:
        trend = TrendSchema(
            value=trend_value,
            direction=td,
            sentiment=ts,
        )

    async with _get_session() as db:
        store = DatabaseDashboardStore(db, owner=owner_email)
        svc = DashboardService(store)

        component_schema = ComponentCreateSchema(
            component_type="key_value",
            position=GridPositionSchema(row=row, col=col, width=width, height=height),
            key_value=KeyValueCreateSchema(
                label=label,
                value=parsed_value,
                unit=unit or None,
                description=description or None,
                trend=trend,
            ),
        )
        req = AddComponentRequest(component=component_schema)

        dashboard = await svc.add_component(dashboard_id, req)
        return {
            "success": True,
            "dashboard_id": dashboard.id,
            "component_count": len(dashboard.components),
            "message": f"KPI '{label}' added to dashboard '{dashboard.title}'.",
        }


async def _async_publish_dashboard(
    dashboard_id: str,
    visibility: str,
    owner_email: str,
) -> dict:
    from th2agent.bi.dashboards.core import DashboardVisibility
    from th2agent.bi.dashboards.service import DashboardService
    from th2agent.bi.db_stores import DatabaseDashboardStore

    try:
        vis = DashboardVisibility(visibility.lower())
    except ValueError:
        valid = [v.value for v in DashboardVisibility]
        return {"success": False, "error": f"Invalid visibility '{visibility}'. Valid: {valid}"}

    async with _get_session() as db:
        store = DatabaseDashboardStore(db, owner=owner_email)
        svc = DashboardService(store)

        dashboard = await svc.publish(dashboard_id, visibility=vis)
        return {
            "success": True,
            "dashboard_id": dashboard.id,
            "title": dashboard.title,
            "slug": dashboard.slug,
            "status": dashboard.status.value,
            "visibility": dashboard.visibility.value,
            "message": f"Dashboard '{dashboard.title}' published ({vis.value}).",
        }


async def _async_list_dashboards(
    page: int,
    page_size: int,
    owner_email: str,
) -> dict:
    from th2agent.bi.dashboards.service import DashboardService
    from th2agent.bi.db_stores import DatabaseDashboardStore

    async with _get_session() as db:
        store = DatabaseDashboardStore(db, owner=owner_email)
        svc = DashboardService(store)

        dashboards, total = await svc.list(page=page, page_size=page_size)
        items = [
            {
                "id": d.id,
                "title": d.title,
                "slug": d.slug,
                "status": d.status.value,
                "visibility": d.visibility.value,
                "component_count": len(d.components),
                "created_at": d.created_at.isoformat(),
            }
            for d in dashboards
        ]
        return {
            "success": True,
            "dashboards": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "has_next": (page * page_size) < total,
        }


async def _async_update_chart_data(
    chart_id: str,
    query: str,
    connection_config_id: str,
    time_field: str,
    owner_email: str,
) -> dict:
    from th2agent.bi.charts.schemas import ChartUpdateRequest, DataSourceSchema
    from th2agent.bi.charts.service import ChartService
    from th2agent.bi.db_stores import DatabaseChartStore

    async with _get_session() as db:
        store = DatabaseChartStore(db, owner=owner_email)
        svc = ChartService(store, db)

        # Resolve chart_id: accept UUID directly or look up by name
        resolved, err = await _resolve_chart_ref(chart_id, store)
        if err:
            return {"success": False, "error": err}
        chart_id = resolved

        # Build partial source update
        source_fields: dict[str, Any] = {}
        if query:
            source_fields["query"] = query
        if connection_config_id:
            source_fields["connection_config_id"] = connection_config_id
        if time_field:
            source_fields["time_field"] = time_field

        if not source_fields:
            return {"success": False, "error": "At least one of query, connection_config_id, or time_field must be provided."}

        # Get existing chart to merge source fields
        chart = await svc.get(chart_id)
        existing_source = chart.source.model_dump()
        existing_source.update(source_fields)

        req = ChartUpdateRequest(
            source=DataSourceSchema(**existing_source),
        )

        updated = await svc.update(chart_id, req)
        return {
            "success": True,
            "chart_id": updated.id,
            "title": updated.title,
            "message": f"Chart '{updated.title}' data source updated.",
        }


async def _async_update_chart(
    chart_id: str,
    title: str,
    chart_type: str,
    query: str,
    connection_config_id: str,
    time_field: str,
    description: str,
    owner_email: str,
) -> dict:
    from th2agent.bi.charts.schemas import ChartUpdateRequest, DataSourceSchema
    from th2agent.bi.charts.service import ChartService
    from th2agent.bi.data.service import ChartDataService
    from th2agent.bi.db_stores import DatabaseChartStore

    async with _get_session() as db:
        store = DatabaseChartStore(db, owner=owner_email)
        svc = ChartService(store, db)

        resolved, err = await _resolve_chart_ref(chart_id, store)
        if err:
            return {"success": False, "error": err}
        chart_id = resolved

        fields: dict[str, Any] = {}
        if title:
            fields["title"] = title
        if description:
            fields["description"] = description
        if chart_type:
            fields["chart_type"] = chart_type  # pydantic coerces str -> ChartType

        # Source (query / connection / time field) is merged onto the existing
        # one, like tool_update_chart_data, so a partial change is valid.
        source_fields: dict[str, Any] = {}
        if query:
            source_fields["query"] = query
        if connection_config_id:
            source_fields["connection_config_id"] = connection_config_id
        if time_field:
            source_fields["time_field"] = time_field

        if not fields and not source_fields:
            return {"success": False, "error": (
                "Provide at least one of title, chart_type, query, "
                "connection_config_id, time_field, or description."
            )}

        if source_fields:
            chart = await svc.get(chart_id)
            existing_source = chart.source.model_dump()
            existing_source.update(source_fields)
            fields["source"] = DataSourceSchema(**existing_source)

        try:
            req = ChartUpdateRequest(**fields)
        except Exception as e:  # invalid chart_type / source
            return {"success": False, "error": f"Invalid update: {e}"}

        updated = await svc.update(chart_id, req)
        ChartDataService.invalidate_cache(chart_id)
        ctype = getattr(updated.chart_type, "value", str(updated.chart_type))
        return {
            "success": True,
            "chart_id": updated.id,
            "title": updated.title,
            "chart_type": ctype,
            "message": f"Chart '{updated.title}' updated.",
        }


async def _async_remove_chart_from_dashboard(
    chart_id: str,
    dashboard_id: str,
    owner_email: str,
) -> dict:
    from th2agent.bi.dashboards.core import ComponentType
    from th2agent.bi.dashboards.service import DashboardNotFoundError, DashboardService
    from th2agent.bi.db_stores import DatabaseChartStore, DatabaseDashboardStore

    async with _get_session() as db:
        chart_store = DatabaseChartStore(db, owner=owner_email)
        resolved, err = await _resolve_chart_ref(chart_id, chart_store)
        if err:
            return {"success": False, "error": err}
        chart_id = resolved

        store = DatabaseDashboardStore(db, owner=owner_email)
        svc = DashboardService(store)
        try:
            dashboard = await svc.get(dashboard_id)
        except DashboardNotFoundError:
            dashboard = None
        if dashboard is None:
            return {"success": False, "error": (
                f"Dashboard '{dashboard_id}' not found (or not yours)."
            )}

        comp_ids = [
            c.id
            for c in dashboard.components
            if c.component_type == ComponentType.CHART
            and c.chart is not None
            and c.chart.chart_id == chart_id
        ]
        if not comp_ids:
            return {"success": False, "error": (
                "This chart is not on this dashboard — nothing to remove."
            )}

        for cid in comp_ids:
            dashboard = await svc.remove_component(dashboard_id, cid)

        return {
            "success": True,
            "dashboard_id": dashboard.id,
            "removed": len(comp_ids),
            "component_count": len(dashboard.components),
            "message": (
                f"Removed chart from dashboard '{dashboard.title}'. The chart "
                "itself is kept and can be re-added or used on other dashboards."
            ),
        }


# ---------------------------------------------------------------------------
# Agent context helpers — read owner/org/project from env vars
# set by to_agent() in core/agent_helpers.py
# ---------------------------------------------------------------------------


def _agent_owner() -> str:
    """Return the current agent's owner email from environment."""
    import os
    return os.getenv("AGENT_OWNER", "")


def _agent_org() -> str:
    """Return the current agent's organization_id from environment."""
    import os
    return os.getenv("AGENT_ORGANIZATION_ID", "default")


def _agent_project() -> str:
    """Return the current agent's project_id from environment."""
    import os
    return os.getenv("AGENT_PROJECT_ID", "thaink2")


# ---------------------------------------------------------------------------
# Reusable per-user "Charts du chat" dashboard — charts created in the chat are
# collected into ONE dashboard per owner so the chat card's "Open dashboard"
# link opens a real, manageable dashboard instead of a dead route.
# ---------------------------------------------------------------------------

_CHAT_DASHBOARD_SLUG = "charts-du-chat"
_CHAT_DASHBOARD_TITLE = "Charts du chat"


def _chat_dashboard_identity(session_id: str | None = None) -> tuple[str, str]:
    """(slug, title) of the 'Charts du chat' dashboard for a chat.

    Charts a user sends to the dashboard are collected into a board scoped to
    THAT conversation, so charts from different chats no longer pile into one
    shared board. ``session_id`` is passed explicitly by HTTP callers (the
    "send to dashboard" button); agent runs fall back to the per-request
    ``AGENT_CHAT_SESSION_ID`` env. Falls back to the legacy shared dashboard
    when no session is known. Dashboard mini-chats (session ``dashboard-chat-*``)
    embed into their linked dashboard via AGENT_DASHBOARD_ID, so they keep the
    legacy slug here.
    """
    import os
    sid = (session_id or os.getenv("AGENT_CHAT_SESSION_ID", "")).strip()
    if not sid or sid.startswith("dashboard-chat-"):
        return _CHAT_DASHBOARD_SLUG, _CHAT_DASHBOARD_TITLE
    suffix = re.sub(r"[^a-z0-9]+", "-", sid.lower()).strip("-")[:80] or "default"
    # Title must be UNIQUE per session: the dashboard's name has a (name, type,
    # org, project) unique constraint. Session ids share a prefix
    # ("session_<ts>"), so sid[:8] collided for every chat ("session_") and made
    # the 2nd+ chat's dashboard creation fail. Derive the title from the unique
    # suffix instead.
    return f"{_CHAT_DASHBOARD_SLUG}-{suffix}", f"{_CHAT_DASHBOARD_TITLE} ({suffix})"


async def _async_ensure_chat_dashboard(
    chart_id: str, owner_email: str, session_id: str | None = None,
) -> dict:
    """Find-or-create the chat's 'Charts du chat' dashboard and add the chart
    to it (idempotent). Returns {'success': bool, 'dashboard_id': str}."""
    from th2agent.bi.dashboards.service import SlugConflictError
    from th2agent.bi.db_stores import DatabaseChartStore, DatabaseDashboardStore

    chat_slug, chat_title = _chat_dashboard_identity(session_id)

    async with _get_session() as db:
        chart_store = DatabaseChartStore(db, owner=owner_email)
        resolved, err = await _resolve_chart_ref(chart_id, chart_store)
        if err:
            return {"success": False, "error": err}
        chart_id = resolved

        store = DatabaseDashboardStore(db, owner=owner_email)
        try:
            dash = await store.get_by_slug(chat_slug)
        except Exception:
            dash = None
        dash_id = dash.id if dash else None
        existing_ids = set()
        existing_count = 0
        if dash:
            for c in (dash.components or []):
                cw = getattr(c, "chart", None)
                cid = getattr(cw, "chart_id", None) if cw else None
                if cid:
                    existing_ids.add(cid)
            existing_count = len(dash.components or [])

    if not dash_id:
        try:
            res = await _async_create_dashboard(
                chat_title,
                "Graphiques generes depuis le chat.",
                chat_slug,
                owner_email,
            )
            dash_id = res.get("dashboard_id")
        except Exception:
            # SlugConflictError, or a name UniqueViolation if the dashboard was
            # created concurrently (race) — in both cases it already exists, so
            # just re-resolve it by slug instead of failing the whole send.
            async with _get_session() as db:
                store = DatabaseDashboardStore(db, owner=owner_email)
                dash = await store.get_by_slug(chat_slug)
                dash_id = dash.id if dash else None
        existing_ids = set()
        existing_count = 0

    if not dash_id:
        return {"success": False, "error": "could not resolve chat dashboard"}

    if chart_id not in existing_ids:
        await _async_add_chart_to_dashboard(
            dash_id, chart_id,
            row=(existing_count // 2) * 4, col=(existing_count % 2) * 6,
            width=6, height=4, title_override="", owner_email=owner_email,
        )
    return {"success": True, "dashboard_id": dash_id}


def ensure_chart_in_chat_dashboard(chart_id: str):
    """Best-effort: return the owner's 'Charts du chat' dashboard_id (creating
    it + adding the chart on first use), or None on any failure. Never blocks
    chart embedding."""
    owner = _agent_owner()
    if not owner:
        return None
    try:
        res = _run_async(_async_ensure_chat_dashboard(chart_id, owner))
        return res.get("dashboard_id") if res.get("success") else None
    except Exception:
        logger.exception("[BI] ensure_chart_in_chat_dashboard failed")
        return None


def get_chart_title(chart_id: str) -> str | None:
    """Best-effort: the chart's stored title (content-based label) so an inline
    embed shows the chart's meaning instead of 'Chart #<uuid>'."""
    owner = _agent_owner()
    if not owner:
        return None
    try:
        from th2agent.bi.db_stores import DatabaseChartStore

        async def _q():
            async with _get_session() as db:
                store = DatabaseChartStore(db, owner=owner)
                cid, err = await _resolve_chart_ref(chart_id, store)
                if err:
                    return None
                ch = await store.get(cid)
                if not ch:
                    return None
                return getattr(ch, "title", None) or getattr(ch, "name", None)

        return _run_async(_q())
    except Exception:
        logger.exception("[BI] get_chart_title failed")
        return None


def resolve_chart_for_embed(chart_id: str) -> tuple[str, "str | None"]:
    """(state, title) for embedding. state is 'ok' | 'missing' | 'unknown'.

    'missing' means the chart genuinely does not exist for this owner (the model
    passed a wrong/invented chart_id) — embed_chart uses this to refuse instead
    of rendering a card that 404s in the UI. 'unknown' (no owner / DB error)
    fails OPEN so a transient glitch never blocks a real chart.
    """
    owner = _agent_owner()
    if not owner:
        return ("unknown", None)
    from th2agent.bi.db_stores import DatabaseChartStore

    async def _q():
        async with _get_session() as db:
            store = DatabaseChartStore(db, owner=owner)
            cid, err = await _resolve_chart_ref(chart_id, store)
            if err:
                return ("missing", None)
            ch = await store.get(cid)
            if not ch:
                return ("missing", None)
            return ("ok", getattr(ch, "title", None) or getattr(ch, "name", None))

    try:
        return _run_async(_q())
    except Exception:
        logger.exception("[BI] resolve_chart_for_embed failed")
        return ("unknown", None)


# ---------------------------------------------------------------------------
# Public sync tools (ADK function-calling interface)
#
# These are discovered by ToolsStore.get_tools_in_category() which scans for
# functions starting with "tool_". They read the agent context (owner, org,
# project) from environment variables set by to_agent().
# ---------------------------------------------------------------------------


def tool_send_chart_to_dashboard(chart_id: str, folder_name: str = "") -> dict:
    """Send a chart the user made in this chat to THEIR chat dashboard.

    Charts are NOT added to a dashboard automatically — call this ONLY when the
    user explicitly asks to add/send/save a chart to the dashboard. Adds it to
    the current conversation's "Charts du chat" dashboard (created on first use).

    Args:
        chart_id:    The chart_id (UUID) returned by tool_create_chart.
        folder_name: Agent folder name (injected automatically).

    Returns:
        dict with success status and the dashboard_id.
    """
    owner = _agent_owner()
    if not owner:
        return {"success": False, "error": "No owner context."}
    try:
        dashboard_id = ensure_chart_in_chat_dashboard(chart_id)
        if dashboard_id:
            return {
                "success": True,
                "dashboard_id": dashboard_id,
                "message": "Chart added to the chat dashboard.",
            }
        return {"success": False, "error": "Could not add the chart to the dashboard."}
    except Exception as e:
        logger.exception("[BI] tool_send_chart_to_dashboard failed")
        return {"success": False, "error": str(e)}


def tool_create_chart(
    name: str,
    title: str,
    chart_type: str,
    query: str,
    organization_id: str = "",
    project_id: str = "",
    description: str = "",
    refresh_interval: int = 60,
    time_field: str = "",
    group_by: str = "",
    aggregation: str = "",
    connection_config_id: str = "",
    config: str = "",
    folder_name: str = "",
) -> dict:
    """Creates a new chart definition for use in dashboards.

    Use this tool to define a chart that queries data and renders as a
    visualization. The chart is persisted and can be embedded in dashboards
    using tool_add_chart_to_dashboard.

    Supported chart_type values: "bar", "line", "pie", "donut", "scatter",
    "area", "stat", "table", "histogram".

    The optional config parameter accepts a JSON string for KPI-specific
    settings (e.g. {"kpi_column": "revenue", "kpi_aggregation": "sum"}).

    Args:
        name:                 Unique machine name for the chart (e.g. "monthly_revenue_bar").
        title:                Human-readable title displayed on the chart.
        chart_type:           Visualization type (bar, line, pie, donut, scatter, area, stat, table, histogram).
        query:                SQL query or query key that provides the chart data.
        organization_id:      Organization that owns the chart (auto-detected if blank).
        project_id:           Project scope (auto-detected if blank).
        description:          Optional description of the chart.
        refresh_interval:     Auto-refresh interval in seconds (default 60).
        time_field:           Column name used as time axis for timeseries charts.
        group_by:             Comma-separated list of columns to group by.
        aggregation:          Aggregation function (count, sum, avg, min, max, p50, p95, p99).
        connection_config_id: ID of the database connection config to use.
        config:               JSON string with additional chart configuration.
        folder_name:          Agent folder name (injected automatically).

    Returns:
        dict with success status, chart_id, name, title, and chart_type.
    """
    try:
        return _run_async(_async_create_chart(
            name=name, title=title, chart_type=chart_type, query=query,
            organization_id=organization_id or _agent_org(),
            project_id=project_id or _agent_project(),
            description=description, refresh_interval=refresh_interval,
            time_field=time_field, group_by=group_by, aggregation=aggregation,
            connection_config_id=connection_config_id, config=config,
            owner_email=_agent_owner(),
        ))
    except Exception as e:
        logger.exception("[BI] tool_create_chart failed")
        return {"success": False, "error": str(e)}


def tool_create_dashboard(
    title: str,
    description: str = "",
    slug: str = "",
    folder_name: str = "",
) -> dict:
    """Creates a new empty dashboard.

    Use this tool to create a dashboard container. After creation, add charts
    and KPIs with tool_add_chart_to_dashboard and tool_add_kpi_to_dashboard,
    then publish with tool_publish_dashboard.

    Args:
        title:       Dashboard title displayed in the UI.
        description: Optional description of the dashboard.
        slug:        URL-friendly identifier (e.g. "sales-overview"). Auto-generated from title if blank.
        folder_name: Agent folder name (injected automatically).

    Returns:
        dict with success status, dashboard_id, title, slug, and status.
    """
    try:
        return _run_async(_async_create_dashboard(
            title=title, description=description, slug=slug,
            owner_email=_agent_owner(),
        ))
    except Exception as e:
        logger.exception("[BI] tool_create_dashboard failed")
        # A duplicate title hits the (name, type, org, project) unique constraint
        # and would surface as a raw psycopg2 IntegrityError — unreadable for the
        # model. Return a clear business message so it asks for another name.
        msg = str(e)
        if "unique" in msg.lower() or "duplicate key" in msg.lower() or "already exists" in msg.lower():
            return {
                "success": False,
                "error": f"A dashboard named '{title}' already exists. Ask the user for a different name.",
            }
        return {"success": False, "error": msg}


def tool_add_chart_to_dashboard(
    chart_id: str,
    dashboard_id: str = "",
    row: int = 0,
    col: int = 0,
    width: int = 6,
    height: int = 4,
    title_override: str = "",
    folder_name: str = "",
) -> dict:
    """Adds an existing chart to a dashboard at a specific grid position.

    The dashboard uses a 12-column grid. Each component has a position
    (row, col) and a size (width in columns, height in grid units).

    Args:
        chart_id:       ID of the chart to embed (from tool_create_chart).
        dashboard_id:   Target dashboard id. OPTIONAL — in a dashboard's own
                        chat it defaults to that dashboard automatically.
        row:            Grid row position (0-based, top to bottom).
        col:            Grid column position (0-based, 0-11).
        width:          Column span (1-12, default 6 = half width).
        height:         Row span in grid units (default 4).
        title_override: Optional title to display instead of the chart's own title.
        folder_name:    Agent folder name (injected automatically).

    Returns:
        dict with success status, dashboard_id, and component_count.
    """
    import os
    dashboard_id = (dashboard_id or os.getenv("AGENT_DASHBOARD_ID", "")).strip()
    if not dashboard_id:
        return {"success": False, "error": (
            "No dashboard specified. From a dashboard's chat I target it "
            "automatically; otherwise pass a dashboard_id (e.g. from "
            "tool_list_dashboards) or create one with tool_create_dashboard."
        )}
    try:
        return _run_async(_async_add_chart_to_dashboard(
            dashboard_id=dashboard_id, chart_id=chart_id,
            row=row, col=col, width=width, height=height,
            title_override=title_override, owner_email=_agent_owner(),
        ))
    except Exception as e:
        logger.exception("[BI] tool_add_chart_to_dashboard failed")
        return {"success": False, "error": str(e)}


def tool_add_kpi_to_dashboard(
    label: str,
    value: str,
    dashboard_id: str = "",
    unit: str = "",
    description: str = "",
    trend_value: float = 0.0,
    trend_direction: str = "neutral",
    trend_sentiment: str = "neutral",
    row: int = 0,
    col: int = 0,
    width: int = 4,
    height: int = 2,
    folder_name: str = "",
) -> dict:
    """Adds a KPI tile to a dashboard.

    A KPI tile displays a single metric value with optional unit and trend
    indicator. Use this for key numbers like "Total Revenue", "Active Users",
    "Error Rate", etc.

    Args:
        label:           KPI label (e.g. "Total Revenue").
        dashboard_id:    Target dashboard id. OPTIONAL — in a dashboard's own
                         chat it defaults to that dashboard automatically.
        value:           KPI value as string (e.g. "12450", "98.5%", "$1.2M").
        unit:            Optional unit label (e.g. "users", "ms", "EUR").
        description:     Optional description shown on hover.
        trend_value:     Numeric trend delta or percentage (e.g. 12.5). Set to 0 for no trend.
        trend_direction: Trend arrow direction: "up", "down", or "neutral".
        trend_sentiment: Trend color meaning: "positive" (green), "negative" (red), or "neutral".
        row:             Grid row position.
        col:             Grid column position.
        width:           Column span (default 4).
        height:          Row span (default 2).
        folder_name:     Agent folder name (injected automatically).

    Returns:
        dict with success status, dashboard_id, and component_count.
    """
    import os
    dashboard_id = (dashboard_id or os.getenv("AGENT_DASHBOARD_ID", "")).strip()
    if not dashboard_id:
        return {"success": False, "error": (
            "No dashboard specified. From a dashboard's chat I target it "
            "automatically; otherwise pass a dashboard_id (e.g. from "
            "tool_list_dashboards) or create one with tool_create_dashboard."
        )}
    try:
        return _run_async(_async_add_kpi_to_dashboard(
            dashboard_id=dashboard_id, label=label, value=value,
            unit=unit, description=description, trend_value=trend_value,
            trend_direction=trend_direction, trend_sentiment=trend_sentiment,
            row=row, col=col, width=width, height=height,
            owner_email=_agent_owner(),
        ))
    except Exception as e:
        logger.exception("[BI] tool_add_kpi_to_dashboard failed")
        return {"success": False, "error": str(e)}


def tool_publish_dashboard(
    dashboard_id: str,
    visibility: str = "organization",
    folder_name: str = "",
) -> dict:
    """Publishes a dashboard, making it visible to users.

    A dashboard must be published before it appears in the UI. The visibility
    level controls who can see it.

    Args:
        dashboard_id: ID of the dashboard to publish.
        visibility:   Access level: "private", "organization", or "public".
        folder_name:  Agent folder name (injected automatically).

    Returns:
        dict with success status, dashboard_id, title, slug, status, and visibility.
    """
    try:
        return _run_async(_async_publish_dashboard(
            dashboard_id=dashboard_id, visibility=visibility,
            owner_email=_agent_owner(),
        ))
    except Exception as e:
        logger.exception("[BI] tool_publish_dashboard failed")
        return {"success": False, "error": str(e)}


def tool_list_dashboards(
    page: int = 1,
    page_size: int = 20,
    folder_name: str = "",
) -> dict:
    """Lists dashboards owned by the current user.

    Returns a paginated list of dashboards with their status and component count.

    Args:
        page:        Page number (1-based, default 1).
        page_size:   Number of dashboards per page (default 20).
        folder_name: Agent folder name (injected automatically).

    Returns:
        dict with success status, dashboards list, total count, and pagination info.
    """
    try:
        return _run_async(_async_list_dashboards(
            page=page, page_size=page_size,
            owner_email=_agent_owner(),
        ))
    except Exception as e:
        logger.exception("[BI] tool_list_dashboards failed")
        return {"success": False, "error": str(e)}


async def _async_get_dashboard_data(
    dashboard_id: str,
    owner_email: str,
    per_chart_limit: int = 200,
) -> dict:
    """Fetch every chart + KPI of a dashboard as raw rows.

    The agent uses this to *read* a dashboard it is linked to (or any
    dashboard by id). Each chart contribution contains labels + rows so the
    LLM can summarise, compare, or answer questions about the data.
    """
    from th2agent.bi.dashboards.service import DashboardService
    from th2agent.bi.charts.service import ChartService
    from th2agent.bi.db_stores import DatabaseDashboardStore, DatabaseChartStore
    from th2agent.bi.data.service import ChartDataService
    from th2agent.bi.data.schema import DataRequest
    from th2agent.helpers.jsonify import to_jsonable

    async with _get_session() as db:
        dash_store = DatabaseDashboardStore(db, owner=owner_email)
        chart_store = DatabaseChartStore(db, owner=owner_email)
        dash_svc = DashboardService(dash_store)
        chart_svc = ChartService(chart_store, db)
        data_svc = ChartDataService(chart_svc)

        dashboard = await dash_svc.get(dashboard_id)
        charts_data: list[dict] = []

        for comp in dashboard.components:
            if comp.chart and comp.chart.chart_id:
                try:
                    req = DataRequest(page=1, page_size=per_chart_limit)
                    result = await data_svc.fetch(
                        comp.chart.chart_id,
                        req,
                        user_id=owner_email,
                        db_session=db,
                    )
                    charts_data.append({
                        "type": "chart",
                        "chart_id": result.chart_id,
                        "title": result.title,
                        "chart_type": result.chart_type.value
                            if hasattr(result.chart_type, "value")
                            else str(result.chart_type),
                        "labels": result.labels,
                        "rows": result.rows,
                        "total_rows": result.pagination.total,
                    })
                except Exception as exc:
                    charts_data.append({
                        "type": "chart",
                        "chart_id": comp.chart.chart_id,
                        "title": comp.chart.title_override or comp.chart.chart_id,
                        "error": str(exc),
                    })
            elif comp.key_value:
                charts_data.append({
                    "type": "kpi",
                    "label": comp.key_value.label,
                    "value": comp.key_value.value,
                    "unit": comp.key_value.unit,
                })

        return to_jsonable({
            "success": True,
            "dashboard_id": dashboard_id,
            "dashboard_title": dashboard.title,
            "components": charts_data,
        })


def tool_get_dashboard_data(dashboard_id: str = "", folder_name: str = "") -> dict:
    """Read every chart and KPI of a dashboard so you can analyse its content.

    Returns the dashboard title plus a list of components. Each chart entry
    contains its ``title``, ``chart_type``, column ``labels`` and the actual
    ``rows`` (first 200). Each KPI entry contains ``label`` + ``value``.

    Call this BEFORE answering questions like "how many leads are in my
    dashboard?", "what's the top row?", "summarise my campaign status".
    Never fabricate numbers — always fetch them first.

    Args:
        dashboard_id: Dashboard UUID. Leave empty to auto-use the dashboard
            the current chat session is attached to (set via
            ``AGENT_DASHBOARD_ID`` by the mini chat).
        folder_name: Agent folder name (injected automatically).

    Returns:
        dict with ``success``, ``dashboard_title`` and ``components``
        (list of chart / kpi entries with their rows).
    """
    import os

    did = (dashboard_id or "").strip()
    if not did:
        did = os.getenv("AGENT_DASHBOARD_ID", "").strip()
    if not did:
        return {
            "success": False,
            "error": (
                "No dashboard_id provided and no AGENT_DASHBOARD_ID found "
                "in the current context. Pass the UUID of the dashboard "
                "you want to read."
            ),
        }
    try:
        return _run_async(_async_get_dashboard_data(
            dashboard_id=did, owner_email=_agent_owner(),
        ))
    except Exception as e:
        logger.exception("[BI] tool_get_dashboard_data failed")
        return {"success": False, "error": str(e)}


def tool_update_chart_data(
    chart_id: str,
    query: str = "",
    connection_config_id: str = "",
    time_field: str = "",
    folder_name: str = "",
) -> dict:
    """Updates the data source of an existing chart.

    Use this to change the SQL query, database connection, or time field
    of a chart without recreating it. At least one field must be provided.

    Args:
        chart_id:             ID of the chart to update.
        query:                New SQL query or query key.
        connection_config_id: New database connection config ID.
        time_field:           New time field column name.
        folder_name:          Agent folder name (injected automatically).

    Returns:
        dict with success status, chart_id, and title.
    """
    try:
        return _run_async(_async_update_chart_data(
            chart_id=chart_id, query=query,
            connection_config_id=connection_config_id, time_field=time_field,
            owner_email=_agent_owner(),
        ))
    except Exception as e:
        logger.exception("[BI] tool_update_chart_data failed")
        return {"success": False, "error": str(e)}


def tool_update_chart(
    chart_id: str,
    title: str = "",
    chart_type: str = "",
    query: str = "",
    connection_config_id: str = "",
    time_field: str = "",
    description: str = "",
    folder_name: str = "",
) -> dict:
    """Modifies an existing chart's definition (not just its data).

    Use this to change a chart's title, its visualization type, or its SQL
    query/connection without recreating it. To change ONLY the data source,
    tool_update_chart_data also works. At least one field must be provided.

    Supported chart_type values: "bar", "line", "pie", "donut", "scatter",
    "area", "stat", "table", "histogram".

    Args:
        chart_id:             ID (or name) of the chart to modify.
        title:                New human-readable title.
        chart_type:           New visualization type.
        query:                New SQL query or query key.
        connection_config_id: New database connection config ID.
        time_field:           New time field column name.
        description:          New description.
        folder_name:          Agent folder name (injected automatically).

    Returns:
        dict with success status, chart_id, title, and chart_type.
    """
    try:
        return _run_async(_async_update_chart(
            chart_id=chart_id, title=title, chart_type=chart_type, query=query,
            connection_config_id=connection_config_id, time_field=time_field,
            description=description, owner_email=_agent_owner(),
        ))
    except Exception as e:
        logger.exception("[BI] tool_update_chart failed")
        return {"success": False, "error": str(e)}


def tool_remove_chart_from_dashboard(
    chart_id: str,
    dashboard_id: str = "",
    folder_name: str = "",
) -> dict:
    """Removes a chart from a dashboard (detaches the widget; keeps the chart).

    The chart object itself is NOT deleted — only its placement on this
    dashboard is removed, so it can be re-added or stays available on other
    dashboards. Use this when the user asks to remove/take a chart off a
    dashboard. ALWAYS confirm with the user which chart before removing it;
    never remove a chart on your own initiative.

    Args:
        chart_id:     ID (or name) of the chart to remove from the dashboard.
        dashboard_id: Target dashboard id. OPTIONAL — in a dashboard's own chat
                      it defaults to that dashboard automatically.
        folder_name:  Agent folder name (injected automatically).

    Returns:
        dict with success status, dashboard_id, removed count, component_count.
    """
    import os
    dashboard_id = (dashboard_id or os.getenv("AGENT_DASHBOARD_ID", "")).strip()
    if not dashboard_id:
        return {"success": False, "error": (
            "No dashboard specified. From a dashboard's chat I target it "
            "automatically; otherwise pass a dashboard_id (e.g. from "
            "tool_list_dashboards)."
        )}
    try:
        return _run_async(_async_remove_chart_from_dashboard(
            chart_id=chart_id, dashboard_id=dashboard_id,
            owner_email=_agent_owner(),
        ))
    except Exception as e:
        logger.exception("[BI] tool_remove_chart_from_dashboard failed")
        return {"success": False, "error": str(e)}


# ---------------------------------------------------------------------------
# Factory — returns tools bound to agent context
# ---------------------------------------------------------------------------


def make_bi_tools(
    folder_name: str,
    owner_email: str,
    organization_id: str,
    project_id: str,
) -> list:
    """Returns BI tools bound to the agent's context.

    Creates closures that automatically inject owner_email, organization_id,
    and project_id into each tool call, so the agent only needs to provide
    the business-relevant parameters.

    Usage:
        from th2agent.tools_store.portfolio.business_intelligence import make_bi_tools
        tools_funcs.extend(make_bi_tools("agent280", "user@example.com", "org1", "thaink2"))

    Args:
        folder_name:     Agent folder name.
        owner_email:     Email of the agent's owner (used for DB ownership).
        organization_id: Default organization ID for new charts.
        project_id:      Default project ID for new charts.

    Returns:
        List of 7 bound tool functions.
    """
    if not folder_name:
        raise ValueError("folder_name is required")
    if not owner_email:
        raise ValueError("owner_email is required")

    import th2agent.tools_store.portfolio.business_intelligence as _bi_module

    def tool_create_chart(
        name: str,
        title: str,
        chart_type: str,
        query: str,
        organization_id: str = organization_id,
        project_id: str = project_id,
        description: str = "",
        refresh_interval: int = 60,
        time_field: str = "",
        group_by: str = "",
        aggregation: str = "",
        connection_config_id: str = "",
        config: str = "",
    ) -> dict:
        """Creates a new chart definition for use in dashboards.

        Use this tool to define a chart that queries data and renders as a
        visualization. The chart is persisted and can be embedded in dashboards
        using tool_add_chart_to_dashboard.

        Supported chart_type values: "bar", "line", "pie", "donut", "scatter",
        "area", "stat", "table", "histogram".

        The optional config parameter accepts a JSON string for KPI-specific
        settings (e.g. {"kpi_column": "revenue", "kpi_aggregation": "sum"}).

        Args:
            name:                 Unique machine name for the chart.
            title:                Human-readable title displayed on the chart.
            chart_type:           Visualization type (bar, line, pie, donut, scatter, area, stat, table, histogram).
            query:                SQL query or query key that provides the chart data.
            organization_id:      Organization that owns the chart.
            project_id:           Project scope (default "thaink2").
            description:          Optional description of the chart.
            refresh_interval:     Auto-refresh interval in seconds (default 60).
            time_field:           Column name used as time axis for timeseries charts.
            group_by:             Comma-separated list of columns to group by.
            aggregation:          Aggregation function (count, sum, avg, min, max, p50, p95, p99).
            connection_config_id: ID of the database connection config to use.
            config:               JSON string with additional chart configuration.

        Returns:
            dict with success status, chart_id, name, title, and chart_type.
        """
        try:
            return _run_async(_bi_module._async_create_chart(
                name=name, title=title, chart_type=chart_type, query=query,
                organization_id=organization_id, project_id=project_id,
                description=description, refresh_interval=refresh_interval,
                time_field=time_field, group_by=group_by, aggregation=aggregation,
                connection_config_id=connection_config_id, config=config,
                owner_email=owner_email,
            ))
        except Exception as e:
            logger.exception("[BI] tool_create_chart failed")
            return {"success": False, "error": str(e)}

    def tool_create_dashboard(
        title: str,
        description: str = "",
        slug: str = "",
    ) -> dict:
        """Creates a new empty dashboard.

        Use this tool to create a dashboard container. After creation, add charts
        and KPIs with tool_add_chart_to_dashboard and tool_add_kpi_to_dashboard,
        then publish with tool_publish_dashboard.

        Args:
            title:       Dashboard title displayed in the UI.
            description: Optional description of the dashboard.
            slug:        URL-friendly identifier (e.g. "sales-overview"). Auto-generated from title if blank.

        Returns:
            dict with success status, dashboard_id, title, slug, and status.
        """
        try:
            return _run_async(_bi_module._async_create_dashboard(
                title=title, description=description, slug=slug,
                owner_email=owner_email,
            ))
        except Exception as e:
            logger.exception("[BI] tool_create_dashboard failed")
            return {"success": False, "error": str(e)}

    def tool_add_chart_to_dashboard(
        dashboard_id: str,
        chart_id: str,
        row: int = 0,
        col: int = 0,
        width: int = 6,
        height: int = 4,
        title_override: str = "",
    ) -> dict:
        """Adds an existing chart to a dashboard at a specific grid position.

        The dashboard uses a 12-column grid. Each component has a position
        (row, col) and a size (width in columns, height in grid units).

        Args:
            dashboard_id:   ID of the target dashboard.
            chart_id:       ID of the chart to embed (from tool_create_chart).
            row:            Grid row position (0-based, top to bottom).
            col:            Grid column position (0-based, 0-11).
            width:          Column span (1-12, default 6 = half width).
            height:         Row span in grid units (default 4).
            title_override: Optional title to display instead of the chart's own title.

        Returns:
            dict with success status, dashboard_id, and component_count.
        """
        try:
            return _run_async(_bi_module._async_add_chart_to_dashboard(
                dashboard_id=dashboard_id, chart_id=chart_id,
                row=row, col=col, width=width, height=height,
                title_override=title_override, owner_email=owner_email,
            ))
        except Exception as e:
            logger.exception("[BI] tool_add_chart_to_dashboard failed")
            return {"success": False, "error": str(e)}

    def tool_add_kpi_to_dashboard(
        dashboard_id: str,
        label: str,
        value: str,
        unit: str = "",
        description: str = "",
        trend_value: float = 0.0,
        trend_direction: str = "neutral",
        trend_sentiment: str = "neutral",
        row: int = 0,
        col: int = 0,
        width: int = 4,
        height: int = 2,
    ) -> dict:
        """Adds a KPI tile to a dashboard.

        A KPI tile displays a single metric value with optional unit and trend
        indicator. Use this for key numbers like "Total Revenue", "Active Users",
        "Error Rate", etc.

        Args:
            dashboard_id:    ID of the target dashboard.
            label:           KPI label (e.g. "Total Revenue").
            value:           KPI value as string (e.g. "12450", "98.5%", "$1.2M").
            unit:            Optional unit label (e.g. "users", "ms", "EUR").
            description:     Optional description shown on hover.
            trend_value:     Numeric trend delta or percentage (e.g. 12.5). Set to 0 for no trend.
            trend_direction: Trend arrow direction: "up", "down", or "neutral".
            trend_sentiment: Trend color meaning: "positive" (green), "negative" (red), or "neutral".
            row:             Grid row position.
            col:             Grid column position.
            width:           Column span (default 4).
            height:          Row span (default 2).

        Returns:
            dict with success status, dashboard_id, and component_count.
        """
        try:
            return _run_async(_bi_module._async_add_kpi_to_dashboard(
                dashboard_id=dashboard_id, label=label, value=value,
                unit=unit, description=description, trend_value=trend_value,
                trend_direction=trend_direction, trend_sentiment=trend_sentiment,
                row=row, col=col, width=width, height=height,
                owner_email=owner_email,
            ))
        except Exception as e:
            logger.exception("[BI] tool_add_kpi_to_dashboard failed")
            return {"success": False, "error": str(e)}

    def tool_publish_dashboard(
        dashboard_id: str,
        visibility: str = "organization",
    ) -> dict:
        """Publishes a dashboard, making it visible to users.

        A dashboard must be published before it appears in the UI. The visibility
        level controls who can see it.

        Args:
            dashboard_id: ID of the dashboard to publish.
            visibility:   Access level: "private", "organization", or "public".

        Returns:
            dict with success status, dashboard_id, title, slug, status, and visibility.
        """
        try:
            return _run_async(_bi_module._async_publish_dashboard(
                dashboard_id=dashboard_id, visibility=visibility,
                owner_email=owner_email,
            ))
        except Exception as e:
            logger.exception("[BI] tool_publish_dashboard failed")
            return {"success": False, "error": str(e)}

    def tool_list_dashboards(
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """Lists dashboards owned by the current user.

        Returns a paginated list of dashboards with their status and component count.

        Args:
            page:      Page number (1-based, default 1).
            page_size: Number of dashboards per page (default 20).

        Returns:
            dict with success status, dashboards list, total count, and pagination info.
        """
        try:
            return _run_async(_bi_module._async_list_dashboards(
                page=page, page_size=page_size,
                owner_email=owner_email,
            ))
        except Exception as e:
            logger.exception("[BI] tool_list_dashboards failed")
            return {"success": False, "error": str(e)}

    def tool_update_chart_data(
        chart_id: str,
        query: str = "",
        connection_config_id: str = "",
        time_field: str = "",
    ) -> dict:
        """Updates the data source of an existing chart.

        Use this to change the SQL query, database connection, or time field
        of a chart without recreating it. At least one field must be provided.

        Args:
            chart_id:             ID of the chart to update.
            query:                New SQL query or query key.
            connection_config_id: New database connection config ID.
            time_field:           New time field column name.

        Returns:
            dict with success status, chart_id, and title.
        """
        try:
            return _run_async(_bi_module._async_update_chart_data(
                chart_id=chart_id, query=query,
                connection_config_id=connection_config_id, time_field=time_field,
                owner_email=owner_email,
            ))
        except Exception as e:
            logger.exception("[BI] tool_update_chart_data failed")
            return {"success": False, "error": str(e)}

    # -----------------------------------------------------------------------
    # Dashboard data reading tools
    # -----------------------------------------------------------------------

    async def _async_get_dashboard_data(dashboard_id: str, owner_email: str) -> dict:
        """Fetch all chart data from a dashboard."""
        from th2agent.bi.dashboards.service import DashboardService
        from th2agent.bi.dashboards.db_stores import DatabaseDashboardStore
        from th2agent.bi.data.service import ChartDataService
        from th2agent.bi.data.schema import DataRequest
        from th2agent.bi.charts.db_stores import DatabaseChartStore

        async with _get_session() as session:
            dash_store = DatabaseDashboardStore(session)
            chart_store = DatabaseChartStore(session)
            dash_svc = DashboardService(dash_store, chart_store)
            data_svc = ChartDataService(chart_store)

            dashboard = await dash_svc.get(dashboard_id)
            charts_data = []

            for comp in dashboard.components:
                if comp.chart and comp.chart.chart_id:
                    try:
                        req = DataRequest(page=1, page_size=200)
                        result = await data_svc.fetch(
                            comp.chart.chart_id, req, user_id=owner_email
                        )
                        charts_data.append({
                            "chart_id": result.chart_id,
                            "title": result.title,
                            "chart_type": result.chart_type,
                            "labels": result.labels,
                            "rows": result.rows[:200],
                            "total_rows": result.pagination.total,
                        })
                    except Exception as e:
                        charts_data.append({
                            "chart_id": comp.chart.chart_id,
                            "title": comp.chart.title_override or comp.chart.chart_id,
                            "error": str(e),
                        })

                elif comp.key_value:
                    charts_data.append({
                        "type": "kpi",
                        "label": comp.key_value.label,
                        "value": comp.key_value.value,
                        "unit": comp.key_value.unit,
                        "trend": comp.key_value.trend.model_dump() if comp.key_value.trend else None,
                    })

            return {
                "dashboard_id": dashboard_id,
                "dashboard_title": dashboard.title,
                "charts": charts_data,
            }

    def tool_get_dashboard_data(dashboard_id: str = "") -> dict:
        """Reads all chart data and KPIs from a dashboard.

        Use this tool to access the actual data displayed on a dashboard.
        Returns every chart's title, type, column labels, and data rows,
        plus any KPI tiles. This lets you analyze, summarize, or compare
        the dashboard's content.

        Args:
            dashboard_id: The dashboard UUID. If empty, tries to find the
                          dashboard linked to the current agent.

        Returns:
            dict with dashboard_title and a list of charts, each containing
            title, chart_type, labels, rows, and total_rows.
        """
        import os
        owner = _agent_owner()
        if not dashboard_id:
            dashboard_id = os.getenv("AGENT_DASHBOARD_ID", "")
        if not dashboard_id:
            return {"success": False, "error": "No dashboard_id provided and no linked dashboard found."}
        try:
            return _run_async(_bi_module._async_get_dashboard_data(
                dashboard_id=dashboard_id, owner_email=owner,
            ))
        except Exception as e:
            logger.exception("[BI] tool_get_dashboard_data failed")
            return {"success": False, "error": str(e)}

    return [
        tool_create_chart,
        tool_create_dashboard,
        tool_add_chart_to_dashboard,
        tool_add_kpi_to_dashboard,
        tool_publish_dashboard,
        tool_list_dashboards,
        tool_update_chart_data,
        tool_update_chart,
        tool_remove_chart_from_dashboard,
        tool_get_dashboard_data,
    ]
