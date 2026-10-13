"""
bi/spec/templates_purchasing.py
-------------------------------
Spec v2 template « Achats et fournisseurs »: one row per purchase order or purchase invoice line.

*Dependency* is the share of the spend held by the five biggest suppliers of the filtered
period. A delivery is *on time* when received on or before the promised date; the rate and the
average delay cover the received lines and need both date columns.
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

TOP5_GOOD = 0.5
TOP5_WARN = 0.7
ON_TIME_GOOD = 0.95
ON_TIME_WARN = 0.90

DESCRIPTOR: dict[str, Any] = {
    "id": "purchasing_suppliers",
    "label": {"fr": "Achats et fournisseurs", "en": "Purchasing and suppliers"},
    "description": {
        "fr": (
            "Suivi des achats à partir d'une ligne par commande ou facture d'achat : dépenses totales et par mois, "
            "nombre de fournisseurs, dépendance aux cinq premiers fournisseurs, dépenses par fournisseur et par article. "
            "Avec la quantité, le prix moyen par article ; avec les dates promise et de réception, le taux de livraison "
            "à l'heure et le retard moyen."
        ),
        "en": (
            "Purchasing tracking from one row per purchase order or purchase invoice line: total spend and spend "
            "by month, number of suppliers, dependence on the top five suppliers, spend by supplier and by item. "
            "With the quantity, the average price per item; with the promised and received dates, the on-time "
            "delivery rate and the average delay."
        ),
    },
    "version": 1,
    "columns": [
        {"key": "date", "label": {"fr": "Date", "en": "Date"}, "type": "date", "required": True, "default": "date"},
        {"key": "supplier", "label": {"fr": "Fournisseur", "en": "Supplier"}, "type": "string", "required": True, "default": "fournisseur"},
        {"key": "item", "label": {"fr": "Article ou famille", "en": "Item or family"}, "type": "string", "required": True, "default": "article"},
        {"key": "quantity", "label": {"fr": "Quantité", "en": "Quantity"}, "type": "number", "required": False, "default": None},
        {"key": "amount", "label": {"fr": "Montant", "en": "Amount"}, "type": "number", "required": True, "default": "montant"},
        {"key": "promised_date", "label": {"fr": "Date promise", "en": "Promised date"}, "type": "date", "required": False, "default": None},
        {"key": "received_date", "label": {"fr": "Date de réception", "en": "Received date"}, "type": "date", "required": False, "default": None},
    ],
    "params": [],
}
_REQUIRED = [c["key"] for c in DESCRIPTOR["columns"] if c["required"]]
_OPTIONAL = [c["key"] for c in DESCRIPTOR["columns"] if not c["required"]]


def purchasing_suppliers(
    dataset_id: str,
    *,
    date: str = "date",
    supplier: str = "fournisseur",
    item: str = "article",
    amount: str = "montant",
    quantity: str | None = None,
    promised_date: str | None = None,
    received_date: str | None = None,
    default_period: str = "12m",
    lang: str = DEFAULT_LANG,
) -> dict:
    """Build the « Achats et fournisseurs » spec for a dataset of purchase lines, in ``lang``."""
    T = translator(lang)
    if not dataset_id:
        raise ValueError("dataset_id is required.")
    dt = check_ident("date", date)
    su = check_ident("supplier", supplier)
    it = check_ident("item", item)
    am = check_ident("amount", amount)
    qt = check_ident("quantity", quantity) if quantity else None
    prom = check_ident("promised_date", promised_date) if promised_date else None
    recv = check_ident("received_date", received_date) if received_date else None
    delivery = bool(prom and recv)

    A, S = quote(am), quote(su)

    # ---- transforms --------------------------------------------------
    transforms = [
        DeriveStep(
            column="periode_mois",
            expr=f"date_trunc('month', CAST({quote(dt)} AS TIMESTAMP))",
            provenance=Provenance.REAL,
        )
    ]
    if delivery:
        day_p, day_r = f"CAST({quote(prom)} AS DATE)", f"CAST({quote(recv)} AS DATE)"
        transforms += [
            DeriveStep(
                column="livraison_a_lheure",
                expr=f"CASE WHEN {quote(recv)} IS NULL OR {quote(prom)} IS NULL THEN NULL "
                f"WHEN {day_r} <= {day_p} THEN 1 ELSE 0 END",
            ),
            DeriveStep(
                column="retard_jours",
                expr=f"CASE WHEN {quote(recv)} IS NOT NULL AND {quote(prom)} IS NOT NULL AND {day_r} > {day_p} "
                f"THEN date_diff('day', {day_p}, {day_r}) END",
            ),
        ]

    # ---- fields ------------------------------------------------------
    fields = [
        FieldSpec(name=dt, label=T("Date", "Date"), type="date", role="date"),
        FieldSpec(name=su, label=T("Fournisseur", "Supplier"), role="dimension"),
        FieldSpec(name=it, label=T("Article ou famille", "Item or family"), role="dimension"),
        FieldSpec(name=am, label=T("Montant", "Amount"), type="number", role="measure", format="compact"),
    ]
    if qt:
        fields.append(FieldSpec(name=qt, label=T("Quantité", "Quantity"), type="number", role="measure", format="compact"))
    if prom:
        fields.append(FieldSpec(name=prom, label=T("Date promise", "Promised date"), type="date", role="date"))
    if recv:
        fields.append(FieldSpec(name=recv, label=T("Date de réception", "Received date"), type="date", role="date"))
    fields.append(FieldSpec(name="periode_mois", label=T("Mois", "Month"), type="date", role="dimension"))
    if delivery:
        fields += [
            FieldSpec(name="livraison_a_lheure", label=T("Livré à l'heure", "Delivered on time"), type="number", role="measure",
                      format="integer"),
            FieldSpec(name="retard_jours", label=T("Retard (jours)", "Delay (days)"), type="number", role="measure", format="decimal1"),
        ]

    # ---- measures ----------------------------------------------------
    measures = [
        MeasureSpec(name="depenses", label=T("Dépenses", "Spend"), expr=f"SUM({A})", format="compact"),
        MeasureSpec(name="commandes", label=T("Lignes d'achat", "Purchase lines"), expr="COUNT(*)", format="integer",
                    unit=T("lignes", "lines")),
        MeasureSpec(name="fournisseurs", label=T("Fournisseurs", "Suppliers"), expr=f"COUNT(DISTINCT {S})", format="integer"),
        MeasureSpec(
            name="part_top5",
            label=T("Part du top 5", "Top 5 share"),
            expr=(
                f"(SELECT SUM(s) FROM (SELECT SUM({A}) AS s FROM frame GROUP BY {S} ORDER BY s DESC LIMIT 5)) "
                f"/ NULLIF(SUM({A}), 0)"
            ),
            format="percent",
            thresholds=ThresholdSpec(direction="lower_is_better", good=TOP5_GOOD, warn=TOP5_WARN),
            description=T(
                "Dépenses des cinq premiers fournisseurs sur les dépenses totales de la période filtrée. "
                "À lire sur l'ensemble de la sélection, pas par fournisseur.",
                "Spend of the top five suppliers over total spend of the filtered period. "
                "Read across the whole selection, not per supplier.",
            ),
        ),
    ]
    if qt:
        measures += [
            MeasureSpec(name="qte_totale", label=T("Quantité", "Quantity"), expr=f"SUM({quote(qt)})", format="compact"),
            MeasureSpec(name="prix_moyen", label=T("Prix moyen", "Average price"), expr=f"SUM({A}) / NULLIF(SUM({quote(qt)}), 0)",
                        format="decimal2",
                        description=T("Montant divisé par la quantité, à lire par article.",
                                      "Amount divided by quantity, read per item.")),
        ]
    if delivery:
        rate_thr = ThresholdSpec(direction="higher_is_better", good=ON_TIME_GOOD, warn=ON_TIME_WARN)
        measures += [
            MeasureSpec(name="a_l_heure", label=T("Livré à l'heure", "Delivered on time"), expr="AVG(livraison_a_lheure)", format="percent",
                        thresholds=rate_thr,
                        description=T("Lignes reçues à la date promise ou avant, sur les lignes reçues.",
                                      "Lines received on or before the promised date, over received lines.")),
            MeasureSpec(name="retard_moyen", label=T("Retard moyen", "Average delay"), expr="AVG(retard_jours)",
                        format="decimal1", unit=T("jours", "days"),
                        description=T("Jours de retard moyens des lignes reçues en retard.",
                                      "Average days late of the lines received late.")),
        ]
    source = SourceSpec(
        name="achats",
        label=T("Lignes d'achat", "Purchase lines"),
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
        FilterSpec(id="periode", label=T("Période", "Period"), field=dt, type="relative_period", default=default_period,
                   presets=presets),
        FilterSpec(id="fournisseur", label=T("Fournisseur", "Supplier"), field=su, type="select", default=None),
        FilterSpec(id="article", label=T("Article ou famille", "Item or family"), field=it, type="select", default=None),
    ]

    # ---- visuals -----------------------------------------------------
    def kpi(title: str, measure: str, sparkline: bool = True, **kw: Any) -> VisualSpec:
        return VisualSpec(type="kpi", title=title, source="achats", x="periode_mois",
                          kpi=KpiSpec(measure=measure, sparkline=measure if sparkline else None, **kw))

    text = T(
        "{{filters.fournisseur}}, du {{period.from}} au {{period.to}} : {{depenses|compact}} de dépenses auprès de "
        "{{fournisseurs|integer}} fournisseurs, dont {{part_top5|percent}} pour les cinq premiers.",
        "{{filters.fournisseur}}, from {{period.from}} to {{period.to}}: {{depenses|compact}} spent with "
        "{{fournisseurs|integer}} suppliers, of which {{part_top5|percent}} with the top five.",
    )
    if delivery:
        text += T(" {{a_l_heure|percent}} des lignes reçues sont arrivées à l'heure.",
                  " {{a_l_heure|percent}} of received lines arrived on time.")
    synthese = VisualSpec(type="text", source="achats", text=text)
    kpis = [
        kpi(T("Dépenses totales", "Total spend"), "depenses"),
        kpi(T("Fournisseurs", "Suppliers"), "fournisseurs"),
        kpi(T("Part du top 5", "Top 5 share"), "part_top5", sparkline=False,
            secondary=T("des dépenses, cinq premiers fournisseurs", "of spend, top five suppliers"),
            target_label=T("vigilance au-delà de 50 %", "watch above 50%")),
    ]
    if delivery:
        kpis += [
            kpi(T("Livré à l'heure", "Delivered on time"), "a_l_heure", target_label=T("cible ≥ 95 %", "target ≥ 95%")),
            kpi(T("Retard moyen", "Average delay"), "retard_moyen",
                secondary=T("des lignes reçues en retard", "of lines received late")),
        ]
    par_mois = VisualSpec(
        type="bar",
        title=T("Dépenses par mois", "Spend by month"),
        subtitle=T("Selon la date de la ligne d'achat.", "By purchase line date."),
        source="achats",
        x="periode_mois",
        series=[SeriesSpec(key="depenses", label=T("Dépenses", "Spend"), mark="bar", color=BLUE)],
        sort="x_asc",
    )
    par_fournisseur = VisualSpec(
        type="hbar",
        title=T("Dépenses par fournisseur", "Spend by supplier"),
        subtitle=T("Dix premiers fournisseurs. Cliquer pour filtrer le tableau de bord.",
                   "Top ten suppliers. Click to filter the dashboard."),
        source="achats",
        x=su,
        series=[SeriesSpec(key="depenses", label=T("Dépenses", "Spend"), mark="bar", color=BROWN)],
        sort="value_desc",
        top_n=10,
        click_filters={"fournisseur": su},
        ignore_filters=["fournisseur"],
    )
    supplier_columns = [
        ColumnSpec(key=su, label=T("Fournisseur", "Supplier")),
        ColumnSpec(key="depenses", label=T("Dépenses", "Spend")),
        ColumnSpec(key="commandes", label=T("Lignes", "Lines")),
    ]
    if delivery:
        supplier_columns += [
            ColumnSpec(key="a_l_heure", label=T("À l'heure", "On time"), use_thresholds=True),
            ColumnSpec(key="retard_moyen", label=T("Retard moyen", "Average delay")),
        ]
    table_fournisseurs = VisualSpec(
        type="table",
        title=T("Détail par fournisseur", "Detail by supplier"),
        subtitle=T("La ligne « Toutes » reprend les définitions globales, pas la moyenne des fournisseurs.",
                   "The “All” row uses the overall definitions, not the average of the suppliers."),
        source="achats",
        x=su,
        columns=supplier_columns,
        sort="value_desc",
        total_row=True,
        click_filters={"fournisseur": su},
        ignore_filters=["fournisseur"],
        highlight_filter="fournisseur",
    )
    item_columns = [ColumnSpec(key=it, label=T("Article ou famille", "Item or family")),
                    ColumnSpec(key="depenses", label=T("Dépenses", "Spend"))]
    if qt:
        item_columns += [ColumnSpec(key="qte_totale", label=T("Quantité", "Quantity")),
                         ColumnSpec(key="prix_moyen", label=T("Prix moyen", "Average price"))]
    table_articles = VisualSpec(
        type="table",
        title=T("Détail par article", "Detail by item"),
        subtitle=T("Quinze premiers articles par dépenses.", "Top fifteen items by spend.")
        + (T(" Prix moyen = montant / quantité.", " Average price = amount / quantity.") if qt else ""),
        source="achats",
        x=it,
        columns=item_columns,
        sort="value_desc",
        top_n=15,
        click_filters={"article": it},
        ignore_filters=["article"],
        highlight_filter="article",
    )
    notes = [
        VisualSpec(
            type="note",
            title=T("Définitions", "Definitions"),
            badges=[Provenance.REAL],
            text=T(
                "**Part du top 5** : dépenses des cinq premiers fournisseurs sur les dépenses totales de la période "
                "filtrée (dépendance). Vert sous 50 %, orange jusqu'à 70 %, rouge au-delà. "
                + (
                    "**À l'heure** : reçu à la date promise ou avant, sur les lignes reçues. "
                    if delivery
                    else "Sans dates promise et de réception, le suivi des livraisons n'est pas affiché. "
                )
                + ("**Prix moyen** : montant divisé par la quantité." if qt else ""),
                "**Top 5 share**: spend of the top five suppliers over total spend of the filtered "
                "period (dependence). Green below 50%, orange up to 70%, red beyond. "
                + (
                    "**On time**: received on or before the promised date, over received lines. "
                    if delivery
                    else "Without promised and received dates, delivery tracking is not shown. "
                )
                + ("**Average price**: amount divided by quantity." if qt else ""),
            ),
        )
    ]

    # ---- grid --------------------------------------------------------
    grid: Placed = [(synthese, dict(row=0, col=0, width=12, height=2))]
    grid += tile_row(kpis, 2, 3)
    placed, row = flow([par_mois, par_fournisseur], 5, 6)
    grid += placed
    grid.append((table_fournisseurs, dict(row=row, col=0, width=12, height=6)))
    row += 6
    grid.append((table_articles, dict(row=row, col=0, width=12, height=6)))
    row += 6
    grid += tile_row(notes, row, 4)
    return assemble([source], filters, grid)


def build(dataset_id: str, columns: dict, params: dict, lang: str = DEFAULT_LANG) -> dict:
    """Catalog entry point: column mapping (blank optional → None) to the template, in ``lang``."""
    mapping: dict[str, Any] = {key: columns.get(key) or "" for key in _REQUIRED}
    mapping.update({key: columns.get(key) or None for key in _OPTIONAL})
    return purchasing_suppliers(dataset_id, **mapping, lang=lang)
