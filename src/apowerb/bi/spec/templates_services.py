"""
bi/spec/templates_services.py
-----------------------------
Spec v2 template « Services B2B »: one row per consultant and per day (or week), from a
time-tracking export.

Definitions (all measures are valid per week, per consultant and over the whole period):

* occupancy = hours worked / hours available;
* billable rate = billable hours / hours worked;
* a day is ``HOURS_PER_DAY`` hours: it turns billable hours into billable days;
* average day rate (TJM) = the ``tjm`` column weighted by billable hours; without it,
  the amount invoiced divided by the billable days of the invoiced rows;
* revenue = ``montant_facture`` summed; without it, billable days valued at the ``tjm``;
* left to invoice = billable days valued at the ``tjm`` minus the amount invoiced
  (needs both columns, never below zero).

``heures_disponibles`` may be repeated on every row of a consultant-day, or given once
(the other rows holding 0): the available hours of a consultant-day are counted once
and shared evenly between its rows.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from apowerb.bi.spec.models import (
    IDENT,
    ColumnSpec,
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
_PERIOD_RE = re.compile(r"^(all|\d{1,3}[wm])$")

BROWN = "#9a6b3f"
BLUE = "#3b6fd8"

HOURS_PER_DAY = 7
WARN_GAP_POINTS = 10  # warn threshold = target minus 10 points

DESCRIPTOR: dict[str, Any] = {
    "id": "services_b2b",
    "label": "Services B2B / Occupation et facturation",
    "description": (
        "Pilotage d'une activité de services (conseil, ingénierie, agence) à partir d'une ligne par consultant et "
        "par jour ou semaine (export des temps passés) : taux d'occupation, taux facturable, TJM moyen, chiffre "
        "d'affaires par consultant, reste à facturer, principaux clients et occupation par consultant."
    ),
    "version": 1,
    "columns": [
        {"key": "date", "label": "Date (jour ou semaine)", "type": "date", "required": True, "default": "date"},
        {"key": "consultant", "label": "Consultant", "type": "string", "required": True, "default": "consultant"},
        {"key": "client", "label": "Client", "type": "string", "required": False, "default": "client"},
        {"key": "projet", "label": "Projet", "type": "string", "required": False, "default": "projet"},
        {"key": "heures", "label": "Heures passées", "type": "number", "required": True, "default": "heures"},
        {"key": "facturable", "label": "Facturable (booléen ou 0/1)", "type": "number", "required": True,
         "default": "facturable"},
        {"key": "heures_disponibles", "label": "Heures disponibles", "type": "number", "required": True,
         "default": "heures_disponibles"},
        {"key": "tjm", "label": "TJM (taux journalier)", "type": "number", "required": False, "default": "tjm"},
        {"key": "montant_facture", "label": "Montant facturé", "type": "number", "required": False,
         "default": "montant_facture"},
    ],
    "params": [
        {"key": "billable_target", "label": "Taux facturable cible (%)", "type": "integer", "default": 75,
         "min": 1, "max": 100},
        {"key": "occupancy_target", "label": "Taux d'occupation cible (%)", "type": "integer", "default": 85,
         "min": 1, "max": 100},
    ],
}
_REQUIRED = [c["key"] for c in DESCRIPTOR["columns"] if c["required"]]
_OPTIONAL = [c["key"] for c in DESCRIPTOR["columns"] if not c["required"]]
_PARAM_DEFAULTS = {p["key"]: p["default"] for p in DESCRIPTOR["params"]}


def _check_ident(label: str, value: str) -> str:
    if not _IDENT_RE.match(value or ""):
        raise ValueError(
            f"{label}='{value}' is not a valid column name: use lowercase letters, "
            "digits and underscores (start with a letter), e.g. 'heures_disponibles'."
        )
    return value


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True)


def _q(column: str) -> str:
    return f'"{column}"'


def _spread(count: int) -> list[int]:
    """Widths filling a 12-column row with ``count`` tiles."""
    base, extra = divmod(12, count)
    return [base + (1 if i < extra else 0) for i in range(count)]


def _flow(items: list[VisualSpec], row: int, height: int) -> tuple[list[tuple[VisualSpec, dict[str, int]]], int]:
    """Two tiles per row; a last lone tile takes the full width."""
    placed: list[tuple[VisualSpec, dict[str, int]]] = []
    for i in range(0, len(items), 2):
        pair = items[i : i + 2]
        widths = [12] if len(pair) == 1 else [6, 6]
        col = 0
        for visual, width in zip(pair, widths):
            placed.append((visual, dict(row=row, col=col, width=width, height=height)))
            col += width
        row += height
    return placed, row


def _threshold(target_pct: int) -> ThresholdSpec:
    warn = max(target_pct - WARN_GAP_POINTS, 0)
    return ThresholdSpec(direction="higher_is_better", good=target_pct / 100, warn=warn / 100)


def services_b2b(
    dataset_id: str,
    *,
    date: str = "date",
    consultant: str = "consultant",
    client: str | None = None,
    project: str | None = None,
    hours: str = "heures",
    billable: str = "facturable",
    available_hours: str = "heures_disponibles",
    day_rate: str | None = None,
    invoiced: str | None = None,
    billable_target: int = 75,
    occupancy_target: int = 85,
    default_period: str = "3m",
) -> dict:
    """Build the « Services B2B » spec for a dataset with one row per consultant and day or week."""
    if not dataset_id:
        raise ValueError("dataset_id is required.")
    da = _check_ident("date", date)
    co = _check_ident("consultant", consultant)
    cl = _check_ident("client", client) if client else None
    pr = _check_ident("project", project) if project else None
    ho = _check_ident("hours", hours)
    bi = _check_ident("billable", billable)
    av = _check_ident("available_hours", available_hours)
    tj = _check_ident("day_rate", day_rate) if day_rate else None
    iv = _check_ident("invoiced", invoiced) if invoiced else None
    if not _PERIOD_RE.match(default_period):
        raise ValueError(f"default_period='{default_period}' is not valid: expected 13w, 26w, 52w, 3m, 12m or all.")
    for label, target in (("billable_target", billable_target), ("occupancy_target", occupancy_target)):
        if not 1 <= target <= 100:
            raise ValueError(f"{label} must be between 1 and 100 (percent).")

    D, CO, H, B, AV = _q(da), _q(co), _q(ho), _q(bi), _q(av)
    day = f"CAST({D} AS DATE)"
    perday = f"PARTITION BY {CO}, {day}"
    flag = f"LOWER(TRIM(CAST({B} AS VARCHAR))) IN ('1', '1.0', 'true', 't', 'oui', 'o', 'vrai', 'yes', 'y', 'x')"

    # ---- transforms --------------------------------------------------
    transforms = [
        DeriveStep(column="semaine", expr=f"date_trunc('week', CAST({D} AS TIMESTAMP))", provenance=Provenance.REAL),
        DeriveStep(column="est_facturable", expr=f"CASE WHEN {flag} THEN 1 ELSE 0 END", provenance=Provenance.REAL),
        DeriveStep(column="heures_facturables_ligne", expr=f"COALESCE({H}, 0) * est_facturable",
                   provenance=Provenance.REAL),
        DeriveStep(
            column="heures_dispo_ligne",
            expr=f"MAX({AV}) OVER ({perday}) / NULLIF(COUNT(*) OVER ({perday}), 0)",
            provenance=Provenance.REAL,
        ),
    ]
    if tj:
        T = _q(tj)
        transforms += [
            DeriveStep(column="valeur_tjm_ligne", expr=f"heures_facturables_ligne * COALESCE({T}, 0) / {HOURS_PER_DAY}",
                       provenance=Provenance.REAL),
            DeriveStep(column="tjm_x_heures_ligne", expr=f"CASE WHEN {T} IS NOT NULL THEN heures_facturables_ligne * {T} END",
                       provenance=Provenance.REAL),
            DeriveStep(column="heures_avec_tjm_ligne",
                       expr=f"CASE WHEN {T} IS NOT NULL THEN heures_facturables_ligne END", provenance=Provenance.REAL),
        ]
    if iv:
        transforms.append(DeriveStep(
            column="heures_facturees_ligne",
            expr=f"CASE WHEN {_q(iv)} IS NOT NULL THEN heures_facturables_ligne END",
            provenance=Provenance.REAL,
        ))

    # ---- fields ------------------------------------------------------
    fields = [
        FieldSpec(name=da, label="Date", type="date", role="date"),
        FieldSpec(name=co, label="Consultant", role="dimension"),
    ]
    if cl:
        fields.append(FieldSpec(name=cl, label="Client", role="dimension"))
    if pr:
        fields.append(FieldSpec(name=pr, label="Projet", role="dimension"))
    fields += [
        FieldSpec(name=ho, label="Heures passées", type="number", role="measure", format="compact"),
        FieldSpec(name=av, label="Heures disponibles", type="number", role="measure", format="compact"),
    ]
    if tj:
        fields.append(FieldSpec(name=tj, label="TJM", type="number", role="measure", format="compact"))
    if iv:
        fields.append(FieldSpec(name=iv, label="Montant facturé", type="number", role="measure", format="compact"))
    fields += [
        FieldSpec(name="semaine", label="Semaine", type="date", role="dimension"),
        FieldSpec(name="est_facturable", label="Ligne facturable", type="number", role="measure", format="integer"),
        FieldSpec(name="heures_facturables_ligne", label="Heures facturables", type="number", role="measure",
                  format="compact"),
        FieldSpec(name="heures_dispo_ligne", label="Heures disponibles réparties", type="number", role="measure",
                  format="compact"),
    ]
    if tj:
        fields += [
            FieldSpec(name="valeur_tjm_ligne", label="Valeur au TJM", type="number", role="measure", format="compact"),
            FieldSpec(name="tjm_x_heures_ligne", label="TJM x heures", type="number", role="measure", format="compact"),
            FieldSpec(name="heures_avec_tjm_ligne", label="Heures avec TJM", type="number", role="measure",
                      format="compact"),
        ]
    if iv:
        fields.append(FieldSpec(name="heures_facturees_ligne", label="Heures des lignes facturées", type="number",
                                role="measure", format="compact"))

    # ---- measures ----------------------------------------------------
    occ_thr, bill_thr = _threshold(occupancy_target), _threshold(billable_target)
    measures = [
        MeasureSpec(name="heures_total", label="Heures passées", expr=f"SUM({H})", format="compact", unit="h"),
        MeasureSpec(name="heures_dispo_total", label="Heures disponibles", expr="SUM(heures_dispo_ligne)",
                    format="compact", unit="h"),
        MeasureSpec(name="heures_facturables", label="Heures facturables", expr="SUM(heures_facturables_ligne)",
                    format="compact", unit="h"),
        MeasureSpec(name="jours_facturables", label="Jours facturables",
                    expr=f"SUM(heures_facturables_ligne) / {HOURS_PER_DAY}.0", format="decimal1", unit="j",
                    description=f"Heures facturables ÷ {HOURS_PER_DAY} h par jour."),
        MeasureSpec(name="occupation", label="Taux d'occupation",
                    expr=f"SUM({H}) / NULLIF(SUM(heures_dispo_ligne), 0)", format="percent", thresholds=occ_thr,
                    description="Heures passées ÷ heures disponibles."),
        MeasureSpec(name="taux_facturable", label="Taux facturable",
                    expr=f"SUM(heures_facturables_ligne) / NULLIF(SUM({H}), 0)", format="percent",
                    thresholds=bill_thr, description="Heures facturables ÷ heures passées."),
    ]
    ca_measure = None
    if iv:
        ca_measure = ("CA facturé", f"SUM(COALESCE({_q(iv)}, 0))", "Montants facturés de la période.")
    elif tj:
        ca_measure = ("CA valorisé au TJM", "SUM(valeur_tjm_ligne)", "Jours facturables valorisés au TJM.")
    if tj:
        measures.append(MeasureSpec(
            name="tjm_moyen", label="TJM moyen", expr="SUM(tjm_x_heures_ligne) / NULLIF(SUM(heures_avec_tjm_ligne), 0)",
            format="compact", unit="€ / j", description="TJM pondéré par les heures facturables."))
    elif iv:
        measures.append(MeasureSpec(
            name="tjm_moyen", label="TJM moyen (facturé)",
            expr=f"SUM(COALESCE({_q(iv)}, 0)) / NULLIF(SUM(heures_facturees_ligne) / {HOURS_PER_DAY}.0, 0)",
            format="compact", unit="€ / j",
            description="Montant facturé ÷ jours facturables des lignes facturées."))
    if ca_measure:
        label, expr, description = ca_measure
        measures.append(MeasureSpec(name="ca", label=label, expr=expr, format="compact", unit="€",
                                    description=description))
        measures.append(MeasureSpec(
            name="ca_par_consultant", label="CA par consultant",
            expr=f"({expr}) / NULLIF(COUNT(DISTINCT {CO}), 0)", format="compact", unit="€",
            description="CA ÷ nombre de consultants."))
    if tj and iv:
        measures.append(MeasureSpec(
            name="reste_a_facturer", label="Reste à facturer",
            expr=f"GREATEST(SUM(valeur_tjm_ligne) - SUM(COALESCE({_q(iv)}, 0)), 0)", format="compact", unit="€",
            description="Jours facturables valorisés au TJM, moins le montant déjà facturé."))
    source = SourceSpec(
        name="activite",
        label="Temps passés par consultant",
        kind="dataset",
        dataset_id=dataset_id,
        transforms=transforms,
        fields=fields,
        measures=measures,
    )

    # ---- filters -----------------------------------------------------
    presets = ["4w", "13w", "3m", "6m", "12m", "all"]
    if default_period not in presets:
        presets.insert(0, default_period)
    filters = [FilterSpec(id="periode", label="Période", field=da, type="relative_period",
                          default=default_period, presets=presets),
               FilterSpec(id="consultant", label="Consultant", field=co, type="select", default=None)]
    if cl:
        filters.append(FilterSpec(id="client", label="Client", field=cl, type="select", default=None))
    if pr:
        filters.append(FilterSpec(id="projet", label="Projet", field=pr, type="select", default=None))

    # ---- visuals -----------------------------------------------------
    def kpi(title: str, measure: str, **kw: Any) -> VisualSpec:
        return VisualSpec(type="kpi", title=title, source="activite", x="semaine",
                          kpi=KpiSpec(measure=measure, sparkline=measure, **kw))

    summary = (
        "{{filters.consultant}}, du {{period.from}} au {{period.to}} : {{heures_total|integer}} h passées pour "
        "{{heures_dispo_total|integer}} h disponibles, soit une occupation de {{occupation|percent}} ; "
        "{{taux_facturable|percent}} des heures sont facturables ({{jours_facturables|decimal1}} jours)"
    )
    summary += ", CA de {{ca|compact}} €." if ca_measure else "."
    synthese = VisualSpec(type="text", source="activite", text=summary)

    kpis = [
        kpi("Taux d'occupation", "occupation", target_label=f"cible ≥ {occupancy_target} %"),
        kpi("Taux facturable", "taux_facturable", target_label=f"cible ≥ {billable_target} %",
            secondary="{{jours_facturables|decimal1}} jours facturables"),
    ]
    if tj or iv:
        kpis.append(kpi("TJM moyen", "tjm_moyen"))
    if ca_measure:
        kpis.append(kpi(ca_measure[0], "ca", compare="previous_period"))
        kpis.append(kpi("CA par consultant", "ca_par_consultant"))
    if tj and iv:
        kpis.append(kpi("Reste à facturer", "reste_a_facturer", secondary="valorisé au TJM, hors déjà facturé"))

    evolution = VisualSpec(
        type="bar",
        title="Taux facturable par semaine",
        subtitle=f"Heures facturables ÷ heures passées. Cible {billable_target} %.",
        source="activite",
        x="semaine",
        series=[SeriesSpec(key="taux_facturable", label="Taux facturable", mark="bar", use_thresholds=True)],
        reference_lines=[ReferenceLine(value=billable_target / 100, label=f"Cible {billable_target} %", color="good")],
        sort="x_asc",
    )
    occupation_chart = VisualSpec(
        type="hbar",
        title="Occupation par consultant",
        subtitle="Les moins occupés en premier. Cliquer pour filtrer le tableau de bord.",
        source="activite",
        x=co,
        series=[SeriesSpec(key="occupation", label="Occupation", mark="bar", use_thresholds=True)],
        sort="value_asc",
        reference_lines=[ReferenceLine(value=occupancy_target / 100, label=f"Cible {occupancy_target} %", color="good")],
        click_filters={"consultant": co},
        ignore_filters=["consultant"],
    )
    charts = []
    if cl:
        by_ca = ca_measure is not None
        charts.append(VisualSpec(
            type="hbar",
            title="Top clients",
            subtitle=("Par chiffre d'affaires. " if by_ca else "Par heures facturables. ")
            + "Cliquer pour filtrer le tableau de bord.",
            source="activite",
            x=cl,
            series=[SeriesSpec(key="ca" if by_ca else "heures_facturables",
                               label=ca_measure[0] if by_ca else "Heures facturables", mark="bar", color=BLUE)],
            sort="value_desc",
            top_n=10,
            click_filters={"client": cl},
            ignore_filters=["client"],
        ))
    charts.append(occupation_chart)

    table_columns = [
        ColumnSpec(key=co, label="Consultant"),
        ColumnSpec(key="heures_total", label="Heures"),
        ColumnSpec(key="occupation", label="Occupation", use_thresholds=True),
        ColumnSpec(key="taux_facturable", label="Taux facturable", use_thresholds=True),
    ]
    if tj or iv:
        table_columns.append(ColumnSpec(key="tjm_moyen", label="TJM moyen"))
    if ca_measure:
        table_columns.append(ColumnSpec(key="ca", label=ca_measure[0]))
    if tj and iv:
        table_columns.append(ColumnSpec(key="reste_a_facturer", label="Reste à facturer"))
    table = VisualSpec(
        type="table",
        title="Activité par consultant",
        subtitle="La ligne « Toutes » reprend les définitions globales, pas la moyenne des consultants.",
        source="activite",
        x=co,
        columns=table_columns,
        total_row=True,
        click_filters={"consultant": co},
        ignore_filters=["consultant"],
        highlight_filter="consultant",
    )
    notes = [
        VisualSpec(
            type="note",
            title="Définitions",
            badges=[Provenance.REAL],
            text=(
                f"**Occupation** : heures passées ÷ heures disponibles. **Taux facturable** : heures facturables ÷ heures "
                f"passées. Un jour compte {HOURS_PER_DAY} h. Les heures disponibles d'un consultant sur une date sont "
                "comptées une fois et réparties entre ses lignes."
            ),
        ),
        VisualSpec(
            type="note",
            title="Seuils",
            text=(
                f"Vert à partir de la cible ({occupancy_target} % d'occupation, {billable_target} % facturable), orange "
                f"jusqu'à {WARN_GAP_POINTS} points en dessous, rouge ensuite. Cibles réglables à la création du tableau "
                "de bord."
            ),
        ),
        VisualSpec(
            type="note",
            title="Chiffre d'affaires",
            text=(
                "Le CA est le montant facturé si la colonne existe, sinon les jours facturables valorisés au TJM. Le reste à "
                "facturer demande le TJM et le montant facturé : jours facturables valorisés moins montant déjà facturé."
            ),
        ),
    ]

    # ---- grid --------------------------------------------------------
    grid: list[tuple[VisualSpec, dict[str, int]]] = [(synthese, dict(row=0, col=0, width=12, height=2))]
    col = 0
    for tile, width in zip(kpis, _spread(len(kpis))):
        grid.append((tile, dict(row=2, col=col, width=width, height=3)))
        col += width
    grid.append((evolution, dict(row=5, col=0, width=12, height=6)))
    placed, row = _flow(charts, 11, 6)
    grid += placed
    grid.append((table, dict(row=row, col=0, width=12, height=6)))
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
    """Catalog entry point: column mapping (blank optional → None) and parameters (defaults filled in)."""
    mapping: dict[str, Any] = {key: columns.get(key) or "" for key in _REQUIRED}
    mapping.update({key: columns.get(key) or None for key in _OPTIONAL})
    values = {key: (params or {}).get(key) if (params or {}).get(key) is not None else default
              for key, default in _PARAM_DEFAULTS.items()}
    return services_b2b(
        dataset_id,
        date=mapping["date"],
        consultant=mapping["consultant"],
        client=mapping["client"],
        project=mapping["projet"],
        hours=mapping["heures"],
        billable=mapping["facturable"],
        available_hours=mapping["heures_disponibles"],
        day_rate=mapping["tjm"],
        invoiced=mapping["montant_facture"],
        **values,
    )
