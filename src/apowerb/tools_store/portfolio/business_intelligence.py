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
import contextvars
import contextlib
import json
import threading
import re
from logging import getLogger
from typing import Any

from apowerb.schema.forecast_schema import MAX_DATA_ROWS
from apowerb.tools_store.portfolio import api_call
from apowerb.tools_store.portfolio.bi_datasets import (
    _agent_db_connection,
    _load_agent_sql_rows,
    _load_owned_dataset_rows,
    _load_uploaded_file_rows,
    _materialize_uploaded_file_dataset,
    _query_owned_dataset,
    validate_forecast_columns,
)

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
# Rows of a dataset query handed back to the agent with the chart.
_MAX_RETURNED_ROWS = 100

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

        # A new thread starts with an empty context: run in a copy of ours so
        # the coroutine sees the invocation's agent identity (ContextVars).
        t = threading.Thread(target=contextvars.copy_context().run, args=(_run,))
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

    from apowerb.helpers.database_connection import DBConfig
    from apowerb.configs.settings import get_settings

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
    dataset_id: str = "",
) -> dict:
    from apowerb.bi.charts.core import ChartOrigin, ChartType, DataSource, AggregationFunc, SourceType
    from apowerb.bi.charts.schemas import ChartCreateRequest, DataSourceSchema
    from apowerb.bi.charts.service import ChartConflictError, ChartService
    from apowerb.bi.db_stores import DatabaseChartStore

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

    # An imported dataset is not a table of the agent's database: run the
    # query on the dataset itself and chart the stored result. The chart data
    # service applies neither group_by nor aggregation, so the aggregation has
    # to happen here. The dataset is a static CSV, so a snapshot loses nothing.
    source_type = None
    source_limit = None
    data_rows: list[dict] | None = None
    if dataset_id:
        if connection_config_id:
            return {"success": False, "error": (
                "Pass either dataset_id (an imported dataset) or connection_config_id "
                "(a database), not both."
            )}
        queried = await _query_owned_dataset(dataset_id, query, owner_email)
        if not queried["success"]:
            return {"success": False, "error": queried["error"]}
        data_rows = queried["rows"]
        if not data_rows:
            return {"success": False, "error": "The query returned no rows: the chart was NOT created."}
        stored_id = await _async_store_rows_dataset(
            data_rows, name, owner_email, organization_id, project_id, origin="chart_query",
        )
        query, connection_config_id = f"csv://{stored_id}", ""
        source_type, source_limit = SourceType.CSV, len(data_rows)

    # Owner used to load the DB connection config when validating the query
    # below. Defaults to the chart owner; overridden to the agent's owner when
    # we auto-detect the connection config (that is who the config is scoped to).
    _probe_owner = owner_email

    # Auto-detect connection_config_id for SQL queries.
    # When the query looks like SQL (SELECT, WITH, etc.) and no explicit
    # connection_config_id was given, use the agent's DB tool config, looked
    # up for the agent's owner so a foreign tenant's config is never picked.
    if not connection_config_id and query and _looks_like_sql(query):
        try:
            found = _agent_db_connection()
            if found:
                connection_config_id, _probe_owner = found
                logger.info(f"[BI] Auto-detected connection_config_id={connection_config_id} for SQL query")
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
        from apowerb.bi.charts.core import DataSource
        from apowerb.bi.data.db_executor import DatabaseQueryExecutor
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
                        "If the data is an imported dataset (see tool_list_datasets), it is not a "
                        "table of this database: call tool_create_chart again with its dataset_id "
                        "and a query FROM dataset, e.g. SELECT strftime(date_trunc('month', "
                        "<date column>), '%Y-%m') AS month, SUM(<amount column>) AS total "
                        "FROM dataset GROUP BY 1 ORDER BY 1 (real column names from "
                        "tool_describe_dataset). For a table of this database, do not hand-write "
                        "SQL against an assumed schema: call tool_text_to_sql with the user's "
                        "question, then pass its sql_query to tool_create_chart."
                    ),
                }
            logger.warning("[BI] chart query validation skipped (non-SQL error): %s", exc)

    source_schema = DataSourceSchema(
        query=query,
        aggregation=agg,
        group_by=group_by_list,
        time_field=time_field or None,
        connection_config_id=connection_config_id or None,
        **({"source_type": source_type, "limit": source_limit} if source_type else {}),
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
            return await svc.create(req, created_by=owner_email, origin=ChartOrigin.CHAT)

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
    created = {
        "success": True,
        "chart_id": chart.id,
        "name": chart.name,
        "title": chart.title,
        "chart_type": chart.chart_type.value,
        "message": f"Chart '{chart.title}' created successfully.",
    }
    if data_rows is not None:
        # The figures themselves, so the agent can quote them in its answer
        # (in a workflow, only its text reaches the next node).
        created["data"] = data_rows[:_MAX_RETURNED_ROWS]
        created["row_count"] = len(data_rows)
    return created


async def _async_create_dashboard(
    title: str,
    description: str,
    slug: str,
    owner_email: str,
) -> dict:
    from apowerb.bi.dashboards.schema import DashboardCreateRequest
    from apowerb.bi.dashboards.service import DashboardService
    from apowerb.bi.db_stores import DatabaseDashboardStore

    from apowerb.core.invocation_context import get_root_agent_id
    agent_id_str = get_root_agent_id()
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


async def _resolve_position(svc, dashboard_id: str, row: int, col: int) -> tuple[int, int]:
    """Traduit une position non precisee en "sous tout le reste".

    ``row < 0`` est la sentinelle : elle dit que l'appelant n'a pas choisi de
    place. C'est le cas par defaut des outils d'ajout, parce qu'un modele qui
    choisit sa ligne le fait sans connaitre l'occupation reelle de la grille --
    il rejoue le gabarit de son instruction et pose son element au milieu de
    ceux que l'utilisateur avait ranges.

    Un ajout empile part aussi de la colonne 0 : il prend la largeur demandee a
    gauche, et l'utilisateur le deplace ensuite a la souris s'il le souhaite.
    Une position explicite (``row >= 0``) est respectee telle quelle.
    """
    if row >= 0:
        return row, col
    dashboard = await svc.get(dashboard_id)
    return dashboard.next_free_row(), 0


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
    from apowerb.bi.dashboards.core import DashboardComponent, GridPosition
    from apowerb.bi.dashboards.schema import AddComponentRequest, ComponentCreateSchema, ChartWidgetSchema, GridPositionSchema
    from apowerb.bi.dashboards.service import DashboardService
    from apowerb.bi.db_stores import DatabaseDashboardStore

    async with _get_session() as db:
        store = DatabaseDashboardStore(db, owner=owner_email)
        svc = DashboardService(store)
        row, col = await _resolve_position(svc, dashboard_id, row, col)
        # Resolve chart_id: accept UUID directly or look up by name
        from apowerb.bi.db_stores import DatabaseChartStore
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
    from apowerb.bi.dashboards.core import TrendDirection, TrendSentiment
    from apowerb.bi.dashboards.schema import (
        AddComponentRequest,
        ComponentCreateSchema,
        GridPositionSchema,
        KeyValueCreateSchema,
        TrendSchema,
    )
    from apowerb.bi.dashboards.service import DashboardService
    from apowerb.bi.db_stores import DatabaseDashboardStore

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
        row, col = await _resolve_position(svc, dashboard_id, row, col)

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
    from apowerb.bi.dashboards.core import DashboardVisibility
    from apowerb.bi.dashboards.service import DashboardService
    from apowerb.bi.db_stores import DatabaseDashboardStore

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
    from apowerb.bi.dashboards.service import DashboardService
    from apowerb.bi.db_stores import DatabaseDashboardStore

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
    from apowerb.bi.charts.schemas import ChartUpdateRequest, DataSourceSchema
    from apowerb.bi.charts.service import ChartService
    from apowerb.bi.db_stores import DatabaseChartStore

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
    from apowerb.bi.charts.schemas import ChartUpdateRequest, DataSourceSchema
    from apowerb.bi.charts.service import ChartService
    from apowerb.bi.data.service import ChartDataService
    from apowerb.bi.db_stores import DatabaseChartStore

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
    from apowerb.bi.dashboards.core import ComponentType
    from apowerb.bi.dashboards.service import DashboardNotFoundError, DashboardService
    from apowerb.bi.db_stores import DatabaseChartStore, DatabaseDashboardStore

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
    """Return the owner email of the running agent (per-invocation)."""
    from apowerb.core.invocation_context import get_agent_owner
    return get_agent_owner()


def _agent_org() -> str:
    """Return the organization_id of the running agent (per-invocation)."""
    from apowerb.core.invocation_context import get_agent_organization_id
    return get_agent_organization_id()


def _agent_project() -> str:
    """Return the project_id of the running agent (per-invocation)."""
    from apowerb.core.invocation_context import get_agent_project_id
    return get_agent_project_id()


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
    "send to dashboard" button); agent runs fall back to the chat session of
    the invocation (``invocation_context.get_chat_session_id``). Falls back to
    the legacy shared dashboard when no session is known. Dashboard mini-chats
    (session ``dashboard-chat-*``) embed into their linked dashboard, so they
    keep the legacy slug here.
    """
    from apowerb.core.invocation_context import get_chat_session_id
    sid = (session_id or get_chat_session_id()).strip()
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
    from apowerb.bi.dashboards.service import SlugConflictError
    from apowerb.bi.db_stores import DatabaseChartStore, DatabaseDashboardStore

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
        from apowerb.bi.db_stores import DatabaseChartStore

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
    from apowerb.bi.db_stores import DatabaseChartStore

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
    dataset_id: str = "",
) -> dict:
    """Creates a new chart definition for use in dashboards.

    Use this tool to define a chart that queries data and renders as a
    visualization. The chart is persisted and can be embedded in dashboards
    using tool_add_chart_to_dashboard.

    Supported chart_type values: "bar", "line", "pie", "donut", "scatter",
    "area", "stat", "table", "histogram", "forecast". For "forecast", use
    tool_create_forecast_chart instead — it builds the CSV source and
    forecast config this tool does not set up on its own.

    The optional config parameter accepts a JSON string for KPI-specific
    settings (e.g. {"kpi_column": "revenue", "kpi_aggregation": "sum"}).

    On an imported dataset (dataset_id from tool_list_datasets), pass
    dataset_id and write query as one SELECT over the table named
    `dataset` — never the file name. Date columns are real dates. Monthly
    revenue, for example:
        SELECT strftime(date_trunc('month', order_date), '%Y-%m') AS month,
               SUM(order_amount) AS revenue
        FROM dataset GROUP BY 1 ORDER BY 1
    The result rows are returned in `data` so you can quote the figures.

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
        dataset_id:           Imported dataset to query (query then reads FROM dataset).

    Returns:
        dict with success status, chart_id, name, title, and chart_type;
        with dataset_id, also data (up to 100 result rows) and row_count.
    """
    try:
        return _run_async(_async_create_chart(
            name=name, title=title, chart_type=chart_type, query=query,
            organization_id=organization_id or _agent_org(),
            project_id=project_id or _agent_project(),
            description=description, refresh_interval=refresh_interval,
            time_field=time_field, group_by=group_by, aggregation=aggregation,
            connection_config_id=connection_config_id, config=config,
            owner_email=_agent_owner(), dataset_id=dataset_id,
        ))
    except Exception as e:
        logger.exception("[BI] tool_create_chart failed")
        return {"success": False, "error": str(e)}


async def _async_save_forecast_chart(
    source_schema,
    source_label: str,
    target_var: str,
    title: str,
    config: dict,
    organization_id: str,
    project_id: str,
    owner_email: str,
):
    from apowerb.bi.charts.core import ChartOrigin, ChartType
    from apowerb.bi.charts.schemas import ChartCreateRequest
    from apowerb.bi.charts.service import ChartConflictError, ChartService
    from apowerb.bi.db_stores import DatabaseChartStore

    async def _create_with_name(chart_name: str):
        async with _get_session() as db:
            store = DatabaseChartStore(db, owner=owner_email)
            svc = ChartService(store, db)
            req = ChartCreateRequest(
                name=chart_name, title=title, chart_type=ChartType.FORECAST,
                source=source_schema, organization_id=organization_id,
                project_id=project_id, config=config,
            )
            return await svc.create(req, created_by=owner_email, origin=ChartOrigin.CHAT)

    machine_name = f"forecast-{source_label}-{target_var}"
    try:
        return await _create_with_name(machine_name)
    except ChartConflictError:
        import uuid
        return await _create_with_name(f"{machine_name}-{uuid.uuid4().hex[:6]}")


def tool_create_forecast_chart(
    date_var: str,
    target_var: str,
    horizon: int,
    title: str,
    dataset_id: str = "",
    sql: str = "",
    group_var: str = "",
    frequency: str = "",
    folder_name: str = "",
    file_id: str = "",
    sheet: str = "",
    correct_outliers: bool = False,
) -> dict:
    """Creates a forecast widget chart and computes its forecast summary,
    from exactly one source: an imported dataset (dataset_id), a SELECT
    query on this agent's database connection (sql), or a file the user
    attached to the chat (file_id).

    Use tool_describe_dataset (dataset) or tool_describe_sql (query) first to
    pick real date_var/target_var/group_var column names. Call
    embed_chart(chart_id, title) afterwards to show the widget in the
    conversation — do not repeat the forecast numbers yourself beyond
    commenting the summary.

    Args:
        date_var:    Name of the date column.
        target_var:  Name of the column to forecast.
        horizon:     Number of future periods to forecast (1-366).
        title:       Human-readable title displayed on the chart.
        dataset_id:  The dataset_id (UUID) returned by tool_list_datasets.
        sql:         A single SELECT query on the agent's database (write it
                     with tool_text_to_sql; a query starting with WITH is
                     refused). The chart re-runs it when displayed.
        group_var:   Optional column to forecast independently per group.
        frequency:   Optional series frequency (day, week, month, quarter, year).
        folder_name: Agent folder name (injected automatically).
        file_id:     Name of a file attached to the chat, exactly as shown in
                     the `[Uploaded files: ...]` line of the user's message
                     (csv, tsv, txt, xlsx, xlsm, xls, ods, json, parquet). No
                     need to import it first: it is stored as a dataset of the
                     agent's owner so the chart can re-forecast when displayed.
                     Never copy its rows anywhere.
        sheet:       Sheet name for a spreadsheet file_id (first sheet if omitted).
        correct_outliers: R engine only. Detect and correct outliers before
                     training; the widget forecasts the same way when displayed.
                     Default: False.

    Returns:
        dict with success status; on success, chart_id, title, and summary
        (the same compact forecast summary tool_thaink2_forecast returns).
        On failure (no or two sources, unknown owner/dataset, no database
        connection, query error, unknown column, too many rows, or the
        forecast service failing), no chart is created.
    """
    owner_email = _agent_owner()
    if not owner_email:
        return {"success": False, "error": "No owner context."}
    try:
        loaded = _load_forecast_rows(
            owner_email, dataset_id, sql, file_id, sheet, date_var, target_var, group_var,
        )
        if not loaded["success"]:
            return loaded

        preprocessing = {"outliers": True} if correct_outliers else None
        # Hors de _run_async (plafonné à 30 s alors que Chronos peut prendre
        # une minute) et AVANT l'enregistrement : un graphique n'est créé que
        # si sa prévision a réussi.
        forecast_summary = api_call._execute_forecast(
            date_var=date_var, target_var=target_var, horizon=horizon,
            data_rows=loaded["rows"], group_var=group_var or None,
            frequency=frequency or None, models=["auto"],
            preprocessing=preprocessing,
        )
        if forecast_summary.get("status") != "success":
            return {"success": False, "error": _forecast_error(forecast_summary)}

        chart = _save_forecast_widget(
            owner_email, loaded, dataset_id, sql, file_id, sheet, title,
            date_var, target_var, group_var, horizon, frequency, preprocessing,
        )
        if isinstance(chart, dict):
            return chart
        return {
            "success": True,
            "chart_id": chart.id,
            "title": chart.title,
            "summary": forecast_summary,
        }
    except Exception as e:
        logger.exception("[BI] tool_create_forecast_chart failed")
        return {"success": False, "error": str(e)}


def _forecast_error(response: dict) -> str:
    return response.get("message") or str(response.get("errors"))


def _load_forecast_rows(
    owner_email: str, dataset_id: str, sql: str, file_id: str, sheet: str,
    date_var: str, target_var: str, group_var: str,
) -> dict:
    """Rows of exactly one forecast source, with the columns checked."""
    if sum(1 for s in (dataset_id, sql, file_id) if s) != 1:
        return {"success": False, "error": "Fournir exactement une source : dataset_id, sql ou file_id."}
    if sql:
        loaded = _run_async(_load_agent_sql_rows(sql, owner_email, MAX_DATA_ROWS))
    elif file_id:
        loaded = _load_uploaded_file_rows(file_id, owner_email, MAX_DATA_ROWS, sheet or None)
    else:
        loaded = _run_async(_load_owned_dataset_rows(dataset_id, owner_email, MAX_DATA_ROWS))
    if not loaded["success"]:
        return {"success": False, "error": loaded["error"]}
    if loaded["truncated"]:
        what = "Le résultat de la requête" if sql else "Le jeu de données"
        return {"success": False, "error": api_call._too_large_message(what)}
    column_error = validate_forecast_columns(loaded["columns"], date_var, target_var, group_var)
    if column_error:
        return {"success": False, "error": column_error}
    return loaded


def _save_forecast_widget(
    owner_email: str, loaded: dict, dataset_id: str, sql: str, file_id: str, sheet: str,
    title: str, date_var: str, target_var: str, group_var: str, horizon: int,
    frequency: str, preprocessing: dict | None,
):
    """Persist the forecast widget once its forecast succeeded; returns the
    chart, or an error dict."""
    from apowerb.bi.charts.core import SourceType
    from apowerb.bi.charts.schemas import DataSourceSchema

    if sql:
        # Same connection the rows came from: the chart re-runs the query
        # with its owner's config when displayed.
        source = DataSourceSchema(
            source_type=SourceType.DATABASE, query=sql,
            connection_config_id=loaded["connection_config_id"], limit=MAX_DATA_ROWS,
        )
        label = "sql"
    else:
        if file_id:
            # Après la prévision : un échec ne laisse ni graphique ni jeu orphelin.
            stored = _run_async(_materialize_uploaded_file_dataset(
                file_id, owner_email, _agent_org(), _agent_project(), sheet or None,
            ))
            if not stored["success"]:
                return {"success": False, "error": stored["error"]}
            dataset_id = stored["dataset_id"]
        source = DataSourceSchema(
            source_type=SourceType.CSV, query=f"csv://{dataset_id}", limit=MAX_DATA_ROWS,
        )
        label = dataset_id
    config = {
        "date_var": date_var,
        "target_var": target_var,
        "group_var": group_var,
        "horizon": horizon,
        "frequency": frequency,
        "models": ["auto"],
        "confidence_levels": [0.8, 0.95],
    }
    if preprocessing:
        config["preprocessing"] = preprocessing
    return _run_async(_async_save_forecast_chart(
        source_schema=source, source_label=label, target_var=target_var,
        title=title, config=config,
        organization_id=_agent_org(), project_id=_agent_project(),
        owner_email=owner_email,
    ))


# ---------------------------------------------------------------------------
# Forecast dashboard: one forecast, explained by several charts.
# ---------------------------------------------------------------------------


def tool_create_forecast_dashboard(
    date_var: str,
    target_var: str,
    horizon: int,
    title: str,
    dataset_id: str = "",
    sql: str = "",
    group_var: str = "",
    frequency: str = "",
    folder_name: str = "",
    file_id: str = "",
    sheet: str = "",
    correct_outliers: bool = False,
) -> dict:
    """Forecasts a series ONCE and builds a whole dashboard that explains it:
    4 KPI tiles (total forecast, change vs the same period last year,
    reliable series, peak period), the forecast widget with its 80/95 %
    bands, then forecast vs last year per series, share of each series,
    forecast volume per period, uncertainty per series and a detail table.

    Same sources as tool_create_forecast_chart — exactly one of dataset_id,
    sql (a single SELECT on this agent's database connection) or file_id (a
    file attached to the chat). Check the columns first with
    tool_describe_dataset / tool_describe_sql / read_uploaded_file. Use it
    when the user asks for a forecast dashboard, a full analysis or several
    charts; use tool_create_forecast_chart for a single chart.

    Args:
        date_var:    Name of the date column.
        target_var:  Name of the column to forecast.
        horizon:     Number of future periods to forecast (1-366).
        title:       Dashboard title (made unique if already taken).
        dataset_id:  The dataset_id (UUID) returned by tool_list_datasets.
        sql:         A single SELECT query on the agent's database.
        group_var:   Optional column to forecast independently per group
                     (product family, site...): the dashboard compares them.
        frequency:   Optional series frequency (day, week, month, quarter, year).
        folder_name: Agent folder name (injected automatically).
        file_id:     Name of a file attached to the chat, exactly as in the
                     `[Uploaded files: ...]` line. Never copy its rows.
        sheet:       Sheet name for a spreadsheet file_id.
        correct_outliers: R engine only. Correct outliers before training.

    Returns:
        dict with success status; on success dashboard_id, dashboard_title,
        forecast_chart_id (pass it to embed_chart to show the forecast in the
        chat), chart_ids, headline (the KPI numbers), series (one row per
        series: totals, change vs last year, reliability, uncertainty) and
        summary (the compact forecast summary). Comment headline and series;
        never invent numbers. On failure nothing is created if the forecast
        itself failed; a later failure reports the dashboard_id built so far.
    """
    from apowerb.bi import forecast_dashboard as fd

    owner_email = _agent_owner()
    if not owner_email:
        return {"success": False, "error": "No owner context."}
    try:
        loaded = _load_forecast_rows(
            owner_email, dataset_id, sql, file_id, sheet, date_var, target_var, group_var,
        )
        if not loaded["success"]:
            return loaded
        preprocessing = {"outliers": True} if correct_outliers else None
        response = api_call._forecast_raw(
            date_var=date_var, target_var=target_var, horizon=horizon,
            data_rows=loaded["rows"], group_var=group_var or None,
            frequency=frequency or None, models=["auto"],
            preprocessing=preprocessing,
        )
        if response.get("status") != "success":
            return {"success": False, "error": _forecast_error(response)}
        table = fd.series_table(response)
        if not table:
            return {"success": False, "error": "La prévision n'a renvoyé aucune série exploitable."}

        forecast_chart = _save_forecast_widget(
            owner_email, loaded, dataset_id, sql, file_id, sheet, title,
            date_var, target_var, group_var, horizon, frequency, preprocessing,
        )
        if isinstance(forecast_chart, dict):
            return forecast_chart

        periods = fd.period_table(response, table)
        headline = fd.headline(table, periods)
        built = _run_async(_async_build_forecast_dashboard(
            title=title, target_var=target_var, frequency=response.get("frequency") or frequency,
            forecast_chart_id=forecast_chart.id, table=table, shares=fd.share_table(table), periods=periods,
            headline=headline, owner_email=owner_email,
            organization_id=_agent_org(), project_id=_agent_project(),
        ))
        if not built["success"]:
            return {**built, "forecast_chart_id": forecast_chart.id}
        return {
            **built,
            "forecast_chart_id": forecast_chart.id,
            "headline": headline,
            "series": table[:20],
            "summary": api_call._compact_response(response),
        }
    except Exception as e:
        logger.exception("[BI] tool_create_forecast_dashboard failed")
        return {"success": False, "error": str(e)}


async def _async_store_rows_dataset(
    rows: list[dict], name: str, owner_email: str, organization_id: str, project_id: str,
    origin: str = "forecast_dashboard",
) -> str:
    """Store computed rows as a CSV dataset of ``owner_email``; returns its id."""
    import uuid

    from apowerb.bi.data.tabular_import import store_tabular_as_csv
    from apowerb.bi.db_stores import DatabaseDataStore
    from apowerb.helpers.tabular_loader import TabularData

    dataset_id = str(uuid.uuid4())
    data = TabularData(columns=list(rows[0].keys()), rows=rows, format="csv")
    async with _get_session() as db:
        store = DatabaseDataStore(db, owner=owner_email)
        await store_tabular_as_csv(
            store, organization_id=organization_id, project_id=project_id,
            file_id=dataset_id, name=f"{name} #{dataset_id[:8]}", data=data,
            uploaded_by=owner_email, extra_metadata={"source": origin},
        )
    return dataset_id


async def _async_save_dataset_chart(
    chart_type: str, dataset_id: str, title: str, description: str, config: dict,
    owner_email: str, organization_id: str, project_id: str,
):
    import uuid

    from apowerb.bi.charts.core import ChartOrigin, ChartType, SourceType
    from apowerb.bi.charts.schemas import ChartCreateRequest, DataSourceSchema
    from apowerb.bi.charts.service import ChartService
    from apowerb.bi.db_stores import DatabaseChartStore

    async with _get_session() as db:
        store = DatabaseChartStore(db, owner=owner_email)
        svc = ChartService(store, db)
        req = ChartCreateRequest(
            name=f"forecast-dashboard-{chart_type}-{uuid.uuid4().hex[:8]}",
            title=title, chart_type=ChartType(chart_type), description=description,
            source=DataSourceSchema(
                source_type=SourceType.CSV, query=f"csv://{dataset_id}", limit=MAX_DATA_ROWS,
            ),
            organization_id=organization_id, project_id=project_id, config=config,
        )
        return await svc.create(req, created_by=owner_email, origin=ChartOrigin.CHAT)


async def _async_create_unique_dashboard(title: str, owner_email: str) -> dict:
    """Create the dashboard, suffixing the title when it is already taken."""
    from datetime import datetime

    try:
        return await _async_create_dashboard(title=title, description="", slug="", owner_email=owner_email)
    except Exception as e:
        msg = str(e).lower()
        if not any(s in msg for s in ("unique", "duplicate key", "already exists")):
            raise
    stamped = f"{title} ({datetime.now().strftime('%d/%m/%Y %H:%M:%S')})"
    return await _async_create_dashboard(title=stamped, description="", slug="", owner_email=owner_email)


def _check(result: dict) -> dict:
    """The dashboard helpers report failures as ``success: False``."""
    if not result.get("success"):
        raise RuntimeError(result.get("error") or "échec sans message")
    return result


def _format_number(value: float) -> str:
    return f"{value:,.0f}".replace(",", " ") if abs(value) >= 100 else f"{value:.2f}".rstrip("0").rstrip(".")


async def _async_build_forecast_dashboard(
    title: str, target_var: str, frequency: str | None, forecast_chart_id: str, table: list[dict],
    shares: list[dict], periods: list[dict], headline: dict, owner_email: str,
    organization_id: str, project_id: str,
) -> dict:
    """Dashboard around an already saved forecast widget: KPI tiles, then
    charts on small datasets computed from the same forecast."""
    from datetime import datetime

    from apowerb.bi.forecast_dashboard import displayed_series, format_period

    snapshot = f"Instantané de la prévision du {datetime.now().strftime('%d/%m/%Y %H:%M')}."
    dashboard_id = None
    chart_ids: list[str] = []
    try:
        dashboard = await _async_create_unique_dashboard(title, owner_email)
        dashboard_id = dashboard["dashboard_id"]
        multi = len(table) > 1
        change = headline["change_pct"]
        kpis = [
            dict(label="Total prévu sur l'horizon", value=_format_number(headline["total"]),
                 description=f"Somme des valeurs prévues de « {target_var} », toutes séries."),
            dict(label="Écart vs même période N-1",
                 value=f"{change:+.1f}" if change is not None else "n/d",
                 unit="%" if change is not None else "",
                 description="Prévision comparée aux mêmes semaines ou mois de l'an dernier."
                 if change is not None else "Historique trop court pour comparer à l'an dernier.",
                 trend_value=change or 0.0,
                 trend_direction="up" if (change or 0) > 0 else "down" if (change or 0) < 0 else "neutral"),
            dict(label="Séries fiables", value=f"{headline['reliable']} / {headline['series']}",
                 description="Séries dont le modèle est jugé fiable au test sur le passé."),
            dict(label="Pic prévu",
                 value=format_period(headline["peak_period"], frequency) if headline["peak_period"] else "n/d",
                 description=f"Période la plus chargée : {_format_number(headline['peak_value'])}."
                 if headline["peak_value"] is not None else ""),
        ]
        for i, kpi in enumerate(kpis):
            _check(await _async_add_kpi_to_dashboard(
                dashboard_id=dashboard_id, unit=kpi.pop("unit", ""),
                trend_value=kpi.pop("trend_value", 0.0),
                trend_direction=kpi.pop("trend_direction", "neutral"), trend_sentiment="neutral",
                row=0, col=i * 3, width=3, height=2, owner_email=owner_email, **kpi,
            ))
        _check(await _async_add_chart_to_dashboard(
            dashboard_id=dashboard_id, chart_id=forecast_chart_id, row=2, col=0,
            width=12, height=6, title_override="", owner_email=owner_email,
        ))

        async def add(rows, chart_type, chart_title, config, row, col, width):
            dataset_id = await _async_store_rows_dataset(
                rows, f"{title} - {chart_title}", owner_email, organization_id, project_id,
            )
            chart = await _async_save_dataset_chart(
                chart_type, dataset_id, chart_title, snapshot, config,
                owner_email, organization_id, project_id,
            )
            chart_ids.append(chart.id)
            _check(await _async_add_chart_to_dashboard(
                dashboard_id=dashboard_id, chart_id=chart.id, row=row, col=col,
                width=width, height=5, title_override="", owner_email=owner_email,
            ))

        has_last_year = any(r["meme_periode_n1"] for r in table)
        comparison = (
            ["prevu_total", "meme_periode_n1"] if has_last_year else ["prevu_total"]
        )
        await add(
            [{k: r[k] for k in ("serie", *comparison)} for r in table],
            "bar", "Prévu vs même période N-1" if has_last_year else "Prévu par série",
            {"labelColumn": "serie", "valueColumns": comparison}, 8, 0, 6 if multi else 12,
        )
        if multi:
            await add(shares, "donut", "Part de chaque série dans la prévision",
                      {"labelColumn": "serie", "valueColumns": ["prevu_total"]}, 8, 6, 6)
        series_columns = [k for k in periods[0] if k != "periode"]
        await add(periods, "bar", "Volume prévu par période",
                  {"labelColumn": "periode", "valueColumns": series_columns, "stacked": True}, 13, 0, 12)
        shown = set(displayed_series(table))
        uncertainty = [
            {"serie": r["serie"], "incertitude_80_pct": r["incertitude_80_pct"]}
            for r in table if r["serie"] in shown and r["incertitude_80_pct"] is not None
        ]
        if uncertainty:
            await add(uncertainty, "bar", "Incertitude par série (largeur de la bande 80 %, en % du prévu)",
                      {"labelColumn": "serie", "valueColumns": ["incertitude_80_pct"]}, 18, 0, 6)
        await add(table, "table", "Détail par série", {}, 18, 6 if uncertainty else 0, 6 if uncertainty else 12)
        _check(await _async_publish_dashboard(dashboard_id=dashboard_id, visibility="private", owner_email=owner_email))
    except Exception as e:
        logger.exception("[BI] forecast dashboard build failed")
        return {
            "success": False, "dashboard_id": dashboard_id, "chart_ids": chart_ids,
            "error": f"Tableau de bord incomplet : {e}" if dashboard_id else f"Tableau de bord non créé : {e}",
        }
    return {
        "success": True,
        "dashboard_id": dashboard_id,
        "dashboard_title": dashboard["title"],
        "chart_ids": chart_ids,
    }


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
    row: int = -1,
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
        row:            Grid row position (0-based, top to bottom). Leave
                        unset to stack the component under everything
                        already on the dashboard -- that is the default.
        col:            Grid column position (0-based, 0-11).
        width:          Column span (1-12, default 6 = half width).
        height:         Row span in grid units (default 4).
        title_override: Optional title to display instead of the chart's own title.
        folder_name:    Agent folder name (injected automatically).

    Returns:
        dict with success status, dashboard_id, and component_count.
    """
    from apowerb.core.invocation_context import get_dashboard_id
    dashboard_id = (dashboard_id or get_dashboard_id()).strip()
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
    row: int = -1,
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
        row:             Grid row position. Leave unset to stack under the
                         existing components -- that is the default.
        col:             Grid column position.
        width:           Column span (default 4).
        height:          Row span (default 2).
        folder_name:     Agent folder name (injected automatically).

    Returns:
        dict with success status, dashboard_id, and component_count.
    """
    from apowerb.core.invocation_context import get_dashboard_id
    dashboard_id = (dashboard_id or get_dashboard_id()).strip()
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

    A spec v2 dashboard (built from a BI template) keeps its components in
    ``visual``, which the loop below does not read: it is computed by the spec
    engine instead, otherwise the agent sees an empty dashboard.
    """
    from apowerb.tools_store.portfolio.bi_dashboard_tools import _async_spec_dashboard_data

    spec = await _async_spec_dashboard_data(dashboard_id, owner_email)
    if spec is not None:
        return spec

    from apowerb.bi.dashboards.service import DashboardService
    from apowerb.bi.charts.service import ChartService
    from apowerb.bi.db_stores import DatabaseDashboardStore, DatabaseChartStore
    from apowerb.bi.data.service import ChartDataService
    from apowerb.bi.data.schema import DataRequest
    from apowerb.helpers.jsonify import to_jsonable

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
            the current chat session is attached to.
        folder_name: Agent folder name (injected automatically).

    Returns:
        dict with ``success``, ``dashboard_title`` and ``components``
        (list of chart / kpi entries with their rows).
    """
    from apowerb.core.invocation_context import get_dashboard_id

    did = (dashboard_id or "").strip()
    if not did:
        did = get_dashboard_id().strip()
    if not did:
        return {
            "success": False,
            "error": (
                "No dashboard_id provided and no dashboard linked to "
                "this chat. Pass the UUID of the dashboard "
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
    from apowerb.core.invocation_context import get_dashboard_id
    dashboard_id = (dashboard_id or get_dashboard_id()).strip()
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
        from apowerb.tools_store.portfolio.business_intelligence import make_bi_tools
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

    import apowerb.tools_store.portfolio.business_intelligence as _bi_module

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
        dataset_id: str = "",
    ) -> dict:
        """Creates a new chart definition for use in dashboards.

        Use this tool to define a chart that queries data and renders as a
        visualization. The chart is persisted and can be embedded in dashboards
        using tool_add_chart_to_dashboard.

        Supported chart_type values: "bar", "line", "pie", "donut", "scatter",
        "area", "stat", "table", "histogram".

        The optional config parameter accepts a JSON string for KPI-specific
        settings (e.g. {"kpi_column": "revenue", "kpi_aggregation": "sum"}).

        On an imported dataset (dataset_id from tool_list_datasets), pass
        dataset_id and write query as one SELECT over the table named
        `dataset` — never the file name. Date columns are real dates. Monthly
        revenue, for example:
            SELECT strftime(date_trunc('month', order_date), '%Y-%m') AS month,
                   SUM(order_amount) AS revenue
            FROM dataset GROUP BY 1 ORDER BY 1
        The result rows are returned in `data` so you can quote the figures.

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
            dataset_id:           Imported dataset to query (query then reads FROM dataset).

        Returns:
            dict with success status, chart_id, name, title, and chart_type;
            with dataset_id, also data (up to 100 result rows) and row_count.
        """
        try:
            return _run_async(_bi_module._async_create_chart(
                name=name, title=title, chart_type=chart_type, query=query,
                organization_id=organization_id, project_id=project_id,
                description=description, refresh_interval=refresh_interval,
                time_field=time_field, group_by=group_by, aggregation=aggregation,
                connection_config_id=connection_config_id, config=config,
                owner_email=owner_email, dataset_id=dataset_id,
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
        row: int = -1,
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
            row:            Grid row position (0-based, top to bottom). Leave
                            unset to stack under the existing components. Leave
                        unset to stack the component under everything
                        already on the dashboard -- that is the default.
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
        row: int = -1,
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
            row:             Grid row position. Leave unset to stack under
                             the existing components. Leave unset to stack under the
                         existing components -- that is the default.
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
            dict with success, dashboard_title and components (list of chart
            entries with title, chart_type, labels, rows and total_rows, and
            kpi entries with label, value and unit).
        """
        from apowerb.core.invocation_context import get_dashboard_id
        if not dashboard_id:
            dashboard_id = get_dashboard_id()
        if not dashboard_id:
            return {"success": False, "error": "No dashboard_id provided and no linked dashboard found."}
        try:
            return _run_async(_bi_module._async_get_dashboard_data(
                dashboard_id=dashboard_id, owner_email=owner_email,
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
