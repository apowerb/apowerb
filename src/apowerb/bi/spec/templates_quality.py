"""
bi/spec/templates_quality.py
----------------------------
Spec v2 template « Qualité production »: one row per production lot or shift.

Every rate is a ratio of sums, so a group and the whole data set share the same
definition (the « Toutes » row of a table is computed on the total, not as the
mean of its rows). The TRS (OEE) is built only when planned time, run time and
ideal rate are all provided: availability = run / planned, performance =
produced / (run x ideal rate) capped at 1, quality = good / produced.
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

REJECT_TARGET = 0.02
REJECT_WARN = 0.05
TRS_TARGET = 0.85
TRS_WARN = 0.60

DESCRIPTOR: dict[str, Any] = {
    "id": "production_quality",
    "label": "Qualité production",
    "description": (
        "Suivi de la qualité de production à partir d'une ligne par lot ou par poste : taux de rebut, "
        "bon du premier coup, volumes, causes de rebut (Pareto) et, avec les temps planifiés, de marche et la "
        "cadence idéale, le TRS (disponibilité, performance, qualité)."
    ),
    "version": 1,
    "columns": [
        {"key": "date", "label": "Date", "type": "date", "required": True, "default": "date"},
        {"key": "line", "label": "Ligne de production", "type": "string", "required": True, "default": "ligne"},
        {"key": "product", "label": "Produit", "type": "string", "required": False, "default": None},
        {"key": "produced", "label": "Quantité produite", "type": "number", "required": True, "default": "qte_produite"},
        {"key": "rejected", "label": "Quantité rebutée", "type": "number", "required": True, "default": "qte_rebut"},
        {"key": "defect", "label": "Cause de rebut", "type": "string", "required": False, "default": None},
        {"key": "planned_time", "label": "Temps planifié", "type": "number", "required": False, "default": None},
        {"key": "run_time", "label": "Temps de marche", "type": "number", "required": False, "default": None},
        {"key": "ideal_rate", "label": "Cadence idéale (par unité de temps)", "type": "number", "required": False,
         "default": None},
    ],
    "params": [],
}
_REQUIRED = [c["key"] for c in DESCRIPTOR["columns"] if c["required"]]
_OPTIONAL = [c["key"] for c in DESCRIPTOR["columns"] if not c["required"]]


def _check_ident(label: str, value: str) -> str:
    if not _IDENT_RE.match(value or ""):
        raise ValueError(
            f"{label}='{value}' is not a valid column name: use lowercase letters, "
            "digits and underscores (start with a letter), e.g. 'qte_rebut'."
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


def production_quality(
    dataset_id: str,
    *,
    date: str = "date",
    line: str = "ligne",
    product: str | None = None,
    produced: str = "qte_produite",
    rejected: str = "qte_rebut",
    defect: str | None = None,
    planned_time: str | None = None,
    run_time: str | None = None,
    ideal_rate: str | None = None,
    default_period: str = "12w",
) -> dict:
    """Build the « Qualité production » spec for a dataset of lots or shifts."""
    if not dataset_id:
        raise ValueError("dataset_id is required.")
    d = _check_ident("date", date)
    ln = _check_ident("line", line)
    pr = _check_ident("product", product) if product else None
    qp = _check_ident("produced", produced)
    qr = _check_ident("rejected", rejected)
    df = _check_ident("defect", defect) if defect else None
    tp = _check_ident("planned_time", planned_time) if planned_time else None
    tr = _check_ident("run_time", run_time) if run_time else None
    ir = _check_ident("ideal_rate", ideal_rate) if ideal_rate else None
    with_trs = bool(tp and tr and ir)

    bucket, unit = _bucket(default_period)
    bucket_label = "Semaine" if unit == "week" else "Mois"
    P, R = f"SUM({_q(qp)})", f"SUM({_q(qr)})"

    # ---- transforms --------------------------------------------------
    transforms = [
        DeriveStep(column=bucket, expr=f"date_trunc('{unit}', CAST({_q(d)} AS TIMESTAMP))",
                   provenance=Provenance.REAL),
    ]

    # ---- fields ------------------------------------------------------
    fields = [
        FieldSpec(name=d, label="Date", type="date", role="date"),
        FieldSpec(name=ln, label="Ligne", role="dimension"),
    ]
    if pr:
        fields.append(FieldSpec(name=pr, label="Produit", role="dimension"))
    fields += [
        FieldSpec(name=qp, label="Quantité produite", type="number", role="measure", format="compact"),
        FieldSpec(name=qr, label="Quantité rebutée", type="number", role="measure", format="compact"),
    ]
    if df:
        fields.append(FieldSpec(name=df, label="Cause de rebut", role="dimension"))
    if with_trs:
        fields += [
            FieldSpec(name=tp, label="Temps planifié", type="number", role="measure", format="number"),
            FieldSpec(name=tr, label="Temps de marche", type="number", role="measure", format="number"),
            FieldSpec(name=ir, label="Cadence idéale", type="number", role="measure", format="number"),
        ]
    fields.append(FieldSpec(name=bucket, label=bucket_label, type="date", role="dimension"))

    # ---- measures ----------------------------------------------------
    reject_thr = ThresholdSpec(direction="lower_is_better", good=REJECT_TARGET, warn=REJECT_WARN)
    first_time_thr = ThresholdSpec(direction="higher_is_better", good=1 - REJECT_TARGET, warn=1 - REJECT_WARN)
    trs_thr = ThresholdSpec(direction="higher_is_better", good=TRS_TARGET, warn=TRS_WARN)
    measures = [
        MeasureSpec(name="volume_produit", label="Volume produit", expr=P, format="compact", unit="unités"),
        MeasureSpec(name="volume_rebut", label="Volume rebuté", expr=R, format="compact", unit="unités"),
        MeasureSpec(name="taux_rebut", label="Taux de rebut", expr=f"{R} / NULLIF({P}, 0)", format="percent",
                    thresholds=reject_thr, description="Quantité rebutée ÷ quantité produite."),
        MeasureSpec(name="bon_premier_coup", label="Bon du premier coup", expr=f"1 - {R} / NULLIF({P}, 0)",
                    format="percent", thresholds=first_time_thr,
                    description="Part de la production conforme dès le premier passage : 1 - rebut ÷ produit."),
    ]
    if df:
        top = lambda n: (  # noqa: E731
            f"(SELECT SUM(r) FROM (SELECT SUM({_q(qr)}) AS r FROM frame WHERE {_q(df)} IS NOT NULL "
            f"GROUP BY {_q(df)} ORDER BY r DESC LIMIT {n})) / NULLIF({R}, 0)"
        )
        for n, label in ((1, "Première cause"), (2, "Deux premières causes"), (3, "Trois premières causes")):
            measures.append(MeasureSpec(
                name=f"part_top{n}", label=f"{label} (part du rebut)", expr=top(n), format="percent",
                description="Part cumulée du rebut expliquée par les causes les plus fréquentes.",
            ))
    if with_trs:
        run, planned, ideal = f"SUM({_q(tr)})", f"SUM({_q(tp)})", f"SUM({_q(tr)} * {_q(ir)})"
        availability = f"{run} / NULLIF({planned}, 0)"
        performance = f"LEAST(1, {P} / NULLIF({ideal}, 0))"
        quality = f"({P} - {R}) / NULLIF({P}, 0)"
        measures += [
            MeasureSpec(name="disponibilite", label="Disponibilité", expr=availability, format="percent",
                        description="Temps de marche ÷ temps planifié."),
            MeasureSpec(name="performance", label="Performance", expr=performance, format="percent",
                        description="Quantité produite ÷ (temps de marche × cadence idéale), plafonnée à 100 %."),
            MeasureSpec(name="qualite", label="Qualité", expr=quality, format="percent",
                        description="Quantité bonne ÷ quantité produite."),
            MeasureSpec(name="trs", label="TRS", expr=f"({availability}) * {performance} * ({quality})",
                        format="percent", thresholds=trs_thr,
                        description="Taux de rendement synthétique : disponibilité × performance × qualité."),
        ]
    source = SourceSpec(
        name="production",
        label="Lots de production",
        kind="dataset",
        dataset_id=dataset_id,
        transforms=transforms,
        fields=fields,
        measures=measures,
    )

    # ---- filters -----------------------------------------------------
    presets = ["4w", "12w", "26w", "52w", "all"]
    if default_period not in presets:
        presets.insert(0, default_period)
    filters = [
        FilterSpec(id="periode", label="Période", field=d, type="relative_period", default=default_period,
                   presets=presets),
        FilterSpec(id="ligne", label="Ligne", field=ln, type="select", default=None),
    ]
    if pr:
        filters.append(FilterSpec(id="produit", label="Produit", field=pr, type="select", default=None))

    # ---- visuals -----------------------------------------------------
    def kpi(title: str, measure: str, **kw: Any) -> VisualSpec:
        return VisualSpec(type="kpi", title=title, source="production", x=bucket,
                          kpi=KpiSpec(measure=measure, sparkline=measure, **kw))

    trs_sentence = " Le TRS est de {{trs|percent}}." if with_trs else ""
    synthese = VisualSpec(
        type="text",
        source="production",
        text=(
            "{{filters.ligne}}, du {{period.from}} au {{period.to}} : {{volume_produit|compact}} unités produites, "
            "dont {{volume_rebut|compact}} rebutées, soit un taux de rebut de {{taux_rebut|percent}} "
            "({{bon_premier_coup|percent}} bon du premier coup)." + trs_sentence
        ),
    )
    kpis = [
        kpi("Taux de rebut", "taux_rebut", target_label="cible ≤ 2 %"),
        kpi("Bon du premier coup", "bon_premier_coup", target_label="cible ≥ 98 %"),
        kpi("Volume produit", "volume_produit", secondary="{{volume_rebut|compact}} rebutées"),
        kpi("Volume rebuté", "volume_rebut", secondary="{{taux_rebut|percent}} de la production"),
    ]
    if with_trs:
        kpis.append(kpi("TRS", "trs",
                        secondary="Dispo. {{disponibilite|percent}} · perf. {{performance|percent}} · "
                                  "qualité {{qualite|percent}}",
                        target_label="cible ≥ 85 %"))
    evolution = VisualSpec(
        type="combo",
        title=f"Taux de rebut par {bucket_label.lower()}",
        subtitle="Volume produit en barres, taux de rebut en ligne.",
        source="production",
        x=bucket,
        series=[
            SeriesSpec(key="volume_produit", label="Volume produit", mark="bar", color="muted", axis="right"),
            SeriesSpec(key="taux_rebut", label="Taux de rebut", mark="line", color="bad"),
        ],
        reference_lines=[ReferenceLine(value=REJECT_TARGET, label="Objectif 2 %", color="good")],
        sort="x_asc",
    )

    def reject_by(title: str, column: str, filter_id: str) -> VisualSpec:
        return VisualSpec(
            type="hbar",
            title=title,
            subtitle="Les plus élevés en premier. Cliquer pour filtrer le tableau de bord.",
            source="production",
            x=column,
            series=[SeriesSpec(key="taux_rebut", label="Taux de rebut", mark="bar", use_thresholds=True)],
            sort="value_desc",
            reference_lines=[ReferenceLine(value=REJECT_TARGET, label="2 %", color="good")],
            click_filters={filter_id: column},
            ignore_filters=[filter_id],
        )

    charts = [reject_by("Taux de rebut par ligne", ln, "ligne")]
    if pr:
        charts.append(reject_by("Taux de rebut par produit", pr, "produit"))
    if df:
        charts.append(VisualSpec(
            type="hbar",
            title="Causes de rebut",
            subtitle="Quantité rebutée par cause, de la plus fréquente à la moins fréquente (Pareto).",
            source="production",
            x=df,
            series=[SeriesSpec(key="volume_rebut", label="Rebut", mark="bar", color=BROWN)],
            sort="value_desc",
        ))
    pareto_text = VisualSpec(
        type="text",
        title="Part cumulée des causes",
        source="production",
        text=(
            "La première cause explique {{part_top1|percent}} du rebut, les deux premières {{part_top2|percent}} "
            "et les trois premières {{part_top3|percent}}."
        ),
    ) if df else None

    table_columns = [
        ColumnSpec(key=ln, label="Ligne"),
        ColumnSpec(key="volume_produit", label="Produit"),
        ColumnSpec(key="volume_rebut", label="Rebut"),
        ColumnSpec(key="taux_rebut", label="Taux de rebut", use_thresholds=True),
    ]
    if with_trs:
        table_columns.append(ColumnSpec(key="trs", label="TRS", use_thresholds=True))
    table = VisualSpec(
        type="table",
        title="Performance par ligne",
        subtitle="La ligne « Toutes » est calculée sur le total, pas comme la moyenne des lignes.",
        source="production",
        x=ln,
        columns=table_columns,
        total_row=True,
        click_filters={"ligne": ln},
        ignore_filters=["ligne"],
        highlight_filter="ligne",
    )
    notes = [
        VisualSpec(
            type="note",
            title="Définitions",
            badges=[Provenance.REAL],
            text=(
                "**Taux de rebut** : quantité rebutée ÷ quantité produite. **Bon du premier coup** : 1 - taux de "
                "rebut. Chaque taux est calculé sur les quantités cumulées, jamais comme la moyenne de taux."
            ),
        ),
        VisualSpec(
            type="note",
            title="Seuils",
            text=(
                "Rebut : vert jusqu'à 2 %, orange jusqu'à 5 %, rouge au-delà."
                + (" TRS : vert à partir de 85 %, orange à partir de 60 %, rouge en dessous." if with_trs else "")
            ),
        ),
    ]
    if with_trs:
        notes.append(VisualSpec(
            type="note",
            title="TRS",
            text=(
                "**TRS** = disponibilité × performance × qualité. **Disponibilité** : temps de marche ÷ temps "
                "planifié. **Performance** : quantité produite ÷ (temps de marche × cadence idéale), plafonnée à "
                "100 %. **Qualité** : quantité bonne ÷ quantité produite."
            ),
        ))

    # ---- grid --------------------------------------------------------
    grid: list[tuple[VisualSpec, dict[str, int]]] = [(synthese, dict(row=0, col=0, width=12, height=2))]
    col = 0
    for tile, width in zip(kpis, _spread(len(kpis))):
        grid.append((tile, dict(row=2, col=col, width=width, height=3)))
        col += width
    grid.append((evolution, dict(row=5, col=0, width=12, height=6)))
    placed, row = _flow(charts, 11, 6)
    grid += placed
    if pareto_text is not None:
        grid.append((pareto_text, dict(row=row, col=0, width=12, height=2)))
        row += 2
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


def build(dataset_id: str, columns: dict, params: dict) -> dict:
    """Catalog entry point: column mapping (blank optional → None) to the template."""
    mapping: dict[str, Any] = {key: columns.get(key) or "" for key in _REQUIRED}
    mapping.update({key: columns.get(key) or None for key in _OPTIONAL})
    return production_quality(dataset_id, **mapping)
