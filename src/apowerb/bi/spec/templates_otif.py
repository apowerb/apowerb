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

_IDENT_RE = re.compile(IDENT)
_PERIOD_RE = re.compile(r"^(\d{1,3})([wm])$")

BROWN = "#9a6b3f"
BLUE = "#3b6fd8"

OTIF_TARGET = 0.95
OTIF_WARN = 0.90

DESCRIPTOR: dict[str, Any] = {
    "id": "delivery_otif",
    "label": "Livraisons / OTIF",
    "description": (
        "Suivi des livraisons clients à partir d'une ligne par ligne de commande : livré à l'heure, "
        "livré complet et OTIF (les deux), retard moyen, retards en cours et taux de service quantité, "
        "par client, transporteur et famille."
    ),
    "version": 1,
    "columns": [
        {"key": "order_date", "label": "Date de commande", "type": "date", "required": True, "default": "date_commande"},
        {"key": "promised_date", "label": "Date promise", "type": "date", "required": True, "default": "date_promise"},
        {"key": "delivered_date", "label": "Date de livraison", "type": "date", "required": True, "default": "date_livraison"},
        {"key": "qty_ordered", "label": "Quantité commandée", "type": "number", "required": True, "default": "qte_commandee"},
        {"key": "qty_delivered", "label": "Quantité livrée", "type": "number", "required": True, "default": "qte_livree"},
        {"key": "customer", "label": "Client", "type": "string", "required": True, "default": "client"},
        {"key": "carrier", "label": "Transporteur", "type": "string", "required": False, "default": None},
        {"key": "family", "label": "Famille", "type": "string", "required": False, "default": None},
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
) -> dict:
    """Build the « Livraisons / OTIF » spec for a dataset of order lines."""
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
    bucket_label = "Semaine" if unit == "week" else "Mois"
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
                f"CASE WHEN {D} IS NULL OR {P} IS NULL THEN NULL WHEN retard_jours IS NULL THEN '0 j' "
                "WHEN retard_jours <= 2 THEN '1-2 j' WHEN retard_jours <= 5 THEN '3-5 j' "
                "WHEN retard_jours <= 10 THEN '6-10 j' ELSE '> 10 j' END"
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
        FieldSpec(name=od, label="Date de commande", type="date", role="date"),
        FieldSpec(name=pd_, label="Date promise", type="date", role="date"),
        FieldSpec(name=dd, label="Date de livraison", type="date", role="date"),
        FieldSpec(name=qo, label="Quantité commandée", type="number", role="measure", format="compact"),
        FieldSpec(name=qd, label="Quantité livrée", type="number", role="measure", format="compact"),
        FieldSpec(name=cu, label="Client", role="dimension"),
    ]
    if ca:
        fields.append(FieldSpec(name=ca, label="Transporteur", role="dimension"))
    if fa:
        fields.append(FieldSpec(name=fa, label="Famille", role="dimension"))
    fields += [
        FieldSpec(name="ligne_livree", label="Ligne livrée", type="number", role="measure", format="integer"),
        FieldSpec(name="ligne_a_lheure", label="Ligne livrée à l'heure", type="number", role="measure", format="integer"),
        FieldSpec(name="ligne_complete", label="Ligne livrée complète", type="number", role="measure", format="integer"),
        FieldSpec(name="ligne_otif", label="Ligne OTIF", type="number", role="measure", format="integer"),
        FieldSpec(name="retard_jours", label="Retard (jours)", type="number", role="measure", format="decimal1"),
        FieldSpec(name="tranche_retard", label="Tranche de retard", role="dimension"),
        FieldSpec(name="ligne_en_retard_ouverte", label="Ligne en retard non livrée", type="number", role="measure",
                  format="integer"),
        FieldSpec(name="ligne_echue", label="Ligne échue", type="number", role="measure", format="integer"),
        FieldSpec(name="qte_due", label="Quantité due", type="number", role="measure", format="compact"),
        FieldSpec(name="qte_servie", label="Quantité servie", type="number", role="measure", format="compact"),
        FieldSpec(name=bucket, label=bucket_label, type="date", role="dimension"),
    ]

    # ---- measures ----------------------------------------------------
    rate_thr = ThresholdSpec(direction="higher_is_better", good=OTIF_TARGET, warn=OTIF_WARN)
    measures = [
        MeasureSpec(name="lignes", label="Lignes de commande", expr="COUNT(*)", format="integer", unit="lignes"),
        MeasureSpec(name="lignes_livrees", label="Lignes livrées", expr="SUM(ligne_livree)", format="integer",
                    unit="lignes"),
        MeasureSpec(name="a_l_heure", label="Livré à l'heure", expr="AVG(ligne_a_lheure)", format="percent",
                    thresholds=rate_thr, description="Lignes livrées à la date promise ou avant, sur les lignes livrées."),
        MeasureSpec(name="complet", label="Livré complet", expr="AVG(ligne_complete)", format="percent",
                    thresholds=rate_thr, description="Lignes livrées avec au moins la quantité commandée."),
        MeasureSpec(name="otif", label="OTIF", expr="AVG(ligne_otif)", format="percent", thresholds=rate_thr,
                    description="On Time In Full : lignes livrées à l'heure et complètes, sur les lignes livrées."),
        MeasureSpec(name="retard_moyen", label="Retard moyen", expr="AVG(retard_jours)", format="decimal1",
                    unit="jours", description="Jours de retard moyens des lignes livrées en retard."),
        MeasureSpec(name="retards_en_cours", label="En retard non livrées",
                    expr="COALESCE(SUM(ligne_en_retard_ouverte), 0)", format="integer", unit="lignes",
                    description="Lignes non livrées dont la date promise précède la dernière livraison connue."),
        MeasureSpec(name="taux_service_qte", label="Taux de service quantité",
                    expr="SUM(qte_servie) / NULLIF(SUM(qte_due), 0)", format="percent",
                    description="Quantité livrée (plafonnée à la commande) sur la quantité commandée des lignes échues."),
    ]
    source = SourceSpec(
        name="livraisons",
        label="Lignes de commande livrées",
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
        FilterSpec(id="periode", label="Période (date promise)", field=pd_, type="relative_period",
                   default=default_period, presets=presets),
        FilterSpec(id="client", label="Client", field=cu, type="select", default=None),
    ]
    if ca:
        filters.append(FilterSpec(id="transporteur", label="Transporteur", field=ca, type="select", default=None))
    if fa:
        filters.append(FilterSpec(id="famille", label="Famille", field=fa, type="select", default=None))

    # ---- visuals -----------------------------------------------------
    def kpi(title: str, measure: str, **kw: Any) -> VisualSpec:
        return VisualSpec(type="kpi", title=title, source="livraisons", x=bucket,
                          kpi=KpiSpec(measure=measure, sparkline=measure, **kw))

    synthese = VisualSpec(
        type="text",
        source="livraisons",
        text=(
            "{{filters.client}}, du {{period.from}} au {{period.to}} : {{lignes|integer}} lignes de commande, "
            "{{lignes_livrees|integer}} livrées. OTIF de {{otif|percent}} : {{a_l_heure|percent}} livrées à l'heure "
            "et {{complet|percent}} complètes."
        ),
    )
    kpis = [
        kpi("OTIF", "otif", target_label="cible ≥ 95 %"),
        kpi("Livré à l'heure", "a_l_heure", target_label="cible ≥ 95 %"),
        kpi("Livré complet", "complet", secondary="Taux de service quantité : {{taux_service_qte|percent}}",
            target_label="cible ≥ 95 %"),
        kpi("Retard moyen", "retard_moyen", secondary="des lignes livrées en retard"),
        kpi("En retard non livrées", "retards_en_cours", secondary="date promise dépassée"),
    ]
    evolution = VisualSpec(
        type="combo",
        title="OTIF et livraisons à l'heure",
        subtitle=f"Par {bucket_label.lower()}, selon la date promise.",
        source="livraisons",
        x=bucket,
        series=[
            SeriesSpec(key="otif", label="OTIF", mark="bar", color=BLUE, use_thresholds=True),
            SeriesSpec(key="a_l_heure", label="À l'heure", mark="line", color=BROWN),
        ],
        reference_lines=[ReferenceLine(value=OTIF_TARGET, label="Objectif 95 %", color="good")],
        sort="x_asc",
    )

    def otif_by(title: str, column: str, filter_id: str) -> VisualSpec:
        return VisualSpec(
            type="hbar",
            title=title,
            subtitle="Les moins bons en premier. Cliquer pour filtrer le tableau de bord.",
            source="livraisons",
            x=column,
            series=[SeriesSpec(key="otif", label="OTIF", mark="bar", use_thresholds=True)],
            sort="value_asc",
            reference_lines=[ReferenceLine(value=OTIF_TARGET, label="95 %", color="good")],
            click_filters={filter_id: column},
            ignore_filters=[filter_id],
        )

    charts = [otif_by("OTIF par client", cu, "client")]
    if ca:
        charts.append(otif_by("OTIF par transporteur", ca, "transporteur"))
    if fa:
        charts.append(otif_by("OTIF par famille", fa, "famille"))

    table = VisualSpec(
        type="table",
        title="Performance par client",
        subtitle="La ligne « Toutes » reprend les définitions globales, pas la moyenne des clients.",
        source="livraisons",
        x=cu,
        columns=[
            ColumnSpec(key=cu, label="Client"),
            ColumnSpec(key="lignes", label="Lignes"),
            ColumnSpec(key="otif", label="OTIF", use_thresholds=True),
            ColumnSpec(key="a_l_heure", label="À l'heure", use_thresholds=True),
            ColumnSpec(key="complet", label="Complet", use_thresholds=True),
            ColumnSpec(key="retard_moyen", label="Retard moyen"),
        ],
        total_row=True,
        click_filters={"client": cu},
        ignore_filters=["client"],
        highlight_filter="client",
    )
    retards = VisualSpec(
        type="bar",
        title="Distribution des retards",
        subtitle="Lignes livrées, par jours de retard sur la date promise.",
        source="livraisons",
        x="tranche_retard",
        series=[SeriesSpec(key="lignes_livrees", label="Lignes livrées", mark="bar", color=BROWN)],
        sort="x_asc",
    )
    lecture = VisualSpec(
        type="text",
        title="Lecture des retards",
        source="livraisons",
        text=(
            "Les lignes en retard sont livrées en moyenne {{retard_moyen|decimal1}} jours après la date promise. "
            "{{retards_en_cours|integer}} lignes ne sont pas encore livrées alors que leur date promise est "
            "dépassée. Le taux de service quantité est de {{taux_service_qte|percent}}."
        ),
    )
    notes = [
        VisualSpec(
            type="note",
            title="Définitions",
            badges=[Provenance.REAL],
            text=(
                "**À l'heure** : livrée à la date promise ou avant. **Complet** : quantité livrée au moins égale à la "
                "quantité commandée. **OTIF** : à l'heure et complet. Les taux sont calculés sur les lignes livrées."
            ),
        ),
        VisualSpec(
            type="note",
            title="Seuils",
            text=(
                "Vert à partir de 95 % d'OTIF, orange entre 90 % et 95 %, rouge en dessous. La période se mesure sur la "
                "date promise."
            ),
        ),
        VisualSpec(
            type="note",
            title="Lignes non livrées",
            text=(
                "Une ligne non livrée est en retard si sa date promise précède la dernière livraison connue du fichier "
                "(et non la date du jour). Elle n'entre pas dans les taux tant qu'elle n'est pas livrée. Le taux de "
                "service quantité compte les lignes échues, livrées ou en retard."
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


def build(dataset_id: str, columns: dict, params: dict) -> dict:
    """Catalog entry point: column mapping (blank optional → None) to the template."""
    mapping: dict[str, Any] = {key: columns.get(key) or "" for key in _REQUIRED}
    mapping.update({key: columns.get(key) or None for key in _OPTIONAL})
    return delivery_otif(dataset_id, **mapping)
