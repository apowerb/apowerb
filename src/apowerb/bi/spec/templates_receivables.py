"""
bi/spec/templates_receivables.py
--------------------------------
Spec v2 template « Balance âgée clients »: one row per customer invoice.

The reference date is the last invoice date of the dataset (not today), like the OTIF
template. An invoice is *overdue* when its due date precedes it; the remaining amount is
the "Reste dû" column, else the amount minus the "Montant réglé" column, else the full
amount of the invoices without a payment date, else the full amount.

DSO = outstanding amount / invoicing of the 90 days up to the reference date x 90.
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

OVERDUE_GOOD = 0.10
OVERDUE_WARN = 0.25
DSO_GOOD = 45.0
DSO_WARN = 60.0

BUCKETS = ("1. Non échu", "2. 0-30 j", "3. 31-60 j", "4. 61-90 j", "5. > 90 j")

DESCRIPTOR: dict[str, Any] = {
    "id": "receivables_aging",
    "label": "Balance âgée clients",
    "description": (
        "Suivi des créances clients à partir d'une ligne par facture : encours total et échu, part échue, DSO, "
        "répartition par tranche d'ancienneté (non échu, 0-30, 31-60, 61-90 et plus de 90 jours après l'échéance), "
        "clients les plus en retard et facturation mensuelle. Le retard se mesure à la dernière date de facture du "
        "fichier (et non à la date du jour). Le DSO est l'encours divisé par la facturation des 90 derniers jours, "
        "multiplié par 90. Le reste dû vient de la colonne Reste dû, sinon du Montant réglé, sinon de la Date de "
        "paiement ; sans aucune des trois, toutes les factures sont considérées comme ouvertes."
    ),
    "version": 1,
    "columns": [
        {"key": "customer", "label": "Client", "type": "string", "required": True, "default": "client"},
        {"key": "invoice", "label": "Facture", "type": "string", "required": False, "default": None},
        {"key": "invoice_date", "label": "Date de facture", "type": "date", "required": True,
         "default": "date_facture"},
        {"key": "due_date", "label": "Date d'échéance", "type": "date", "required": True, "default": "date_echeance"},
        {"key": "amount", "label": "Montant", "type": "number", "required": True, "default": "montant"},
        {"key": "open_amount", "label": "Reste dû", "type": "number", "required": False, "default": None},
        {"key": "paid_amount", "label": "Montant réglé", "type": "number", "required": False, "default": None},
        {"key": "payment_date", "label": "Date de paiement", "type": "date", "required": False, "default": None},
    ],
    "params": [],
}
_REQUIRED = [c["key"] for c in DESCRIPTOR["columns"] if c["required"]]
_OPTIONAL = [c["key"] for c in DESCRIPTOR["columns"] if not c["required"]]


def receivables_aging(
    dataset_id: str,
    *,
    customer: str = "client",
    invoice_date: str = "date_facture",
    due_date: str = "date_echeance",
    amount: str = "montant",
    invoice: str | None = None,
    open_amount: str | None = None,
    paid_amount: str | None = None,
    payment_date: str | None = None,
    default_period: str = "all",
) -> dict:
    """Build the « Balance âgée clients » spec for a dataset of customer invoices."""
    if not dataset_id:
        raise ValueError("dataset_id is required.")
    cu = check_ident("customer", customer)
    idate = check_ident("invoice_date", invoice_date)
    ddate = check_ident("due_date", due_date)
    am = check_ident("amount", amount)
    inv = check_ident("invoice", invoice) if invoice else None
    op = check_ident("open_amount", open_amount) if open_amount else None
    pa = check_ident("paid_amount", paid_amount) if paid_amount else None
    pd_ = check_ident("payment_date", payment_date) if payment_date else None

    INV, D, A = quote(idate), quote(ddate), quote(am)
    day_i, day_d = f"CAST({INV} AS DATE)", f"CAST({D} AS DATE)"
    as_of = f"CAST(MAX({INV}) OVER () AS DATE)"

    if op:
        remaining = f"COALESCE({quote(op)}, 0)"
    elif pa:
        remaining = f"GREATEST({A} - COALESCE({quote(pa)}, 0), 0)"
    elif pd_:
        remaining = f"CASE WHEN {quote(pd_)} IS NULL THEN {A} ELSE 0 END"
    else:
        remaining = A

    # ---- transforms --------------------------------------------------
    transforms = [
        DeriveStep(column="reste_du", expr=f"COALESCE({remaining}, 0)"),
        DeriveStep(
            column="jours_retard",
            expr=f"CASE WHEN reste_du > 0 AND {D} IS NOT NULL AND {day_d} < {as_of} "
            f"THEN date_diff('day', {day_d}, {as_of}) END",
        ),
        DeriveStep(column="reste_echu", expr="CASE WHEN jours_retard IS NOT NULL THEN reste_du END"),
        DeriveStep(
            column="tranche_anciennete",
            expr=(
                f"CASE WHEN reste_du > 0 AND {D} IS NOT NULL THEN "
                f"CASE WHEN jours_retard IS NULL THEN '{BUCKETS[0]}' WHEN jours_retard <= 30 THEN '{BUCKETS[1]}' "
                f"WHEN jours_retard <= 60 THEN '{BUCKETS[2]}' WHEN jours_retard <= 90 THEN '{BUCKETS[3]}' "
                f"ELSE '{BUCKETS[4]}' END END"
            ),
        ),
        DeriveStep(column="ca_90j", expr=f"CASE WHEN date_diff('day', {day_i}, {as_of}) < 90 THEN {A} END"),
        DeriveStep(
            column="periode_mois",
            expr=f"date_trunc('month', CAST({INV} AS TIMESTAMP))",
            provenance=Provenance.REAL,
        ),
    ]
    if pd_:
        transforms.append(
            DeriveStep(
                column="delai_reglement_jours",
                expr=f"CASE WHEN {quote(pd_)} IS NOT NULL THEN date_diff('day', {day_i}, CAST({quote(pd_)} AS DATE)) END",
            )
        )

    # ---- fields ------------------------------------------------------
    fields = [
        FieldSpec(name=cu, label="Client", role="dimension"),
        FieldSpec(name=idate, label="Date de facture", type="date", role="date"),
        FieldSpec(name=ddate, label="Date d'échéance", type="date", role="date"),
        FieldSpec(name=am, label="Montant", type="number", role="measure", format="compact"),
    ]
    if inv:
        fields.append(FieldSpec(name=inv, label="Facture", role="dimension"))
    if op:
        fields.append(FieldSpec(name=op, label="Reste dû", type="number", role="measure", format="compact"))
    if pa:
        fields.append(FieldSpec(name=pa, label="Montant réglé", type="number", role="measure", format="compact"))
    if pd_:
        fields.append(FieldSpec(name=pd_, label="Date de paiement", type="date", role="date"))
    fields += [
        FieldSpec(name="reste_du", label="Reste dû", type="number", role="measure", format="compact"),
        FieldSpec(name="jours_retard", label="Jours de retard", type="number", role="measure", format="integer"),
        FieldSpec(name="reste_echu", label="Reste dû échu", type="number", role="measure", format="compact"),
        FieldSpec(name="tranche_anciennete", label="Tranche d'ancienneté", role="dimension"),
        FieldSpec(name="ca_90j", label="Facturé sur 90 jours", type="number", role="measure", format="compact"),
        FieldSpec(name="periode_mois", label="Mois", type="date", role="dimension"),
    ]
    if pd_:
        fields.append(
            FieldSpec(name="delai_reglement_jours", label="Délai de règlement (jours)", type="number", role="measure",
                      format="decimal1")
        )

    # ---- measures ----------------------------------------------------
    overdue_thr = ThresholdSpec(direction="lower_is_better", good=OVERDUE_GOOD, warn=OVERDUE_WARN)
    dso_thr = ThresholdSpec(direction="lower_is_better", good=DSO_GOOD, warn=DSO_WARN)
    measures = [
        MeasureSpec(name="factures", label="Factures", expr="COUNT(*)", format="integer", unit="factures"),
        MeasureSpec(name="ca_facture", label="Facturation", expr=f"SUM({A})",
                    format="compact"),
        MeasureSpec(name="encours", label="Encours total", expr="COALESCE(SUM(reste_du), 0)", format="compact",
                    description="Reste dû sur l'ensemble des factures, échues ou non."),
        MeasureSpec(name="encours_echu", label="Encours échu", expr="COALESCE(SUM(reste_echu), 0)", format="compact",
                    description="Reste dû des factures dont l'échéance précède la dernière date de facture du fichier."),
        MeasureSpec(name="pct_echu", label="% échu", expr="COALESCE(SUM(reste_echu), 0) / NULLIF(SUM(reste_du), 0)",
                    format="percent", thresholds=overdue_thr, description="Encours échu sur encours total."),
        MeasureSpec(name="dso", label="DSO", expr="SUM(reste_du) / NULLIF(SUM(ca_90j), 0) * 90", format="decimal1",
                    unit="jours", thresholds=dso_thr,
                    description="Encours divisé par la facturation des 90 derniers jours (jusqu'à la dernière date de "
                    "facture du fichier), multiplié par 90."),
        MeasureSpec(name="retard_moyen", label="Retard moyen", expr="AVG(jours_retard)", format="decimal1",
                    unit="jours", description="Jours de retard moyens des factures échues non soldées."),
        MeasureSpec(name="retard_max", label="Retard maximal", expr="MAX(jours_retard)", format="integer",
                    unit="jours"),
    ]
    if pd_:
        measures.append(
            MeasureSpec(name="delai_reglement", label="Délai de règlement moyen", expr="AVG(delai_reglement_jours)",
                        format="decimal1", unit="jours",
                        description="Jours entre la date de facture et la date de paiement, factures payées.")
        )
    source = SourceSpec(
        name="creances",
        label="Factures clients",
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
    filters = [
        FilterSpec(id="periode", label="Période (date de facture)", field=idate, type="relative_period",
                   default=default_period, presets=presets),
        FilterSpec(id="client", label="Client", field=cu, type="select", default=None),
        FilterSpec(id="tranche", label="Ancienneté", field="tranche_anciennete", type="select", default=None),
    ]

    # ---- visuals -----------------------------------------------------
    def kpi(title: str, measure: str, sparkline: bool = True, **kw: Any) -> VisualSpec:
        return VisualSpec(type="kpi", title=title, source="creances", x="periode_mois",
                          kpi=KpiSpec(measure=measure, sparkline=measure if sparkline else None, **kw))

    synthese = VisualSpec(
        type="text",
        source="creances",
        text=(
            "{{filters.client}}, du {{period.from}} au {{period.to}} : {{factures|integer}} factures pour "
            "{{ca_facture|compact}}. Encours de {{encours|compact}}, dont {{encours_echu|compact}} échu "
            "({{pct_echu|percent}}). DSO de {{dso|decimal1}} jours."
        ),
    )
    kpis = [
        kpi("Encours total", "encours"),
        kpi("Encours échu", "encours_echu"),
        kpi("% échu", "pct_echu", target_label="cible ≤ 10 %"),
        kpi("DSO", "dso", sparkline=False, target_label="cible ≤ 45 jours"),
    ]
    if pd_:
        kpis.append(kpi("Délai de règlement moyen", "delai_reglement", secondary="entre facture et paiement"))
    facturation = VisualSpec(
        type="combo",
        title="Facturation mensuelle",
        subtitle="Facturation du mois et part encore due sur ces factures.",
        source="creances",
        x="periode_mois",
        series=[
            SeriesSpec(key="ca_facture", label="Facturation", mark="bar", color=BLUE),
            SeriesSpec(key="encours", label="Encore dû", mark="line", color=BROWN),
        ],
        sort="x_asc",
    )
    anciennete = VisualSpec(
        type="bar",
        title="Encours par ancienneté",
        subtitle="Reste dû par jours de retard après l'échéance. Cliquer pour filtrer le tableau de bord.",
        source="creances",
        x="tranche_anciennete",
        series=[SeriesSpec(key="encours", label="Encours", mark="bar", color=BROWN)],
        sort="x_asc",
        click_filters={"tranche": "tranche_anciennete"},
        ignore_filters=["tranche"],
    )
    retardataires = VisualSpec(
        type="hbar",
        title="Clients les plus en retard",
        subtitle="Dix plus gros encours échus. Cliquer pour filtrer le tableau de bord.",
        source="creances",
        x=cu,
        series=[SeriesSpec(key="encours_echu", label="Encours échu", mark="bar", color=BROWN)],
        sort="value_desc",
        top_n=10,
        click_filters={"client": cu},
        ignore_filters=["client"],
    )
    table = VisualSpec(
        type="table",
        title="Encours par client",
        subtitle="La ligne « Toutes » reprend les définitions globales, pas la moyenne des clients.",
        source="creances",
        x=cu,
        columns=[
            ColumnSpec(key=cu, label="Client"),
            ColumnSpec(key="encours", label="Encours"),
            ColumnSpec(key="encours_echu", label="Encours échu"),
            ColumnSpec(key="pct_echu", label="% échu", use_thresholds=True),
            ColumnSpec(key="retard_moyen", label="Retard moyen"),
            ColumnSpec(key="factures", label="Factures"),
        ],
        sort="value_desc",
        total_row=True,
        click_filters={"client": cu},
        ignore_filters=["client"],
        highlight_filter="client",
    )
    plus_grosses = None
    if inv:
        plus_grosses = VisualSpec(
            type="table",
            title="Plus grosses factures échues",
            subtitle="Dix plus gros restes dus échus.",
            source="creances",
            x=inv,
            columns=[
                ColumnSpec(key=inv, label="Facture"),
                ColumnSpec(key="encours_echu", label="Reste dû échu"),
                ColumnSpec(key=cu, label="Client"),
                ColumnSpec(key="retard_max", label="Retard (jours)"),
            ],
            sort="value_desc",
            top_n=10,
        )
    notes = [
        VisualSpec(
            type="note",
            title="Définitions",
            badges=[Provenance.REAL],
            text=(
                "**Échu** : facture non soldée dont l'échéance précède la date de référence. **Tranches** : jours de "
                "retard après l'échéance (non échu, 0-30, 31-60, 61-90, plus de 90). **% échu** : encours échu sur "
                "encours total. La période se mesure sur la date de facture."
            ),
        ),
        VisualSpec(
            type="note",
            title="Date de référence et DSO",
            text=(
                "La date de référence est la dernière date de facture du fichier (et non la date du jour). Le DSO est "
                "l'encours divisé par la facturation des 90 derniers jours jusqu'à cette date, multiplié par 90 ; il "
                "suit les filtres actifs. Vert sous 45 jours, orange jusqu'à 60, rouge au-delà."
            ),
        ),
    ]

    # ---- grid --------------------------------------------------------
    grid: Placed = [(synthese, dict(row=0, col=0, width=12, height=2))]
    grid += tile_row(kpis, 2, 3)
    grid.append((facturation, dict(row=5, col=0, width=12, height=6)))
    placed, row = flow([anciennete, retardataires], 11, 6)
    grid += placed
    grid.append((table, dict(row=row, col=0, width=12, height=6)))
    row += 6
    if plus_grosses is not None:
        grid.append((plus_grosses, dict(row=row, col=0, width=12, height=6)))
        row += 6
    grid += tile_row(notes, row, 4)
    return assemble([source], filters, grid)


def build(dataset_id: str, columns: dict, params: dict) -> dict:
    """Catalog entry point: column mapping (blank optional → None) to the template."""
    mapping: dict[str, Any] = {key: columns.get(key) or "" for key in _REQUIRED}
    mapping.update({key: columns.get(key) or None for key in _OPTIONAL})
    return receivables_aging(dataset_id, **mapping)
