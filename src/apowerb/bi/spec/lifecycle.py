"""
bi/spec/lifecycle.py
--------------------
Pure helpers shared by the dashboard template lifecycle (user templates, file
replacement, upgrade): the single root dataset source of a dashboard, the dataset
columns the spec really needs, and the column mapping.

A mapping never rewrites the spec: it prepends to the root source's transforms one
``DeriveStep(column=<name used by the spec>, expr="<column of the user's file>")``
per renamed column. Those leading pure renames are not business logic and are
stripped before columns are listed or a model is frozen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from apowerb.bi.dashboards.core import Dashboard
from apowerb.bi.spec.duck import quote_ident
from apowerb.bi.spec.errors import SpecError
from apowerb.bi.spec.frames import identifiers
from apowerb.bi.spec.models import (
    AggregateStep,
    DeriveStep,
    ForecastStep,
    InventorySimStep,
    NaiveForecastStep,
    Provenance,
    ReplenishmentPlanStep,
    SourceSpec,
    TransformStep,
)
from apowerb.bi.spec.validate import _INVENTORY_OUTPUTS, _PLAN_OUTPUTS

_PURE_RENAME_RE = re.compile(r'^"((?:[^"]|"")+)"$')


@dataclass(frozen=True)
class SourceColumn:
    key: str
    label: str
    type: str
    required: bool
    current: str | None


def root_dataset_source(sources: list[SourceSpec]) -> SourceSpec:
    """The only ``dataset`` source (child sources, sql and connector sources do not count)."""
    datasets = [s for s in sources if s.kind == "dataset"]
    if len(datasets) != 1:
        raise SpecError(
            "Le tableau doit avoir exactement une source de type fichier (jeu de données) ; "
            f"il en a {len(datasets)}."
        )
    return datasets[0]


def pure_renames(transforms: list[TransformStep]) -> tuple[dict[str, str], list[TransformStep]]:
    """Leading derives that only copy one column (``name := "column"``), and the other steps."""
    renames: dict[str, str] = {}
    index = 0
    for step in transforms:
        match = _PURE_RENAME_RE.match(step.expr.strip()) if isinstance(step, DeriveStep) else None
        if match is None:
            break
        renames[step.column] = match.group(1).replace('""', '"')
        index += 1
    return renames, list(transforms[index:])


def apply_mapping(transforms: list[TransformStep], mapping: dict[str, str | None]) -> list[TransformStep]:
    """``transforms`` preceded by one rename per column whose file column differs from the spec name."""
    renamed = {name: column for name, column in mapping.items() if column and column != name}
    if set(renamed) & set(renamed.values()):
        raise SpecError("Un échange de colonnes (A vers B et B vers A) n'est pas pris en charge.")
    derives: list[TransformStep] = [DeriveStep(column=name, expr=quote_ident(column)) for name, column in renamed.items()]
    return [*derives, *transforms]


def _produced(transforms: list[TransformStep]) -> set[str]:
    names: set[str] = set()
    for step in transforms:
        if isinstance(step, DeriveStep):
            names.add(step.column)
        elif isinstance(step, (NaiveForecastStep, ForecastStep)):
            names.add(step.output)
        elif isinstance(step, InventorySimStep):
            names.update(_INVENTORY_OUTPUTS)
        elif isinstance(step, ReplenishmentPlanStep):
            names.update(_PLAN_OUTPUTS)
            names.add(step.forecast)
    return names


def _strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, (list, tuple)):
        return [s for v in value for s in _strings(v)]
    return []


def source_columns(dashboard: Dashboard) -> list[SourceColumn]:
    """Real columns of the root dataset source, flagged ``required`` when the spec references them."""
    root = root_dataset_source(dashboard.sources)
    renames, rest = pure_renames(root.transforms)
    produced = _produced(rest)

    candidates: dict[str, tuple[str, str]] = {}  # name -> (label, type)
    for f in root.fields:
        if f.provenance == Provenance.REAL and f.name not in produced:
            candidates[f.name] = (f.label or f.name, "string" if f.type == "boolean" else f.type)
    for step in rest:
        if isinstance(step, AggregateStep):
            for name in step.group_by:
                if name not in produced:
                    candidates.setdefault(name, (name, "string"))
            for name in step.aggregates:
                if name not in produced:
                    candidates.setdefault(name, (name, "number"))
    for source in dashboard.sources:
        for step in source.transforms:
            if isinstance(step, ReplenishmentPlanStep):
                for name in (step.stock, step.safety_column, step.lead_time_column):
                    if name and name not in produced:
                        candidates.setdefault(name, (name, "number"))

    others = [s.model_dump(mode="json") for s in dashboard.sources if s.name != root.name]
    spec_part = [
        root.model_copy(update={"fields": [], "transforms": rest}).model_dump(mode="json"),
        others,
        [f.model_dump(mode="json") for f in dashboard.filters],
        [c.visual.model_dump(mode="json") for c in dashboard.components if c.visual is not None],
    ]
    used: set[str] = set()
    for text in _strings(spec_part):
        used |= identifiers(text)

    return [
        SourceColumn(name, label, kind, name.lower() in used, renames.get(name, name))
        for name, (label, kind) in candidates.items()
    ]
