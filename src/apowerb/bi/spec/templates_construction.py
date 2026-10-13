"""
bi/spec/templates_construction.py
---------------------------------
Spec v2 template « Chantiers BTP »: one row per site and per month (situation de
travaux). Costs incurred, progress and invoiced amounts are cumulated.

The KPIs, the table and the margin chart read the latest situation of each site
(never a sum over months, the amounts being cumulated); the drift chart reads
every month. For a situation with a progress above 0:

* cost at completion = cost incurred ÷ progress
* estimated margin = sale budget - cost at completion
* budgeted margin = sale budget - cost budget; gap = estimated - budgeted margin
* cost drift = cost at completion - cost budget (in % of the cost budget)
* billing gap = invoiced to date - progress x sale budget (positive: invoiced
  ahead of the work, negative: billing late)

A site with no progress yet cannot be estimated: it is listed but left out of
the margin and drift totals. Progress is read as a fraction, or as a percentage
when any value of the dataset exceeds 1.
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

BROWN = "#9a6b3f"
BLUE = "#3b6fd8"

DRIFT_THRESHOLD = 5

DESCRIPTOR: dict[str, Any] = {
    "id": "construction_sites",
    "label": {"fr": "Chantiers BTP", "en": "Construction sites"},
    "description": {
        "fr": (
            "Suivi de la rentabilité des chantiers à partir d'une situation par chantier et par mois (montants "
            "cumulés) : marge à terminaison estimée contre marge budgétée, dérive des coûts, décalage entre "
            "facturation et avancement (surfacturé ou en retard de facturation) et chantiers en dérive au-delà d'un "
            "seuil réglable."
        ),
        "en": (
            "Construction site profitability tracking from one progress statement per site and per month "
            "(cumulative amounts): estimated margin at completion against budgeted margin, cost drift, gap "
            "between billing and progress (overbilled or billing late) and sites drifting beyond an "
            "adjustable threshold."
        ),
    },
    "version": 1,
    "columns": [
        {"key": "month", "label": {"fr": "Mois de la situation", "en": "Statement month"}, "type": "date", "required": True, "default": "mois"},
        {"key": "site", "label": {"fr": "Chantier", "en": "Site"}, "type": "string", "required": True, "default": "chantier"},
        {"key": "sale_budget", "label": {"fr": "Budget de vente", "en": "Sales budget"}, "type": "number", "required": True,
         "default": "budget_vente"},
        {"key": "cost_budget", "label": {"fr": "Budget de coût", "en": "Cost budget"}, "type": "number", "required": True,
         "default": "budget_cout"},
        {"key": "cost_incurred", "label": {"fr": "Coût engagé (cumul)", "en": "Cost incurred (cumulative)"}, "type": "number", "required": True,
         "default": "cout_engage"},
        {"key": "progress", "label": {"fr": "Avancement (0-1 ou %)", "en": "Progress (0-1 or %)"}, "type": "number", "required": True,
         "default": "avancement"},
        {"key": "billed_cumul", "label": {"fr": "Facturé (cumul)", "en": "Invoiced (cumulative)"}, "type": "number", "required": True,
         "default": "facture_cumul"},
        {"key": "client", "label": {"fr": "Client", "en": "Customer"}, "type": "string", "required": False, "default": None},
    ],
    "params": [
        {"key": "drift_threshold", "label": {"fr": "Seuil de dérive des coûts (% du budget de coût)",
                                         "en": "Cost drift threshold (% of cost budget)"}, "type": "integer",
         "default": DRIFT_THRESHOLD, "min": 1, "max": 100},
    ],
}
_REQUIRED = [c["key"] for c in DESCRIPTOR["columns"] if c["required"]]
_OPTIONAL = [c["key"] for c in DESCRIPTOR["columns"] if not c["required"]]
_PARAMS = {p["key"]: p for p in DESCRIPTOR["params"]}


def _check_ident(label: str, value: str) -> str:
    if not _IDENT_RE.match(value or ""):
        raise ValueError(
            f"{label}='{value}' is not a valid column name: use lowercase letters, "
            "digits and underscores (start with a letter), e.g. 'cout_engage'."
        )
    return value


def _check_params(params: dict[str, Any]) -> dict[str, int]:
    """Parameters with defaults filled in; ValueError when out of range."""
    clean: dict[str, int] = {}
    for key, spec in _PARAMS.items():
        value = params.get(key)
        value = spec["default"] if value is None else value
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"Parameter '{key}' must be an integer.")
        if not spec["min"] <= value <= spec["max"]:
            raise ValueError(f"Parameter '{key}' must be between {spec['min']} and {spec['max']}.")
        clean[key] = value
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


def construction_sites(
    dataset_id: str,
    *,
    month: str = "mois",
    site: str = "chantier",
    sale_budget: str = "budget_vente",
    cost_budget: str = "budget_cout",
    cost_incurred: str = "cout_engage",
    progress: str = "avancement",
    billed_cumul: str = "facture_cumul",
    client: str | None = None,
    drift_threshold: int = DRIFT_THRESHOLD,
    default_period: str = "12m",
    lang: str = DEFAULT_LANG,
) -> dict:
    """Build the « Chantiers BTP » spec for a dataset of monthly situations, in ``lang``."""
    T = translator(lang)
    if not dataset_id:
        raise ValueError("dataset_id is required.")
    m = _check_ident("month", month)
    st = _check_ident("site", site)
    sv = _check_ident("sale_budget", sale_budget)
    cb = _check_ident("cost_budget", cost_budget)
    ci = _check_ident("cost_incurred", cost_incurred)
    pr = _check_ident("progress", progress)
    bc = _check_ident("billed_cumul", billed_cumul)
    cl = _check_ident("client", client) if client else None

    bucket = "periode_mois"
    SV, CB, CI, PR, BC, ST = (_q(c) for c in (sv, cb, ci, pr, bc, st))
    drift = drift_threshold / 100

    # ---- transforms --------------------------------------------------
    transforms = [
        DeriveStep(column=bucket, expr=f"date_trunc('month', CAST({_q(m)} AS TIMESTAMP))",
                   provenance=Provenance.REAL),
        DeriveStep(
            column="avancement_n",
            expr=(f"CASE WHEN {PR} IS NULL THEN NULL ELSE LEAST(1, GREATEST(0, "
                  f"CASE WHEN MAX(CAST({PR} AS DOUBLE)) OVER () > 1 THEN CAST({PR} AS DOUBLE) / 100.0 "
                  f"ELSE CAST({PR} AS DOUBLE) END)) END"),
        ),
        DeriveStep(
            column="ligne_estimable",
            expr=(f"CASE WHEN avancement_n > 0 AND {CI} IS NOT NULL AND {SV} IS NOT NULL AND {CB} IS NOT NULL "
                  "THEN 1 ELSE 0 END"),
        ),
        DeriveStep(column="cout_terminaison", expr=f"CASE WHEN ligne_estimable = 1 THEN {CI} / avancement_n END"),
        DeriveStep(column="marge_budget_ligne", expr=f"{SV} - {CB}"),
        DeriveStep(column="marge_estimee_ligne",
                   expr=f"CASE WHEN ligne_estimable = 1 THEN {SV} - cout_terminaison END"),
        DeriveStep(column="derive_cout_ligne", expr=f"CASE WHEN ligne_estimable = 1 THEN cout_terminaison - {CB} END"),
        DeriveStep(column="derive_pct_ligne", expr=f"ROUND(derive_cout_ligne / NULLIF({CB}, 0), 6)"),
        DeriveStep(
            column="decalage_ligne",
            expr=f"CASE WHEN {BC} IS NOT NULL AND avancement_n IS NOT NULL AND {SV} IS NOT NULL "
                 f"THEN {BC} - avancement_n * {SV} END",
        ),
        DeriveStep(
            column="derniere_situation",
            expr=f"CASE WHEN ROW_NUMBER() OVER (PARTITION BY {ST} ORDER BY {bucket} DESC NULLS LAST) = 1 "
                 "THEN 1 ELSE 0 END",
        ),
        DeriveStep(column="ligne_en_derive",
                   expr=f"CASE WHEN ligne_estimable = 1 AND derive_pct_ligne > {drift} THEN 1 ELSE 0 END"),
        DeriveStep(
            column="statut_chantier",
            expr=(f"CASE WHEN ligne_estimable = 0 OR derive_pct_ligne IS NULL THEN '{T('Non évaluable', 'Not assessable')}' "
                  f"WHEN derive_pct_ligne > {drift} THEN '{T('En dérive', 'Drifting')}' "
                  f"WHEN derive_pct_ligne > 0 THEN '{T('À surveiller', 'To watch')}' "
                  f"ELSE '{T('Maîtrisé', 'Under control')}' END"),
        ),
    ]

    # ---- fields ------------------------------------------------------
    fields = [
        FieldSpec(name=m, label=T("Mois de la situation", "Statement month"), type="date", role="date"),
        FieldSpec(name=st, label=T("Chantier", "Site"), role="dimension"),
        FieldSpec(name=sv, label=T("Budget de vente", "Sales budget"), type="number", role="measure", format="compact"),
        FieldSpec(name=cb, label=T("Budget de coût", "Cost budget"), type="number", role="measure", format="compact"),
        FieldSpec(name=ci, label=T("Coût engagé (cumul)", "Cost incurred (cumulative)"), type="number", role="measure", format="compact"),
        FieldSpec(name=pr, label=T("Avancement", "Progress"), type="number", role="measure", format="number"),
        FieldSpec(name=bc, label=T("Facturé (cumul)", "Invoiced (cumulative)"), type="number", role="measure", format="compact"),
    ]
    if cl:
        fields.append(FieldSpec(name=cl, label=T("Client", "Customer"), role="dimension"))
    fields += [
        FieldSpec(name=bucket, label=T("Mois", "Month"), type="date", role="dimension"),
        FieldSpec(name="avancement_n", label=T("Avancement (0-1)", "Progress (0-1)"), type="number", role="measure", format="percent"),
        FieldSpec(name="ligne_estimable", label=T("Situation estimable", "Assessable statement"), type="number", role="measure",
                  format="integer"),
        FieldSpec(name="cout_terminaison", label=T("Coût à terminaison", "Cost at completion"), type="number", role="measure",
                  format="compact"),
        FieldSpec(name="marge_budget_ligne", label=T("Marge budgétée", "Budgeted margin"), type="number", role="measure",
                  format="compact"),
        FieldSpec(name="marge_estimee_ligne", label=T("Marge estimée", "Estimated margin"), type="number", role="measure",
                  format="compact"),
        FieldSpec(name="derive_cout_ligne", label=T("Dérive de coût", "Cost drift"), type="number", role="measure", format="compact"),
        FieldSpec(name="derive_pct_ligne", label=T("Dérive de coût (%)", "Cost drift (%)"), type="number", role="measure",
                  format="percent"),
        FieldSpec(name="decalage_ligne", label=T("Décalage facturation / avancement", "Billing vs progress gap"), type="number", role="measure",
                  format="compact"),
        FieldSpec(name="derniere_situation", label=T("Dernière situation du chantier", "Latest site statement"), type="number", role="measure",
                  format="integer"),
        FieldSpec(name="ligne_en_derive", label=T("Situation en dérive", "Drifting statement"), type="number", role="measure",
                  format="integer"),
        FieldSpec(name="statut_chantier", label=T("Statut", "Status"), role="dimension"),
    ]

    # ---- measures ----------------------------------------------------
    last = "derniere_situation = 1"
    est = f"{last} AND ligne_estimable = 1"

    def latest(column: str, cond: str = last) -> str:
        return f"SUM(CASE WHEN {cond} THEN {column} END)"

    drift_cost = latest("derive_cout_ligne", est)
    cost_base = latest(CB, est)
    drift_thr = ThresholdSpec(direction="lower_is_better", good=0, warn=drift)
    gap_thr = ThresholdSpec(direction="higher_is_better", good=0, warn=0)
    measures = [
        MeasureSpec(name="chantiers", label=T("Chantiers suivis", "Sites tracked"), expr="SUM(CASE WHEN derniere_situation = 1 THEN 1 END)",
                    format="integer", unit=T("chantiers", "sites")),
        MeasureSpec(name="carnet", label=T("Budget de vente", "Sales budget"), expr=latest(SV), format="compact", unit="€",
                    description=T("Budget de vente des chantiers (dernière situation de chacun).",
                                  "Sales budget of the sites (latest statement of each).")),
        MeasureSpec(name="avancement_actuel", label=T("Avancement", "Progress"), expr="MAX(CASE WHEN derniere_situation = 1 "
                    "THEN avancement_n END)", format="percent", description=T("Avancement de la dernière situation.", "Progress of the latest statement.")),
        MeasureSpec(name="marge_budget", label=T("Marge budgétée", "Budgeted margin"), expr=latest("marge_budget_ligne", est),
                    format="compact", unit="€",
                    description=T("Budget de vente - budget de coût, sur les chantiers estimables.",
                                  "Sales budget - cost budget, on assessable sites.")),
        MeasureSpec(name="marge_estimee", label=T("Marge à terminaison estimée", "Estimated margin at completion"),
                    expr=latest("marge_estimee_ligne", est), format="compact", unit="€",
                    description=T("Budget de vente - coût engagé ÷ avancement, dernière situation de chaque chantier.",
                                  "Sales budget - cost incurred ÷ progress, latest statement of each site.")),
        MeasureSpec(name="ecart_marge", label=T("Écart vs marge budgétée", "Gap vs budgeted margin"),
                    expr=latest("marge_estimee_ligne - marge_budget_ligne", est), format="compact", unit="€",
                    thresholds=gap_thr, description=T("Marge estimée - marge budgétée (négatif : marge érodée).",
                                                      "Estimated margin - budgeted margin (negative: eroded margin).")),
        MeasureSpec(name="derive_cout", label=T("Dérive de coûts", "Cost drift"), expr=drift_cost, format="compact", unit="€",
                    description=T("Coût à terminaison estimé - budget de coût.", "Estimated cost at completion - cost budget.")),
        MeasureSpec(name="derive_pct", label=T("Dérive de coûts (%)", "Cost drift (%)"), expr=f"{drift_cost} / NULLIF({cost_base}, 0)",
                    format="percent", thresholds=drift_thr,
                    description=T("Dérive de coûts ÷ budget de coût, sur les chantiers estimables.",
                                  "Cost drift ÷ cost budget, on assessable sites.")),
        MeasureSpec(name="surfacturation", label=T("Surfacturé", "Overbilled"),
                    expr=f"COALESCE({latest('decalage_ligne', f'{last} AND decalage_ligne > 0')}, 0)",
                    format="compact", unit="€", description=T("Facturé au-delà de l'avancement.", "Invoiced beyond progress.")),
        MeasureSpec(name="retard_facturation", label=T("En retard de facturation", "Billing behind"),
                    expr=f"COALESCE(-{latest('decalage_ligne', f'{last} AND decalage_ligne < 0')}, 0)",
                    format="compact", unit="€", description=T("Avancement non encore facturé.", "Progress not yet invoiced.")),
        MeasureSpec(name="decalage_net", label=T("Décalage facturation / avancement", "Billing vs progress gap"),
                    expr=latest("decalage_ligne"), format="compact", unit="€",
                    description=T("Facturé cumulé - avancement x budget de vente (positif : surfacturé).",
                                  "Cumulative invoiced - progress x sales budget (positive: overbilled).")),
        MeasureSpec(name="chantiers_derive", label=T("Chantiers en dérive", "Drifting sites"),
                    expr=f"SUM(CASE WHEN {last} AND ligne_en_derive = 1 THEN 1 ELSE 0 END)", format="integer",
                    unit=T("chantiers", "sites"),
                    description=T(f"Chantiers dont la dérive de coûts dépasse {drift_threshold} %.",
                                  f"Sites whose cost drift exceeds {drift_threshold}%.")),
        MeasureSpec(name="statut", label=T("Statut", "Status"), expr=f"MAX(CASE WHEN {last} THEN statut_chantier END)"),
        MeasureSpec(
            name="derive_pct_mois", label=T("Dérive de coûts (%) du mois", "Monthly cost drift (%)"),
            expr=("SUM(CASE WHEN ligne_estimable = 1 THEN derive_cout_ligne END) / "
                  "NULLIF(SUM(CASE WHEN ligne_estimable = 1 THEN " + CB + " END), 0)"),
            format="percent", thresholds=drift_thr,
            description=T("Dérive de coûts des situations du mois ÷ leur budget de coût.",
                            "Cost drift of the month's statements ÷ their cost budget."),
        ),
    ]
    source = SourceSpec(
        name="chantiers",
        label=T("Situations de chantier", "Site statements"),
        kind="dataset",
        dataset_id=dataset_id,
        transforms=transforms,
        fields=fields,
        measures=measures,
    )

    # ---- filters -----------------------------------------------------
    presets = ["6m", "12m", "24m", "all"]
    if default_period not in presets:
        presets.insert(0, default_period)
    filters = [
        FilterSpec(id="periode", label=T("Période", "Period"), field=m, type="relative_period", default=default_period,
                   presets=presets),
        FilterSpec(id="chantier", label=T("Chantier", "Site"), field=st, type="select", default=None),
    ]
    if cl:
        filters.append(FilterSpec(id="client", label=T("Client", "Customer"), field=cl, type="select", default=None))

    # ---- visuals -----------------------------------------------------
    def kpi(title: str, measure: str, **kw: Any) -> VisualSpec:
        return VisualSpec(type="kpi", title=title, source="chantiers", x=bucket, kpi=KpiSpec(measure=measure, **kw))

    synthese = VisualSpec(
        type="text",
        source="chantiers",
        text=T(
            (
                "{{filters.chantier}}, du {{period.from}} au {{period.to}} : {{chantiers|integer}} chantiers suivis. "
                "Marge à terminaison estimée de {{marge_estimee|compact}} € contre {{marge_budget|compact}} € budgétés "
                f"(écart de {{{{ecart_marge|compact}}}} €). Dérive de coûts de {{{{derive_pct|percent}}}} ; "
                f"{{{{chantiers_derive|integer}}}} chantiers au-delà de {drift_threshold} %."
            ),
            (
                "{{filters.chantier}}, from {{period.from}} to {{period.to}}: {{chantiers|integer}} sites tracked. "
                "Estimated margin at completion of {{marge_estimee|compact}} € against {{marge_budget|compact}} € budgeted "
                f"(gap of {{{{ecart_marge|compact}}}} €). Cost drift of {{{{derive_pct|percent}}}}; "
                f"{{{{chantiers_derive|integer}}}} sites beyond {drift_threshold}%."
            ),
        ),
    )
    kpis = [
        kpi(T("Marge à terminaison estimée", "Estimated margin at completion"), "marge_estimee",
            secondary=T("Budgétée : {{marge_budget|compact}} €", "Budgeted: {{marge_budget|compact}} €")),
        kpi(T("Écart vs marge budgétée", "Gap vs budgeted margin"), "ecart_marge",
            secondary=T("Estimée - budgétée", "Estimated - budgeted")),
        kpi(T("Dérive de coûts", "Cost drift"), "derive_pct", sparkline="derive_pct_mois",
            target_label=T(f"seuil {drift_threshold} %", f"threshold {drift_threshold}%"),
            secondary=T("{{derive_cout|compact}} € de surcoût estimé", "{{derive_cout|compact}} € estimated overrun")),
        kpi(T("Décalage facturation / avancement", "Billing vs progress gap"), "decalage_net",
            secondary=T("{{surfacturation|compact}} € surfacturé · {{retard_facturation|compact}} € en retard",
                        "{{surfacturation|compact}} € overbilled · {{retard_facturation|compact}} € behind")),
        kpi(T("Chantiers en dérive", "Drifting sites"), "chantiers_derive",
            secondary=T("Chantiers suivis : {{chantiers|integer}}", "Sites tracked: {{chantiers|integer}}")),
    ]
    margins = VisualSpec(
        type="bar",
        title=T("Marge budgétée vs estimée par chantier", "Budgeted vs estimated margin by site"),
        subtitle=T("Dernière situation de chaque chantier. Un chantier sans avancement n'est pas estimable.",
                   "Latest statement of each site. A site with no progress cannot be assessed."),
        source="chantiers",
        x=st,
        series=[
            SeriesSpec(key="marge_budget", label=T("Marge budgétée", "Budgeted margin"), mark="bar", color="muted"),
            SeriesSpec(key="marge_estimee", label=T("Marge estimée", "Estimated margin"), mark="bar", color=BLUE),
        ],
        sort="x_asc",
        click_filters={"chantier": st},
        ignore_filters=["chantier"],
    )
    drift_chart = VisualSpec(
        type="line",
        title=T("Dérive des coûts par mois", "Cost drift by month"),
        subtitle=T("Coût à terminaison estimé contre budget de coût, situations du mois.",
                   "Estimated cost at completion against cost budget, statements of the month."),
        source="chantiers",
        x=bucket,
        series=[SeriesSpec(key="derive_pct_mois", label=T("Dérive de coûts", "Cost drift"), mark="line", color=BLUE)],
        reference_lines=[
            ReferenceLine(value=0, label=T("Budget", "Budget"), color="good"),
            ReferenceLine(value=drift, label=T(f"Seuil {drift_threshold} %", f"Threshold {drift_threshold}%"), color="bad"),
        ],
        sort="x_asc",
    )
    columns = [
        ColumnSpec(key="derive_pct", label=T("Dérive de coûts", "Cost drift"), use_thresholds=True),
        ColumnSpec(key="statut", label=T("Statut", "Status")),
        ColumnSpec(key="avancement_actuel", label=T("Avancement", "Progress")),
        ColumnSpec(key="carnet", label=T("Budget de vente", "Sales budget")),
        ColumnSpec(key="marge_budget", label=T("Marge budgétée", "Budgeted margin")),
        ColumnSpec(key="marge_estimee", label=T("Marge estimée", "Estimated margin")),
        ColumnSpec(key="ecart_marge", label=T("Écart de marge", "Margin gap"), use_thresholds=True),
        ColumnSpec(key="decalage_net", label=T("Décalage facturation", "Billing gap")),
    ]
    if cl:
        columns.insert(1, ColumnSpec(key=cl, label=T("Client", "Customer")))
    table = VisualSpec(
        type="table",
        title=T("Chantiers par statut", "Sites by status"),
        subtitle=T("Dernière situation de chaque chantier, les plus en dérive d'abord. Cliquer pour filtrer.",
                   "Latest statement of each site, most drifting first. Click to filter."),
        source="chantiers",
        x=st,
        columns=[ColumnSpec(key=st, label=T("Chantier", "Site")), *columns],
        sort="value_desc",
        click_filters={"chantier": st},
        ignore_filters=["chantier"],
        highlight_filter="chantier",
    )
    notes = [
        VisualSpec(
            type="note",
            title=T("Définitions", "Definitions"),
            badges=[Provenance.REAL],
            text=T(
                (
                    "**Coût à terminaison** = coût engagé ÷ avancement. **Marge estimée** = budget de vente - coût à "
                    "terminaison ; **marge budgétée** = budget de vente - budget de coût. **Dérive** = coût à "
                    "terminaison - budget de coût. **Décalage** = facturé cumulé - avancement x budget de vente "
                    "(positif : surfacturé, négatif : retard de facturation)."
                ),
                (
                    "**Cost at completion** = cost incurred ÷ progress. **Estimated margin** = sales budget - cost at "
                    "completion; **budgeted margin** = sales budget - cost budget. **Drift** = cost at "
                    "completion - cost budget. **Gap** = cumulative invoiced - progress x sales budget "
                    "(positive: overbilled, negative: billing behind)."
                ),
            ),
        ),
        VisualSpec(
            type="note",
            title=T("Statuts", "Statuses"),
            text=T(
                (
                    f"**En dérive** : dérive de coûts au-delà de {drift_threshold} % du budget de coût (seuil réglable "
                    "à la création). **À surveiller** : dérive positive sous le seuil. **Maîtrisé** : coût à "
                    "terminaison dans le budget. **Non évaluable** : avancement à 0 %."
                ),
                (
                    f"**Drifting**: cost drift beyond {drift_threshold}% of the cost budget (threshold adjustable "
                    "at creation). **To watch**: positive drift below the threshold. **Under control**: cost at "
                    "completion within budget. **Not assessable**: progress at 0%."
                ),
            ),
        ),
        VisualSpec(
            type="note",
            title=T("Lecture", "How to read"),
            text=T(
                (
                    "Les montants étant cumulés, les indicateurs lisent la dernière situation de chaque chantier, jamais "
                    "une somme de mois. Les chantiers non évaluables sont exclus des totaux de marge et de dérive. "
                    "L'avancement est lu en fraction, ou en pourcentage si une valeur du fichier dépasse 1."
                ),
                (
                    "As amounts are cumulative, indicators read the latest statement of each site, never "
                    "a sum of months. Sites that cannot be assessed are excluded from margin and drift totals. "
                    "Progress is read as a fraction, or as a percentage if any value in the file exceeds 1."
                ),
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
    grid.append((margins, dict(row=row, col=0, width=6, height=6)))
    grid.append((drift_chart, dict(row=row, col=6, width=6, height=6)))
    row += 6
    grid.append((table, dict(row=row, col=0, width=12, height=7)))
    row += 7
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
    """Catalog entry point: column mapping (blank optional → None) to the template, in ``lang``."""
    mapping: dict[str, Any] = {key: columns.get(key) or "" for key in _REQUIRED}
    mapping.update({key: columns.get(key) or None for key in _OPTIONAL})
    return construction_sites(dataset_id, **mapping, **_check_params(params), lang=lang)
