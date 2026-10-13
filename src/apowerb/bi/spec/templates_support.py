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
from apowerb.bi.spec.templates_common import (
    DEFAULT_LANG,
    Placed,
    assemble,
    check_ident,
    flow,
    quote,
    tile_row,
    translator,
)

BROWN = "#9a6b3f"
BLUE = "#3b6fd8"

SLA_GOOD = 0.95
SLA_WARN = 0.90

RESPONSE_TARGET = (4, 1, 168)
RESOLUTION_TARGET = (48, 1, 720)

DESCRIPTOR: dict[str, Any] = {
    "id": "support_sla",
    "label": {"fr": "Support et SLA", "en": "Support and SLA"},
    "description": {
        "fr": (
            "Suivi du support à partir d'une ligne par ticket : tickets reçus, backlog ouvert, délai médian de première "
            "réponse, part des tickets dans le SLA, tickets par semaine et par priorité, et tableau par équipe. Un ticket "
            "est dans le SLA si sa première réponse et sa résolution respectent les objectifs en heures (paramétrables) ; "
            "un ticket ouvert dont l'objectif est déjà dépassé compte comme hors SLA, la date de référence étant la plus "
            "récente du fichier. Sans date de résolution, pas de backlog."
        ),
        "en": (
            "Support tracking from one row per ticket: tickets received, open backlog, median first response "
            "time, share of tickets within SLA, tickets per week and per priority, and a table per team. A ticket "
            "is within SLA if its first response and its resolution meet the objectives in hours (configurable); "
            "an open ticket whose objective is already exceeded counts as outside SLA, the reference date being the "
            "most recent one in the file. Without a resolution date, there is no backlog."
        ),
    },
    "version": 1,
    "columns": [
        {"key": "ticket", "label": {"fr": "Identifiant du ticket", "en": "Ticket ID"}, "type": "string",
         "required": True, "default": "ticket_id"},
        {"key": "opened_at", "label": {"fr": "Date d'ouverture", "en": "Opening date"}, "type": "date",
         "required": True, "default": "date_ouverture"},
        {"key": "first_response_at", "label": {"fr": "Date de première réponse", "en": "First response date"},
         "type": "date", "required": False, "default": None},
        {"key": "resolved_at", "label": {"fr": "Date de résolution", "en": "Resolution date"}, "type": "date",
         "required": False, "default": None},
        {"key": "priority", "label": {"fr": "Priorité ou catégorie", "en": "Priority or category"},
         "type": "string", "required": False, "default": None},
        {"key": "team", "label": {"fr": "Agent ou équipe", "en": "Agent or team"}, "type": "string",
         "required": False, "default": None},
    ],
    "params": [
        {"key": "response_target_hours",
         "label": {"fr": "Objectif de première réponse (heures)", "en": "First response objective (hours)"},
         "type": "integer", "default": RESPONSE_TARGET[0], "min": RESPONSE_TARGET[1], "max": RESPONSE_TARGET[2]},
        {"key": "resolution_target_hours",
         "label": {"fr": "Objectif de résolution (heures)", "en": "Resolution objective (hours)"},
         "type": "integer", "default": RESOLUTION_TARGET[0], "min": RESOLUTION_TARGET[1],
         "max": RESOLUTION_TARGET[2]},
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
    lang: str = DEFAULT_LANG,
) -> dict:
    """Build the « Support et SLA » spec for a dataset of tickets, in ``lang``."""
    T = translator(lang)
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
        FieldSpec(name=op, label=T("Date d'ouverture", "Opening date"), type="date", role="date"),
    ]
    if fr:
        fields.append(FieldSpec(name=fr, label=T("Date de première réponse", "First response date"), type="date",
                                role="date"))
    if rs:
        fields.append(FieldSpec(name=rs, label=T("Date de résolution", "Resolution date"), type="date", role="date"))
    if pr:
        fields.append(FieldSpec(name=pr, label=T("Priorité", "Priority"), role="dimension"))
    if tm:
        fields.append(FieldSpec(name=tm, label=T("Équipe", "Team"), role="dimension"))
    fields.append(FieldSpec(name="periode_semaine", label=T("Semaine", "Week"), type="date", role="dimension"))
    if fr:
        fields += [
            FieldSpec(name="delai_reponse_h", label=T("Délai de première réponse (h)", "First response time (h)"),
                      type="number", role="measure", format="decimal1"),
            FieldSpec(name="reponse_hors_delai", label=T("Réponse hors objectif", "Response outside objective"),
                      type="number", role="measure", format="integer"),
        ]
    if rs:
        fields += [
            FieldSpec(name="ticket_ouvert", label=T("Ticket ouvert", "Open ticket"), type="number", role="measure",
                      format="integer"),
            FieldSpec(name="delai_resolution_h", label=T("Délai de résolution (h)", "Resolution time (h)"),
                      type="number", role="measure", format="decimal1"),
            FieldSpec(name="resolution_hors_delai",
                      label=T("Résolution hors objectif", "Resolution outside objective"), type="number",
                      role="measure", format="integer"),
        ]
    if breaches:
        fields.append(FieldSpec(name="sla_respecte", label=T("SLA respecté", "SLA met"), type="number",
                                role="measure", format="integer"))

    # ---- measures ----------------------------------------------------
    measures = [MeasureSpec(name="tickets", label=T("Tickets reçus", "Tickets received"),
                            expr=f"COUNT(DISTINCT {quote(tk)})", format="integer", unit="tickets")]
    if rs:
        measures += [
            MeasureSpec(name="backlog", label=T("Backlog ouvert", "Open backlog"),
                        expr="COALESCE(SUM(ticket_ouvert), 0)", format="integer", unit="tickets",
                        description=T("Tickets sans date de résolution.", "Tickets without a resolution date.")),
            MeasureSpec(name="delai_resolution", label=T("Délai médian de résolution", "Median resolution time"),
                        expr="MEDIAN(delai_resolution_h)", format="decimal1", unit="h",
                        thresholds=ThresholdSpec(direction="lower_is_better", good=float(p_h), warn=float(2 * p_h)),
                        description=T("Heures entre l'ouverture et la résolution, tickets résolus.",
                                      "Hours between opening and resolution, resolved tickets.")),
        ]
    if fr:
        measures.append(
            MeasureSpec(name="delai_reponse",
                        label=T("Délai médian de première réponse", "Median first response time"),
                        expr="MEDIAN(delai_reponse_h)", format="decimal1", unit="h",
                        thresholds=ThresholdSpec(direction="lower_is_better", good=float(r_h), warn=float(2 * r_h)),
                        description=T("Heures entre l'ouverture et la première réponse, tickets répondus.",
                                      "Hours between opening and first response, answered tickets."))
        )
    if breaches:
        measures.append(
            MeasureSpec(name="sla", label=T("SLA respecté", "SLA met"), expr="AVG(sla_respecte)", format="percent",
                        thresholds=ThresholdSpec(direction="higher_is_better", good=SLA_GOOD, warn=SLA_WARN),
                        description=T("Tickets sans objectif dépassé (première réponse, résolution), "
                                      "ouverts dont l'objectif n'est pas encore échu compris.",
                                      "Tickets with no exceeded objective (first response, resolution), "
                                      "including open ones whose objective is not yet due."))
        )
    source = SourceSpec(
        name="tickets",
        label=T("Tickets de support", "Support tickets"),
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
        FilterSpec(id="periode", label=T("Période (date d'ouverture)", "Period (opening date)"), field=op, type="relative_period",
                   default=default_period, presets=presets)
    ]
    if tm:
        filters.append(FilterSpec(id="equipe", label=T("Équipe", "Team"), field=tm, type="select", default=None))
    if pr:
        filters.append(FilterSpec(id="priorite", label=T("Priorité", "Priority"), field=pr, type="select", default=None))

    # ---- visuals -----------------------------------------------------
    def kpi(title: str, measure: str, **kw: Any) -> VisualSpec:
        return VisualSpec(type="kpi", title=title, source="tickets", x="periode_semaine",
                          kpi=KpiSpec(measure=measure, sparkline=measure, **kw))

    scope = "{{filters.equipe}}, du" if tm else "{{filters.priorite}}, du" if pr else "Du"
    scope_en = "{{filters.equipe}}, " if tm else "{{filters.priorite}}, " if pr else "From "
    text = T(
        f"{scope} {{{{period.from}}}} au {{{{period.to}}}} : {{{{tickets|integer}}}} tickets reçus",
        f"{scope_en}{{{{period.from}}}} to {{{{period.to}}}}: {{{{tickets|integer}}}} tickets received",
    )
    text += T(", {{backlog|integer}} encore ouverts.", ", {{backlog|integer}} still open.") if rs else "."
    if breaches:
        text += T(" {{sla|percent}} des tickets respectent le SLA.", " {{sla|percent}} of tickets meet the SLA.")
    synthese = VisualSpec(type="text", source="tickets", text=text)

    kpis = [kpi(T("Tickets reçus", "Tickets received"), "tickets")]
    if rs:
        kpis.append(kpi(T("Backlog ouvert", "Open backlog"), "backlog",
                        secondary=T("sans date de résolution", "without a resolution date")))
    if fr:
        kpis.append(kpi(T("Délai médian de première réponse", "Median first response time"), "delai_reponse",
                        target_label=T(f"cible ≤ {r_h} h", f"target ≤ {r_h} h")))
    if breaches:
        objectives = ([T(f"réponse {r_h} h", f"response {r_h} h")] if fr else []) + (
            [T(f"résolution {p_h} h", f"resolution {p_h} h")] if rs else [])
        kpis.append(kpi(T("SLA respecté", "SLA met"), "sla",
                        secondary=T("objectifs : ", "objectives: ") + ", ".join(objectives),
                        target_label=T("cible ≥ 95 %", "target ≥ 95%")))

    series = [SeriesSpec(key="tickets", label="Tickets", mark="bar", color=BLUE)]
    if breaches:
        series.append(SeriesSpec(key="sla", label=T("SLA respecté", "SLA met"), mark="line", color=BROWN,
                                 axis="right"))
    par_semaine = VisualSpec(
        type="combo",
        title=T("Tickets par semaine", "Tickets per week"),
        subtitle=T("Selon la date d'ouverture.", "By opening date.") + (
            T(" Part de tickets dans le SLA sur l'axe de droite.", " Share of tickets within SLA on the right axis.")
            if breaches else ""),
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
                title=T("Tickets par priorité", "Tickets by priority"),
                subtitle=T("Cliquer pour filtrer le tableau de bord.", "Click to filter the dashboard."),
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
        columns = [ColumnSpec(key=tm, label=T("Équipe", "Team")), ColumnSpec(key="tickets", label="Tickets")]
        if rs:
            columns += [ColumnSpec(key="backlog", label="Backlog"),
                        ColumnSpec(key="delai_resolution", label=T("Résolution (médiane, h)", "Resolution (median, h)"),
                                   use_thresholds=True)]
        if fr:
            columns.append(ColumnSpec(key="delai_reponse", label=T("Réponse (médiane, h)", "Response (median, h)"),
                                      use_thresholds=True))
        if breaches:
            columns.append(ColumnSpec(key="sla", label=T("SLA respecté", "SLA met"), use_thresholds=True))
        table = VisualSpec(
            type="table",
            title=T("SLA par équipe", "SLA by team"),
            subtitle=T("La ligne « Toutes » reprend les définitions globales, pas la moyenne des équipes.",
                       "The “All” row uses the overall definitions, not the average of the teams."),
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
            title=T("Définitions", "Definitions"),
            badges=[Provenance.REAL],
            text=T(
                f"Objectifs : première réponse sous {r_h} h, résolution sous {p_h} h (paramétrables à la création). "
                "Un ticket est hors SLA si un objectif est dépassé, y compris pour un ticket encore sans réponse ou "
                "sans résolution dont l'échéance est passée. La date de référence est la plus récente du fichier "
                "(et non la date du jour). Vert à partir de 95 % de SLA, orange entre 90 % et 95 %, rouge en dessous.",
                f"Objectives: first response within {r_h} h, resolution within {p_h} h (configurable at creation). "
                "A ticket is outside SLA if an objective is exceeded, including a ticket still without a response or "
                "a resolution whose deadline has passed. The reference date is the most recent one in the file "
                "(not today's date). Green from 95% SLA, orange between 90% and 95%, red below.",
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


def build(dataset_id: str, columns: dict, params: dict, lang: str = DEFAULT_LANG) -> dict:
    """Catalog entry point: column mapping (blank optional → None) and objectives to the template, in ``lang``."""
    mapping: dict[str, Any] = {key: columns.get(key) or "" for key in _REQUIRED}
    mapping.update({key: columns.get(key) or None for key in _OPTIONAL})
    for param in DESCRIPTOR["params"]:
        if param["key"] in params:
            mapping[param["key"]] = params[param["key"]]
    return support_sla(dataset_id, **mapping, lang=lang)
