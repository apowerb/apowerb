"""
bi/spec/templates_hr.py
-----------------------
Spec v2 template « RH / Ressources humaines »: one row per employee and per month
(payroll export from Silae, PayFit or a spreadsheet).

Definitions (all measures are valid both per month and over the whole period):

* absenteeism = absence hours / theoretical hours;
* headcount = distinct employee-months / distinct months (the monthly headcount
  for a month, the average headcount over a period);
* turnover = employees leaving in the period / average headcount (with a month
  as the period: leavers of the month / headcount of the month);
* payroll = sum of the loaded gross payroll; average cost = payroll per employee
  and per month.

Entries and exits need the ``entree`` / ``sortie`` columns; without ``sortie``
there is no turnover.
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
from apowerb.bi.spec.templates_common import DEFAULT_LANG, translator

_IDENT_RE = re.compile(IDENT)
_PERIOD_RE = re.compile(r"^(all|\d{1,3}[wm])$")

BROWN = "#9a6b3f"
BLUE = "#3b6fd8"
GREEN = "#3f8f5f"

DESCRIPTOR: dict[str, Any] = {
    "id": "hr_people",
    "label": {"fr": "RH / Absentéisme et masse salariale", "en": "HR / Absenteeism and payroll"},
    "description": {
        "fr": (
            "Tableau de bord RH d'une PME à partir d'une ligne par salarié et par mois (export de paie Silae, "
            "PayFit ou Excel) : taux d'absentéisme, turnover, effectif, masse salariale et son évolution, coût moyen "
            "par salarié, entrées et sorties, motifs d'absence et répartition par service."
        ),
        "en": (
            "HR dashboard for a small business from one row per employee and month (Silae, PayFit or Excel "
            "payroll export): absenteeism rate, turnover, headcount, payroll and its trend, average cost "
            "per employee, hires and exits, absence reasons and breakdown by department."
        ),
    },
    "version": 1,
    "columns": [
        {"key": "mois", "label": {"fr": "Mois", "en": "Month"}, "type": "date", "required": True, "default": "mois"},
        {"key": "salarie", "label": {"fr": "Salarié", "en": "Employee"}, "type": "string", "required": True, "default": "salarie"},
        {"key": "service", "label": {"fr": "Service", "en": "Department"}, "type": "string", "required": False, "default": "service"},
        {"key": "heures_theoriques", "label": {"fr": "Heures théoriques", "en": "Theoretical hours"}, "type": "number", "required": True,
         "default": "heures_theoriques"},
        {"key": "heures_absence", "label": {"fr": "Heures d'absence", "en": "Absence hours"}, "type": "number", "required": True,
         "default": "heures_absence"},
        {"key": "masse_salariale", "label": {"fr": "Masse salariale (brut chargé)", "en": "Payroll (loaded gross)"}, "type": "number", "required": True,
         "default": "masse_salariale"},
        {"key": "entree", "label": {"fr": "Date d'entrée", "en": "Hire date"}, "type": "date", "required": False, "default": "entree"},
        {"key": "sortie", "label": {"fr": "Date de sortie", "en": "Exit date"}, "type": "date", "required": False, "default": "sortie"},
        {"key": "motif_absence", "label": {"fr": "Motif d'absence", "en": "Absence reason"}, "type": "string", "required": False,
         "default": "motif_absence"},
    ],
    "params": [
        {"key": "absenteeism_warn", "label": {"fr": "Absentéisme : seuil d'alerte (%)", "en": "Absenteeism: warning threshold (%)"}, "type": "integer", "default": 5,
         "min": 1, "max": 50},
        {"key": "absenteeism_bad", "label": {"fr": "Absentéisme : seuil critique (%)", "en": "Absenteeism: critical threshold (%)"}, "type": "integer", "default": 8,
         "min": 1, "max": 100},
        {"key": "turnover_warn", "label": {"fr": "Turnover : seuil d'alerte (%)", "en": "Turnover: warning threshold (%)"}, "type": "integer", "default": 10,
         "min": 1, "max": 100},
        {"key": "turnover_bad", "label": {"fr": "Turnover : seuil critique (%)", "en": "Turnover: critical threshold (%)"}, "type": "integer", "default": 20,
         "min": 1, "max": 200},
    ],
}
_REQUIRED = [c["key"] for c in DESCRIPTOR["columns"] if c["required"]]
_OPTIONAL = [c["key"] for c in DESCRIPTOR["columns"] if not c["required"]]
_PARAM_DEFAULTS = {p["key"]: p["default"] for p in DESCRIPTOR["params"]}


def _check_ident(label: str, value: str) -> str:
    if not _IDENT_RE.match(value or ""):
        raise ValueError(
            f"{label}='{value}' is not a valid column name: use lowercase letters, "
            "digits and underscores (start with a letter), e.g. 'masse_salariale'."
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


def hr_people(
    dataset_id: str,
    *,
    month: str = "mois",
    employee: str = "salarie",
    department: str | None = None,
    theoretical_hours: str = "heures_theoriques",
    absence_hours: str = "heures_absence",
    payroll: str = "masse_salariale",
    hire_date: str | None = None,
    exit_date: str | None = None,
    absence_reason: str | None = None,
    absenteeism_warn: int = 5,
    absenteeism_bad: int = 8,
    turnover_warn: int = 10,
    turnover_bad: int = 20,
    default_period: str = "12m",
    lang: str = DEFAULT_LANG,
) -> dict:
    """Build the « RH » spec for a dataset with one row per employee and month, in ``lang``."""
    T = translator(lang)
    if not dataset_id:
        raise ValueError("dataset_id is required.")
    mo = _check_ident("month", month)
    em = _check_ident("employee", employee)
    de = _check_ident("department", department) if department else None
    th = _check_ident("theoretical_hours", theoretical_hours)
    ah = _check_ident("absence_hours", absence_hours)
    pa = _check_ident("payroll", payroll)
    hi = _check_ident("hire_date", hire_date) if hire_date else None
    ex = _check_ident("exit_date", exit_date) if exit_date else None
    ar = _check_ident("absence_reason", absence_reason) if absence_reason else None
    if not _PERIOD_RE.match(default_period):
        raise ValueError(f"default_period='{default_period}' is not valid: expected 13w, 26w, 52w, 3m, 12m or all.")
    if absenteeism_warn >= absenteeism_bad:
        raise ValueError("absenteeism_warn must be lower than absenteeism_bad.")
    if turnover_warn >= turnover_bad:
        raise ValueError("turnover_warn must be lower than turnover_bad.")

    M, E, TH, AH, PA = _q(mo), _q(em), _q(th), _q(ah), _q(pa)
    month_start = f"date_trunc('month', CAST({M} AS TIMESTAMP))"

    # ---- transforms --------------------------------------------------
    transforms = [
        DeriveStep(column="periode_mois", expr=month_start, provenance=Provenance.REAL),
        DeriveStep(
            column="cle_salarie_mois",
            expr=f"CAST({E} AS VARCHAR) || '|' || CAST(periode_mois AS VARCHAR)",
            provenance=Provenance.REAL,
        ),
    ]
    if hi:
        transforms.append(DeriveStep(
            column="entree_ce_mois",
            expr=(f"CASE WHEN {_q(hi)} IS NOT NULL AND date_trunc('month', CAST({_q(hi)} AS TIMESTAMP)) = periode_mois "
                  "THEN 1 ELSE 0 END"),
            provenance=Provenance.REAL,
        ))
    if ex:
        transforms.append(DeriveStep(
            column="sortie_ce_mois",
            expr=(f"CASE WHEN {_q(ex)} IS NOT NULL AND date_trunc('month', CAST({_q(ex)} AS TIMESTAMP)) = periode_mois "
                  "THEN 1 ELSE 0 END"),
            provenance=Provenance.REAL,
        ))
    if ar:
        transforms.append(DeriveStep(
            column="motif_affiche",
            expr=(f"CASE WHEN COALESCE({AH}, 0) > 0 THEN COALESCE(NULLIF(TRIM(CAST({_q(ar)} AS VARCHAR)), ''), "
                  f"'{T('Non précisé', 'Unspecified')}') END"),
            provenance=Provenance.REAL,
        ))

    # ---- fields ------------------------------------------------------
    fields = [
        FieldSpec(name=mo, label=T("Mois", "Month"), type="date", role="date"),
        FieldSpec(name=em, label=T("Salarié", "Employee"), role="dimension"),
    ]
    if de:
        fields.append(FieldSpec(name=de, label=T("Service", "Department"), role="dimension"))
    fields += [
        FieldSpec(name=th, label=T("Heures théoriques", "Theoretical hours"), type="number", role="measure",
                  format="compact"),
        FieldSpec(name=ah, label=T("Heures d'absence", "Absence hours"), type="number", role="measure",
                  format="compact"),
        FieldSpec(name=pa, label=T("Masse salariale", "Payroll"), type="number", role="measure", format="compact"),
    ]
    if hi:
        fields.append(FieldSpec(name=hi, label=T("Date d'entrée", "Hire date"), type="date", role="date"))
    if ex:
        fields.append(FieldSpec(name=ex, label=T("Date de sortie", "Exit date"), type="date", role="date"))
    if ar:
        fields.append(FieldSpec(name=ar, label=T("Motif d'absence", "Absence reason"), role="dimension"))
    fields += [
        FieldSpec(name="periode_mois", label=T("Mois", "Month"), type="date", role="dimension"),
        FieldSpec(name="cle_salarie_mois", label=T("Salarié et mois", "Employee and month"), role="dimension"),
    ]
    if hi:
        fields.append(FieldSpec(name="entree_ce_mois", label=T("Entrée ce mois", "Hired this month"),
                                type="number", role="measure", format="integer"))
    if ex:
        fields.append(FieldSpec(name="sortie_ce_mois", label=T("Sortie ce mois", "Left this month"),
                                type="number", role="measure", format="integer"))
    if ar:
        fields.append(FieldSpec(name="motif_affiche", label=T("Motif d'absence", "Absence reason"),
                                role="dimension"))

    # ---- measures ----------------------------------------------------
    headcount = "COUNT(DISTINCT cle_salarie_mois) * 1.0 / NULLIF(COUNT(DISTINCT periode_mois), 0)"
    absence_thr = ThresholdSpec(direction="lower_is_better", good=absenteeism_warn / 100, warn=absenteeism_bad / 100)
    turnover_thr = ThresholdSpec(direction="lower_is_better", good=turnover_warn / 100, warn=turnover_bad / 100)
    measures = [
        MeasureSpec(name="absenteisme", label=T("Taux d'absentéisme", "Absenteeism rate"),
                    expr=f"SUM({AH}) / NULLIF(SUM({TH}), 0)", format="percent", thresholds=absence_thr,
                    description=T("Heures d'absence ÷ heures théoriques.", "Absence hours ÷ theoretical hours.")),
        MeasureSpec(name="effectif", label=T("Effectif", "Headcount"),
                    expr=headcount, format="decimal1", unit=T("salariés", "employees"),
                    description=T("Salariés présents dans le mois ; effectif moyen sur une période.",
                                  "Employees present in the month; average headcount over a period.")),
        MeasureSpec(name="masse_totale", label=T("Masse salariale", "Payroll"), expr=f"SUM({PA})",
                    format="compact", unit="€",
                    description=T("Somme du brut chargé de la période.", "Sum of the loaded gross pay of the period.")),
        MeasureSpec(name="cout_moyen", label=T("Coût moyen par salarié", "Average cost per employee"),
                    expr=f"SUM({PA}) / NULLIF(COUNT(DISTINCT cle_salarie_mois), 0)", format="compact",
                    unit=T("€ / mois", "€ / month"),
                    description=T("Masse salariale ÷ nombre de salariés-mois.",
                                  "Payroll ÷ number of employee-months.")),
        MeasureSpec(name="heures_absence_total", label=T("Heures d'absence", "Absence hours"), expr=f"SUM({AH})",
                    format="compact", unit="h"),
    ]
    if hi:
        measures.append(MeasureSpec(
            name="entrees", label=T("Entrées", "Hires"),
            expr="COUNT(DISTINCT CASE WHEN entree_ce_mois = 1 THEN cle_salarie_mois END)",
            format="integer", unit=T("salariés", "employees")))
    if ex:
        measures.append(MeasureSpec(
            name="sorties", label=T("Sorties", "Exits"),
            expr="COUNT(DISTINCT CASE WHEN sortie_ce_mois = 1 THEN cle_salarie_mois END)",
            format="integer", unit=T("salariés", "employees")))
        measures.append(MeasureSpec(
            name="turnover", label="Turnover",
            expr=("COUNT(DISTINCT CASE WHEN sortie_ce_mois = 1 THEN cle_salarie_mois END) * 1.0 / "
                  f"NULLIF({headcount}, 0)"),
            format="percent", thresholds=turnover_thr,
            description=T("Départs de la période ÷ effectif moyen de la période.",
                          "Leavers of the period ÷ average headcount of the period.")))
    source = SourceSpec(
        name="rh",
        label=T("Salariés par mois", "Employees by month"),
        kind="dataset",
        dataset_id=dataset_id,
        transforms=transforms,
        fields=fields,
        measures=measures,
    )

    # ---- filters -----------------------------------------------------
    presets = ["3m", "6m", "12m", "all"]
    if default_period not in presets:
        presets.insert(0, default_period)
    filters = [FilterSpec(id="periode", label=T("Période", "Period"), field=mo, type="relative_period",
                          default=default_period, presets=presets)]
    if de:
        filters.append(FilterSpec(id="service", label=T("Service", "Department"), field=de, type="select", default=None))

    # ---- visuals -----------------------------------------------------
    def kpi(title: str, measure: str, **kw: Any) -> VisualSpec:
        return VisualSpec(type="kpi", title=title, source="rh", x="periode_mois",
                          kpi=KpiSpec(measure=measure, sparkline=measure, **kw))

    scope = "{{filters.service}}, " if de else ""
    summary = T(
        f"{scope}du {{{{period.from}}}} au {{{{period.to}}}} : effectif moyen de {{{{effectif|decimal1}}}} salariés, "
        "absentéisme de {{absenteisme|percent}}, masse salariale de {{masse_totale|compact}} €",
        f"{scope}{{{{period.from}}}} to {{{{period.to}}}}: average headcount of {{{{effectif|decimal1}}}} employees, "
        "absenteeism of {{absenteisme|percent}}, payroll of {{masse_totale|compact}} €",
    )
    if ex:
        summary += T(", turnover de {{turnover|percent}}", ", turnover of {{turnover|percent}}")
    summary += "."
    synthese = VisualSpec(type="text", source="rh", text=summary)

    kpis = [kpi(T("Taux d'absentéisme", "Absenteeism rate"), "absenteisme",
                target_label=T(f"alerte > {absenteeism_warn} %", f"warning > {absenteeism_warn}%"))]
    if ex:
        kpis.append(kpi("Turnover", "turnover",
                        secondary=T("{{sorties|integer}} départs sur la période",
                                    "{{sorties|integer}} leavers over the period"),
                        target_label=T(f"alerte > {turnover_warn} %", f"warning > {turnover_warn}%")))
    kpis.append(kpi(T("Effectif", "Headcount"), "effectif",
                    secondary=T("effectif moyen de la période", "average headcount of the period")))
    kpis.append(kpi(T("Masse salariale", "Payroll"), "masse_totale", compare="previous_period",
                    secondary=T("évolution face à la période précédente", "change versus the previous period")))
    kpis.append(kpi(T("Coût moyen par salarié", "Average cost per employee"), "cout_moyen"))

    absence_chart = VisualSpec(
        type="bar",
        title=T("Absentéisme par mois", "Absenteeism by month"),
        subtitle=T(f"Heures d'absence ÷ heures théoriques. Alerte à {absenteeism_warn} %, critique à {absenteeism_bad} %.",
                   f"Absence hours ÷ theoretical hours. Warning at {absenteeism_warn}%, critical at {absenteeism_bad}%."),
        source="rh",
        x="periode_mois",
        series=[SeriesSpec(key="absenteisme", label=T("Absentéisme", "Absenteeism"), mark="bar", use_thresholds=True)],
        reference_lines=[
            ReferenceLine(value=absenteeism_warn / 100, label=T(f"Alerte {absenteeism_warn} %", f"Warning {absenteeism_warn}%"),
                          color="warn"),
            ReferenceLine(value=absenteeism_bad / 100, label=T(f"Critique {absenteeism_bad} %", f"Critical {absenteeism_bad}%"),
                          color="bad"),
        ],
        sort="x_asc",
    )
    payroll_chart = VisualSpec(
        type="combo",
        title=T("Masse salariale et effectif", "Payroll and headcount"),
        subtitle=T("Brut chargé par mois, effectif en ligne.", "Loaded gross pay by month, headcount as a line."),
        source="rh",
        x="periode_mois",
        series=[
            SeriesSpec(key="masse_totale", label=T("Masse salariale", "Payroll"), mark="bar", color=BLUE),
            SeriesSpec(key="effectif", label=T("Effectif", "Headcount"), mark="line", color=BROWN, axis="right"),
        ],
        sort="x_asc",
    )
    charts = [payroll_chart]
    if de:
        charts.append(VisualSpec(
            type="hbar",
            title=T("Masse salariale par service", "Payroll by department"),
            subtitle=T("Cliquer pour filtrer le tableau de bord.", "Click to filter the dashboard."),
            source="rh",
            x=de,
            series=[SeriesSpec(key="masse_totale", label=T("Masse salariale", "Payroll"), mark="bar", color=BLUE)],
            sort="value_desc",
            click_filters={"service": de},
            ignore_filters=["service"],
        ))
    flow_series = []
    if hi:
        flow_series.append(SeriesSpec(key="entrees", label=T("Entrées", "Hires"), mark="bar", color=GREEN))
    if ex:
        flow_series.append(SeriesSpec(key="sorties", label=T("Sorties", "Exits"), mark="bar", color=BROWN))
    if flow_series:
        charts.append(VisualSpec(
            type="combo",
            title=T("Entrées et sorties par mois", "Hires and exits by month"),
            subtitle=T("Salariés entrés ou sortis dans le mois.", "Employees hired or leaving in the month."),
            source="rh",
            x="periode_mois",
            series=flow_series,
            sort="x_asc",
        ))
    if ar:
        charts.append(VisualSpec(
            type="hbar",
            title=T("Motifs d'absence", "Absence reasons"),
            subtitle=T("Heures d'absence par motif.", "Absence hours by reason."),
            source="rh",
            x="motif_affiche",
            series=[SeriesSpec(key="heures_absence_total", label=T("Heures d'absence", "Absence hours"), mark="bar", color=BROWN)],
            sort="value_desc",
            top_n=8,
        ))

    table = None
    if de:
        table_columns = [
            ColumnSpec(key=de, label=T("Service", "Department")),
            ColumnSpec(key="effectif", label=T("Effectif", "Headcount")),
            ColumnSpec(key="masse_totale", label=T("Masse salariale", "Payroll")),
            ColumnSpec(key="cout_moyen", label=T("Coût moyen", "Average cost")),
            ColumnSpec(key="absenteisme", label=T("Absentéisme", "Absenteeism"), use_thresholds=True),
        ]
        if ex:
            table_columns.append(ColumnSpec(key="turnover", label="Turnover", use_thresholds=True))
        table = VisualSpec(
            type="table",
            title=T("Indicateurs par service", "Indicators by department"),
            subtitle=T("La ligne « Toutes » reprend les définitions globales, pas la moyenne des services.",
                       "The \"All\" row uses the global definitions, not the average of the departments."),
            source="rh",
            x=de,
            columns=table_columns,
            total_row=True,
            click_filters={"service": de},
            ignore_filters=["service"],
            highlight_filter="service",
        )

    notes = [
        VisualSpec(
            type="note",
            title=T("Définitions", "Definitions"),
            badges=[Provenance.REAL],
            text=T(
                "**Absentéisme** : heures d'absence ÷ heures théoriques. **Effectif** : salariés présents dans le mois "
                "(moyenne sur la période). **Turnover** : départs de la période ÷ effectif moyen. **Coût moyen** : "
                "masse salariale ÷ nombre de salariés-mois.",
                "**Absenteeism**: absence hours ÷ theoretical hours. **Headcount**: employees present in the month "
                "(average over the period). **Turnover**: leavers of the period ÷ average headcount. **Average "
                "cost**: payroll ÷ number of employee-months.",
            ),
        ),
        VisualSpec(
            type="note",
            title=T("Seuils", "Thresholds"),
            text=T(
                f"Absentéisme : vert jusqu'à {absenteeism_warn} %, orange jusqu'à {absenteeism_bad} %, rouge au-delà. "
                f"Turnover : vert jusqu'à {turnover_warn} %, orange jusqu'à {turnover_bad} %, rouge au-delà, sur la "
                "période affichée (12 mois par défaut). Réglables à la création du tableau de bord.",
                f"Absenteeism: green up to {absenteeism_warn}%, orange up to {absenteeism_bad}%, red beyond. "
                f"Turnover: green up to {turnover_warn}%, orange up to {turnover_bad}%, red beyond, over the "
                "period shown (12 months by default). Adjustable when the dashboard is created.",
            ),
        ),
    ]

    # ---- grid --------------------------------------------------------
    grid: list[tuple[VisualSpec, dict[str, int]]] = [(synthese, dict(row=0, col=0, width=12, height=2))]
    col = 0
    for tile, width in zip(kpis, _spread(len(kpis))):
        grid.append((tile, dict(row=2, col=col, width=width, height=3)))
        col += width
    grid.append((absence_chart, dict(row=5, col=0, width=12, height=6)))
    placed, row = _flow(charts, 11, 6)
    grid += placed
    if table is not None:
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


def build(dataset_id: str, columns: dict, params: dict, lang: str = DEFAULT_LANG) -> dict:
    """Catalog entry point: column mapping (blank optional → None) and parameters (defaults filled in)."""
    mapping: dict[str, Any] = {key: columns.get(key) or "" for key in _REQUIRED}
    mapping.update({key: columns.get(key) or None for key in _OPTIONAL})
    values = {key: (params or {}).get(key) if (params or {}).get(key) is not None else default
              for key, default in _PARAM_DEFAULTS.items()}
    return hr_people(
        dataset_id,
        month=mapping["mois"],
        employee=mapping["salarie"],
        department=mapping["service"],
        theoretical_hours=mapping["heures_theoriques"],
        absence_hours=mapping["heures_absence"],
        payroll=mapping["masse_salariale"],
        hire_date=mapping["entree"],
        exit_date=mapping["sortie"],
        absence_reason=mapping["motif_absence"],
        lang=lang,
        **values,
    )
