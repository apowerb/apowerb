"""
bi/spec/validate.py
-------------------
Static validation of a spec v2 dashboard, without reading any data.

``validate_spec`` returns readable (French) messages: unknown source, unknown
measure, source cycle, filter on a field no source has, forbidden SQL… An empty
list means the spec is consistent. Columns of dataset / SQL sources are only
known at run time, so a field is reported missing only when every source that
could carry it has a closed, fully known schema.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from apowerb.bi.dashboards.core import ComponentType, Dashboard
from apowerb.bi.spec import sql_guard
from apowerb.bi.spec.errors import SpecError
from apowerb.bi.spec.formatting import FORMATS
from apowerb.bi.spec.models import (
    AggregateStep,
    DeriveStep,
    FilterStep,
    ForecastStep,
    InventorySimStep,
    JoinStep,
    NaiveForecastStep,
    SourceSpec,
    SqlStep,
    VisualSpec,
)

TEMPLATE_TOKEN_RE = re.compile(r"\{\{\s*([A-Za-z0-9_.]+)\s*(?:\|\s*([A-Za-z0-9_]+)\s*)?\}\}")
_PRESET_RE = re.compile(r"^(all|\d{1,3}[wm])$")
_PERIOD_KEYS = ("from", "to", "last")
_CHART_TYPES = ("combo", "bar", "hbar", "line", "area", "diverging_bar", "pie")
_INVENTORY_OUTPUTS = (
    "stock_debut", "appro", "ventes_servies", "demande_non_servie",
    "stock_fin", "stock_securite", "commande",
)


@dataclass
class Schema:
    """Columns statically known for a source; ``open`` when others may exist."""

    columns: set[str] = field(default_factory=set)
    open: bool = True

    def has(self, column: str) -> bool:
        return self.open or column in self.columns


def source_dependencies(source: SourceSpec) -> list[str]:
    """Names of the sources ``source`` reads from (parent and joins)."""
    deps: list[str] = []
    if source.kind == "source" and source.parent:
        deps.append(source.parent)
    deps.extend(t.source for t in source.transforms if isinstance(t, JoinStep))
    return deps


def find_cycle_members(sources: list[SourceSpec]) -> set[str]:
    """Names of the sources that belong to (or sit on) a dependency cycle."""
    by_name = {s.name: s for s in sources}
    members: set[str] = set()
    state: dict[str, int] = {}  # 1 = visiting, 2 = done

    def visit(name: str, stack: list[str]) -> None:
        if name not in by_name:
            return
        if state.get(name) == 2:
            return
        if state.get(name) == 1:
            members.update(stack[stack.index(name):])
            return
        state[name] = 1
        for dep in source_dependencies(by_name[name]):
            visit(dep, [*stack, name])
        state[name] = 2

    for s in sources:
        visit(s.name, [])
    return members


def _schema_of(
    source: SourceSpec,
    by_name: dict[str, SourceSpec],
    memo: dict[str, Schema],
    visiting: set[str],
) -> Schema:
    if source.name in memo:
        return memo[source.name]
    if source.name in visiting:  # cycle: reported elsewhere
        return Schema()
    visiting.add(source.name)
    if source.kind == "source" and source.parent in by_name:
        parent = _schema_of(by_name[source.parent], by_name, memo, visiting)
        schema = Schema(set(parent.columns), parent.open)
    else:
        schema = Schema(set(), True)
    schema.columns.update(f.name for f in source.fields)
    for step in source.transforms:
        if isinstance(step, DeriveStep):
            schema.columns.add(step.column)
        elif isinstance(step, AggregateStep):
            schema = Schema({*step.group_by, *step.aggregates}, False)
        elif isinstance(step, SqlStep):
            schema = Schema(set(), True)
        elif isinstance(step, NaiveForecastStep):
            schema.columns.add(step.output)
        elif isinstance(step, ForecastStep):
            schema.columns.add(step.output)
        elif isinstance(step, InventorySimStep):
            schema.columns.update(_INVENTORY_OUTPUTS)
        elif isinstance(step, JoinStep) and step.source in by_name:
            other = _schema_of(by_name[step.source], by_name, memo, visiting)
            if step.columns is not None:
                schema.columns.update(step.columns)
            else:
                schema.columns.update(other.columns)
                schema.open = schema.open or other.open
    visiting.discard(source.name)
    memo[source.name] = schema
    return schema


def template_tokens(template: str | None) -> list[tuple[str, str | None]]:
    """``[(name, format)]`` of the ``{{...}}`` placeholders of a text template."""
    if not template:
        return []
    return [(m.group(1), m.group(2)) for m in TEMPLATE_TOKEN_RE.finditer(template)]


def validate_spec(dashboard: Dashboard) -> list[str]:
    """Readable problems of the dashboard's v2 spec; empty when consistent."""
    errors: list[str] = []

    def add(message: str) -> None:
        if message not in errors:
            errors.append(message)

    sources = dashboard.sources
    by_name: dict[str, SourceSpec] = {}
    for s in sources:
        if s.name in by_name:
            add(f"Source en double : « {s.name} ».")
        by_name[s.name] = s

    # --- sources -----------------------------------------------------------
    for s in sources:
        label = f"Source « {s.name} »"
        if s.kind == "dataset" and not s.dataset_id:
            add(f"{label} : dataset_id manquant.")
        if s.kind == "sql":
            if not s.connection_config_id:
                add(f"{label} : connection_config_id manquant.")
            if not s.query:
                add(f"{label} : requête SQL manquante.")
            else:
                _guard(add, sql_guard.check_query, s.query, f"{label}, requête")
        if s.kind == "source":
            if not s.parent:
                add(f"{label} : `parent` manquant.")
            elif s.parent not in by_name:
                add(f"{label} : source parente inconnue « {s.parent} ».")
        seen_measures: set[str] = set()
        for m in s.measures:
            if m.name in seen_measures:
                add(f"{label} : mesure en double « {m.name} ».")
            seen_measures.add(m.name)
            _guard(add, sql_guard.check_expression, m.expr, f"{label}, mesure « {m.name} »")
        for i, step in enumerate(s.transforms, start=1):
            where = f"{label}, transform {i} ({step.op})"
            if isinstance(step, FilterStep):
                _guard(add, sql_guard.check_expression, step.where, where)
            elif isinstance(step, DeriveStep):
                _guard(add, sql_guard.check_expression, step.expr, where)
            elif isinstance(step, AggregateStep):
                if not step.aggregates and not step.group_by:
                    add(f"{where} : rien à agréger.")
                for key, agg in step.aggregates.items():
                    _guard(add, sql_guard.check_expression, agg, f"{where} ({key})")
            elif isinstance(step, SqlStep):
                _guard(add, sql_guard.check_query, step.query, where)
            elif isinstance(step, JoinStep) and step.source not in by_name:
                add(f"{where} : source jointe inconnue « {step.source} ».")

    cycle = find_cycle_members(sources)
    if cycle:
        add("Cycle de sources : " + " -> ".join(sorted(cycle)) + ".")

    memo: dict[str, Schema] = {}
    schemas = {s.name: _schema_of(s, by_name, memo, set()) for s in sources}

    for s in sources:
        schema = schemas[s.name]
        for i, step in enumerate(s.transforms, start=1):
            if isinstance(step, (NaiveForecastStep, ForecastStep)):
                needed = [step.date, step.value, *step.by]
            elif isinstance(step, InventorySimStep):
                needed = [step.date, *step.by]
            elif isinstance(step, AggregateStep):
                needed = list(step.group_by)
            else:
                continue
            for col in needed:
                if not schema.has(col) and not _has_before(s, i, col, by_name, memo):
                    add(f"Source « {s.name} », transform {i} ({step.op}) : colonne inconnue « {col} ».")

    # --- filters -----------------------------------------------------------
    filter_ids: set[str] = set()
    for f in dashboard.filters:
        if f.id in filter_ids:
            add(f"Filtre en double : « {f.id} ».")
        filter_ids.add(f.id)
        targets = [s for s in sources if f.applies_to is None or s.name in f.applies_to]
        for name in f.applies_to or []:
            if name not in by_name:
                add(f"Filtre « {f.id} » : source inconnue « {name} » dans applies_to.")
        if not any(schemas[s.name].has(f.field) for s in targets):
            add(f"Filtre « {f.id} » : le champ « {f.field} » n'existe dans aucune source.")
        if f.type == "relative_period" and f.default is not None and not _PRESET_RE.match(str(f.default)):
            add(f"Filtre « {f.id} » : période par défaut « {f.default} » inconnue (13w, 26w, 52w, 3m, 12m, all).")

    # --- visuals -----------------------------------------------------------
    seen_components: set[str] = set()
    for comp in dashboard.components:
        if comp.id in seen_components:
            add(f"Composant en double : « {comp.id} ».")
        seen_components.add(comp.id)
        if comp.component_type != ComponentType.VISUAL or comp.visual is None:
            continue
        _validate_visual(comp.visual, comp.id, by_name, schemas, filter_ids, add)

    return errors


def _has_before(
    source: SourceSpec, upto: int, column: str, by_name: dict[str, SourceSpec], memo: dict[str, Schema]
) -> bool:
    """Whether ``column`` exists in the schema reached just before transform ``upto``."""
    partial = source.model_copy(update={"transforms": source.transforms[: upto - 1], "name": f"{source.name}#{upto}"})
    return _schema_of(partial, by_name, dict(memo), set()).has(column)


def _guard(add, check, value: str, what: str) -> None:  # type: ignore[no-untyped-def]
    try:
        check(value, what)
    except SpecError as exc:
        add(str(exc))


def _validate_visual(
    v: VisualSpec,
    component_id: str,
    by_name: dict[str, SourceSpec],
    schemas: dict[str, Schema],
    filter_ids: set[str],
    add,  # type: ignore[no-untyped-def]
) -> None:
    name = f"Visuel « {v.title or component_id} »"
    source = by_name.get(v.source) if v.source else None

    if v.type != "note" and not v.source and not (v.type == "text" and not template_tokens(v.text)):
        add(f"{name} : `source` manquante.")
    elif v.source and source is None:
        add(f"{name} : source inconnue « {v.source} ».")
    schema = schemas.get(v.source or "")

    measures = {m.name: m for m in source.measures} if source else {}

    def field_ok(col: str) -> bool:
        return schema is None or schema.has(col)

    def need_measure(key: str, where: str) -> None:
        if source is not None and key not in measures:
            add(f"{name} : mesure inconnue « {key} » ({where}) dans la source « {source.name} ».")

    def need_key(key: str, where: str) -> None:
        if source is not None and key not in measures and not field_ok(key):
            add(f"{name} : « {key} » n'est ni une mesure ni un champ de la source « {source.name} » ({where}).")

    if v.type == "kpi":
        if v.kpi is None:
            add(f"{name} : bloc `kpi` manquant.")
        else:
            need_measure(v.kpi.measure, "kpi.measure")
            if v.kpi.sparkline:
                need_measure(v.kpi.sparkline, "kpi.sparkline")
    elif v.type in _CHART_TYPES:
        if not v.x:
            add(f"{name} : `x` manquant.")
        elif source is not None and not field_ok(v.x):
            add(f"{name} : champ x inconnu « {v.x} » dans la source « {source.name} ».")
        if not v.series:
            add(f"{name} : au moins une série est requise.")
        for s in v.series:
            need_key(s.key, "série")
    elif v.type == "waterfall":
        if not v.steps:
            add(f"{name} : au moins une étape est requise (`steps`).")
        for step in v.steps:
            need_measure(step.measure, f"étape « {step.label} »")
    elif v.type == "table":
        if v.x and source is not None and not field_ok(v.x):
            add(f"{name} : champ x inconnu « {v.x} » dans la source « {source.name} ».")
        for col in v.columns:
            need_key(col.key, "colonne")
            if col.total_measure:
                need_measure(col.total_measure, "colonne (total_measure)")
    elif v.type in ("text", "note") and not v.text:
        add(f"{name} : `text` manquant.")

    for line in v.reference_lines:
        if line.value is None and line.measure is None:
            add(f"{name} : une ligne de référence a besoin de `value` ou `measure`.")
        if line.measure:
            need_measure(line.measure, "ligne de référence")

    for template, where in ((v.text if v.type == "text" else None, "text"), (v.subtitle, "subtitle"),
                            (v.kpi.secondary if v.kpi else None, "kpi.secondary")):
        for token, fmt in template_tokens(template):
            if token.startswith("filters."):
                if token.split(".", 1)[1] not in filter_ids:
                    add(f"{name} : filtre inconnu « {token} » dans {where}.")
            elif token.startswith("period."):
                if token.split(".", 1)[1] not in _PERIOD_KEYS:
                    add(f"{name} : « {token} » inconnu dans {where} (period.from, period.to, period.last).")
            else:
                need_measure(token, where)
            if fmt and fmt not in FORMATS:
                add(f"{name} : format inconnu « {fmt} » dans {where}.")

    for fid in v.ignore_filters:
        if fid not in filter_ids:
            add(f"{name} : ignore_filters référence un filtre inconnu « {fid} ».")
    for fid in v.click_filters:
        if fid not in filter_ids:
            add(f"{name} : click_filters référence un filtre inconnu « {fid} ».")
    if v.highlight_filter and v.highlight_filter not in filter_ids:
        add(f"{name} : highlight_filter référence un filtre inconnu « {v.highlight_filter} ».")
