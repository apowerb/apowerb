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

OVERDUE_GOOD = 0.10
OVERDUE_WARN = 0.25
DSO_GOOD = 45.0
DSO_WARN = 60.0

BUCKETS = ("1. Non échu", "2. 0-30 j", "3. 31-60 j", "4. 61-90 j", "5. > 90 j")
BUCKETS_EN = ("1. Not due", "2. 0-30 d", "3. 31-60 d", "4. 61-90 d", "5. > 90 d")

DESCRIPTOR: dict[str, Any] = {
    "id": "receivables_aging",
    "label": {"fr": "Balance âgée clients", "en": "Customer receivables aging"},
    "description": {
        "fr": (
            "Suivi des créances clients à partir d'une ligne par facture : encours total et échu, part échue, DSO, "
            "répartition par tranche d'ancienneté (non échu, 0-30, 31-60, 61-90 et plus de 90 jours après l'échéance), "
            "clients les plus en retard et facturation mensuelle. Le retard se mesure à la dernière date de facture du "
            "fichier (et non à la date du jour). Le DSO est l'encours divisé par la facturation des 90 derniers jours, "
            "multiplié par 90. Le reste dû vient de la colonne Reste dû, sinon du Montant réglé, sinon de la Date de "
            "paiement ; sans aucune des trois, toutes les factures sont considérées comme ouvertes."
        ),
        "en": (
            "Customer receivables tracking from one row per invoice: total and overdue outstanding, overdue share, DSO, "
            "breakdown by aging band (not due, 0-30, 31-60, 61-90 and over 90 days past due), "
            "most overdue customers and monthly billing. Lateness is measured at the last invoice date of the "
            "file (not today's date). DSO is the outstanding amount divided by the billing of the last 90 days, "
            "multiplied by 90. The open amount comes from the Open amount column, otherwise from the Paid amount, "
            "otherwise from the Payment date; with none of the three, all invoices are considered open."
        ),
    },
    "version": 1,
    "columns": [
        {"key": "customer", "label": {"fr": "Client", "en": "Customer"}, "type": "string", "required": True,
         "default": "client"},
        {"key": "invoice", "label": {"fr": "Facture", "en": "Invoice"}, "type": "string", "required": False,
         "default": None},
        {"key": "invoice_date", "label": {"fr": "Date de facture", "en": "Invoice date"}, "type": "date",
         "required": True, "default": "date_facture"},
        {"key": "due_date", "label": {"fr": "Date d'échéance", "en": "Due date"}, "type": "date", "required": True,
         "default": "date_echeance"},
        {"key": "amount", "label": {"fr": "Montant", "en": "Amount"}, "type": "number", "required": True,
         "default": "montant"},
        {"key": "open_amount", "label": {"fr": "Reste dû", "en": "Open amount"}, "type": "number",
         "required": False, "default": None},
        {"key": "paid_amount", "label": {"fr": "Montant réglé", "en": "Paid amount"}, "type": "number",
         "required": False, "default": None},
        {"key": "payment_date", "label": {"fr": "Date de paiement", "en": "Payment date"}, "type": "date",
         "required": False, "default": None},
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
    lang: str = DEFAULT_LANG,
) -> dict:
    """Build the « Balance âgée clients » spec for a dataset of customer invoices, in ``lang``."""
    T = translator(lang)
    buckets = tuple(T(fr, en) for fr, en in zip(BUCKETS, BUCKETS_EN))
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
                f"CASE WHEN jours_retard IS NULL THEN '{buckets[0]}' WHEN jours_retard <= 30 THEN '{buckets[1]}' "
                f"WHEN jours_retard <= 60 THEN '{buckets[2]}' WHEN jours_retard <= 90 THEN '{buckets[3]}' "
                f"ELSE '{buckets[4]}' END END"
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
        FieldSpec(name=cu, label=T("Client", "Customer"), role="dimension"),
        FieldSpec(name=idate, label=T("Date de facture", "Invoice date"), type="date", role="date"),
        FieldSpec(name=ddate, label=T("Date d'échéance", "Due date"), type="date", role="date"),
        FieldSpec(name=am, label=T("Montant", "Amount"), type="number", role="measure", format="compact"),
    ]
    if inv:
        fields.append(FieldSpec(name=inv, label=T("Facture", "Invoice"), role="dimension"))
    if op:
        fields.append(FieldSpec(name=op, label=T("Reste dû", "Open amount"), type="number", role="measure",
                                format="compact"))
    if pa:
        fields.append(FieldSpec(name=pa, label=T("Montant réglé", "Paid amount"), type="number", role="measure",
                                format="compact"))
    if pd_:
        fields.append(FieldSpec(name=pd_, label=T("Date de paiement", "Payment date"), type="date", role="date"))
    fields += [
        FieldSpec(name="reste_du", label=T("Reste dû", "Open amount"), type="number", role="measure",
                  format="compact"),
        FieldSpec(name="jours_retard", label=T("Jours de retard", "Days overdue"), type="number", role="measure",
                  format="integer"),
        FieldSpec(name="reste_echu", label=T("Reste dû échu", "Overdue open amount"), type="number", role="measure",
                  format="compact"),
        FieldSpec(name="tranche_anciennete", label=T("Tranche d'ancienneté", "Aging band"), role="dimension"),
        FieldSpec(name="ca_90j", label=T("Facturé sur 90 jours", "Billed over 90 days"), type="number",
                  role="measure", format="compact"),
        FieldSpec(name="periode_mois", label=T("Mois", "Month"), type="date", role="dimension"),
    ]
    if pd_:
        fields.append(
            FieldSpec(name="delai_reglement_jours", label=T("Délai de règlement (jours)", "Payment delay (days)"),
                      type="number", role="measure", format="decimal1")
        )

    # ---- measures ----------------------------------------------------
    overdue_thr = ThresholdSpec(direction="lower_is_better", good=OVERDUE_GOOD, warn=OVERDUE_WARN)
    dso_thr = ThresholdSpec(direction="lower_is_better", good=DSO_GOOD, warn=DSO_WARN)
    measures = [
        MeasureSpec(name="factures", label=T("Factures", "Invoices"), expr="COUNT(*)", format="integer",
                    unit=T("factures", "invoices")),
        MeasureSpec(name="ca_facture", label=T("Facturation", "Billing"), expr=f"SUM({A})",
                    format="compact"),
        MeasureSpec(name="encours", label=T("Encours total", "Total outstanding"),
                    expr="COALESCE(SUM(reste_du), 0)", format="compact",
                    description=T("Reste dû sur l'ensemble des factures, échues ou non.",
                                  "Open amount across all invoices, overdue or not.")),
        MeasureSpec(name="encours_echu", label=T("Encours échu", "Overdue outstanding"),
                    expr="COALESCE(SUM(reste_echu), 0)", format="compact",
                    description=T("Reste dû des factures dont l'échéance précède la dernière date de facture du fichier.",
                                  "Open amount of invoices whose due date is before the last invoice date of the file.")),
        MeasureSpec(name="pct_echu", label=T("% échu", "% overdue"),
                    expr="COALESCE(SUM(reste_echu), 0) / NULLIF(SUM(reste_du), 0)",
                    format="percent", thresholds=overdue_thr,
                    description=T("Encours échu sur encours total.", "Overdue outstanding over total outstanding.")),
        MeasureSpec(name="dso", label="DSO", expr="SUM(reste_du) / NULLIF(SUM(ca_90j), 0) * 90", format="decimal1",
                    unit=T("jours", "days"), thresholds=dso_thr,
                    description=T("Encours divisé par la facturation des 90 derniers jours (jusqu'à la dernière date de "
                                  "facture du fichier), multiplié par 90.",
                                  "Outstanding amount divided by the billing of the last 90 days (up to the last "
                                  "invoice date of the file), multiplied by 90.")),
        MeasureSpec(name="retard_moyen", label=T("Retard moyen", "Average delay"), expr="AVG(jours_retard)",
                    format="decimal1", unit=T("jours", "days"),
                    description=T("Jours de retard moyens des factures échues non soldées.",
                                  "Average days overdue of unpaid overdue invoices.")),
        MeasureSpec(name="retard_max", label=T("Retard maximal", "Maximum delay"), expr="MAX(jours_retard)",
                    format="integer", unit=T("jours", "days")),
    ]
    if pd_:
        measures.append(
            MeasureSpec(name="delai_reglement", label=T("Délai de règlement moyen", "Average payment delay"),
                        expr="AVG(delai_reglement_jours)", format="decimal1", unit=T("jours", "days"),
                        description=T("Jours entre la date de facture et la date de paiement, factures payées.",
                                      "Days between the invoice date and the payment date, paid invoices."))
        )
    source = SourceSpec(
        name="creances",
        label=T("Factures clients", "Customer invoices"),
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
        FilterSpec(id="periode", label=T("Période (date de facture)", "Period (invoice date)"), field=idate, type="relative_period",
                   default=default_period, presets=presets),
        FilterSpec(id="client", label=T("Client", "Customer"), field=cu, type="select", default=None),
        FilterSpec(id="tranche", label=T("Ancienneté", "Aging"), field="tranche_anciennete", type="select", default=None),
    ]

    # ---- visuals -----------------------------------------------------
    def kpi(title: str, measure: str, sparkline: bool = True, **kw: Any) -> VisualSpec:
        return VisualSpec(type="kpi", title=title, source="creances", x="periode_mois",
                          kpi=KpiSpec(measure=measure, sparkline=measure if sparkline else None, **kw))

    synthese = VisualSpec(
        type="text",
        source="creances",
        text=T(
            "{{filters.client}}, du {{period.from}} au {{period.to}} : {{factures|integer}} factures pour "
            "{{ca_facture|compact}}. Encours de {{encours|compact}}, dont {{encours_echu|compact}} échu "
            "({{pct_echu|percent}}). DSO de {{dso|decimal1}} jours.",
            "{{filters.client}}, {{period.from}} to {{period.to}}: {{factures|integer}} invoices for "
            "{{ca_facture|compact}}. Outstanding of {{encours|compact}}, of which {{encours_echu|compact}} overdue "
            "({{pct_echu|percent}}). DSO of {{dso|decimal1}} days.",
        ),
    )
    kpis = [
        kpi(T("Encours total", "Total outstanding"), "encours"),
        kpi(T("Encours échu", "Overdue outstanding"), "encours_echu"),
        kpi(T("% échu", "% overdue"), "pct_echu", target_label=T("cible ≤ 10 %", "target ≤ 10%")),
        kpi("DSO", "dso", sparkline=False, target_label=T("cible ≤ 45 jours", "target ≤ 45 days")),
    ]
    if pd_:
        kpis.append(kpi(T("Délai de règlement moyen", "Average payment delay"), "delai_reglement",
                        secondary=T("entre facture et paiement", "between invoice and payment")))
    facturation = VisualSpec(
        type="combo",
        title=T("Facturation mensuelle", "Monthly billing"),
        subtitle=T("Facturation du mois et part encore due sur ces factures.",
                   "Billing of the month and the share still due on these invoices."),
        source="creances",
        x="periode_mois",
        series=[
            SeriesSpec(key="ca_facture", label=T("Facturation", "Billing"), mark="bar", color=BLUE),
            SeriesSpec(key="encours", label=T("Encore dû", "Still due"), mark="line", color=BROWN),
        ],
        sort="x_asc",
    )
    anciennete = VisualSpec(
        type="bar",
        title=T("Encours par ancienneté", "Outstanding by aging"),
        subtitle=T("Reste dû par jours de retard après l'échéance. Cliquer pour filtrer le tableau de bord.",
                   "Open amount by days overdue after the due date. Click to filter the dashboard."),
        source="creances",
        x="tranche_anciennete",
        series=[SeriesSpec(key="encours", label=T("Encours", "Outstanding"), mark="bar", color=BROWN)],
        sort="x_asc",
        click_filters={"tranche": "tranche_anciennete"},
        ignore_filters=["tranche"],
    )
    retardataires = VisualSpec(
        type="hbar",
        title=T("Clients les plus en retard", "Most overdue customers"),
        subtitle=T("Dix plus gros encours échus. Cliquer pour filtrer le tableau de bord.",
                   "Ten largest overdue outstanding amounts. Click to filter the dashboard."),
        source="creances",
        x=cu,
        series=[SeriesSpec(key="encours_echu", label=T("Encours échu", "Overdue outstanding"), mark="bar",
                           color=BROWN)],
        sort="value_desc",
        top_n=10,
        click_filters={"client": cu},
        ignore_filters=["client"],
    )
    table = VisualSpec(
        type="table",
        title=T("Encours par client", "Outstanding by customer"),
        subtitle=T("La ligne « Toutes » reprend les définitions globales, pas la moyenne des clients.",
                   "The “All” row uses the overall definitions, not the average of the customers."),
        source="creances",
        x=cu,
        columns=[
            ColumnSpec(key=cu, label=T("Client", "Customer")),
            ColumnSpec(key="encours", label=T("Encours", "Outstanding")),
            ColumnSpec(key="encours_echu", label=T("Encours échu", "Overdue outstanding")),
            ColumnSpec(key="pct_echu", label=T("% échu", "% overdue"), use_thresholds=True),
            ColumnSpec(key="retard_moyen", label=T("Retard moyen", "Average delay")),
            ColumnSpec(key="factures", label=T("Factures", "Invoices")),
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
            title=T("Plus grosses factures échues", "Largest overdue invoices"),
            subtitle=T("Dix plus gros restes dus échus.", "Ten largest overdue open amounts."),
            source="creances",
            x=inv,
            columns=[
                ColumnSpec(key=inv, label=T("Facture", "Invoice")),
                ColumnSpec(key="encours_echu", label=T("Reste dû échu", "Overdue open amount")),
                ColumnSpec(key=cu, label=T("Client", "Customer")),
                ColumnSpec(key="retard_max", label=T("Retard (jours)", "Delay (days)")),
            ],
            sort="value_desc",
            top_n=10,
        )
    notes = [
        VisualSpec(
            type="note",
            title=T("Définitions", "Definitions"),
            badges=[Provenance.REAL],
            text=T(
                "**Échu** : facture non soldée dont l'échéance précède la date de référence. **Tranches** : jours de "
                "retard après l'échéance (non échu, 0-30, 31-60, 61-90, plus de 90). **% échu** : encours échu sur "
                "encours total. La période se mesure sur la date de facture.",
                "**Overdue**: unpaid invoice whose due date is before the reference date. **Bands**: days "
                "overdue after the due date (not due, 0-30, 31-60, 61-90, over 90). **% overdue**: overdue "
                "outstanding over total outstanding. The period is measured on the invoice date.",
            ),
        ),
        VisualSpec(
            type="note",
            title=T("Date de référence et DSO", "Reference date and DSO"),
            text=T(
                "La date de référence est la dernière date de facture du fichier (et non la date du jour). Le DSO est "
                "l'encours divisé par la facturation des 90 derniers jours jusqu'à cette date, multiplié par 90 ; il "
                "suit les filtres actifs. Vert sous 45 jours, orange jusqu'à 60, rouge au-delà.",
                "The reference date is the last invoice date of the file (not today's date). DSO is the "
                "outstanding amount divided by the billing of the last 90 days up to that date, multiplied by 90; it "
                "follows the active filters. Green under 45 days, orange up to 60, red beyond.",
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


def build(dataset_id: str, columns: dict, params: dict, lang: str = DEFAULT_LANG) -> dict:
    """Catalog entry point: column mapping (blank optional → None) to the template, in ``lang``."""
    mapping: dict[str, Any] = {key: columns.get(key) or "" for key in _REQUIRED}
    mapping.update({key: columns.get(key) or None for key in _OPTIONAL})
    return receivables_aging(dataset_id, **mapping, lang=lang)
