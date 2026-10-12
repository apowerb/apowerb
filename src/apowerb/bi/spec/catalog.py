"""
bi/spec/catalog.py
------------------
Registry of the dashboard templates a user can instantiate on one of their datasets.

A template declares the dataset columns it needs and its tunable parameters, and
builds the spec (sources, filters, components) from them. The REST routes
``GET /dashboard-templates`` and ``POST /dashboard-templates/{id}/dashboards`` are
driven entirely by this registry.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any

from apowerb.bi.spec.templates import stock_pilotage


@dataclass(frozen=True)
class TemplateColumn:
    key: str
    label: str
    type: str
    required: bool
    default: str | None


@dataclass(frozen=True)
class TemplateParam:
    key: str
    label: str
    type: str
    default: int
    min: int
    max: int


@dataclass(frozen=True)
class DashboardTemplate:
    id: str
    label: str
    description: str
    columns: tuple[TemplateColumn, ...]
    params: tuple[TemplateParam, ...]
    build: Callable[[str, dict[str, str | None], dict[str, Any]], dict]

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "columns": [asdict(c) for c in self.columns],
            "params": [asdict(p) for p in self.params],
        }

    def clean_columns(self, columns: dict[str, str | None]) -> tuple[dict[str, str | None], list[str]]:
        """Column mapping with blanks as None, and the problems found (in French)."""
        errors: list[str] = []
        known = {c.key for c in self.columns}
        for key in columns:
            if key not in known:
                errors.append(f"Colonne inconnue pour ce gabarit : {key}.")
        clean: dict[str, str | None] = {}
        for col in self.columns:
            value = (columns.get(col.key) or "").strip() or None
            if value is None and col.required:
                errors.append(f"La colonne « {col.label} » ({col.key}) est obligatoire.")
            clean[col.key] = value
        return clean, errors

    def clean_params(self, params: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Parameters with defaults filled in, and the problems found (in French)."""
        errors: list[str] = []
        known = {p.key for p in self.params}
        for key in params:
            if key not in known:
                errors.append(f"Paramètre inconnu pour ce gabarit : {key}.")
        clean: dict[str, Any] = {}
        for param in self.params:
            value = params.get(param.key, param.default)
            if value is None:
                value = param.default
            if isinstance(value, bool) or not isinstance(value, int):
                errors.append(f"Le paramètre « {param.label} » doit être un nombre entier.")
            elif not param.min <= value <= param.max:
                errors.append(f"Le paramètre « {param.label} » doit être compris entre {param.min} et {param.max}.")
            clean[param.key] = value
        return clean, errors


def _build_stock_pilotage(dataset_id: str, columns: dict[str, str | None], params: dict[str, Any]) -> dict:
    return stock_pilotage(
        dataset_id,
        date=columns["date"] or "",
        family=columns["family"] or "",
        sales=columns["sales"] or "",
        site=columns.get("site"),
        horizon=params["horizon"],
    )


_STOCK_PILOTAGE = DashboardTemplate(
    id="stock_pilotage",
    label="Pilotage stocks",
    description=(
        "Tableau de bord de pilotage des stocks à partir de ventes hebdomadaires par famille : "
        "couverture, taux de service, fiabilité de la prévision et semaines sous le stock de sécurité. "
        "Le stock et les approvisionnements sont simulés à partir des ventes."
    ),
    columns=(
        TemplateColumn("date", "Semaine", "date", True, "semaine"),
        TemplateColumn("family", "Famille", "string", True, "famille"),
        TemplateColumn("sales", "Ventes", "number", True, "ventes"),
        TemplateColumn("site", "Site", "string", False, None),
    ),
    params=(TemplateParam("horizon", "Horizon de prévision (semaines)", "integer", 8, 1, 26),),
    build=_build_stock_pilotage,
)

_TEMPLATES: dict[str, DashboardTemplate] = {_STOCK_PILOTAGE.id: _STOCK_PILOTAGE}


def list_templates() -> list[DashboardTemplate]:
    return list(_TEMPLATES.values())


def get_template(template_id: str) -> DashboardTemplate | None:
    return _TEMPLATES.get(template_id)
