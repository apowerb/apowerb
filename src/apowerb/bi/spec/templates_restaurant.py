"""
bi/spec/templates_restaurant.py
-------------------------------
Spec v2 template « Restauration »: one row per day (or per service).

Every ratio is a ratio of sums, so a week, a service and the whole period share
the same definition: food cost % = food purchases ÷ revenue, labour cost % =
payroll ÷ revenue, prime cost = (food purchases + payroll) ÷ revenue, average
ticket = revenue ÷ covers. The target bands are drawn as two reference lines;
the traffic light turns orange up to 5 points above the upper bound of the
band, red beyond (a ratio under the lower bound is not penalised).
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from apowerb.bi.spec.models import (
    IDENT,
    DeriveStep,
    FieldSpec,
    FilterSpec,
    KpiSpec,
    MeasureSpec,
    Provenance,
    ReferenceLine,
    SeriesSpec,
    SourceSpec,
    ThresholdSpec,
    VisualSpec,
)

_IDENT_RE = re.compile(IDENT)

BROWN = "#9a6b3f"
BLUE = "#3b6fd8"

FOOD_COST_MIN = 25
FOOD_COST_MAX = 35
PRIME_COST_MIN = 55
PRIME_COST_MAX = 65
WARN_MARGIN = 5  # points above the upper bound of a band before the ratio turns red

DESCRIPTOR: dict[str, Any] = {
    "id": "restaurant",
    "label": "Restauration",
    "description": (
        "Pilotage d'un restaurant à partir d'une ligne par jour (ou par service) : coût matière et coût du "
        "personnel en % du chiffre d'affaires, prime cost avec ses cibles réglables, ticket moyen, couverts et "
        "chiffre d'affaires par jour de semaine."
    ),
    "version": 1,
    "columns": [
        {"key": "date", "label": "Date", "type": "date", "required": True, "default": "date"},
        {"key": "revenue", "label": "Chiffre d'affaires", "type": "number", "required": True, "default": "ca"},
        {"key": "covers", "label": "Couverts", "type": "number", "required": True, "default": "couverts"},
        {"key": "food_cost", "label": "Achats matières", "type": "number", "required": True,
         "default": "achats_matieres"},
        {"key": "labor_cost", "label": "Masse salariale", "type": "number", "required": True,
         "default": "masse_salariale"},
        {"key": "service", "label": "Service (midi / soir)", "type": "string", "required": False, "default": None},
    ],
    "params": [
        {"key": "food_cost_min", "label": "Coût matière : cible basse (%)", "type": "integer",
         "default": FOOD_COST_MIN, "min": 0, "max": 99},
        {"key": "food_cost_max", "label": "Coût matière : cible haute (%)", "type": "integer",
         "default": FOOD_COST_MAX, "min": 1, "max": 100},
        {"key": "prime_cost_min", "label": "Prime cost : cible basse (%)", "type": "integer",
         "default": PRIME_COST_MIN, "min": 0, "max": 99},
        {"key": "prime_cost_max", "label": "Prime cost : cible haute (%)", "type": "integer",
         "default": PRIME_COST_MAX, "min": 1, "max": 100},
    ],
}
_REQUIRED = [c["key"] for c in DESCRIPTOR["columns"] if c["required"]]
_OPTIONAL = [c["key"] for c in DESCRIPTOR["columns"] if not c["required"]]
_PARAMS = {p["key"]: p for p in DESCRIPTOR["params"]}

_WEEKDAYS = ("Lun", "Mar", "Mer", "Jeu", "Ven", "Sam", "Dim")


def _check_ident(label: str, value: str) -> str:
    if not _IDENT_RE.match(value or ""):
        raise ValueError(
            f"{label}='{value}' is not a valid column name: use lowercase letters, "
            "digits and underscores (start with a letter), e.g. 'achats_matieres'."
        )
    return value


def _check_params(params: dict[str, Any]) -> dict[str, int]:
    """Parameters with defaults filled in; ValueError when out of range or inconsistent."""
    clean: dict[str, int] = {}
    for key, spec in _PARAMS.items():
        value = params.get(key)
        value = spec["default"] if value is None else value
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"Parameter '{key}' must be an integer.")
        if not spec["min"] <= value <= spec["max"]:
            raise ValueError(f"Parameter '{key}' must be between {spec['min']} and {spec['max']}.")
        clean[key] = value
    for low, high in (("food_cost_min", "food_cost_max"), ("prime_cost_min", "prime_cost_max")):
        if clean[low] >= clean[high]:
            raise ValueError(f"Parameter '{low}' must be lower than '{high}'.")
    return clean


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True)


def _q(column: str) -> str:
    return f'"{column}"'


def _split(count: int, per_row: int = 4) -> list[int]:
    """Tiles per row, as even as possible, at most ``per_row``."""
    rows = -(-count // per_row)
    base, extra = divmod(count, rows)
    return [base + (1 if i < extra else 0) for i in range(rows)]


def _spread(count: int) -> list[int]:
    """Widths filling a 12-column row with ``count`` tiles."""
    base, extra = divmod(12, count)
    return [base + (1 if i < extra else 0) for i in range(count)]


def restaurant(
    dataset_id: str,
    *,
    date: str = "date",
    revenue: str = "ca",
    covers: str = "couverts",
    food_cost: str = "achats_matieres",
    labor_cost: str = "masse_salariale",
    service: str | None = None,
    food_cost_min: int = FOOD_COST_MIN,
    food_cost_max: int = FOOD_COST_MAX,
    prime_cost_min: int = PRIME_COST_MIN,
    prime_cost_max: int = PRIME_COST_MAX,
    default_period: str = "12w",
) -> dict:
    """Build the « Restauration » spec for a dataset of days (or services)."""
    if not dataset_id:
        raise ValueError("dataset_id is required.")
    d = _check_ident("date", date)
    ca = _check_ident("revenue", revenue)
    cv = _check_ident("covers", covers)
    fc = _check_ident("food_cost", food_cost)
    lc = _check_ident("labor_cost", labor_cost)
    sv = _check_ident("service", service) if service else None

    bucket, weekday = "periode_semaine", "jour_semaine"
    CA, CV, FC, LC = (f"SUM({_q(c)})" for c in (ca, cv, fc, lc))

    # ---- transforms --------------------------------------------------
    names = " ".join(f"WHEN {i} THEN '{i}-{name}'" for i, name in enumerate(_WEEKDAYS, start=1))
    transforms = [
        DeriveStep(column=bucket, expr=f"date_trunc('week', CAST({_q(d)} AS TIMESTAMP))", provenance=Provenance.REAL),
        DeriveStep(column=weekday, expr=f"CASE isodow(CAST({_q(d)} AS DATE)) {names} END",
                   provenance=Provenance.REAL),
    ]

    # ---- fields ------------------------------------------------------
    fields = [
        FieldSpec(name=d, label="Date", type="date", role="date"),
        FieldSpec(name=ca, label="Chiffre d'affaires", type="number", role="measure", format="compact"),
        FieldSpec(name=cv, label="Couverts", type="number", role="measure", format="integer"),
        FieldSpec(name=fc, label="Achats matières", type="number", role="measure", format="compact"),
        FieldSpec(name=lc, label="Masse salariale", type="number", role="measure", format="compact"),
    ]
    if sv:
        fields.append(FieldSpec(name=sv, label="Service", role="dimension"))
    fields += [
        FieldSpec(name=bucket, label="Semaine", type="date", role="dimension"),
        FieldSpec(name=weekday, label="Jour de la semaine", role="dimension"),
    ]

    # ---- measures ----------------------------------------------------
    def band(high: int) -> ThresholdSpec:
        return ThresholdSpec(direction="lower_is_better", good=high / 100,
                             warn=min(high + WARN_MARGIN, 100) / 100)

    measures = [
        MeasureSpec(name="ca", label="Chiffre d'affaires", expr=CA, format="compact", unit="€"),
        MeasureSpec(name="couverts", label="Couverts", expr=CV, format="integer", unit="couverts"),
        MeasureSpec(name="achats", label="Achats matières", expr=FC, format="compact", unit="€"),
        MeasureSpec(name="masse_salariale", label="Masse salariale", expr=LC, format="compact", unit="€"),
        MeasureSpec(name="cout_matiere", label="Coût matière", expr=f"{FC} / NULLIF({CA}, 0)", format="percent",
                    thresholds=band(food_cost_max),
                    description="Achats matières ÷ chiffre d'affaires."),
        MeasureSpec(name="cout_personnel", label="Coût du personnel", expr=f"{LC} / NULLIF({CA}, 0)",
                    format="percent", description="Masse salariale ÷ chiffre d'affaires."),
        MeasureSpec(name="prime_cost", label="Prime cost", expr=f"({FC} + {LC}) / NULLIF({CA}, 0)",
                    format="percent", thresholds=band(prime_cost_max),
                    description="(Achats matières + masse salariale) ÷ chiffre d'affaires."),
        MeasureSpec(name="ticket_moyen", label="Ticket moyen", expr=f"{CA} / NULLIF({CV}, 0)", format="decimal1",
                    unit="€", description="Chiffre d'affaires ÷ couverts."),
        MeasureSpec(name="ca_moyen_jour", label="CA moyen par jour",
                    expr=f"{CA} / NULLIF(COUNT(DISTINCT CAST({_q(d)} AS DATE)), 0)", format="compact", unit="€",
                    description="Chiffre d'affaires ÷ nombre de jours distincts (les deux services d'un jour comptent "
                                "pour un seul jour)."),
    ]
    source = SourceSpec(
        name="exploitation",
        label="Exploitation du restaurant",
        kind="dataset",
        dataset_id=dataset_id,
        transforms=transforms,
        fields=fields,
        measures=measures,
    )

    # ---- filters -----------------------------------------------------
    presets = ["4w", "12w", "26w", "52w", "all"]
    if default_period not in presets:
        presets.insert(0, default_period)
    filters = [FilterSpec(id="periode", label="Période", field=d, type="relative_period", default=default_period,
                          presets=presets)]
    if sv:
        filters.append(FilterSpec(id="service", label="Service", field=sv, type="select", default=None))

    # ---- visuals -----------------------------------------------------
    def kpi(title: str, measure: str, **kw: Any) -> VisualSpec:
        return VisualSpec(type="kpi", title=title, source="exploitation", x=bucket,
                          kpi=KpiSpec(measure=measure, sparkline=measure, **kw))

    scope = "{{filters.service}}, d" if sv else "D"
    synthese = VisualSpec(
        type="text",
        source="exploitation",
        text=(
            f"{scope}u {{{{period.from}}}} au {{{{period.to}}}} : {{{{ca|compact}}}} € de chiffre d'affaires pour "
            "{{couverts|integer}} couverts (ticket moyen {{ticket_moyen|decimal1}} €). Coût matière "
            "{{cout_matiere|percent}}, personnel {{cout_personnel|percent}}, prime cost {{prime_cost|percent}}."
        ),
    )
    kpis = [
        kpi("Chiffre d'affaires", "ca", secondary="{{couverts|integer}} couverts"),
        kpi("Coût matière", "cout_matiere", target_label=f"cible {food_cost_min}-{food_cost_max} %",
            secondary="{{achats|compact}} € d'achats"),
        kpi("Coût du personnel", "cout_personnel", secondary="{{masse_salariale|compact}} € de masse salariale"),
        kpi("Prime cost", "prime_cost", target_label=f"cible {prime_cost_min}-{prime_cost_max} %"),
        kpi("Ticket moyen", "ticket_moyen", secondary="par couvert"),
    ]

    def edges(low: int, high: int, label: str) -> list[ReferenceLine]:
        return [
            ReferenceLine(value=low / 100, label=f"{label} mini {low} %", color="good"),
            ReferenceLine(value=high / 100, label=f"{label} maxi {high} %", color="good"),
        ]

    ratios = VisualSpec(
        type="line",
        title="Coût matière et personnel par semaine",
        subtitle=f"En % du chiffre d'affaires. Lignes vertes : cible matière {food_cost_min}-{food_cost_max} %.",
        source="exploitation",
        x=bucket,
        series=[
            SeriesSpec(key="cout_matiere", label="Coût matière", mark="line", color=BLUE),
            SeriesSpec(key="cout_personnel", label="Coût du personnel", mark="line", color=BROWN),
        ],
        reference_lines=edges(food_cost_min, food_cost_max, "Matière"),
        sort="x_asc",
    )
    prime = VisualSpec(
        type="line",
        title="Prime cost par semaine",
        subtitle=f"(Matières + personnel) ÷ CA. Lignes vertes : cible {prime_cost_min}-{prime_cost_max} %.",
        source="exploitation",
        x=bucket,
        series=[SeriesSpec(key="prime_cost", label="Prime cost", mark="line", color=BLUE)],
        reference_lines=edges(prime_cost_min, prime_cost_max, "Prime cost"),
        sort="x_asc",
    )
    activity = VisualSpec(
        type="combo",
        title="CA et couverts par semaine",
        subtitle="Chiffre d'affaires en barres, couverts en ligne.",
        source="exploitation",
        x=bucket,
        series=[
            SeriesSpec(key="ca", label="Chiffre d'affaires", mark="bar", color=BLUE),
            SeriesSpec(key="couverts", label="Couverts", mark="line", color=BROWN, axis="right"),
        ],
        sort="x_asc",
    )
    by_weekday = VisualSpec(
        type="combo",
        title="CA moyen par jour de semaine",
        subtitle="Barres : CA moyen d'un jour ouvert. Ligne : CA total sur la période.",
        source="exploitation",
        x=weekday,
        series=[
            SeriesSpec(key="ca_moyen_jour", label="CA moyen par jour", mark="bar", color=BLUE),
            SeriesSpec(key="ca", label="CA total", mark="line", color=BROWN, axis="right"),
        ],
        sort="x_asc",
    )
    by_service = VisualSpec(
        type="pie",
        title="CA par service",
        source="exploitation",
        x=sv or "",
        series=[SeriesSpec(key="ca", label="Chiffre d'affaires")],
    ) if sv else None

    notes = [
        VisualSpec(
            type="note",
            title="Définitions",
            badges=[Provenance.REAL],
            text=(
                "**Coût matière** : achats matières ÷ CA. **Coût du personnel** : masse salariale ÷ CA. **Prime "
                "cost** : (matières + personnel) ÷ CA. **Ticket moyen** : CA ÷ couverts. Chaque ratio est calculé "
                "sur les sommes de la période, jamais comme une moyenne de ratios."
            ),
        ),
        VisualSpec(
            type="note",
            title="Cibles",
            text=(
                f"Coût matière : cible {food_cost_min}-{food_cost_max} %. Prime cost : cible "
                f"{prime_cost_min}-{prime_cost_max} %. Vert jusqu'à la borne haute, orange jusqu'à "
                f"{WARN_MARGIN} points au-dessus, rouge au-delà. Un ratio sous la borne basse n'est pas pénalisé : "
                "vérifier alors que tous les achats sont saisis. Les cibles se règlent à la création."
            ),
        ),
    ]

    # ---- grid --------------------------------------------------------
    grid: list[tuple[VisualSpec, dict[str, int]]] = [(synthese, dict(row=0, col=0, width=12, height=2))]
    row, index = 2, 0
    for count in _split(len(kpis)):
        col = 0
        for width in _spread(count):
            grid.append((kpis[index], dict(row=row, col=col, width=width, height=3)))
            col += width
            index += 1
        row += 3
    grid.append((ratios, dict(row=row, col=0, width=6, height=6)))
    grid.append((prime, dict(row=row, col=6, width=6, height=6)))
    row += 6
    grid.append((activity, dict(row=row, col=0, width=12, height=6)))
    row += 6
    if by_service is not None:
        grid.append((by_weekday, dict(row=row, col=0, width=8, height=6)))
        grid.append((by_service, dict(row=row, col=8, width=4, height=6)))
    else:
        grid.append((by_weekday, dict(row=row, col=0, width=12, height=6)))
    row += 6
    col = 0
    for tile, width in zip(notes, _spread(len(notes))):
        grid.append((tile, dict(row=row, col=col, width=width, height=4)))
        col += width

    return {
        "sources": [_dump(source)],
        "filters": [_dump(f) for f in filters],
        "components": [{"visual": _dump(v), "position": pos} for v, pos in grid],
    }


def build(dataset_id: str, columns: dict, params: dict) -> dict:
    """Catalog entry point: column mapping (blank optional → None) to the template."""
    mapping: dict[str, Any] = {key: columns.get(key) or "" for key in _REQUIRED}
    mapping.update({key: columns.get(key) or None for key in _OPTIONAL})
    return restaurant(dataset_id, **mapping, **_check_params(params))
