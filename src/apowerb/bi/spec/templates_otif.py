"""
bi/spec/templates_otif.py
-------------------------
Spec v2 template « Livraisons / OTIF »: one row per order line with its promised
and delivered dates and quantities.

A line is *on time* when delivered on or before the promised date, *complete*
when the delivered quantity reaches the ordered one, and *OTIF* when both hold.
Rates are computed on the delivered lines. The reference date of the late open
lines is the last known delivery of the dataset.
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
_PERIOD_RE = re.compile(r"^(\d{1,3})([wm])$")

BROWN = "#9a6b3f"
BLUE = "#3b6fd8"

OTIF_TARGET = 0.95
OTIF_WARN = 0.90

DESCRIPTOR: dict[str, Any] = {
    "id": "delivery_otif",
    "label": {"fr": "Livraisons / OTIF", "en": "Deliveries / OTIF"},
    "description": {
        "fr": (
            "Suivi des livraisons clients à partir d'une ligne par ligne de commande : livré à l'heure, "
            "livré complet et OTIF (les deux), retard moyen, retards en cours et taux de service quantité, "
            "par client, transporteur et famille."
        ),
        "en": (
            "Customer delivery tracking from one row per order line: delivered on time, "
            "delivered in full and OTIF (both), average delay, open late lines and quantity fill rate, "
            "by customer, carrier and product family."
        ),
    },
    "version": 1,
    "columns": [
        {"key": "order_date", "label": {"fr": "Date de commande", "en": "Order date"}, "type": "date", "required": True, "default": "date_commande"},
        {"key": "promised_date", "label": {"fr": "Date promise", "en": "Promised date"}, "type": "date", "required": True, "default": "date_promise"},
        {"key": "delivered_date", "label": {"fr": "Date de livraison", "en": "Delivery date"}, "type": "date", "required": True, "default": "date_livraison"},
        {"key": "qty_ordered", "label": {"fr": "Quantité commandée", "en": "Ordered quantity"}, "type": "number", "required": True, "default": "qte_commandee"},
        {"key": "qty_delivered", "label": {"fr": "Quantité livrée", "en": "Delivered quantity"}, "type": "number", "required": True, "default": "qte_livree"},
        {"key": "customer", "label": {"fr": "Client", "en": "Customer"}, "type": "string", "required": True, "default": "client"},
        {"key": "carrier", "label": {"fr": "Transporteur", "en": "Carrier"}, "type": "string", "required": False, "default": None},
        {"key": "family", "label": {"fr": "Famille", "en": "Product family"}, "type": "string", "required": False, "default": None},
    ],
    "params": [],
}
_REQUIRED = [c["key"] for c in DESCRIPTOR["columns"] if c["required"]]
_OPTIONAL = [c["key"] for c in DESCRIPTOR["columns"] if not c["required"]]


def _check_ident(label: str, value: str) -> str:
    if not _IDENT_RE.match(value or ""):
        raise ValueError(
            f"{label}='{value}' is not a valid column name: use lowercase letters, "
            "digits and underscores (start with a letter), e.g. 'date_livraison'."
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


def _bucket(default_period: str) -> tuple[str, str]:
    """Chart granularity ``(column, sql unit)``: months for long periods, else weeks."""
    match = _PERIOD_RE.match(default_period)
    if default_period == "all" or (match and match.group(2) == "m" and int(match.group(1)) >= 6):
        return "periode_mois", "month"
    return "periode_semaine", "week"


def delivery_otif(
    dataset_id: str,
    *,
    order_date: str = "date_commande",
    promised_date: str = "date_promise",
    delivered_date: str = "date_livraison",
    qty_ordered: str = "qte_commandee",
    qty_delivered: str = "qte_livree",
    customer: str = "client",
    carrier: str | None = None,
    family: str | None = None,
    default_period: str = "12m",
    lang: str = DEFAULT_LANG,
) -> dict:
    """Build the « Livraisons / OTIF » spec for a dataset of order lines, in ``lang``."""
    T = translator(lang)
    if not dataset_id:
        raise ValueError("dataset_id is required.")
    od = _check_ident("order_date", order_date)
    pd_ = _check_ident("promised_date", promised_date)
    dd = _check_ident("delivered_date", delivered_date)
    qo = _check_ident("qty_ordered", qty_ordered)
    qd = _check_ident("qty_delivered", qty_delivered)
    cu = _check_ident("customer", customer)
    ca = _check_ident("carrier", carrier) if carrier else None
    fa = _check_ident("family", family) if family else None

    bucket, unit = _bucket(default_period)
    bucket_label = T("Semaine", "Week") if unit == "week" else T("Mois", "Month")
    P, D, QO, QD = _q(pd_), _q(dd), _q(qo), _q(qd)
    day_p, day_d = f"CAST({P} AS DATE)", f"CAST({D} AS DATE)"
    as_of = f"CAST(MAX({D}) OVER () AS DATE)"

    # ---- transforms --------------------------------------------------
    transforms = [
        DeriveStep(column="ligne_livree", expr=f"CASE WHEN {D} IS NULL THEN 0 ELSE 1 END"),
        DeriveStep(
            column="ligne_a_lheure",
            expr=f"CASE WHEN {D} IS NULL OR {P} IS NULL THEN NULL WHEN {day_d} <= {day_p} THEN 1 ELSE 0 END",
        ),
        DeriveStep(
            column="ligne_complete",
            expr=f"CASE WHEN {D} IS NULL THEN NULL WHEN COALESCE({QD}, 0) >= {QO} THEN 1 ELSE 0 END",
        ),
        DeriveStep(column="ligne_otif", expr="ligne_a_lheure * ligne_complete"),
        DeriveStep(
            column="retard_jours",
            expr=(
                f"CASE WHEN {D} IS NOT NULL AND {P} IS NOT NULL AND {day_d} > {day_p} "
                f"THEN date_diff('day', {day_p}, {day_d}) END"
            ),
        ),
        DeriveStep(
            column="tranche_retard",
            expr=(
                f"CASE WHEN {D} IS NULL OR {P} IS NULL THEN NULL WHEN retard_jours IS NULL THEN '{T('0 j', '0 d')}' "
                f"WHEN retard_jours <= 2 THEN '{T('1-2 j', '1-2 d')}' WHEN retard_jours <= 5 THEN '{T('3-5 j', '3-5 d')}' "
                f"WHEN retard_jours <= 10 THEN '{T('6-10 j', '6-10 d')}' ELSE '{T('> 10 j', '> 10 d')}' END"
            ),
        ),
        DeriveStep(
            column="ligne_en_retard_ouverte",
            expr=f"CASE WHEN {D} IS NULL AND {P} IS NOT NULL AND {day_p} < {as_of} THEN 1 ELSE 0 END",
        ),
        DeriveStep(
            column="ligne_echue",
            expr="CASE WHEN ligne_livree = 1 OR ligne_en_retard_ouverte = 1 THEN 1 ELSE 0 END",
        ),
        DeriveStep(column="qte_due", expr=f"CASE WHEN ligne_echue = 1 THEN {QO} END"),
        DeriveStep(
            column="qte_servie",
            expr=f"CASE WHEN ligne_echue = 1 THEN LEAST(COALESCE({QD}, 0), {QO}) END",
        ),
        DeriveStep(
            column=bucket,
            expr=f"date_trunc('{unit}', CAST({P} AS TIMESTAMP))",
            provenance=Provenance.REAL,
        ),
    ]

    # ---- fields ------------------------------------------------------
    fields = [
        FieldSpec(name=od, label=T("Date de commande", "Order date"), type="date", role="date"),
        FieldSpec(name=pd_, label=T("Date promise", "Promised date"), type="date", role="date"),
        FieldSpec(name=dd, label=T("Date de livraison", "Delivery date"), type="date", role="date"),
        FieldSpec(name=qo, label=T("Quantité commandée", "Ordered quantity"), type="number", role="measure", format="compact"),
        FieldSpec(name=qd, label=T("Quantité livrée", "Delivered quantity"), type="number", role="measure", format="compact"),
        FieldSpec(name=cu, label=T("Client", "Customer"), role="dimension"),
    ]
    if ca:
        fields.append(FieldSpec(name=ca, label=T("Transporteur", "Carrier"), role="dimension"))
    if fa:
        fields.append(FieldSpec(name=fa, label=T("Famille", "Product family"), role="dimension"))
    fields += [
        FieldSpec(name="ligne_livree", label=T("Ligne livrée", "Delivered line"), type="number", role="measure", format="integer"),
        FieldSpec(name="ligne_a_lheure", label=T("Ligne livrée à l'heure", "Line delivered on time"), type="number", role="measure", format="integer"),
        FieldSpec(name="ligne_complete", label=T("Ligne livrée complète", "Line delivered in full"), type="number", role="measure", format="integer"),
        FieldSpec(name="ligne_otif", label=T("Ligne OTIF", "OTIF line"), type="number", role="measure", format="integer"),
        FieldSpec(name="retard_jours", label=T("Retard (jours)", "Delay (days)"), type="number", role="measure", format="decimal1"),
        FieldSpec(name="tranche_retard", label=T("Tranche de retard", "Delay band"), role="dimension"),
        FieldSpec(name="ligne_en_retard_ouverte", label=T("Ligne en retard non livrée", "Late undelivered line"), type="number", role="measure",
                  format="integer"),
        FieldSpec(name="ligne_echue", label=T("Ligne échue", "Due line"), type="number", role="measure", format="integer"),
        FieldSpec(name="qte_due", label=T("Quantité due", "Due quantity"), type="number", role="measure", format="compact"),
        FieldSpec(name="qte_servie", label=T("Quantité servie", "Served quantity"), type="number", role="measure", format="compact"),
        FieldSpec(name=bucket, label=bucket_label, type="date", role="dimension"),
    ]

    # ---- measures ----------------------------------------------------
    rate_thr = ThresholdSpec(direction="higher_is_better", good=OTIF_TARGET, warn=OTIF_WARN)
    measures = [
        MeasureSpec(name="lignes", label=T("Lignes de commande", "Order lines"), expr="COUNT(*)", format="integer",
                    unit=T("lignes", "lines")),
        MeasureSpec(name="lignes_livrees", label=T("Lignes livrées", "Delivered lines"), expr="SUM(ligne_livree)", format="integer",
                    unit=T("lignes", "lines")),
        MeasureSpec(name="a_l_heure", label=T("Livré à l'heure", "On time"), expr="AVG(ligne_a_lheure)", format="percent",
                    thresholds=rate_thr, description=T("Lignes livrées à la date promise ou avant, sur les lignes livrées.",
                                                       "Lines delivered on or before the promised date, over delivered lines.")),
        MeasureSpec(name="complet", label=T("Livré complet", "In full"), expr="AVG(ligne_complete)", format="percent",
                    thresholds=rate_thr, description=T("Lignes livrées avec au moins la quantité commandée.",
                                                       "Lines delivered with at least the ordered quantity.")),
        MeasureSpec(name="otif", label="OTIF", expr="AVG(ligne_otif)", format="percent", thresholds=rate_thr,
                    description=T("On Time In Full : lignes livrées à l'heure et complètes, sur les lignes livrées.",
                                  "On Time In Full: lines delivered on time and in full, over delivered lines.")),
        MeasureSpec(name="retard_moyen", label=T("Retard moyen", "Average delay"), expr="AVG(retard_jours)", format="decimal1",
                    unit=T("jours", "days"), description=T("Jours de retard moyens des lignes livrées en retard.",
                                                           "Average days of delay of the lines delivered late.")),
        MeasureSpec(name="retards_en_cours", label=T("En retard non livrées", "Late, not delivered"),
                    expr="COALESCE(SUM(ligne_en_retard_ouverte), 0)", format="integer", unit=T("lignes", "lines"),
                    description=T("Lignes non livrées dont la date promise précède la dernière livraison connue.",
                                  "Undelivered lines whose promised date is before the last known delivery.")),
        MeasureSpec(name="taux_service_qte", label=T("Taux de service quantité", "Quantity fill rate"),
                    expr="SUM(qte_servie) / NULLIF(SUM(qte_due), 0)", format="percent",
                    description=T("Quantité livrée (plafonnée à la commande) sur la quantité commandée des lignes échues.",
                                  "Delivered quantity (capped at the order) over the ordered quantity of due lines.")),
    ]
    source = SourceSpec(
        name="livraisons",
        label=T("Lignes de commande livrées", "Delivered order lines"),
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
        FilterSpec(id="periode", label=T("Période (date promise)", "Period (promised date)"), field=pd_, type="relative_period",
                   default=default_period, presets=presets),
        FilterSpec(id="client", label=T("Client", "Customer"), field=cu, type="select", default=None),
    ]
    if ca:
        filters.append(FilterSpec(id="transporteur", label=T("Transporteur", "Carrier"), field=ca, type="select", default=None))
    if fa:
        filters.append(FilterSpec(id="famille", label=T("Famille", "Product family"), field=fa, type="select", default=None))

    # ---- visuals -----------------------------------------------------
    def kpi(title: str, measure: str, **kw: Any) -> VisualSpec:
        return VisualSpec(type="kpi", title=title, source="livraisons", x=bucket,
                          kpi=KpiSpec(measure=measure, sparkline=measure, **kw))

    synthese = VisualSpec(
        type="text",
        source="livraisons",
        text=T(
            "{{filters.client}}, du {{period.from}} au {{period.to}} : {{lignes|integer}} lignes de commande, "
            "{{lignes_livrees|integer}} livrées. OTIF de {{otif|percent}} : {{a_l_heure|percent}} livrées à l'heure "
            "et {{complet|percent}} complètes.",
            "{{filters.client}}, {{period.from}} to {{period.to}}: {{lignes|integer}} order lines, "
            "{{lignes_livrees|integer}} delivered. OTIF of {{otif|percent}}: {{a_l_heure|percent}} delivered on time "
            "and {{complet|percent}} in full.",
        ),
    )
    kpis = [
        kpi("OTIF", "otif", target_label=T("cible ≥ 95 %", "target ≥ 95%")),
        kpi(T("Livré à l'heure", "On time"), "a_l_heure", target_label=T("cible ≥ 95 %", "target ≥ 95%")),
        kpi(T("Livré complet", "In full"), "complet",
            secondary=T("Taux de service quantité : {{taux_service_qte|percent}}",
                        "Quantity fill rate: {{taux_service_qte|percent}}"),
            target_label=T("cible ≥ 95 %", "target ≥ 95%")),
        kpi(T("Retard moyen", "Average delay"), "retard_moyen",
            secondary=T("des lignes livrées en retard", "of the lines delivered late")),
        kpi(T("En retard non livrées", "Late, not delivered"), "retards_en_cours",
            secondary=T("date promise dépassée", "promised date passed")),
    ]
    evolution = VisualSpec(
        type="combo",
        title=T("OTIF et livraisons à l'heure", "OTIF and on-time deliveries"),
        subtitle=T(f"Par {bucket_label.lower()}, selon la date promise.", f"By {bucket_label.lower()}, on the promised date."),
        source="livraisons",
        x=bucket,
        series=[
            SeriesSpec(key="otif", label="OTIF", mark="bar", color=BLUE, use_thresholds=True),
            SeriesSpec(key="a_l_heure", label=T("À l'heure", "On time"), mark="line", color=BROWN),
        ],
        reference_lines=[ReferenceLine(value=OTIF_TARGET, label=T("Objectif 95 %", "Target 95%"), color="good")],
        sort="x_asc",
    )

    def otif_by(title: str, column: str, filter_id: str) -> VisualSpec:
        return VisualSpec(
            type="hbar",
            title=title,
            subtitle=T("Les moins bons en premier. Cliquer pour filtrer le tableau de bord.",
                       "Worst first. Click to filter the dashboard."),
            source="livraisons",
            x=column,
            series=[SeriesSpec(key="otif", label="OTIF", mark="bar", use_thresholds=True)],
            sort="value_asc",
            reference_lines=[ReferenceLine(value=OTIF_TARGET, label=T("95 %", "95%"), color="good")],
            click_filters={filter_id: column},
            ignore_filters=[filter_id],
        )

    charts = [otif_by(T("OTIF par client", "OTIF by customer"), cu, "client")]
    if ca:
        charts.append(otif_by(T("OTIF par transporteur", "OTIF by carrier"), ca, "transporteur"))
    if fa:
        charts.append(otif_by(T("OTIF par famille", "OTIF by product family"), fa, "famille"))

    table = VisualSpec(
        type="table",
        title=T("Performance par client", "Performance by customer"),
        subtitle=T("La ligne « Toutes » reprend les définitions globales, pas la moyenne des clients.",
                   "The “All” row uses the overall definitions, not the average of the customers."),
        source="livraisons",
        x=cu,
        columns=[
            ColumnSpec(key=cu, label=T("Client", "Customer")),
            ColumnSpec(key="lignes", label=T("Lignes", "Lines")),
            ColumnSpec(key="otif", label="OTIF", use_thresholds=True),
            ColumnSpec(key="a_l_heure", label=T("À l'heure", "On time"), use_thresholds=True),
            ColumnSpec(key="complet", label=T("Complet", "In full"), use_thresholds=True),
            ColumnSpec(key="retard_moyen", label=T("Retard moyen", "Average delay")),
        ],
        total_row=True,
        click_filters={"client": cu},
        ignore_filters=["client"],
        highlight_filter="client",
    )
    retards = VisualSpec(
        type="bar",
        title=T("Distribution des retards", "Delay distribution"),
        subtitle=T("Lignes livrées, par jours de retard sur la date promise.",
                   "Delivered lines, by days of delay on the promised date."),
        source="livraisons",
        x="tranche_retard",
        series=[SeriesSpec(key="lignes_livrees", label=T("Lignes livrées", "Delivered lines"), mark="bar", color=BROWN)],
        sort="x_asc",
    )
    lecture = VisualSpec(
        type="text",
        title=T("Lecture des retards", "Reading the delays"),
        source="livraisons",
        text=T(
            "Les lignes en retard sont livrées en moyenne {{retard_moyen|decimal1}} jours après la date promise. "
            "{{retards_en_cours|integer}} lignes ne sont pas encore livrées alors que leur date promise est "
            "dépassée. Le taux de service quantité est de {{taux_service_qte|percent}}.",
            "Late lines are delivered on average {{retard_moyen|decimal1}} days after the promised date. "
            "{{retards_en_cours|integer}} lines are not delivered yet although their promised date has "
            "passed. The quantity fill rate is {{taux_service_qte|percent}}.",
        ),
    )
    notes = [
        VisualSpec(
            type="note",
            title=T("Définitions", "Definitions"),
            badges=[Provenance.REAL],
            text=T(
                "**À l'heure** : livrée à la date promise ou avant. **Complet** : quantité livrée au moins égale à la "
                "quantité commandée. **OTIF** : à l'heure et complet. Les taux sont calculés sur les lignes livrées.",
                "**On time**: delivered on or before the promised date. **In full**: delivered quantity at least equal "
                "to the ordered quantity. **OTIF**: on time and in full. Rates are computed on delivered lines.",
            ),
        ),
        VisualSpec(
            type="note",
            title=T("Seuils", "Thresholds"),
            text=T(
                "Vert à partir de 95 % d'OTIF, orange entre 90 % et 95 %, rouge en dessous. La période se mesure sur la "
                "date promise.",
                "Green from 95% OTIF, orange between 90% and 95%, red below. The period is measured on the "
                "promised date.",
            ),
        ),
        VisualSpec(
            type="note",
            title=T("Lignes non livrées", "Undelivered lines"),
            text=T(
                "Une ligne non livrée est en retard si sa date promise précède la dernière livraison connue du fichier "
                "(et non la date du jour). Elle n'entre pas dans les taux tant qu'elle n'est pas livrée. Le taux de "
                "service quantité compte les lignes échues, livrées ou en retard.",
                "An undelivered line is late when its promised date is before the last known delivery of the file "
                "(not today's date). It stays out of the rates until it is delivered. The quantity fill rate counts "
                "due lines, delivered or late.",
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
    grid.append((retards, dict(row=row, col=0, width=8, height=5)))
    grid.append((lecture, dict(row=row, col=8, width=4, height=5)))
    row += 5
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
    return delivery_otif(dataset_id, **mapping, lang=lang)
