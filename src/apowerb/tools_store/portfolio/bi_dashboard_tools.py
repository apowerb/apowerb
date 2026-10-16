"""
bi_dashboard_tools.py
---------------------
Agent (chat) tools for the BI dashboard spec v2: a dashboard that owns its
data sources, measures, global filters and inline visuals.

The tools are synchronous (ADK) and reuse the context helpers and the
async-from-sync bridge of ``business_intelligence``. A dashboard is only ever
read or written through a store scoped to the agent's owner, so an agent never
sees another owner's dashboards.

Tools: tool_bi_import_attachment, tool_bi_create_dashboard,
tool_bi_create_stock_dashboard, tool_bi_patch_dashboard,
tool_bi_query_dashboard, tool_bi_describe_dashboard.
"""

from __future__ import annotations

import contextlib
import json
from logging import getLogger
from typing import Any

from pydantic import TypeAdapter, ValidationError

from apowerb.tools_store.portfolio.business_intelligence import (
    _agent_org,
    _agent_owner,
    _agent_project,
    _get_session,
    _run_async,
)

logger = getLogger(__name__)

_MAX_ERRORS = 8
_TABLE_ROWS = 20
_CHART_ROWS = 30
_MAX_FIELDS = 60
_MAX_OPTIONS = 20


# ---------------------------------------------------------------------------
# Seams (kept as small functions so tests can replace them)
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def _dashboard_store(owner: str):
    """Dashboard store scoped to ``owner`` (reads/writes outside it are invisible)."""
    from apowerb.bi.db_stores import DatabaseDashboardStore

    async with _get_session() as db:
        yield DatabaseDashboardStore(db, owner=owner)


def _run_tool(coro):
    """``_run_async`` with dataset lookups on a tool-owned session: the tool's
    loop cannot use the shared pool, bound to the FastAPI loop."""
    from apowerb.bi.data.csv_executor import use_session_factory

    async def scoped():
        with use_session_factory(lambda: _get_session()):
            return await coro

    return _run_async(scoped())


async def _compute(dashboard, filters: dict | None, owner: str, components: list[str] | None = None):
    from apowerb.bi.spec.engine import compute_dashboard
    from apowerb.bi.spec.models import DashboardDataRequest

    request = DashboardDataRequest(filters=filters or {}, components=components)
    return await compute_dashboard(dashboard, request, owner=owner)


def _validate(dashboard) -> list[str]:
    from apowerb.bi.spec.validate import validate_spec

    return list(validate_spec(dashboard))


def _apply_ops(dashboard, ops):
    from apowerb.bi.spec.ops import apply_ops

    return apply_ops(dashboard, ops)


# ---------------------------------------------------------------------------
# Input parsing
# ---------------------------------------------------------------------------


def _fmt_validation(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:_MAX_ERRORS]:
        loc = ".".join(str(p) for p in err["loc"])
        parts.append(f"{loc}: {err['msg']}")
    more = len(exc.errors()) - _MAX_ERRORS
    if more > 0:
        parts.append(f"... and {more} more")
    return "; ".join(parts)


def _loads(text: str, what: str, expected: type | tuple) -> tuple[Any, str | None]:
    kinds = expected if isinstance(expected, tuple) else (expected,)
    try:
        value = json.loads(text) if (text or "").strip() else kinds[0]()
    except json.JSONDecodeError as exc:
        return None, (
            f"{what} is not valid JSON ({exc.msg} at line {exc.lineno}, column {exc.colno}). "
            "Send a single JSON value with double quotes and no trailing commas."
        )
    if not isinstance(value, kinds):
        return None, f"{what} must be a JSON {'object' if kinds[0] is dict else 'array'}."
    return value, None


def _build_dashboard(title: str, description: str, spec: dict, owner: str):
    """Spec dict -> Dashboard (v2). Returns (dashboard, error)."""
    from apowerb.bi.dashboards.core import ComponentType, Dashboard, DashboardComponent, GridPosition
    from apowerb.bi.spec.models import FilterSpec, SourceSpec, VisualSpec
    from apowerb.core.invocation_context import get_root_agent_id

    unknown = set(spec) - {"sources", "filters", "components"}
    if unknown:
        return None, (
            f"Unknown key(s) in spec_json: {sorted(unknown)}. "
            "Expected {\"sources\": [...], \"filters\": [...], \"components\": [{\"visual\": {...}, \"position\": {...}}]}."
        )
    try:
        sources = [SourceSpec.model_validate(s) for s in spec.get("sources") or []]
        filters = [FilterSpec.model_validate(f) for f in spec.get("filters") or []]
        components = []
        for i, item in enumerate(spec.get("components") or []):
            if not isinstance(item, dict) or "visual" not in item:
                return None, f"components[{i}] must be an object {{\"visual\": {{...}}, \"position\": {{...}}}}."
            components.append(
                DashboardComponent(
                    component_type=ComponentType.VISUAL,
                    position=GridPosition(**(item.get("position") or {})),
                    visual=VisualSpec.model_validate(item["visual"]),
                )
            )
    except ValidationError as exc:
        return None, f"Invalid spec: {_fmt_validation(exc)}"
    if not sources:
        return None, "spec_json needs at least one source (e.g. kind='dataset' with a dataset_id)."
    agent_id_str = get_root_agent_id()
    try:
        dashboard = Dashboard.create(
            title=title,
            description=description or None,
            components=components,
            sources=sources,
            filters=filters,
            spec_version=2,
            created_by=owner,
            agent_id=int(agent_id_str) if agent_id_str else None,
        )
    except ValidationError as exc:
        return None, f"Invalid dashboard: {_fmt_validation(exc)}"
    return dashboard, None


# ---------------------------------------------------------------------------
# Result summaries (compact: never thousands of rows)
# ---------------------------------------------------------------------------


def _clean_row(row: dict) -> dict:
    out = {}
    for key, val in row.items():
        if key.startswith("_color_") or key == "_highlight":
            continue
        out[key] = round(val, 4) if isinstance(val, float) else val
    return out


def _columnar(rows: list[dict]) -> dict:
    """Rows as one header + value lists: the keys are not repeated on every row.

    The model rereads a tool result on every following call of the turn, and a
    chart's keys repeated over 30 rows were about half of its characters.
    """
    columns: list[str] = []
    for row in rows:
        columns += [k for k in row if k not in columns]
    return {"columns": columns, "rows": [[row.get(k) for k in columns] for row in rows]}


def _component_title(comp) -> str | None:
    visual = getattr(comp, "visual", None)
    if visual is None:
        return None
    if visual.title:
        return visual.title
    if visual.kpi:
        return visual.kpi.measure
    return None


def _summarize_components(dashboard, response, *, detail: bool) -> tuple[list[dict], dict[str, str]]:
    """Per-component values. ``detail`` adds sample rows (query) instead of counts only."""
    out: list[dict] = []
    errors: dict[str, str] = {}
    by_id = {c.id: c for c in dashboard.components}
    for cid, res in response.components.items():
        comp = by_id.get(cid)
        title = _component_title(comp) if comp else None
        entry: dict[str, Any] = {"id": cid, "type": res.type}
        if title:
            entry["title"] = title
        if res.error:
            entry["error"] = res.error
            errors[cid] = f"{title or res.type}: {res.error}"
            out.append(entry)
            continue
        if res.type == "kpi" and res.value is not None:
            kv = res.value
            entry["value"] = kv.formatted
            if kv.value is not None:
                entry["raw"] = round(kv.value, 4)
            if kv.status:
                entry["status"] = kv.status
            if kv.delta_formatted:
                entry["delta"] = kv.delta_formatted
            if kv.secondary:
                entry["secondary"] = kv.secondary
        elif res.type == "text":
            entry["text"] = res.text
        elif res.type == "note":
            if detail:
                entry["text"] = res.text
            else:
                continue
        elif res.rows is not None:
            entry["row_count"] = len(res.rows)
            limit = _TABLE_ROWS if res.type == "table" else _CHART_ROWS
            if detail:
                entry.update(_columnar([_clean_row(r) for r in res.rows[:limit]]))
                if len(res.rows) > limit:
                    entry["truncated"] = True
            else:
                entry["sample"] = [_clean_row(r) for r in res.rows[:2]]
        out.append(entry)
    return out, errors


async def _preview(
    dashboard, owner: str, filters: dict | None = None, *, detail: bool = False, components: list[str] | None = None
) -> dict:
    """Compute the dashboard and summarise it; a compute failure is reported, not raised."""
    try:
        response = await _compute(dashboard, filters, owner, components)
    except Exception as exc:  # engine/SQL/forecast failure
        logger.warning("[BI_SPEC] preview failed: %s", exc)
        return {"preview_error": f"Computation failed: {exc}", "components": [], "errors": {}}
    summary, errors = _summarize_components(dashboard, response, detail=detail)
    if components:
        summary = [c for c in summary if c["id"] in components]
    result: dict[str, Any] = {
        "components": summary,
        "errors": errors,
        "filters": {
            fid: {"value": rf.value, "label": rf.label, **({"from": rf.date_from, "to": rf.date_to} if rf.date_from else {})}
            for fid, rf in response.filters.items()
        },
    }
    if detail:
        result["filter_options"] = {
            fid: list(opts)[:_MAX_OPTIONS] for fid, opts in response.filter_options.items()
        }
    return result


_NAME_CONSTRAINT = "uq_bi_name_type_org_project"


def _is_duplicate(exc: Exception) -> bool:
    """Whether ``exc`` is a dashboard-name conflict (and nothing else)."""
    from sqlalchemy.exc import IntegrityError

    from apowerb.bi.dashboards.service import DashboardNameConflictError

    if isinstance(exc, DashboardNameConflictError):
        return True
    return isinstance(exc, IntegrityError) and _NAME_CONSTRAINT in str(exc.orig)


# ---------------------------------------------------------------------------
# Async implementations
# ---------------------------------------------------------------------------


async def _async_create(
    title: str, description: str, spec: dict, owner: str, template: dict | None = None, locale: str | None = None
) -> dict:
    dashboard, error = _build_dashboard(title, description, spec, owner)
    if error:
        return {"success": False, "error": error}
    if locale and locale != "fr":
        dashboard = dashboard.model_copy(update={"locale": locale})
    if template is not None:
        from apowerb.bi.dashboards.core import TemplateRef

        dashboard = dashboard.model_copy(update={"template": TemplateRef.model_validate(template)})
    issues = _validate(dashboard)
    if issues:
        return {
            "success": False,
            "error": "The spec is invalid, nothing was created. Fix these problems and retry.",
            "issues": issues[:20],
        }
    from apowerb.bi.dashboards.service import create_with_unique_title

    requested = dashboard.title

    async def save(candidate: str):
        async with _dashboard_store(owner) as store:
            return await store.save(dashboard.model_copy(update={"title": candidate}))

    saved, final_title = await create_with_unique_title(requested, save, is_duplicate=_is_duplicate)
    dashboard = saved or dashboard.model_copy(update={"title": final_title})
    preview = await _preview(dashboard, owner)
    renamed = (
        {"renamed_from": requested} if dashboard.title != requested else {}
    )
    # Relative on purpose: the UI serves it from its own origin. The model once
    # put an invented third-party domain in front of it (PROD, 13/10).
    message = (
        f"Dashboard '{dashboard.title}' created. Link for the user: /bi/{dashboard.id}"
        " (copy this relative path exactly as written; never add a domain in front of it)."
        " Its component ids are not chart ids: never pass them to embed_chart; give this link instead."
    )
    if renamed:
        message = (
            f"Le nom « {requested} » était déjà pris : le tableau s'appelle « {dashboard.title} ». "
            f"Tell the user. {message}"
        )
    if preview["errors"]:
        message += " Some components have errors: see 'errors'."
    return {
        "success": True,
        "dashboard_id": dashboard.id,
        "url": f"/bi/{dashboard.id}",
        "title": dashboard.title,
        **renamed,
        "component_count": len(dashboard.components),
        **preview,
        "message": message,
    }


_NOT_FOUND = "Dashboard '{id}' not found. Use tool_list_dashboards to find the right id."


def _layout_diff(before, after) -> dict:
    b = {c.id for c in before.components}
    a = {c.id for c in after.components}
    return {
        "components_before": len(b),
        "components_after": len(a),
        "added": sorted(a - b),
        "removed": sorted(b - a),
        "sources": [s.name for s in after.sources],
        "filters": [f.id for f in after.filters],
    }


async def _async_patch(dashboard_id: str, ops: list, dry_run: bool, owner: str) -> dict:
    from apowerb.bi.spec.errors import SpecError

    async with _dashboard_store(owner) as store:
        dashboard = await store.get(dashboard_id)
        if dashboard is None:
            return {"success": False, "error": _NOT_FOUND.format(id=dashboard_id)}
        try:
            patched = _apply_ops(dashboard, ops)
        except SpecError as exc:
            return {"success": False, "error": f"Patch rejected, nothing was changed: {exc}"}
        issues = _validate(patched)
        if issues:
            return {
                "success": False,
                "error": "The patched spec is invalid, nothing was changed. Fix these problems and retry.",
                "issues": issues[:20],
            }
        if not dry_run:
            saved = await store.save(patched)
            patched = saved or patched
    preview = await _preview(patched, owner)
    return {
        "success": True,
        "dry_run": dry_run,
        "dashboard_id": dashboard_id,
        "applied_ops": len(ops),
        "summary": _layout_diff(dashboard, patched),
        **preview,
        "message": ("Dry run: nothing was saved." if dry_run else "Dashboard updated."),
    }


def _select_components(dashboard, wanted: str) -> tuple[list[str] | None, str | None]:
    """Component ids from a comma-separated list of ids or titles (case-insensitive)."""
    names = [w.strip() for w in wanted.split(",") if w.strip()]
    if not names:
        return None, None
    ids, unknown = [], []
    for name in names:
        match = next(
            (c.id for c in dashboard.components
             if c.id == name or (_component_title(c) or "").casefold() == name.casefold()),
            None,
        )
        if match:
            ids.append(match)
        else:
            unknown.append(name)
    if unknown:
        valid = "; ".join(f"{c.id} ({_component_title(c) or '-'})" for c in dashboard.components)
        return None, f"Unknown component(s): {', '.join(unknown)}. Valid components: {valid}."
    return ids, None


async def _async_spec_dashboard_data(dashboard_id: str, owner: str) -> dict | None:
    """Computed values of a spec v2 dashboard, for ``tool_get_dashboard_data``.

    None when the dashboard is not spec v2 or not visible to ``owner``: the
    caller then reads it as a classic dashboard (``chart``/``key_value``).
    """
    from apowerb.bi.data.csv_executor import use_session_factory

    async with _dashboard_store(owner) as store:
        dashboard = await store.get(dashboard_id)
    if dashboard is None or not dashboard.sources:
        return None
    with use_session_factory(lambda: _get_session()):
        preview = await _preview(dashboard, owner, detail=True)
    if "preview_error" in preview:
        return {"success": False, "error": preview["preview_error"]}
    return {"success": True, "dashboard_id": dashboard_id, "dashboard_title": dashboard.title, **preview}


async def _async_query(
    dashboard_id: str, filters: dict, owner: str, component_ids: str = "", group_by: str = ""
) -> dict:
    async with _dashboard_store(owner) as store:
        dashboard = await store.get(dashboard_id)
    if dashboard is None:
        return {"success": False, "error": _NOT_FOUND.format(id=dashboard_id)}
    if not dashboard.sources:
        return {
            "success": False,
            "error": "This dashboard has no spec v2 sources (it is made of classic charts). Use tool_get_dashboard_data instead.",
        }
    components, error = _select_components(dashboard, component_ids)
    if error:
        return {"success": False, "error": error}
    if group_by and not components:
        return {"success": False, "error": "group_by needs component_ids: name the figure(s) to compare."}
    preview = await _preview(dashboard, owner, filters, detail=True, components=components)
    if "preview_error" in preview:
        return {"success": False, "error": preview["preview_error"]}
    if not group_by:
        return {"success": True, "dashboard_id": dashboard_id, "title": dashboard.title, **preview}
    return await _grouped(dashboard, owner, filters, components, group_by.strip(), preview)


async def _grouped(dashboard, owner: str, filters: dict, components: list[str], group_by: str, base: dict) -> dict:
    """The selected components once per value of the filter ``group_by``."""
    options = base.get("filter_options", {})
    if group_by not in options:
        return {"success": False, "error": f"Unknown filter {group_by!r} for group_by. Filters: {', '.join(options) or 'none'}."}
    groups, errors = [], {}
    for value in options[group_by]:
        part = await _preview(dashboard, owner, {**filters, group_by: value}, components=components)
        if "preview_error" in part:
            errors[str(value)] = part["preview_error"]
            continue
        groups.append({"value": value, "components": [{k: v for k, v in c.items() if k != "id"} for c in part["components"]]})
        errors.update({f"{value}/{k}": e for k, e in part["errors"].items()})
    return {
        "success": True, "dashboard_id": dashboard.id, "title": dashboard.title, "group_by": group_by,
        "filters": {k: v for k, v in base["filters"].items() if k != group_by}, "groups": groups, "errors": errors,
    }


def _describe_transform(step) -> str:
    op = step.op
    if op == "aggregate":
        return f"aggregate by {', '.join(step.group_by)}"
    if op == "derive":
        return f"derive {step.column} = {step.expr}"
    if op == "filter":
        return f"filter {step.where}"
    if op == "join":
        return f"join {step.source} on {', '.join(step.on)}"
    if op == "sql":
        return "sql"
    if op in ("naive_forecast", "forecast"):
        return f"{op} of {step.value} -> {step.output}"
    if op == "inventory_sim":
        return f"inventory_sim (safety {step.safety_factor}, order {step.order_factor}, lead time {step.lead_time})"
    if op == "replenishment_plan":
        return f"replenishment_plan from {step.stock} over {step.horizon} periods (cover {step.target_cover})"
    return op


def _describe_visual(comp) -> dict:
    v = comp.visual
    entry: dict[str, Any] = {"id": comp.id, "type": v.type}
    if v.title:
        entry["title"] = v.title
    if v.source:
        entry["source"] = v.source
    if v.x:
        entry["x"] = v.x
    if v.kpi:
        entry["measure"] = v.kpi.measure
    if v.series:
        entry["series"] = [s.key for s in v.series]
    if v.columns:
        entry["columns"] = [c.key for c in v.columns]
    pos = comp.position
    entry["position"] = {"row": pos.row, "col": pos.col, "width": pos.width, "height": pos.height}
    return entry


async def _async_describe(dashboard_id: str, owner: str) -> dict:
    async with _dashboard_store(owner) as store:
        dashboard = await store.get(dashboard_id)
    if dashboard is None:
        return {"success": False, "error": _NOT_FOUND.format(id=dashboard_id)}
    inferred: dict[str, list[dict]] = {}
    if dashboard.sources:
        try:
            response = await _compute(dashboard, None, owner, components=[])
            for name, meta in response.sources.items():
                inferred[name] = [
                    {"name": f.name, "label": f.label, "type": f.type, "provenance": f.provenance.value}
                    for f in meta.fields[:_MAX_FIELDS]
                ]
        except Exception as exc:  # describe must work even when data is unreachable
            logger.info("[BI_SPEC] describe: field inference skipped: %s", exc)
    sources = []
    for s in dashboard.sources:
        declared = [
            {"name": f.name, "label": f.label, "type": f.type, "provenance": f.provenance.value}
            for f in s.fields[:_MAX_FIELDS]
        ]
        src: dict[str, Any] = {"name": s.name, "kind": s.kind, "fields": inferred.get(s.name) or declared}
        if s.label:
            src["label"] = s.label
        if s.dataset_id:
            src["dataset_id"] = s.dataset_id
        if s.parent:
            src["parent"] = s.parent
        if s.transforms:
            src["transforms"] = [_describe_transform(t) for t in s.transforms]
        src["measures"] = [
            {
                "name": m.name,
                "label": m.label,
                "expr": m.expr,
                "format": m.format,
                **({"thresholds": m.thresholds.model_dump(mode="json")} if m.thresholds else {}),
            }
            for m in s.measures
        ]
        sources.append(src)
    return {
        "success": True,
        "dashboard_id": dashboard_id,
        "title": dashboard.title,
        "spec_version": dashboard.spec_version,
        "url": f"/bi/{dashboard_id}",
        "sources": sources,
        "filters": [
            {"id": f.id, "label": f.label, "field": f.field, "type": f.type, "default": f.default,
             **({"presets": f.presets} if f.presets else {})}
            for f in dashboard.filters
        ],
        "components": [_describe_visual(c) for c in dashboard.components if c.visual is not None],
        "legacy_components": sum(1 for c in dashboard.components if c.visual is None),
    }


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _fail(tool: str, exc: Exception, title: str = "") -> dict:
    logger.exception("[BI_SPEC] %s failed", tool)
    if title and _is_duplicate(exc):
        return {
            "success": False,
            "error": f"A dashboard named '{title}' already exists. Ask the user for a different name.",
        }
    return {"success": False, "error": str(exc)}


def _create(
    title: str, description: str, spec: dict, tool: str, template: dict | None = None, locale: str | None = None
) -> dict:
    owner = _agent_owner()
    if not owner:
        return {"success": False, "error": "No owner context."}
    if not (title or "").strip():
        return {"success": False, "error": "title is required."}
    try:
        return _run_tool(_async_create(title.strip(), description, spec, owner, template, locale))
    except Exception as exc:
        return _fail(tool, exc, title)


def tool_bi_import_attachment(filename: str, sheet: str = "") -> dict:
    """Imports a file attached to the chat as a BI dataset and returns its dataset_id.

    A dashboard is built on a DATASET, never on a chat attachment directly:
    when the user attaches a file (CSV, Excel, ODS, JSON, Parquet) and asks for
    a dashboard, call this first, then pass the returned ``dataset_id`` to
    tool_bi_create_stock_dashboard or tool_bi_create_dashboard. The dataset
    lands in the user's Data Pool, exactly as if they had uploaded the file in
    the BI page. Importing the same file again reuses the same dataset.
    Do not read the file with read_uploaded_file first: this tool reads it and
    returns its columns, and the raw content would be resent on every step.

    Args:
        filename: Name of the attached file, exactly as shown in
                  ``[Uploaded files: ...]`` (a plain file name, no path).
        sheet:    Sheet name for Excel/ODS files. Empty: the first sheet.

    Returns:
        dict with success, dataset_id, name, columns (names, to use as
        ``date``/``family``/``sales``/``stock``... arguments) and row_count.
        If the file is not found the error lists the available files.
    """
    from apowerb.tools_store.portfolio.bi_datasets import _materialize_uploaded_file_dataset

    owner = _agent_owner()
    if not owner:
        return {"success": False, "error": "No owner context."}
    name = (filename or "").strip()
    if not name:
        return {
            "success": False,
            "error": "filename is required: pass the attached file name as shown in [Uploaded files: ...].",
        }
    try:
        stored = _run_async(_materialize_uploaded_file_dataset(
            name, owner, _agent_org(), _agent_project(), sheet.strip() or None,
        ))
    except Exception as exc:
        return _fail("tool_bi_import_attachment", exc)
    if not stored["success"]:
        return {"success": False, "error": stored["error"]}
    return {
        "success": True,
        "dataset_id": stored["dataset_id"],
        "name": stored["name"],
        "columns": stored["columns"],
        "row_count": stored["row_count"],
        "reused": stored["reused"],
        "message": (
            f"File '{name}' is dataset {stored['dataset_id']}. Pass it as dataset_id to the dashboard tools."
        ),
    }


def tool_bi_create_dashboard(
    title: str,
    spec_json: str,
    description: str = "",
    folder_name: str = "",
) -> dict:
    """Creates an interactive dashboard from a declarative spec (sources, measures, filters, visuals).

    Use it instead of the chart-by-chart tools when the user wants global
    filters (family, period...), KPIs with traffic-light thresholds, linked
    charts or a computed text. For a stock/inventory dashboard prefer
    tool_bi_create_stock_dashboard. To change an existing one use
    tool_bi_describe_dashboard then tool_bi_patch_dashboard.

    If the title is already taken the dashboard is renamed "Title (2)", "Title (3)"...
    and the result carries "renamed_from"; its "message" then starts with the
    sentence for the user: tell them the final title in your answer.

    The dashboard OWNS its data: sources point at a dataset (see
    bi_datasets.tool_list_datasets / tool_describe_dataset), measures are SQL
    aggregates, visuals reference a source and measure names. Never embed rows.

    spec_json is one JSON object:
    {"sources": [{"name": "ventes", "kind": "dataset", "dataset_id": "<uuid>",
        "measures": [{"name": "total", "label": "Ventes", "expr": "SUM(montant)",
                      "format": "compact"},
                     {"name": "taux", "label": "Taux", "expr": "1 - SUM(perte)/NULLIF(SUM(montant),0)",
                      "format": "percent",
                      "thresholds": {"direction": "higher_is_better", "good": 0.95, "warn": 0.9}}]}],
     "filters": [{"id": "region", "label": "Region", "field": "region", "type": "select"},
                 {"id": "periode", "label": "Periode", "field": "date",
                  "type": "relative_period", "default": "26w", "presets": ["13w", "26w", "all"]}],
     "components": [
       {"visual": {"type": "kpi", "title": "Ventes", "source": "ventes", "kpi": {"measure": "total"}},
        "position": {"row": 0, "col": 0, "width": 3, "height": 3}},
       {"visual": {"type": "bar", "title": "Ventes par mois", "source": "ventes", "x": "mois",
                   "series": [{"key": "total"}]},
        "position": {"row": 3, "col": 0, "width": 12, "height": 6}}]}

    Visual types: kpi, combo, bar, hbar, line, area, pie, diverging_bar,
    waterfall, table, text, note. Grid: 12 columns, give explicit positions
    without overlap. Text templates accept {{measure|percent}} and
    {{filters.<id>}}, {{period.last}}. Field names: lowercase identifiers.
    Filter types: select, multi, date_range, relative_period (<n>d, <n>w,
    <n>m, <n>y, ytd, mtd, all), time_grain on a date field (hour, day, week,
    month, quarter, year: buckets the x axis) and aggregation on a number
    field (sum, avg, min, max, count of that column).

    Returns the dashboard id, its relative url (/bi/<id>), the computed KPI
    values and texts, and an "errors" map with one message per component that
    failed to compute (fix them with tool_bi_patch_dashboard). If the spec is
    invalid nothing is created and "issues" lists what to fix.

    Args:
        title:       Dashboard title.
        spec_json:   JSON object described above.
        description: Optional description.
        folder_name: Agent folder name (injected automatically).
    """
    spec, error = _loads(spec_json, "spec_json", dict)
    if error:
        return {"success": False, "error": error}
    return _create(title, description, spec, "tool_bi_create_dashboard")


def tool_bi_create_stock_dashboard(
    dataset_id: str = "",
    title: str = "",
    date: str = "semaine",
    family: str = "famille",
    sales: str = "ventes",
    site: str = "",
    safety_factor: float = 0.5,
    order_factor: float = 1.4,
    lead_time: int = 1,
    default_period: str = "26w",
    horizon: int = 8,
    stock: str = "",
    safety_column: str = "",
    lead_time_column: str = "",
    target_cover: int = 4,
    language: str = "fr",
) -> dict:
    """Creates a complete "Pilotage stocks" dashboard from a weekly sales dataset.

    When the dataset has a stock column, pass it as ``stock`` (and the safety
    threshold / supplier lead time columns when they exist): the dashboard then
    plans the orders from the REAL stock — engine forecast over ``horizon``
    weeks, order placed in the week where the stock would fall under the
    threshold on arrival, quantity = threshold + ``target_cover`` weeks of
    forecast — with 4 KPIs (current stock, next week's forecast, quantity to
    order this week, families at risk), a stock/forecast/replenishment chart,
    the orders week by week and a per-family table.

    Use it when the user wants to steer inventory (stock coverage, service
    rate, forecast reliability, weeks under safety stock) from a dataset of
    weekly sales per product family. It builds a transparent naive forecast
    and a SIMULATED stock and replenishment loop when the data has no stock
    column (the real data is then only the sales), 6 KPIs with traffic-light thresholds, a combined chart, coverage,
    a per-family table, a waterfall, a forecast-error ranking, five notes, a
    family filter and a period filter.

    Call bi_datasets.tool_describe_dataset first to find the column names.
    When the data is a file attached to the chat, import it first with
    tool_bi_import_attachment: it returns the dataset_id (and the columns).

    Args:
        dataset_id:     Dataset id (UUID) holding one row per week and family (and site).
                        Required: from tool_bi_import_attachment (chat attachment)
                        or tool_list_datasets (already imported).
        title:          Dashboard title.
        date:           Week (date) column name, lowercase.
        family:         Product family column name, lowercase.
        sales:          Sales quantity column name, lowercase.
        site:           Site/warehouse column name if the data has one row per
                        site: rows are summed per week and family. Empty if none.
        safety_factor:  Safety stock as a share of the week's forecast (0.5 = 50 %).
        order_factor:   Order up to this multiple of next week's forecast (+ safety stock).
        lead_time:      Delivery lead time in weeks.
        default_period: Initial period: 13w, 26w, 52w or all.
        horizon:        Weeks of engine forecast ahead.
        stock:          Real stock column name, lowercase. Empty: the stock is simulated.
        safety_column:  Safety threshold column (units), with ``stock``. Empty:
                        ``safety_factor`` weeks of forecast.
        lead_time_column: Supplier lead time column (weeks), with ``stock``.
                        Empty: ``lead_time``.
        target_cover:   Weeks of forecast an order covers above the threshold.
        language:       Language of the dashboard texts, "fr" or "en": use the
                        language of the conversation.

    Returns the dashboard id, url (/bi/<id>), computed KPIs and the text
    summary, and an "errors" map for components that failed.
    """
    from apowerb.bi.spec.catalog import STOCK_PILOTAGE_VERSION, get_template
    from apowerb.bi.spec.templates import stock_pilotage
    from apowerb.bi.spec.templates_common import check_lang

    if not (dataset_id or "").strip():
        return {
            "success": False,
            "error": (
                "dataset_id is required. If the data is a file attached to the chat, call "
                "tool_bi_import_attachment(filename=<name from [Uploaded files: ...]>) first and pass "
                "the dataset_id it returns; otherwise pick an existing dataset with tool_list_datasets."
            ),
        }
    try:
        lang = check_lang(language)
        spec = stock_pilotage(
            dataset_id.strip(),
            date=date,
            family=family,
            sales=sales,
            site=site.strip() or None,
            safety_factor=safety_factor,
            order_factor=order_factor,
            lead_time=lead_time,
            default_period=default_period,
            horizon=horizon,
            stock=stock.strip() or None,
            safety_column=safety_column.strip() or None,
            lead_time_column=lead_time_column.strip() or None,
            target_cover=target_cover,
            lang=lang,
        )
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    provenance = {
        "id": "stock_pilotage",
        "version": STOCK_PILOTAGE_VERSION,
        "columns": {
            "date": date,
            "family": family,
            "sales": sales,
            "site": site.strip() or None,
            "stock": stock.strip() or None,
            "safety": safety_column.strip() or None,
            "lead_time": lead_time_column.strip() or None,
        },
        "params": {"horizon": horizon},
    }
    default_title = get_template("stock_pilotage", lang=lang).label
    return _create(title or default_title, "", spec, "tool_bi_create_stock_dashboard", provenance, lang)


def tool_bi_list_dashboard_templates(language: str = "fr") -> dict:
    """Lists the ready-made dashboard templates (stock steering, delivery OTIF, production quality...).

    Each template gives its id, what it shows, the columns it expects (one line
    per column key: type, required or optional, default column name, label) and
    its parameters. Use it
    before tool_bi_create_dashboard_from_template to pick the template that
    fits the user's need and map the dataset columns.

    Args:
        language: "fr" or "en", the language of the conversation.

    Returns {"success": True, "templates": [...]}.
    """
    from apowerb.bi.spec.catalog import list_templates

    try:
        templates = list_templates(language)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    return {"success": True, "templates": [_template_brief(tpl) for tpl in templates]}


def _template_brief(tpl) -> dict:
    """What the model needs to pick a template and map columns, one line per column.

    The full ``describe()`` (versions, ownership, null fields, one object per
    column) was ~7k tokens for 15 templates, reread on every following call.
    """
    columns = {}
    for c in tpl.columns:
        line = f"{c.type}, {'required' if c.required else 'optional'}"
        if c.default:
            line += f", default column: {c.default}"
        columns[c.key] = f"{line} ({c.label})"
    params = {p.key: f"{p.type} {p.min}-{p.max}, default {p.default} ({p.label})" for p in tpl.params}
    brief = {"id": tpl.id, "label": tpl.label, "description": tpl.description, "columns": columns}
    if params:
        brief["params"] = params
    if tpl.origin != "builtin":
        brief["origin"] = tpl.origin
    return brief


def tool_bi_create_dashboard_from_template(
    template_id: str,
    dataset_id: str,
    title: str = "",
    columns_json: str = "{}",
    params_json: str = "{}",
    language: str = "fr",
) -> dict:
    """Creates a complete dashboard from a ready-made template on one dataset.

    Use it when the user's need matches a template listed by
    tool_bi_list_dashboard_templates (for example delivery_otif for on-time
    in-full deliveries, production_quality for scrap and defect rates). Call
    bi_datasets.tool_describe_dataset first and map each template column key
    to the dataset column that holds it; a required column left out uses the
    template default name, an optional one is used only when you map it. The
    dashboard remembers its template so the user can later update it or
    replace its file from the UI.

    Args:
        template_id:  Template id from tool_bi_list_dashboard_templates.
        dataset_id:   Dataset id (UUID).
        title:        Dashboard title. Empty: the template label.
        columns_json: JSON object {template column key: dataset column name}.
        params_json:  JSON object {parameter key: value}; empty uses defaults.
        language:     Language of the dashboard texts, "fr" or "en": use the
                      language of the conversation.

    Returns the dashboard id, url (/bi/<id>), computed KPIs and an "errors"
    map for components that failed to compute.
    """
    from apowerb.bi.spec.catalog import get_template, list_templates

    try:
        template = get_template((template_id or "").strip(), lang=language)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    if template is None:
        known = ", ".join(tpl.id for tpl in list_templates())
        return {"success": False, "error": f"Unknown template '{template_id}'. Available: {known}."}
    mapping, error = _loads(columns_json or "{}", "columns_json", dict)
    if error:
        return {"success": False, "error": error}
    raw_params, error = _loads(params_json or "{}", "params_json", dict)
    if error:
        return {"success": False, "error": error}
    given = {k: v for k, v in mapping.items() if v}
    required = {c.key: c.default for c in template.columns if c.required}
    columns, column_errors = template.clean_columns({**required, **given})
    params, param_errors = template.clean_params(raw_params)
    if column_errors or param_errors:
        return {"success": False, "error": " ".join(column_errors + param_errors)}
    try:
        spec = template.build(dataset_id.strip(), columns, params)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    provenance = {"id": template.id, "version": template.version, "columns": columns, "params": params}
    return _create(title or template.label, "", spec, "tool_bi_create_dashboard_from_template", provenance,
                   template.lang)


def tool_bi_patch_dashboard(dashboard_id: str, ops_json: str, dry_run: bool = False) -> dict:
    """Edits a spec v2 dashboard with a list of operations, atomically.

    Use it to add/change/remove a visual, a source, a measure or a filter, or
    to rename the dashboard. Call tool_bi_describe_dashboard first to get the
    component ids, source and measure names. If any operation is invalid
    nothing is changed and the error says why. Use dry_run=true to check an
    edit and see the computed result without saving it.

    ops_json is a JSON array of operations (or {"ops": [...]}). Operations:
      {"op": "add_visual", "visual": {...VisualSpec...}, "position": {"row": 0, "col": 0, "width": 6, "height": 4}}
      {"op": "update_visual", "component_id": "<id>", "changes": {"title": "New title"}}  (shallow merge)
      {"op": "remove_component", "component_id": "<id>"}
      {"op": "move_component", "component_id": "<id>", "position": {...}}
      {"op": "upsert_source", "source": {...SourceSpec...}}   {"op": "remove_source", "name": "..."}
      {"op": "upsert_measure", "source": "<source>", "measure": {"name": "...", "label": "...", "expr": "SUM(x)"}}
      {"op": "remove_measure", "source": "<source>", "name": "..."}
      {"op": "upsert_filter", "filter": {...FilterSpec...}}   {"op": "remove_filter", "id": "..."}
      {"op": "set_title", "title": "...", "description": "..."}
    Example: [{"op": "update_visual", "component_id": "abc", "changes": {"title": "Ventes 2025"}}]

    Args:
        dashboard_id: Dashboard id (UUID) of one of the user's dashboards.
        ops_json:     JSON array of operations.
        dry_run:      Validate and compute without saving.

    Returns a change summary (added/removed component ids), the computed
    KPIs/texts and an "errors" map per failing component.
    """
    owner = _agent_owner()
    if not owner:
        return {"success": False, "error": "No owner context."}
    raw, error = _loads(ops_json, "ops_json", (list, dict))
    if error:
        return {"success": False, "error": error}
    if isinstance(raw, dict):
        raw = raw.get("ops")
    if not isinstance(raw, list) or not raw:
        return {"success": False, "error": "ops_json must be a non-empty JSON array of operations."}
    from apowerb.bi.spec.models import PatchOp

    try:
        ops = TypeAdapter(list[PatchOp]).validate_python(raw)
    except ValidationError as exc:
        return {"success": False, "error": f"Invalid operations: {_fmt_validation(exc)}"}
    try:
        return _run_tool(_async_patch(dashboard_id.strip(), ops, bool(dry_run), owner))
    except Exception as exc:
        return _fail("tool_bi_patch_dashboard", exc)


def tool_bi_query_dashboard(
    dashboard_id: str, filters_json: str = "{}", component_ids: str = "", group_by: str = ""
) -> dict:
    """Reads the computed values of a spec v2 dashboard to answer a question.

    Use it when the user asks about the numbers of a dashboard built with
    tool_bi_create_dashboard / tool_bi_create_stock_dashboard ("what is the
    coverage for family A over 13 weeks?"). Never invent figures: query first.
    Returns KPI values with their traffic-light status, texts, and chart/table
    data as "columns" + "rows" (tables: first 20 rows, charts: first 30,
    "truncated" is set when cut).

    filters_json is a JSON object of filter id -> value, using the ids shown by
    tool_bi_describe_dashboard. Omitted filters keep their default. Example:
    {"famille": "A", "periode": "13w"}. A select filter set to null means "all".

    component_ids limits the answer to some components (comma-separated ids or
    titles). Once you have seen the dashboard, use it to compare a figure across
    several filter values: {"canal": "Email"} with component_ids="ROAS" returns
    one KPI instead of the whole dashboard; group_by="canal" with
    component_ids="ROAS" returns the ROAS of every channel in one call.

    Args:
        dashboard_id: Dashboard id (UUID).
        filters_json: JSON object of filter values, "{}" for the defaults.
        component_ids: Comma-separated component ids or titles, "" for all.
        group_by: Filter id: component_ids for each of its values, "" for none.
    """
    owner = _agent_owner()
    if not owner:
        return {"success": False, "error": "No owner context."}
    filters, error = _loads(filters_json, "filters_json", dict)
    if error:
        return {"success": False, "error": error}
    try:
        return _run_tool(_async_query(dashboard_id.strip(), filters, owner, component_ids or "", group_by or ""))
    except Exception as exc:
        return _fail("tool_bi_query_dashboard", exc)


def tool_bi_describe_dashboard(dashboard_id: str) -> dict:
    """Describes the spec of a dashboard: sources, fields, measures, filters, components.

    Call it before tool_bi_patch_dashboard or tool_bi_query_dashboard: it gives
    the exact component ids, source names, measure names and their formula
    (expr), the field list with provenance (real / forecast / simulated), the
    filter ids and the position of each component. Compact: no data rows.

    Args:
        dashboard_id: Dashboard id (UUID) of one of the user's dashboards.
    """
    owner = _agent_owner()
    if not owner:
        return {"success": False, "error": "No owner context."}
    try:
        return _run_tool(_async_describe(dashboard_id.strip(), owner))
    except Exception as exc:
        return _fail("tool_bi_describe_dashboard", exc)
