"""
bi/spec/templates_support.py
----------------------------
Spec v2 template « Support et SLA »: one row per support ticket.

A ticket *breaches* the first-response objective when its first response came later than the
objective, or when it has none and the objective has already elapsed; same for the resolution.
The ticket meets the SLA when it breaches none of the objectives the dataset can measure.
The reference date of the open tickets is the latest date found in the ticket columns.
"""

from __future__ import annotations

from typing import Any

from apowerb.bi.spec.models import (
    ColumnSpec,
    DeriveStep,
    FieldSpec,
    FilterSpec,
    KpiSpec,
    MeasureSpec,
    Provenance,
    SeriesSpec,
    SourceSpec,
    ThresholdSpec,
    VisualSpec,
)
from apowerb.bi.spec.templates_common import Placed, assemble, check_ident, flow, quote, tile_row

BROWN = "#9a6b3f"
BLUE = "#3b6fd8"

SLA_GOOD = 0.95
SLA_WARN = 0.90

RESPONSE_TARGET = (4, 1, 168)
RESOLUTION_TARGET = (48, 1, 720)

DESCRIPTOR: dict[str, Any] = {
    "id": "support_sla",
    "label": "Support et SLA",
    "description": (
        "Suivi du support à partir d'une ligne par ticket : tickets reçus, backlog ouvert, délai médian de première "
        "réponse, part des tickets dans le SLA, tickets par semaine et par priorité, et tableau par équipe. Un ticket "
        "est dans le SLA si sa première réponse et sa résolution respectent les objectifs en heures (paramétrables) ; "
        "un ticket ouvert dont l'objectif est déjà dépassé compte comme hors SLA, la date de référence étant la plus "
        "récente du fichier. Sans date de résolution, pas de backlog."
    ),
    "version": 1,
    "columns": [
        {"key": "ticket", "label": "Identifiant du ticket", "type": "string", "required": True,
         "default": "ticket_id"},
        {"key": "opened_at", "label": "Date d'ouverture", "type": "date", "required": True,
         "default": "date_ouverture"},
        {"key": "first_response_at", "label": "Date de première réponse", "type": "date", "required": False,
         "default": None},
        {"key": "resolved_at", "label": "Date de résolution", "type": "date", "required": False, "default": None},
        {"key": "priority", "label": "Priorité ou catégorie", "type": "string", "required": False, "default": None},
        {"key": "team", "label": "Agent ou équipe", "type": "string", "required": False, "default": None},
    ],
    "params": [
        {"key": "response_target_hours", "label": "Objectif de première réponse (heures)", "type": "integer",
         "default": RESPONSE_TARGET[0], "min": RESPONSE_TARGET[1], "max": RESPONSE_TARGET[2]},
        {"key": "resolution_target_hours", "label": "Objectif de résolution (heures)", "type": "integer",
         "default": RESOLUTION_TARGET[0], "min": RESOLUTION_TARGET[1], "max": RESOLUTION_TARGET[2]},
    ],
}
_REQUIRED = [c["key"] for c in DESCRIPTOR["columns"] if c["required"]]
_OPTIONAL = [c["key"] for c in DESCRIPTOR["columns"] if not c["required"]]


def _check_target(label: str, value: Any, bounds: tuple[int, int, int]) -> int:
    _, low, high = bounds
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ValueError(f"{label}: l'objectif doit être un nombre entier d'heures entre {low} et {high}.")
    return value


def support_sla(
    dataset_id: str,
    *,
    ticket: str = "ticket_id",
    opened_at: str = "date_ouverture",
    first_response_at: str | None = None,
    resolved_at: str | None = None,
    priority: str | None = None,
    team: str | None = None,
    response_target_hours: int = RESPONSE_TARGET[0],
    resolution_target_hours: int = RESOLUTION_TARGET[0],
    default_period: str = "26w",
) -> dict:
    """Build the « Support et SLA » spec for a dataset of tickets."""
    if not dataset_id:
        raise ValueError("dataset_id is required.")
    tk = check_ident("ticket", ticket)
    op = check_ident("opened_at", opened_at)
    fr = check_ident("first_response_at", first_response_at) if first_response_at else None
    rs = check_ident("resolved_at", resolved_at) if resolved_at else None
    pr = check_ident("priority", priority) if priority else None
    tm = check_ident("team", team) if team else None
    r_h = _check_target("response_target_hours", response_target_hours, RESPONSE_TARGET)
    p_h = _check_target("resolution_target_hours", resolution_target_hours, RESOLUTION_TARGET)

    ts = {col: f"CAST({quote(col)} AS TIMESTAMP)" for col in (op, fr, rs) if col}
    as_of = "GREATEST(" + ", ".join(f"MAX({expr}) OVER ()" for expr in ts.values()) + ")"
    elapsed = f"date_diff('second', {ts[op]}, {as_of})"

    def breach(column: str, hours: int) -> str:
        spent = f"date_diff('second', {ts[op]}, {ts[column]})"
        return (
            f"CASE WHEN {quote(column)} IS NOT NULL THEN CASE WHEN {spent} > {hours * 3600} THEN 1 ELSE 0 END "
            f"WHEN {elapsed} > {hours * 3600} THEN 1 ELSE 0 END"
        )

    # ---- transforms --------------------------------------------------
    transforms = [
        DeriveStep(column="periode_semaine", expr=f"date_trunc('week', {ts[op]})", provenance=Provenance.REAL)
    ]
    breaches: list[str] = []
    if fr:
        transforms += [
            DeriveStep(column="delai_reponse_h",
                       expr=f"CASE WHEN {quote(fr)} IS NOT NULL THEN date_diff('second', {ts[op]}, {ts[fr]}) / 3600.0 END"),
            DeriveStep(column="reponse_hors_delai", expr=breach(fr, r_h)),
        ]
        breaches.append("reponse_hors_delai")
    if rs:
        transforms += [
            DeriveStep(column="ticket_ouvert", expr=f"CASE WHEN {quote(rs)} IS NULL THEN 1 ELSE 0 END"),
            DeriveStep(column="delai_resolution_h",
                       expr=f"CASE WHEN {quote(rs)} IS NOT NULL THEN date_diff('second', {ts[op]}, {ts[rs]}) / 3600.0 END"),
            DeriveStep(column="resolution_hors_delai", expr=breach(rs, p_h)),
        ]
        breaches.append("resolution_hors_delai")
    if breaches:
        transforms.append(
            DeriveStep(column="sla_respecte", expr=f"CASE WHEN {' OR '.join(f'{b} = 1' for b in breaches)} THEN 0 ELSE 1 END")
        )

    # ---- fields ------------------------------------------------------
    fields = [
        FieldSpec(name=tk, label="Ticket", role="dimension"),
        FieldSpec(name=op, label="Date d'ouverture", type="date", role="date"),
    ]
    if fr:
        fields.append(FieldSpec(name=fr, label="Date de première réponse", type="date", role="date"))
    if rs:
        fields.append(FieldSpec(name=rs, label="Date de résolution", type="date", role="date"))
    if pr:
        fields.append(FieldSpec(name=pr, label="Priorité", role="dimension"))
    if tm:
        fields.append(FieldSpec(name=tm, label="Équipe", role="dimension"))
    fields.append(FieldSpec(name="periode_semaine", label="Semaine", type="date", role="dimension"))
    if fr:
        fields += [
            FieldSpec(name="delai_reponse_h", label="Délai de première réponse (h)", type="number", role="measure",
                      format="decimal1"),
            FieldSpec(name="reponse_hors_delai", label="Réponse hors objectif", type="number", role="measure",
                      format="integer"),
        ]
    if rs:
        fields += [
            FieldSpec(name="ticket_ouvert", label="Ticket ouvert", type="number", role="measure", format="integer"),
            FieldSpec(name="delai_resolution_h", label="Délai de résolution (h)", type="number", role="measure",
                      format="decimal1"),
            FieldSpec(name="resolution_hors_delai", label="Résolution hors objectif", type="number", role="measure",
                      format="integer"),
        ]
    if breaches:
        fields.append(FieldSpec(name="sla_respecte", label="SLA respecté", type="number", role="measure",
                                format="integer"))

    # ---- measures ----------------------------------------------------
    measures = [MeasureSpec(name="tickets", label="Tickets reçus", expr=f"COUNT(DISTINCT {quote(tk)})",
                            format="integer", unit="tickets")]
    if rs:
        measures += [
            MeasureSpec(name="backlog", label="Backlog ouvert", expr="COALESCE(SUM(ticket_ouvert), 0)",
                        format="integer", unit="tickets", description="Tickets sans date de résolution."),
            MeasureSpec(name="delai_resolution", label="Délai médian de résolution",
                        expr="MEDIAN(delai_resolution_h)", format="decimal1", unit="h",
                        thresholds=ThresholdSpec(direction="lower_is_better", good=float(p_h), warn=float(2 * p_h)),
                        description="Heures entre l'ouverture et la résolution, tickets résolus."),
        ]
    if fr:
        measures.append(
            MeasureSpec(name="delai_reponse", label="Délai médian de première réponse",
                        expr="MEDIAN(delai_reponse_h)", format="decimal1", unit="h",
                        thresholds=ThresholdSpec(direction="lower_is_better", good=float(r_h), warn=float(2 * r_h)),
                        description="Heures entre l'ouverture et la première réponse, tickets répondus.")
        )
    if breaches:
        measures.append(
            MeasureSpec(name="sla", label="SLA respecté", expr="AVG(sla_respecte)", format="percent",
                        thresholds=ThresholdSpec(direction="higher_is_better", good=SLA_GOOD, warn=SLA_WARN),
                        description="Tickets sans objectif dépassé (première réponse, résolution), "
                        "ouverts dont l'objectif n'est pas encore échu compris.")
        )
    source = SourceSpec(
        name="tickets",
        label="Tickets de support",
        kind="dataset",
        dataset_id=dataset_id,
        transforms=transforms,
        fields=fields,
        measures=measures,
    )

    # ---- filters -----------------------------------------------------
    presets = ["13w", "26w", "52w", "all"]
    if default_period not in presets:
        presets.insert(0, default_period)
    filters = [
        FilterSpec(id="periode", label="Période (date d'ouverture)", field=op, type="relative_period",
                   default=default_period, presets=presets)
    ]
    if tm:
        filters.append(FilterSpec(id="equipe", label="Équipe", field=tm, type="select", default=None))
    if pr:
        filters.append(FilterSpec(id="priorite", label="Priorité", field=pr, type="select", default=None))

    # ---- visuals -----------------------------------------------------
    def kpi(title: str, measure: str, **kw: Any) -> VisualSpec:
        return VisualSpec(type="kpi", title=title, source="tickets", x="periode_semaine",
                          kpi=KpiSpec(measure=measure, sparkline=measure, **kw))

    scope = "{{filters.equipe}}, du" if tm else "{{filters.priorite}}, du" if pr else "Du"
    text = f"{scope} {{{{period.from}}}} au {{{{period.to}}}} : {{{{tickets|integer}}}} tickets reçus"
    text += ", {{backlog|integer}} encore ouverts." if rs else "."
    if breaches:
        text += " {{sla|percent}} des tickets respectent le SLA."
    synthese = VisualSpec(type="text", source="tickets", text=text)

    kpis = [kpi("Tickets reçus", "tickets")]
    if rs:
        kpis.append(kpi("Backlog ouvert", "backlog", secondary="sans date de résolution"))
    if fr:
        kpis.append(kpi("Délai médian de première réponse", "delai_reponse", target_label=f"cible ≤ {r_h} h"))
    if breaches:
        objectives = ([f"réponse {r_h} h"] if fr else []) + ([f"résolution {p_h} h"] if rs else [])
        kpis.append(kpi("SLA respecté", "sla", secondary="objectifs : " + ", ".join(objectives),
                        target_label="cible ≥ 95 %"))

    series = [SeriesSpec(key="tickets", label="Tickets", mark="bar", color=BLUE)]
    if breaches:
        series.append(SeriesSpec(key="sla", label="SLA respecté", mark="line", color=BROWN, axis="right"))
    par_semaine = VisualSpec(
        type="combo",
        title="Tickets par semaine",
        subtitle="Selon la date d'ouverture." + (" Part de tickets dans le SLA sur l'axe de droite." if breaches else ""),
        source="tickets",
        x="periode_semaine",
        series=series,
        sort="x_asc",
    )
    charts: list[VisualSpec] = []
    if pr:
        charts.append(
            VisualSpec(
                type="hbar",
                title="Tickets par priorité",
                subtitle="Cliquer pour filtrer le tableau de bord.",
                source="tickets",
                x=pr,
                series=[SeriesSpec(key="tickets", label="Tickets", mark="bar", color=BROWN)],
                sort="value_desc",
                click_filters={"priorite": pr},
                ignore_filters=["priorite"],
            )
        )
    table = None
    if tm:
        columns = [ColumnSpec(key=tm, label="Équipe"), ColumnSpec(key="tickets", label="Tickets")]
        if rs:
            columns += [ColumnSpec(key="backlog", label="Backlog"),
                        ColumnSpec(key="delai_resolution", label="Résolution (médiane, h)", use_thresholds=True)]
        if fr:
            columns.append(ColumnSpec(key="delai_reponse", label="Réponse (médiane, h)", use_thresholds=True))
        if breaches:
            columns.append(ColumnSpec(key="sla", label="SLA respecté", use_thresholds=True))
        table = VisualSpec(
            type="table",
            title="SLA par équipe",
            subtitle="La ligne « Toutes » reprend les définitions globales, pas la moyenne des équipes.",
            source="tickets",
            x=tm,
            columns=columns,
            sort="value_desc",
            total_row=True,
            click_filters={"equipe": tm},
            ignore_filters=["equipe"],
            highlight_filter="equipe",
        )
    notes = [
        VisualSpec(
            type="note",
            title="Définitions",
            badges=[Provenance.REAL],
            text=(
                f"Objectifs : première réponse sous {r_h} h, résolution sous {p_h} h (paramétrables à la création). "
                "Un ticket est hors SLA si un objectif est dépassé, y compris pour un ticket encore sans réponse ou "
                "sans résolution dont l'échéance est passée. La date de référence est la plus récente du fichier "
                "(et non la date du jour). Vert à partir de 95 % de SLA, orange entre 90 % et 95 %, rouge en dessous."
            ),
        )
    ]

    # ---- grid --------------------------------------------------------
    grid: Placed = [(synthese, dict(row=0, col=0, width=12, height=2))]
    grid += tile_row(kpis, 2, 3)
    grid.append((par_semaine, dict(row=5, col=0, width=12, height=6)))
    placed, row = flow(charts, 11, 5)
    grid += placed
    if table is not None:
        grid.append((table, dict(row=row, col=0, width=12, height=6)))
        row += 6
    grid += tile_row(notes, row, 3)
    return assemble([source], filters, grid)


def build(dataset_id: str, columns: dict, params: dict) -> dict:
    """Catalog entry point: column mapping (blank optional → None) and objectives to the template."""
    mapping: dict[str, Any] = {key: columns.get(key) or "" for key in _REQUIRED}
    mapping.update({key: columns.get(key) or None for key in _OPTIONAL})
    for param in DESCRIPTOR["params"]:
        if param["key"] in params:
            mapping[param["key"]] = params[param["key"]]
    return support_sla(dataset_id, **mapping)
