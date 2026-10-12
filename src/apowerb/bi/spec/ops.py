"""
bi/spec/ops.py
--------------
Pure application of :data:`PatchOp` operations to a dashboard.

``apply_ops`` never mutates its input: it returns a new, validated
:class:`Dashboard` or raises :class:`SpecError` with readable messages. The
whole list is atomic (one failing operation leaves nothing applied).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from pydantic import ValidationError

from apowerb.bi.dashboards.core import ComponentType, Dashboard, DashboardComponent, GridPosition
from apowerb.bi.spec.errors import SpecError
from apowerb.bi.spec.models import (
    AddVisualOp,
    FilterSpec,
    MoveComponentOp,
    PatchOp,
    RemoveComponentOp,
    RemoveFilterOp,
    RemoveMeasureOp,
    RemoveSourceOp,
    SetTitleOp,
    SourceSpec,
    UpdateVisualOp,
    UpsertFilterOp,
    UpsertMeasureOp,
    UpsertSourceOp,
    VisualSpec,
)
from apowerb.bi.spec.validate import validate_spec

# (width, height) of a visual placed without an explicit position.
_DEFAULT_SIZE: dict[str, tuple[int, int]] = {
    "kpi": (3, 2),
    "text": (6, 2),
    "note": (12, 2),
    "table": (12, 5),
}
_POSITION_KEYS = {"row", "col", "width", "height"}


def _readable(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        parts.append(f"{loc} : {err['msg']}" if loc else err["msg"])
    return "; ".join(parts)


def _position(raw: dict[str, int] | None, base: GridPosition | None, visual_type: str, dashboard: Dashboard) -> GridPosition:
    if raw is not None:
        unknown = set(raw) - _POSITION_KEYS
        if unknown:
            raise SpecError(f"Position : clé(s) inconnue(s) {', '.join(sorted(unknown))} (row, col, width, height).")
    width, height = _DEFAULT_SIZE.get(visual_type, (6, 4))
    start = base.model_dump() if base is not None else {"row": dashboard.next_free_row(), "col": 0, "width": width, "height": height}
    try:
        return GridPosition(**{**start, **(raw or {})})
    except ValidationError as exc:
        raise SpecError(f"Position invalide : {_readable(exc)}") from None


def _find(components: list[DashboardComponent], component_id: str) -> int:
    for i, c in enumerate(components):
        if c.id == component_id:
            return i
    raise SpecError(f"Composant introuvable : « {component_id} ».")


def _upsert(items: list[Any], new: Any, key: str) -> list[Any]:
    out = list(items)
    for i, item in enumerate(out):
        if getattr(item, key) == getattr(new, key):
            out[i] = new
            return out
    out.append(new)
    return out


def apply_ops(dashboard: Dashboard, ops: list[PatchOp]) -> Dashboard:
    """Return ``dashboard`` with ``ops`` applied (and validated)."""
    current = dashboard
    for index, op in enumerate(ops, start=1):
        try:
            current = _apply(current, op)
        except SpecError as exc:
            raise SpecError(f"Opération {index} ({op.op}) : {exc}", errors=[f"Opération {index} ({op.op}) : {e}" for e in exc.errors]) from None
        except ValidationError as exc:
            msg = f"Opération {index} ({op.op}) : {_readable(exc)}"
            raise SpecError(msg) from None

    has_v2 = bool(current.sources or current.filters) or any(
        c.component_type == ComponentType.VISUAL for c in current.components
    )
    current = current.model_copy(
        update={
            "spec_version": max(current.spec_version, 2) if has_v2 else current.spec_version,
            "version": dashboard.version + 1,
            "updated_at": datetime.now(timezone.utc),
        }
    )
    try:
        current = Dashboard.model_validate(current.model_dump())
    except ValidationError as exc:
        raise SpecError(_readable(exc)) from None
    errors = validate_spec(current)
    if errors:
        raise SpecError("; ".join(errors), errors=errors)
    return current


def _apply(d: Dashboard, op: PatchOp) -> Dashboard:
    if isinstance(op, AddVisualOp):
        comp_id = op.component_id or str(uuid.uuid4())
        if any(c.id == comp_id for c in d.components):
            raise SpecError(f"Le composant « {comp_id} » existe déjà.")
        component = DashboardComponent(
            id=comp_id,
            component_type=ComponentType.VISUAL,
            position=_position(op.position, None, op.visual.type, d),
            visual=op.visual,
        )
        return d.model_copy(update={"components": [*d.components, component]})

    if isinstance(op, UpdateVisualOp):
        i = _find(d.components, op.component_id)
        comp = d.components[i]
        if comp.visual is None:
            raise SpecError(f"« {op.component_id} » n'est pas un visuel v2 (type {comp.component_type.value}).")
        unknown = set(op.changes) - set(VisualSpec.model_fields)
        if unknown:
            raise SpecError(f"Champ(s) inconnu(s) : {', '.join(sorted(unknown))}.")
        merged = VisualSpec.model_validate({**comp.visual.model_dump(), **op.changes})
        comps = list(d.components)
        comps[i] = comp.model_copy(update={"visual": merged})
        return d.model_copy(update={"components": comps})

    if isinstance(op, RemoveComponentOp):
        i = _find(d.components, op.component_id)
        return d.model_copy(update={"components": [c for j, c in enumerate(d.components) if j != i]})

    if isinstance(op, MoveComponentOp):
        i = _find(d.components, op.component_id)
        comp = d.components[i]
        position = _position(op.position, comp.position, comp.visual.type if comp.visual else "", d)
        comps = list(d.components)
        comps[i] = comp.model_copy(update={"position": position})
        return d.model_copy(update={"components": comps})

    if isinstance(op, UpsertSourceOp):
        return d.model_copy(update={"sources": _upsert(d.sources, op.source, "name")})

    if isinstance(op, RemoveSourceOp):
        if not any(s.name == op.name for s in d.sources):
            raise SpecError(f"Source introuvable : « {op.name} ».")
        return d.model_copy(update={"sources": [s for s in d.sources if s.name != op.name]})

    if isinstance(op, UpsertMeasureOp):
        src = _source(d, op.source)
        new_src = src.model_copy(update={"measures": _upsert(src.measures, op.measure, "name")})
        return d.model_copy(update={"sources": _upsert(d.sources, new_src, "name")})

    if isinstance(op, RemoveMeasureOp):
        src = _source(d, op.source)
        if not any(m.name == op.name for m in src.measures):
            raise SpecError(f"Mesure introuvable : « {op.name} » dans la source « {op.source} ».")
        new_src = src.model_copy(update={"measures": [m for m in src.measures if m.name != op.name]})
        return d.model_copy(update={"sources": _upsert(d.sources, new_src, "name")})

    if isinstance(op, UpsertFilterOp):
        flt: FilterSpec = op.filter
        return d.model_copy(update={"filters": _upsert(d.filters, flt, "id")})

    if isinstance(op, RemoveFilterOp):
        if not any(f.id == op.id for f in d.filters):
            raise SpecError(f"Filtre introuvable : « {op.id} ».")
        return d.model_copy(update={"filters": [f for f in d.filters if f.id != op.id]})

    if isinstance(op, SetTitleOp):
        update: dict[str, Any] = {"title": op.title}
        if op.description is not None:
            update["description"] = op.description
        return d.model_copy(update=update)

    raise SpecError(f"Opération inconnue : {getattr(op, 'op', op)!r}.")  # pragma: no cover


def _source(d: Dashboard, name: str) -> SourceSpec:
    for s in d.sources:
        if s.name == name:
            return s
    raise SpecError(f"Source introuvable : « {name} ».")
