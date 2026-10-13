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
from apowerb.bi.spec.templates_common import Placed, assemble, check_ident, flow, quote, tile_row

BROWN = "#9a6b3f"
BLUE = "#3b6fd8"

TOP5_GOOD = 0.5
TOP5_WARN = 0.7
ON_TIME_GOOD = 0.95
ON_TIME_WARN = 0.90

DESCRIPTOR: dict[str, Any] = {
    "id": "purchasing_suppliers",
    "label": "Achats et fournisseurs",
    "description": (
        "Suivi des achats à partir d'une ligne par commande ou facture d'achat : dépenses totales et par mois, "
        "nombre de fournisseurs, dépendance aux cinq premiers fournisseurs, dépenses par fournisseur et par article. "
        "Avec la quantité, le prix moyen par article ; avec les dates promise et de réception, le taux de livraison "
        "à l'heure et le retard moyen."
    ),
    "version": 1,
    "columns": [
        {"key": "date", "label": "Date", "type": "date", "required": True, "default": "date"},
        {"key": "supplier", "label": "Fournisseur", "type": "string", "required": True, "default": "fournisseur"},
        {"key": "item", "label": "Article ou famille", "type": "string", "required": True, "default": "article"},
        {"key": "quantity", "label": "Quantité", "type": "number", "required": False, "default": None},
        {"key": "amount", "label": "Montant", "type": "number", "required": True, "default": "montant"},
        {"key": "promised_date", "label": "Date promise", "type": "date", "required": False, "default": None},
        {"key": "received_date", "label": "Date de réception", "type": "date", "required": False, "default": None},
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
) -> dict:
    """Build the « Achats et fournisseurs » spec for a dataset of purchase lines."""
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
        FieldSpec(name=dt, label="Date", type="date", role="date"),
        FieldSpec(name=su, label="Fournisseur", role="dimension"),
        FieldSpec(name=it, label="Article ou famille", role="dimension"),
        FieldSpec(name=am, label="Montant", type="number", role="measure", format="compact"),
    ]
    if qt:
        fields.append(FieldSpec(name=qt, label="Quantité", type="number", role="measure", format="compact"))
    if prom:
        fields.append(FieldSpec(name=prom, label="Date promise", type="date", role="date"))
    if recv:
        fields.append(FieldSpec(name=recv, label="Date de réception", type="date", role="date"))
    fields.append(FieldSpec(name="periode_mois", label="Mois", type="date", role="dimension"))
    if delivery:
        fields += [
            FieldSpec(name="livraison_a_lheure", label="Livré à l'heure", type="number", role="measure",
                      format="integer"),
            FieldSpec(name="retard_jours", label="Retard (jours)", type="number", role="measure", format="decimal1"),
        ]

    # ---- measures ----------------------------------------------------
    measures = [
        MeasureSpec(name="depenses", label="Dépenses", expr=f"SUM({A})", format="compact"),
        MeasureSpec(name="commandes", label="Lignes d'achat", expr="COUNT(*)", format="integer", unit="lignes"),
        MeasureSpec(name="fournisseurs", label="Fournisseurs", expr=f"COUNT(DISTINCT {S})", format="integer"),
        MeasureSpec(
            name="part_top5",
            label="Part du top 5",
            expr=(
                f"(SELECT SUM(s) FROM (SELECT SUM({A}) AS s FROM frame GROUP BY {S} ORDER BY s DESC LIMIT 5)) "
                f"/ NULLIF(SUM({A}), 0)"
            ),
            format="percent",
            thresholds=ThresholdSpec(direction="lower_is_better", good=TOP5_GOOD, warn=TOP5_WARN),
            description="Dépenses des cinq premiers fournisseurs sur les dépenses totales de la période filtrée. "
            "À lire sur l'ensemble de la sélection, pas par fournisseur.",
        ),
    ]
    if qt:
        measures += [
            MeasureSpec(name="qte_totale", label="Quantité", expr=f"SUM({quote(qt)})", format="compact"),
            MeasureSpec(name="prix_moyen", label="Prix moyen", expr=f"SUM({A}) / NULLIF(SUM({quote(qt)}), 0)",
                        format="decimal2", description="Montant divisé par la quantité, à lire par article."),
        ]
    if delivery:
        rate_thr = ThresholdSpec(direction="higher_is_better", good=ON_TIME_GOOD, warn=ON_TIME_WARN)
        measures += [
            MeasureSpec(name="a_l_heure", label="Livré à l'heure", expr="AVG(livraison_a_lheure)", format="percent",
                        thresholds=rate_thr,
                        description="Lignes reçues à la date promise ou avant, sur les lignes reçues."),
            MeasureSpec(name="retard_moyen", label="Retard moyen", expr="AVG(retard_jours)", format="decimal1",
                        unit="jours", description="Jours de retard moyens des lignes reçues en retard."),
        ]
    source = SourceSpec(
        name="achats",
        label="Lignes d'achat",
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
        FilterSpec(id="periode", label="Période", field=dt, type="relative_period", default=default_period,
                   presets=presets),
        FilterSpec(id="fournisseur", label="Fournisseur", field=su, type="select", default=None),
        FilterSpec(id="article", label="Article ou famille", field=it, type="select", default=None),
    ]

    # ---- visuals -----------------------------------------------------
    def kpi(title: str, measure: str, sparkline: bool = True, **kw: Any) -> VisualSpec:
        return VisualSpec(type="kpi", title=title, source="achats", x="periode_mois",
                          kpi=KpiSpec(measure=measure, sparkline=measure if sparkline else None, **kw))

    text = (
        "{{filters.fournisseur}}, du {{period.from}} au {{period.to}} : {{depenses|compact}} de dépenses auprès de "
        "{{fournisseurs|integer}} fournisseurs, dont {{part_top5|percent}} pour les cinq premiers."
    )
    if delivery:
        text += " {{a_l_heure|percent}} des lignes reçues sont arrivées à l'heure."
    synthese = VisualSpec(type="text", source="achats", text=text)
    kpis = [
        kpi("Dépenses totales", "depenses"),
        kpi("Fournisseurs", "fournisseurs"),
        kpi("Part du top 5", "part_top5", sparkline=False, secondary="des dépenses, cinq premiers fournisseurs",
            target_label="vigilance au-delà de 50 %"),
    ]
    if delivery:
        kpis += [
            kpi("Livré à l'heure", "a_l_heure", target_label="cible ≥ 95 %"),
            kpi("Retard moyen", "retard_moyen", secondary="des lignes reçues en retard"),
        ]
    par_mois = VisualSpec(
        type="bar",
        title="Dépenses par mois",
        subtitle="Selon la date de la ligne d'achat.",
        source="achats",
        x="periode_mois",
        series=[SeriesSpec(key="depenses", label="Dépenses", mark="bar", color=BLUE)],
        sort="x_asc",
    )
    par_fournisseur = VisualSpec(
        type="hbar",
        title="Dépenses par fournisseur",
        subtitle="Dix premiers fournisseurs. Cliquer pour filtrer le tableau de bord.",
        source="achats",
        x=su,
        series=[SeriesSpec(key="depenses", label="Dépenses", mark="bar", color=BROWN)],
        sort="value_desc",
        top_n=10,
        click_filters={"fournisseur": su},
        ignore_filters=["fournisseur"],
    )
    supplier_columns = [
        ColumnSpec(key=su, label="Fournisseur"),
        ColumnSpec(key="depenses", label="Dépenses"),
        ColumnSpec(key="commandes", label="Lignes"),
    ]
    if delivery:
        supplier_columns += [
            ColumnSpec(key="a_l_heure", label="À l'heure", use_thresholds=True),
            ColumnSpec(key="retard_moyen", label="Retard moyen"),
        ]
    table_fournisseurs = VisualSpec(
        type="table",
        title="Détail par fournisseur",
        subtitle="La ligne « Toutes » reprend les définitions globales, pas la moyenne des fournisseurs.",
        source="achats",
        x=su,
        columns=supplier_columns,
        sort="value_desc",
        total_row=True,
        click_filters={"fournisseur": su},
        ignore_filters=["fournisseur"],
        highlight_filter="fournisseur",
    )
    item_columns = [ColumnSpec(key=it, label="Article ou famille"), ColumnSpec(key="depenses", label="Dépenses")]
    if qt:
        item_columns += [ColumnSpec(key="qte_totale", label="Quantité"), ColumnSpec(key="prix_moyen", label="Prix moyen")]
    table_articles = VisualSpec(
        type="table",
        title="Détail par article",
        subtitle="Quinze premiers articles par dépenses." + (" Prix moyen = montant / quantité." if qt else ""),
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
            title="Définitions",
            badges=[Provenance.REAL],
            text=(
                "**Part du top 5** : dépenses des cinq premiers fournisseurs sur les dépenses totales de la période "
                "filtrée (dépendance). Vert sous 50 %, orange jusqu'à 70 %, rouge au-delà. "
                + (
                    "**À l'heure** : reçu à la date promise ou avant, sur les lignes reçues. "
                    if delivery
                    else "Sans dates promise et de réception, le suivi des livraisons n'est pas affiché. "
                )
                + ("**Prix moyen** : montant divisé par la quantité." if qt else "")
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


def build(dataset_id: str, columns: dict, params: dict) -> dict:
    """Catalog entry point: column mapping (blank optional → None) to the template."""
    mapping: dict[str, Any] = {key: columns.get(key) or "" for key in _REQUIRED}
    mapping.update({key: columns.get(key) or None for key in _OPTIONAL})
    return purchasing_suppliers(dataset_id, **mapping)
