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
from dataclasses import asdict, dataclass, replace
from functools import partial
from typing import Any

from apowerb.bi.spec import (
    templates_cash,
    templates_construction,
    templates_ecommerce,
    templates_finance,
    templates_forecast,
    templates_hr,
    templates_otif,
    templates_purchasing,
    templates_quality,
    templates_receivables,
    templates_restaurant,
    templates_sales,
    templates_services,
    templates_support,
)
from apowerb.bi.spec.templates import stock_pilotage
from apowerb.bi.spec.templates_common import DEFAULT_LANG, check_lang, pick

# A catalog text: a plain string, or one string per language ({"fr": ..., "en": ...}).
Text = str | dict[str, str]


@dataclass(frozen=True)
class TemplateColumn:
    key: str
    label: Text
    type: str
    required: bool
    default: str | None


@dataclass(frozen=True)
class TemplateParam:
    key: str
    label: Text
    type: str
    default: int
    min: int
    max: int


@dataclass(frozen=True)
class DashboardTemplate:
    id: str
    label: Text
    description: Text
    columns: tuple[TemplateColumn, ...]
    params: tuple[TemplateParam, ...]
    build: Callable[..., dict]
    version: int = 1
    origin: str = "builtin"
    owner: str | None = None
    visibility: str | None = None
    can_delete: bool = False
    # True when ``build`` takes a ``lang`` keyword; ``localized`` then binds it.
    translatable: bool = False
    lang: str = DEFAULT_LANG

    def localized(self, lang: str | None) -> DashboardTemplate:
        """This template with its texts, messages and build in ``lang``."""
        code = check_lang(lang)
        return replace(
            self,
            label=pick(self.label, code),
            description=pick(self.description, code),
            columns=tuple(replace(c, label=pick(c.label, code)) for c in self.columns),
            params=tuple(replace(p, label=pick(p.label, code)) for p in self.params),
            build=partial(self.build, lang=code) if self.translatable else self.build,
            translatable=False,
            lang=code,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "version": self.version,
            "origin": self.origin,
            "owner": self.owner,
            "visibility": self.visibility,
            "can_delete": self.can_delete,
            "columns": [asdict(c) for c in self.columns],
            "params": [asdict(p) for p in self.params],
        }

    def clean_columns(self, columns: dict[str, str | None]) -> tuple[dict[str, str | None], list[str]]:
        """Column mapping with blanks as None, and the problems found (in the template language)."""
        en = self.lang == "en"
        errors: list[str] = []
        known = {c.key for c in self.columns}
        for key in columns:
            if key not in known:
                errors.append(f"Unknown column for this template: {key}." if en
                              else f"Colonne inconnue pour ce gabarit : {key}.")
        clean: dict[str, str | None] = {}
        for col in self.columns:
            value = (columns.get(col.key) or "").strip() or None
            if value is None and col.required:
                errors.append(f"The column “{col.label}” ({col.key}) is required." if en
                              else f"La colonne « {col.label} » ({col.key}) est obligatoire.")
            clean[col.key] = value
        return clean, errors

    def clean_params(self, params: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Parameters with defaults filled in, and the problems found (in the template language)."""
        en = self.lang == "en"
        errors: list[str] = []
        known = {p.key for p in self.params}
        for key in params:
            if key not in known:
                errors.append(f"Unknown parameter for this template: {key}." if en
                              else f"Paramètre inconnu pour ce gabarit : {key}.")
        clean: dict[str, Any] = {}
        for param in self.params:
            value = params.get(param.key, param.default)
            if value is None:
                value = param.default
            if isinstance(value, bool) or not isinstance(value, int):
                errors.append(f"The parameter “{param.label}” must be a whole number." if en
                              else f"Le paramètre « {param.label} » doit être un nombre entier.")
            elif not param.min <= value <= param.max:
                errors.append(f"The parameter “{param.label}” must be between {param.min} and {param.max}." if en
                              else f"Le paramètre « {param.label} » doit être compris entre {param.min} et {param.max}.")
            clean[param.key] = value
        return clean, errors


# Incremented whenever the builder of the template changes.
STOCK_PILOTAGE_VERSION = 1


def _build_stock_pilotage(
    dataset_id: str, columns: dict[str, str | None], params: dict[str, Any], lang: str = DEFAULT_LANG
) -> dict:
    return stock_pilotage(
        dataset_id,
        date=columns["date"] or "",
        family=columns["family"] or "",
        sales=columns["sales"] or "",
        site=columns.get("site"),
        horizon=params["horizon"],
        stock=columns.get("stock"),
        safety_column=columns.get("safety"),
        lead_time_column=columns.get("lead_time"),
        lang=lang,
    )


_STOCK_PILOTAGE = DashboardTemplate(
    id="stock_pilotage",
    label={"fr": "Pilotage stocks", "en": "Inventory steering"},
    description={
        "fr": (
            "Tableau de bord de pilotage des stocks à partir de ventes hebdomadaires par famille : "
            "couverture, taux de service, fiabilité de la prévision et semaines sous le stock de sécurité. "
            "Avec une colonne Stock, le stock réel est utilisé ; sinon stock et approvisionnements sont simulés à partir des ventes."
        ),
        "en": (
            "Inventory steering dashboard built from weekly sales by product family: "
            "coverage, service level, forecast accuracy and weeks below safety stock. "
            "With a Stock column the real stock is used; otherwise stock and replenishments are simulated from sales."
        ),
    },
    columns=(
        TemplateColumn("date", {"fr": "Semaine", "en": "Week"}, "date", True, "semaine"),
        TemplateColumn("family", {"fr": "Famille", "en": "Product family"}, "string", True, "famille"),
        TemplateColumn("sales", {"fr": "Ventes", "en": "Sales"}, "number", True, "ventes"),
        TemplateColumn("site", "Site", "string", False, None),
        TemplateColumn("stock", "Stock", "number", False, "stock"),
        TemplateColumn("safety", {"fr": "Seuil de sécurité", "en": "Safety stock"}, "number", False, "seuil_securite"),
        TemplateColumn(
            "lead_time", {"fr": "Délai fournisseur (semaines)", "en": "Supplier lead time (weeks)"}, "number", False,
            "delai_fournisseur_sem",
        ),
    ),
    params=(
        TemplateParam(
            "horizon", {"fr": "Horizon de prévision (semaines)", "en": "Forecast horizon (weeks)"}, "integer", 8, 1, 26
        ),
    ),
    build=_build_stock_pilotage,
    version=STOCK_PILOTAGE_VERSION,
    translatable=True,
)


def _from_module(module: Any) -> DashboardTemplate:
    """A template module exposes a ``DESCRIPTOR`` dict and a ``build(..., lang=)`` function.

    The descriptor texts (label, description, column and parameter labels) are a
    string or ``{"fr": ..., "en": ...}``.
    """
    d = module.DESCRIPTOR
    return DashboardTemplate(
        id=d["id"],
        label=d["label"],
        description=d["description"],
        columns=tuple(
            TemplateColumn(c["key"], c["label"], c["type"], c["required"], c.get("default")) for c in d["columns"]
        ),
        params=tuple(
            TemplateParam(p["key"], p["label"], p["type"], p.get("default"), p.get("min"), p.get("max"))
            for p in d.get("params", [])
        ),
        build=module.build,
        version=d.get("version", 1),
        translatable=True,
    )


_TEMPLATES: dict[str, DashboardTemplate] = {
    t.id: t
    for t in (
        _STOCK_PILOTAGE,
        _from_module(templates_sales),
        _from_module(templates_forecast),
        _from_module(templates_otif),
        _from_module(templates_quality),
        _from_module(templates_receivables),
        _from_module(templates_purchasing),
        _from_module(templates_support),
        _from_module(templates_finance),
        _from_module(templates_cash),
        _from_module(templates_hr),
        _from_module(templates_services),
        _from_module(templates_ecommerce),
        _from_module(templates_restaurant),
        _from_module(templates_construction),
    )
}


def list_templates(lang: str | None = DEFAULT_LANG) -> list[DashboardTemplate]:
    return [t.localized(lang) for t in _TEMPLATES.values()]


def get_template(template_id: str, lang: str | None = DEFAULT_LANG) -> DashboardTemplate | None:
    template = _TEMPLATES.get(template_id)
    return template.localized(lang) if template is not None else None
