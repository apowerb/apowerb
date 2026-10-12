"""
bi/spec/engine.py
-----------------
Spec v2 engine: ``compute_dashboard`` turns a :class:`Dashboard` and a filter
state into a :class:`DashboardDataResponse`.

1. Every source is materialised once (dataset / SQL rows, or its parent, then
   its ordered transforms) and cached a few minutes.
2. The global filters are resolved against the materialised data.
3. Each ``visual`` component is computed on the filtered frame of its source;
   a failing component reports ``error`` without affecting the others.

``owner`` is the dashboard's owner: it scopes dataset and connection access,
never the viewer.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any

from apowerb.bi.dashboards.core import ComponentType, Dashboard
from apowerb.bi.spec import sources as source_loader
from apowerb.bi.spec import transforms as transform_engine
from apowerb.bi.spec.cache import TTLCache
from apowerb.bi.spec.duck import SqlRunner
from apowerb.bi.spec.errors import SpecError
from apowerb.bi.spec.filters import filter_options, resolve_filters
from apowerb.bi.spec.frames import MatSource, infer_fields
from apowerb.bi.spec.models import (
    ComponentResult,
    DashboardDataRequest,
    DashboardDataResponse,
    SourceMeta,
    SourceSpec,
)
from apowerb.bi.spec.transforms import TransformState, apply_transforms
from apowerb.bi.spec.validate import find_cycle_members, source_dependencies
from apowerb.bi.spec.visuals import ComputeContext, build_component

logger = logging.getLogger(__name__)

COMPUTE_BUDGET_S = 20.0
SOURCE_TTL_S = 300.0
RESULT_TTL_S = 60.0

_source_cache = TTLCache(maxsize=64, ttl=SOURCE_TTL_S)
_result_cache = TTLCache(maxsize=128, ttl=RESULT_TTL_S)


def clear_caches() -> None:
    """Drop every cached source, dataset, forecast and response."""
    _source_cache.clear()
    _result_cache.clear()
    source_loader.clear_cache()
    transform_engine.clear_cache()


def _digest(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def source_signature(name: str, by_name: dict[str, SourceSpec], _seen: frozenset[str] = frozenset()) -> str:
    """Hash of a source definition and of every source it depends on."""
    spec = by_name[name]
    deps = [
        source_signature(d, by_name, _seen | {name}) if d in by_name and d not in _seen else d
        for d in source_dependencies(spec)
    ]
    return _digest({"spec": spec.model_dump(mode="json"), "deps": deps})


class _Materializer:
    """Materialises the sources of one dashboard (each at most once per call)."""

    def __init__(self, dashboard: Dashboard, owner: str) -> None:
        self.dashboard = dashboard
        self.owner = owner
        self.by_name = {s.name: s for s in dashboard.sources}
        self.cycle = find_cycle_members(dashboard.sources)
        self._tasks: dict[str, asyncio.Task[MatSource]] = {}

    async def get_source(self, name: str) -> MatSource:
        if name not in self.by_name:
            raise SpecError(f"Source inconnue « {name} ».")
        if name in self.cycle:
            raise SpecError(f"Cycle de sources : « {name} » dépend de lui-même.")
        if name not in self._tasks:
            self._tasks[name] = asyncio.ensure_future(self._build(name))
        return await self._tasks[name]

    async def _build(self, name: str) -> MatSource:
        spec = self.by_name[name]
        key = (self.owner.lower(), self.dashboard.id, source_signature(name, self.by_name))
        cached = _source_cache.get(key)
        if cached is not None:
            return cached
        if spec.kind == "source":
            if not spec.parent:
                raise SpecError(f"Source « {name} » : `parent` manquant.")
            parent = await self.get_source(spec.parent)
            state = TransformState(
                df=parent.df,
                prov=dict(parent.prov),
                warnings=list(parent.warnings),
                failed_columns=dict(parent.failed_columns),
            )
        else:
            df = await source_loader.load_source(spec, self.owner)
            state = TransformState(df=df, prov={}, warnings=[], failed_columns={})
        state = await apply_transforms(state, spec.transforms, self)
        mat = MatSource(
            name=name,
            df=state.df.reset_index(drop=True),
            prov=state.prov,
            fields=infer_fields(state.df, state.prov, spec.fields),
            warnings=state.warnings,
            failed_columns=state.failed_columns,
        )
        _source_cache.set(key, mat)
        return mat

    async def materialize_all(self) -> tuple[dict[str, MatSource], dict[str, str]]:
        names = list(self.by_name)
        results = await asyncio.gather(*(self.get_source(n) for n in names), return_exceptions=True)
        mats: dict[str, MatSource] = {}
        errors: dict[str, str] = {}
        for name, res in zip(names, results):
            if isinstance(res, MatSource):
                mats[name] = res
            elif isinstance(res, SpecError):
                errors[name] = str(res)
            elif isinstance(res, asyncio.CancelledError):
                raise res
            else:
                logger.error("[BiSpec] source %s failed: %r", name, res)
                errors[name] = "Erreur inattendue lors du chargement de la source."
        return mats, errors


def _source_meta(mat: MatSource) -> SourceMeta:
    date_col = mat.date_field()
    last = mat.real_last_date(date_col) if date_col else None
    return SourceMeta(
        fields=mat.fields,
        row_count=int(len(mat.df)),
        last_date=last.strftime("%Y-%m-%d") if last is not None else None,
        warnings=list(mat.warnings),
    )


async def compute_dashboard(
    dashboard: Dashboard, request: DashboardDataRequest, *, owner: str
) -> DashboardDataResponse:
    """Compute the data of every (or the requested) visual of ``dashboard``."""
    visuals = [
        c for c in dashboard.components if c.component_type == ComponentType.VISUAL and c.visual is not None
    ]
    if request.components is not None:
        wanted = set(request.components)
        visuals = [c for c in visuals if c.id in wanted]

    materializer = _Materializer(dashboard, owner)
    mats, errors = await materializer.materialize_all()
    active = resolve_filters(dashboard.filters, request.filters, mats)

    cache_key = (
        owner.lower(),
        dashboard.id,
        _digest(
            {
                "sources": {n: m.token for n, m in mats.items()},
                "errors": errors,
                "filters": [f.model_dump(mode="json") for f in dashboard.filters],
                "values": {fid: a.public.model_dump(mode="json") for fid, a in active.items()},
                "visuals": [(c.id, c.visual.model_dump(mode="json")) for c in visuals if c.visual],
                "measures": {s.name: [m.model_dump(mode="json") for m in s.measures] for s in dashboard.sources},
            }
        ),
    )
    cached = _result_cache.get(cache_key)
    if cached is not None:
        return cached.model_copy(deep=True, update={"generated_at": datetime.now(timezone.utc)})

    sources_by_name = materializer.by_name

    def run() -> dict[str, ComponentResult]:
        with SqlRunner(budget_s=COMPUTE_BUDGET_S) as runner:
            ctx = ComputeContext(
                sources=sources_by_name, mats=mats, source_errors=errors, active=active, runner=runner
            )
            return {c.id: build_component(ctx, c.id, c.visual) for c in visuals if c.visual}

    components = await asyncio.to_thread(run) if visuals else {}

    source_metas = {name: _source_meta(mat) for name, mat in mats.items()}
    for name, message in errors.items():
        source_metas[name] = SourceMeta(
            fields=[], row_count=0, warnings=[f"Source indisponible : {message}"]
        )
    response = DashboardDataResponse(
        dashboard_id=dashboard.id,
        filters={fid: a.public for fid, a in active.items()},
        filter_options=filter_options(dashboard.filters, mats),
        sources=source_metas,
        components=components,
    )
    _result_cache.set(cache_key, response.model_copy(deep=True))
    return response
