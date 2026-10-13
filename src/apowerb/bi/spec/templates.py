"""
bi/spec/templates.py
--------------------
Ready-made dashboard specs (v2) built from the models in ``bi/spec/models.py``.

``stock_pilotage`` reproduces the "Pilotage stocks" dashboard: one dataset of
weekly sales per product family, a transparent naive forecast, a simulated
stock/replenishment loop, six traffic-light KPIs, charts, a per-family table,
a waterfall and five explanatory notes. Given a ``stock`` column it builds the
replenishment plan instead: real stock, engine forecast over the horizon and
the orders to place, family by family (``replenishment_plan`` step).

The result is plain JSON-serialisable data ``{"sources", "filters",
"components"}`` so it can be stored in a dashboard or passed to the tools.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from apowerb.bi.spec.models import (
    IDENT,
    AggregateStep,
    ColorRule,
    ColumnSpec,
    DeriveStep,
    FieldSpec,
    FilterSpec,
    FilterStep,
    ForecastStep,
    InventorySimStep,
    KpiSpec,
    MeasureSpec,
    NaiveForecastStep,
    Provenance,
    ReplenishmentPlanStep,
    ReferenceLine,
    SeriesSpec,
    SourceSpec,
    ThresholdSpec,
    VisualSpec,
    WaterfallStep,
)

_IDENT_RE = re.compile(IDENT)

BROWN = "#9a6b3f"
BLUE = "#3b6fd8"

_DEFAULT_LOT = InventorySimStep.model_fields["default_lot"].default


def _fr(value: float, digits: int = 2) -> str:
    """French number: decimal comma, no trailing zeros."""
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def _check_ident(label: str, value: str) -> str:
    if not _IDENT_RE.match(value or ""):
        raise ValueError(
            f"{label}='{value}' is not a valid column name: use lowercase letters, "
            "digits and underscores (start with a letter), e.g. 'semaine'."
        )
    return value


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True)


def stock_pilotage(
    dataset_id: str,
    *,
    date: str = "semaine",
    family: str = "famille",
    sales: str = "ventes",
    site: str | None = None,
    safety_factor: float = 0.5,
    order_factor: float = 1.4,
    lead_time: int = 1,
    lot_sizes: dict | None = None,
    default_period: str = "26w",
    horizon: int = 8,
    with_engine_forecast: bool = True,
    stock: str | None = None,
    safety_column: str | None = None,
    lead_time_column: str | None = None,
    target_cover: int = 4,
    keep_history: int = 12,
) -> dict:
    """Build the "Pilotage stocks" spec for a weekly sales dataset.

    ``date``/``family``/``sales`` name the dataset columns; ``site`` is the
    optional column to sum over when the data has one row per site and week.
    ``stock`` names the real stock column: the dashboard then plans the
    orders from it (``safety_column``/``lead_time_column`` per family, else
    ``safety_factor`` weeks of forecast and ``lead_time``) instead of
    simulating the stock.
    """
    d = _check_ident("date", date)
    f = _check_ident("family", family)
    s = _check_ident("sales", sales)
    if site:
        _check_ident("site", site)
    if not dataset_id:
        raise ValueError("dataset_id is required.")
    if stock:
        return _replenishment_spec(
            dataset_id, d, f, s,
            stock=_check_ident("stock", stock),
            site=site,
            safety_column=_check_ident("safety_column", safety_column) if safety_column else None,
            lead_time_column=_check_ident("lead_time_column", lead_time_column) if lead_time_column else None,
            safety_weeks=safety_factor,
            lead_time=lead_time,
            target_cover=target_cover,
            keep_history=keep_history,
            lot_sizes=lot_sizes,
            default_period=default_period,
            horizon=horizon,
            with_engine_forecast=with_engine_forecast,
        )

    last_week = f"{d} = (SELECT MAX({d}) FROM frame)"
    first_week = f"{d} = (SELECT MIN({d}) FROM frame)"
    lots = {str(k): int(v) for k, v in (lot_sizes or {}).items()}

    # ---- transforms --------------------------------------------------
    transforms: list[Any] = []
    if site:
        transforms.append(AggregateStep(group_by=[d, f], aggregates={s: f"SUM({s})"}))
    transforms += [
        NaiveForecastStep(date=d, value=s, by=[f], output="prevision"),
        InventorySimStep(
            date=d,
            by=[f],
            demand=s,
            forecast="prevision",
            safety_factor=safety_factor,
            order_factor=order_factor,
            lead_time=lead_time,
            lot_sizes=lots,
            lot_column=f,
        ),
        DeriveStep(column="ecart", expr=f"{s} - prevision", provenance=Provenance.FORECAST),
        DeriveStep(column="couverture_semaine", expr="stock_fin / NULLIF(prevision, 0)"),
        DeriveStep(
            column="sous_secu",
            expr="CASE WHEN stock_fin < stock_securite THEN 1 ELSE 0 END",
        ),
        DeriveStep(
            column="mois",
            expr=f"strftime(CAST({d} AS DATE), '%Y-%m')",
            provenance=Provenance.REAL,
        ),
    ]

    # ---- fields ------------------------------------------------------
    sim = Provenance.SIMULATED
    fields = [
        FieldSpec(name=d, label=d.replace("_", " ").capitalize(), type="date", role="date"),
        FieldSpec(name=f, label=f.replace("_", " ").capitalize(), role="dimension"),
        FieldSpec(name=s, label="Ventes", type="number", role="measure", provenance=Provenance.REAL, format="compact"),
        FieldSpec(name="prevision", label="Prévision", type="number", role="measure", provenance=Provenance.FORECAST, format="compact"),
        FieldSpec(name="stock_debut", label="Stock début de semaine", type="number", role="measure", provenance=sim, format="compact"),
        FieldSpec(name="appro", label="Approvisionnements reçus", type="number", role="measure", provenance=sim, format="compact"),
        FieldSpec(name="ventes_servies", label="Ventes servies", type="number", role="measure", provenance=sim, format="compact"),
        FieldSpec(name="demande_non_servie", label="Demande non servie", type="number", role="measure", provenance=sim, format="compact"),
        FieldSpec(name="stock_fin", label="Stock fin de semaine", type="number", role="measure", provenance=sim, format="compact"),
        FieldSpec(name="stock_securite", label="Stock de sécurité", type="number", role="measure", provenance=sim, format="compact"),
        FieldSpec(name="commande", label="Commande", type="number", role="measure", provenance=sim, format="compact"),
        FieldSpec(name="ecart", label="Écart ventes - prévision", type="number", role="measure", provenance=Provenance.FORECAST, format="compact"),
        FieldSpec(name="couverture_semaine", label="Couverture", type="number", role="measure", provenance=sim, format="weeks"),
        FieldSpec(name="sous_secu", label="Sous le stock de sécurité", type="boolean", role="measure", provenance=sim),
        FieldSpec(name="mois", label="Mois", type="string", role="dimension", provenance=Provenance.REAL),
    ]

    # ---- measures ----------------------------------------------------
    cov_thr = ThresholdSpec(direction="higher_is_better", good=1, warn=0.5)
    svc_thr = ThresholdSpec(direction="higher_is_better", good=0.99, warn=0.97)
    rel_thr = ThresholdSpec(direction="higher_is_better", good=0.9, warn=0.8)
    risk_thr = ThresholdSpec(direction="lower_is_better", good=0, warn=2)
    stock_last = f"SUM(stock_fin) FILTER (WHERE {last_week})"
    prev_week = f"{d} = (SELECT MAX({d}) FROM frame WHERE {d} < (SELECT MAX({d}) FROM frame))"
    measures = [
        MeasureSpec(name="stock_fin_semaine", label="Stock fin de semaine", expr=stock_last, format="compact", unit="unités",
                    description="Stock de la dernière semaine de la période."),
        MeasureSpec(name="stock_semaine_precedente", label="Stock semaine précédente",
                    expr=f"SUM(stock_fin) FILTER (WHERE {prev_week})", format="compact", unit="unités"),
        MeasureSpec(name="variation_stock", label="Variation du stock",
                    expr=f"{stock_last} / NULLIF(SUM(stock_fin) FILTER (WHERE {prev_week}), 0) - 1",
                    format="percent", description="Stock de la dernière semaine par rapport à la semaine précédente."),
        MeasureSpec(name="couverture", label="Couverture",
                    expr=f"{stock_last} / NULLIF(SUM(prevision) FILTER (WHERE {last_week}), 0)",
                    format="weeks", thresholds=cov_thr,
                    description="Stock fin de semaine ÷ prévision de cette semaine."),
        MeasureSpec(name="taux_service", label="Taux de service",
                    expr=f"1 - SUM(demande_non_servie) / NULLIF(SUM({s}), 0)",
                    format="percent", thresholds=svc_thr,
                    description="Part de la demande servie sur la période."),
        MeasureSpec(name="fiabilite", label="Fiabilité de la prévision",
                    expr=f"1 - SUM(ABS({s} - prevision)) / NULLIF(SUM({s}), 0)",
                    format="percent", thresholds=rel_thr,
                    description="1 moins l'erreur absolue totale rapportée aux ventes."),
        MeasureSpec(name="biais", label="Biais de prévision",
                    expr=f"(SUM({s}) - SUM(prevision)) / NULLIF(SUM(prevision), 0)",
                    format="percent",
                    description="Positif : la prévision est inférieure aux ventes."),
        MeasureSpec(name="semaines_sous_secu", label="Semaines sous le stock de sécurité",
                    expr=(
                        f"COUNT(DISTINCT {d}) FILTER (WHERE {d} IN "
                        f"(SELECT {d} FROM frame GROUP BY {d} HAVING SUM(stock_fin) < SUM(stock_securite)))"
                    ),
                    format="integer", unit="semaines", thresholds=risk_thr,
                    description="Semaines où le stock total est inférieur au stock de sécurité total."),
        MeasureSpec(name="semaines_sous_secu_famille", label="Semaines sous sécu",
                    expr=f"COUNT(DISTINCT {d}) FILTER (WHERE stock_fin < stock_securite)",
                    format="integer", unit="semaines", thresholds=risk_thr,
                    description="Semaines où le stock de la famille est inférieur à son stock de sécurité."),
        MeasureSpec(name="appro_recus", label="Approvisionnements reçus", expr="SUM(appro)", format="compact", unit="unités"),
        MeasureSpec(name="ventes_totales", label="Ventes", expr=f"SUM({s})", format="compact", unit="unités"),
        MeasureSpec(name="stock_debut_periode", label="Stock début de période",
                    expr=f"SUM(stock_debut) FILTER (WHERE {first_week})", format="compact", unit="unités"),
        MeasureSpec(name="ventes_servies_total", label="Ventes servies", expr="SUM(ventes_servies)", format="compact", unit="unités"),
        MeasureSpec(name="erreur_abs", label="Erreur absolue de prévision", expr=f"SUM(ABS({s} - prevision))", format="compact", unit="unités"),
        MeasureSpec(name="stock_total", label="Stock fin de semaine", expr="SUM(stock_fin)", format="compact", unit="unités"),
        MeasureSpec(name="prevision_totale", label="Prévision", expr="SUM(prevision)", format="compact", unit="unités"),
        MeasureSpec(name="appro_total", label="Approvisionnements", expr="SUM(appro)", format="compact", unit="unités"),
        MeasureSpec(name="securite_totale", label="Stock de sécurité", expr="SUM(stock_securite)", format="compact", unit="unités"),
        MeasureSpec(name="ecart_total", label="Écart ventes - prévision", expr="SUM(ecart)", format="compact", unit="unités"),
        MeasureSpec(name="couverture_hebdo", label="Couverture par semaine",
                    expr="SUM(stock_fin) / NULLIF(SUM(prevision), 0)", format="weeks", thresholds=cov_thr),
    ]

    stocks = SourceSpec(
        name="stocks",
        label="Stocks et prévisions",
        kind="dataset",
        dataset_id=dataset_id,
        transforms=transforms,
        fields=fields,
        measures=measures,
    )
    sources: list[SourceSpec] = [stocks]
    if with_engine_forecast:
        sources.append(
            SourceSpec(
                name="prevision_future",
                label="Prévision moteur (périodes futures)",
                kind="source",
                parent="stocks",
                transforms=[ForecastStep(date=d, value=s, by=[f], horizon=horizon, output="prevision_moteur")],
                fields=[
                    FieldSpec(name="prevision_moteur", label="Prévision moteur", type="number", role="measure",
                              provenance=Provenance.FORECAST, format="compact"),
                ],
                measures=[
                    MeasureSpec(name="prevision_moteur_totale", label="Prévision moteur", expr="SUM(prevision_moteur)",
                                format="compact", unit="unités"),
                ],
            )
        )

    # ---- filters -----------------------------------------------------
    filters = [
        FilterSpec(id="famille", label="Famille", field=f, type="select", default=None,
                   applies_to=None),
        FilterSpec(id="periode", label="Période", field=d, type="relative_period", default=default_period,
                   applies_to=["stocks"] if with_engine_forecast else None,
                   presets=["13w", "26w", "52w", "all"]),
    ]

    # ---- visuals -----------------------------------------------------
    def kpi(title: str, measure: str, **kw: Any) -> VisualSpec:
        return VisualSpec(type="kpi", title=title, source="stocks", kpi=KpiSpec(measure=measure, **kw))

    synthese = VisualSpec(
        type="text",
        source="stocks",
        text=(
            "{{filters.famille}}, semaine du {{period.last}} : stock de {{stock_fin_semaine|compact}} unités, "
            "soit {{couverture|weeks}} de couverture. Sur la période, la demande a été servie à "
            "{{taux_service|percent}}, la prévision est fiable à {{fiabilite|percent}} et le stock est passé "
            "{{semaines_sous_secu|integer}} fois sous le stock de sécurité."
        ),
    )
    combo = VisualSpec(
        type="combo",
        title="Stock, prévision et approvisionnements par semaine",
        source="stocks",
        x=d,
        series=[
            SeriesSpec(key="stock_total", label="Stock fin de semaine", mark="area", color="good"),
            SeriesSpec(key="prevision_totale", label="Prévision", mark="line", color=BROWN),
            SeriesSpec(key="appro_total", label="Approvisionnements", mark="bar", color=BLUE),
            SeriesSpec(key="securite_totale", label="Stock de sécurité", mark="line", color="muted", dash=True),
        ],
        sort="x_asc",
    )
    ecart = VisualSpec(
        type="diverging_bar",
        title="Écart ventes - prévision",
        subtitle="Positif : les ventes dépassent la prévision.",
        source="stocks",
        x=d,
        series=[
            SeriesSpec(
                key="ecart_total",
                label="Écart",
                mark="bar",
                color_rules=[ColorRule(op=">", value=0, color="good"), ColorRule(op="<", value=0, color=BROWN)],
            )
        ],
        sort="x_asc",
    )
    couverture = VisualSpec(
        type="bar",
        title="Couverture par semaine",
        subtitle="Semaines de ventes couvertes par le stock.",
        source="stocks",
        x=d,
        series=[SeriesSpec(key="couverture_hebdo", label="Couverture", mark="bar", use_thresholds=True)],
        reference_lines=[
            ReferenceLine(value=1, label="1 semaine", color="good"),
            ReferenceLine(value=0.5, label="0,5 semaine", color="bad"),
        ],
        sort="x_asc",
    )
    table = VisualSpec(
        type="table",
        title="Comparatif par famille",
        subtitle="Cliquer sur une famille pour filtrer le tableau de bord.",
        source="stocks",
        x=f,
        columns=[
            ColumnSpec(key=f, label=f.replace("_", " ").capitalize()),
            ColumnSpec(key="stock_fin_semaine", label="Stock fin"),
            ColumnSpec(key="couverture", label="Couverture", use_thresholds=True),
            ColumnSpec(key="taux_service", label="Taux de service", use_thresholds=True),
            ColumnSpec(key="fiabilite", label="Fiabilité", use_thresholds=True),
            ColumnSpec(key="biais", label="Biais"),
            ColumnSpec(key="semaines_sous_secu_famille", label="Semaines sous sécu", use_thresholds=True,
                       total_measure="semaines_sous_secu"),
        ],
        total_row=True,
        click_filters={"famille": f},
        ignore_filters=["famille"],
        highlight_filter="famille",
    )
    waterfall = VisualSpec(
        type="waterfall",
        title="Pourquoi le stock a bougé",
        subtitle="De la première à la dernière semaine de la période.",
        source="stocks",
        steps=[
            WaterfallStep(label="Stock début", measure="stock_debut_periode", kind="start"),
            WaterfallStep(label="Approvisionnements", measure="appro_recus", kind="increase", color=BLUE),
            WaterfallStep(label="Ventes servies", measure="ventes_servies_total", kind="decrease", color=BROWN),
            WaterfallStep(label="Stock fin", measure="stock_fin_semaine", kind="end"),
        ],
    )
    poids = VisualSpec(
        type="hbar",
        title="Qui pèse dans l'erreur de prévision",
        subtitle="Somme des écarts absolus entre ventes et prévision.",
        source="stocks",
        x=f,
        series=[SeriesSpec(key="erreur_abs", label="Erreur absolue", mark="bar", color=BROWN)],
        sort="value_desc",
        ignore_filters=["famille"],
    )
    biais_mois = VisualSpec(
        type="bar",
        title="Quand la prévision se trompe",
        subtitle="Biais moyen par mois : positif quand les ventes dépassent la prévision.",
        source="stocks",
        x="mois",
        series=[SeriesSpec(key="biais", label="Biais", mark="bar", color="muted")],
        sort="x_asc",
    )

    lot_text = (
        "Les commandes sont arrondies au lot de chaque famille (" + ", ".join(f"{k} : {v}" for k, v in lots.items()) + ")."
        if lots
        else f"Les commandes sont arrondies au lot de {_DEFAULT_LOT} unités."
    )
    notes = [
        VisualSpec(
            type="note",
            title="Origine des données",
            badges=[Provenance.REAL, Provenance.FORECAST, Provenance.SIMULATED],
            text=(
                f"Les ventes (`{s}`) sont les données réelles du fichier importé. La prévision est calculée à partir "
                "de ces ventes. Les stocks, approvisionnements et commandes sont simulés : ils n'ont pas été mesurés "
                "en entrepôt."
            ),
        ),
        VisualSpec(
            type="note",
            title="Méthode de prévision",
            badges=[Provenance.FORECAST],
            text=(
                "La prévision est volontairement simple : au début de l'historique, moyenne des 4 semaines "
                "précédentes ; ensuite, ventes de la même semaine l'an dernier, corrigées de la tendance récente "
                "(bornée entre 0,8 et 1,25)."
            ),
        ),
        VisualSpec(
            type="note",
            title="Règles de réapprovisionnement simulées",
            badges=[Provenance.SIMULATED],
            text=(
                f"Stock de sécurité : {_fr(safety_factor)} × la prévision de la semaine. Commande : "
                f"{_fr(order_factor)} × la prévision de la semaine suivante + stock de sécurité - stock en fin de "
                f"semaine. Délai de livraison : {lead_time} semaine{'s' if lead_time > 1 else ''}. {lot_text}"
            ),
        ),
        VisualSpec(
            type="note",
            title="Définition des KPI",
            text=(
                "**Couverture** : stock de fin de semaine ÷ prévision de la semaine. **Taux de service** : part de la "
                "demande servie. **Fiabilité** : 1 - erreur absolue ÷ ventes. **Biais** : (ventes - prévision) ÷ "
                "prévision. **Semaines sous sécu** : semaines où le stock total passe sous le stock de sécurité total."
            ),
        ),
        VisualSpec(
            type="note",
            title="Limites",
            text=(
                "Les stocks sont simulés : ils illustrent la méthode, ils ne remplacent pas l'inventaire réel. La "
                "prévision ne connaît ni les promotions ni les ruptures passées. Les chiffres dépendent des "
                "paramètres de simulation ci-contre."
            ),
        ),
    ]

    grid: list[tuple[VisualSpec, dict[str, int]]] = [(synthese, dict(row=0, col=0, width=12, height=2))]
    kpis = [
        kpi("Stock fin de semaine", "stock_fin_semaine", sparkline="stock_total", secondary="{{variation_stock|percent}} vs semaine précédente"),
        kpi("Couverture", "couverture", sparkline="couverture_hebdo", secondary="Stock ÷ prévision de la semaine"),
        kpi("Taux de service", "taux_service", sparkline="taux_service", target_label="cible ≥ 99 %"),
        kpi("Fiabilité de la prévision", "fiabilite", secondary="Biais : {{biais|percent}}", target_label="cible ≥ 90 %"),
        kpi("Semaines sous sécu", "semaines_sous_secu", secondary="risque de rupture"),
        kpi("Approvisionnements reçus", "appro_recus", sparkline="appro_total", secondary="Ventes : {{ventes_totales|compact}}"),
    ]
    for i, k in enumerate(kpis):
        grid.append((k, dict(row=2, col=2 * i, width=2, height=3)))
    grid += [
        (combo, dict(row=5, col=0, width=12, height=6)),
        (ecart, dict(row=11, col=0, width=6, height=5)),
        (couverture, dict(row=11, col=6, width=6, height=5)),
        (table, dict(row=16, col=0, width=12, height=5)),
        (waterfall, dict(row=21, col=0, width=6, height=6)),
        (poids, dict(row=21, col=6, width=6, height=6)),
        (biais_mois, dict(row=27, col=0, width=12, height=5)),
        (notes[0], dict(row=32, col=0, width=4, height=4)),
        (notes[1], dict(row=32, col=4, width=4, height=4)),
        (notes[2], dict(row=32, col=8, width=4, height=4)),
        (notes[3], dict(row=36, col=0, width=6, height=4)),
        (notes[4], dict(row=36, col=6, width=6, height=4)),
    ]

    return {
        "sources": [_dump(src) for src in sources],
        "filters": [_dump(flt) for flt in filters],
        "components": [{"visual": _dump(v), "position": pos} for v, pos in grid],
    }


def _replenishment_spec(
    dataset_id: str,
    d: str,
    f: str,
    s: str,
    *,
    stock: str,
    site: str | None,
    safety_column: str | None,
    lead_time_column: str | None,
    safety_weeks: float,
    lead_time: int,
    target_cover: int,
    keep_history: int,
    lot_sizes: dict | None,
    default_period: str,
    horizon: int,
    with_engine_forecast: bool,
) -> dict:
    """Real stock + orders to place over the horizon (see ``stock_pilotage``)."""
    lots = {str(k): int(v) for k, v in (lot_sizes or {}).items()}
    real = Provenance.REAL
    fc = Provenance.FORECAST
    sim = Provenance.SIMULATED

    # ---- history: the dataset as imported --------------------------------
    hist_transforms: list[Any] = []
    if site:
        aggregates = {s: f"SUM({s})", stock: f"SUM({stock})"}
        if safety_column:
            aggregates[safety_column] = f"SUM({safety_column})"
        if lead_time_column:
            aggregates[lead_time_column] = f"MAX({lead_time_column})"
        hist_transforms.append(AggregateStep(group_by=[d, f], aggregates=aggregates))
    hist_fields = [
        FieldSpec(name=d, label=d.replace("_", " ").capitalize(), type="date", role="date"),
        FieldSpec(name=f, label=f.replace("_", " ").capitalize(), role="dimension"),
        FieldSpec(name=s, label="Ventes", type="number", role="measure", provenance=real, format="compact"),
        FieldSpec(name=stock, label="Stock", type="number", role="measure", provenance=real, format="compact"),
    ]
    hist_measures = [
        MeasureSpec(name="stock_hebdo", label="Stock", expr=f"SUM({stock})", format="compact", unit="unités"),
        MeasureSpec(name="ventes_hebdo", label="Ventes", expr=f"SUM({s})", format="compact", unit="unités"),
    ]
    if safety_column:
        hist_fields.append(FieldSpec(name=safety_column, label="Seuil de sécurité", type="number", role="measure",
                                     provenance=real, format="compact"))
        hist_measures.append(MeasureSpec(name="seuil_hebdo", label="Seuil de sécurité", expr=f"SUM({safety_column})",
                                         format="compact", unit="unités"))
    historique = SourceSpec(name="stocks", label="Historique des stocks", kind="dataset", dataset_id=dataset_id,
                            transforms=hist_transforms, fields=hist_fields, measures=hist_measures)

    # ---- plan: engine forecast, then the orders ---------------------------
    plan_transforms: list[Any] = []
    if with_engine_forecast:
        plan_transforms.append(ForecastStep(date=d, value=s, by=[f], horizon=horizon, output="prevision_moteur"))
    plan_transforms.append(
        ReplenishmentPlanStep(
            date=d, by=[f], stock=stock, demand=s, forecast="prevision_moteur",
            safety_column=safety_column, lead_time_column=lead_time_column,
            safety_weeks=safety_weeks, lead_time=lead_time, target_cover=target_cover,
            horizon=horizon, keep_history=keep_history, lot_sizes=lots,
        )
    )
    now = "semaine_plan = 0"
    first = "semaine_plan = 1"
    future = "semaine_plan >= 1"
    weeks = f"COUNT(DISTINCT {d}) FILTER (WHERE {future})"
    stock_now = f"SUM({stock}) FILTER (WHERE {now})"
    plan_fields = [
        FieldSpec(name="prevision_moteur", label="Prévision", type="number", role="measure", provenance=fc,
                  format="compact"),
        FieldSpec(name="stock_projete", label="Stock (réel puis projeté)", type="number", role="measure",
                  provenance=fc, format="compact"),
        FieldSpec(name="stock_sans_commande", label="Stock sans commande", type="number", role="measure",
                  provenance=fc, format="compact"),
        FieldSpec(name="reception", label="Approvisionnements", type="number", role="measure", provenance=sim,
                  format="compact"),
        FieldSpec(name="commande", label="Commande à passer", type="number", role="measure", provenance=sim,
                  format="compact"),
        FieldSpec(name="seuil_plan", label="Seuil de sécurité", type="number", role="measure",
                  provenance=real if safety_column else sim, format="compact"),
        FieldSpec(name="delai_plan", label="Délai fournisseur", type="number", role="measure",
                  provenance=real if lead_time_column else sim, format="integer"),
        FieldSpec(name="risque", label="Risque", type="number", role="measure", provenance=fc, format="integer"),
        FieldSpec(name="semaine_plan", label="Semaine du plan", type="number", role="dimension", format="integer"),
    ]
    no_risk = ThresholdSpec(direction="lower_is_better", good=0, warn=0)
    plan_measures = [
        MeasureSpec(name="stock_actuel", label="Stock actuel", expr=stock_now, format="compact", unit="unités",
                    description="Stock de la dernière semaine connue."),
        MeasureSpec(name="couverture_actuelle", label="Couverture",
                    expr=f"{stock_now} / NULLIF(SUM(prevision_moteur) FILTER (WHERE {future}) / NULLIF({weeks}, 0), 0)",
                    format="weeks", thresholds=ThresholdSpec(direction="higher_is_better", good=4, warn=2),
                    description="Stock actuel ÷ prévision moyenne d'une semaine de l'horizon."),
        MeasureSpec(name="prevision_semaine", label="Prévision de la semaine",
                    expr=f"SUM(prevision_moteur) FILTER (WHERE {first})", format="compact", unit="unités"),
        MeasureSpec(name="prevision_horizon", label="Prévision sur l'horizon",
                    expr=f"SUM(prevision_moteur) FILTER (WHERE {future})", format="compact", unit="unités"),
        MeasureSpec(name="commande_semaine", label="À commander cette semaine",
                    expr=f"COALESCE(SUM(commande) FILTER (WHERE {first}), 0)", format="compact", unit="unités"),
        MeasureSpec(name="familles_a_commander", label="Familles à commander cette semaine",
                    expr=f"COUNT(DISTINCT {f}) FILTER (WHERE {first} AND commande > 0)", format="integer"),
        MeasureSpec(name="commandes_horizon", label="Commandes sur l'horizon",
                    expr=f"COALESCE(SUM(commande) FILTER (WHERE {future}), 0)", format="compact", unit="unités"),
        MeasureSpec(name="prochaine_commande", label="Prochaine commande",
                    expr=f"MIN(semaine_plan) FILTER (WHERE {future} AND commande > 0)", format="integer",
                    unit="semaine", description="Semaine du plan où passer la prochaine commande (1 = cette semaine)."),
        MeasureSpec(name="familles_a_risque", label="Familles à risque",
                    expr=f"COUNT(DISTINCT {f}) FILTER (WHERE risque = 2)", format="integer", thresholds=no_risk,
                    description="Familles qui passent sous le seuil avant qu'une commande puisse arriver."),
        MeasureSpec(name="semaines_sous_seuil", label="Semaines sous le seuil",
                    expr="COUNT(*) FILTER (WHERE risque = 2)", format="integer", unit="semaines", thresholds=no_risk,
                    description="Semaines de l'horizon sous le seuil malgré les commandes."),
        MeasureSpec(name="semaines_avant_rupture", label="Semaines avant rupture sans commande",
                    expr=f"COUNT(*) FILTER (WHERE {future} AND stock_sans_commande > 0)", format="integer",
                    unit="semaines", thresholds=ThresholdSpec(direction="higher_is_better", good=4, warn=2),
                    description=f"Semaines couvertes par le stock actuel sans commande (au plus {horizon})."),
        MeasureSpec(name="semaines_avant_rupture_min", label="Semaines avant rupture sans commande",
                    expr=(f"(SELECT MIN(n) FROM (SELECT COUNT(*) FILTER (WHERE {future} AND stock_sans_commande > 0) "
                          f"AS n FROM frame GROUP BY {f}))"),
                    format="integer", unit="semaines"),
        MeasureSpec(name="stock_plan", label="Stock (réel puis projeté)", expr="SUM(stock_projete)",
                    format="compact", unit="unités"),
        MeasureSpec(name="prevision_plan", label="Prévision", expr="SUM(prevision_moteur)", format="compact",
                    unit="unités"),
        MeasureSpec(name="ventes_plan", label="Ventes", expr=f"SUM({s})", format="compact", unit="unités"),
        MeasureSpec(name="appro_plan", label="Approvisionnements", expr="SUM(reception)", format="compact",
                    unit="unités"),
        MeasureSpec(name="seuil_total", label="Seuil de sécurité", expr="SUM(seuil_plan)", format="compact",
                    unit="unités"),
    ]
    plan = SourceSpec(name="plan", label="Plan d'approvisionnement", kind="source", parent="stocks",
                      transforms=plan_transforms, fields=plan_fields, measures=plan_measures)
    # the plan weeks only, so the orders chart does not show the kept history as empty weeks
    horizon_src = SourceSpec(
        name="horizon", label=f"Plan sur {horizon} semaines", kind="source", parent="plan",
        transforms=[FilterStep(where="semaine_plan >= 1")],
        measures=[MeasureSpec(name="commande_prevue", label="Commande", expr="COALESCE(SUM(commande), 0)",
                              format="compact", unit="unités")],
    )

    filters = [
        FilterSpec(id="famille", label="Famille", field=f, type="select", default=None, applies_to=None),
        FilterSpec(id="periode", label="Période", field=d, type="relative_period", default=default_period,
                   applies_to=["stocks"], presets=["13w", "26w", "52w", "all"]),
    ]

    def kpi(title: str, measure: str, **kw: Any) -> VisualSpec:
        return VisualSpec(type="kpi", title=title, source="plan", kpi=KpiSpec(measure=measure, **kw))

    synthese = VisualSpec(
        type="text",
        source="plan",
        text=(
            "{{filters.famille}} : stock actuel de {{stock_actuel|compact}} unités, soit {{couverture_actuelle|weeks}} "
            "de ventes prévues. À commander cette semaine : {{commande_semaine|compact}} unités "
            "({{familles_a_commander|integer}} famille(s)). {{familles_a_risque|integer}} famille(s) passent sous le "
            "seuil de sécurité avant qu'une commande puisse arriver."
        ),
    )
    kpis = [
        kpi("Stock actuel", "stock_actuel", secondary="Couverture : {{couverture_actuelle|weeks}}"),
        kpi("Prévision de la semaine", "prevision_semaine",
            secondary=f"{{{{prevision_horizon|compact}}}} sur {horizon} semaines"),
        kpi("À commander cette semaine", "commande_semaine",
            secondary="{{familles_a_commander|integer}} famille(s) concernée(s)"),
        kpi("Familles à risque", "familles_a_risque", secondary="sous le seuil avant l'arrivée d'une commande"),
    ]
    combo = VisualSpec(
        type="combo",
        title="Stock, prévision et approvisionnements",
        subtitle=f"Stock réel jusqu'à aujourd'hui, puis projeté sur {horizon} semaines avec les commandes recommandées.",
        source="plan",
        x=d,
        series=[
            SeriesSpec(key="stock_plan", label="Stock", mark="area", color="good"),
            SeriesSpec(key="ventes_plan", label="Ventes", mark="line", color="muted"),
            SeriesSpec(key="prevision_plan", label="Prévision", mark="line", color=BROWN),
            SeriesSpec(key="appro_plan", label="Approvisionnements", mark="bar", color=BLUE),
            SeriesSpec(key="seuil_total", label="Seuil de sécurité", mark="line", color="bad", dash=True),
        ],
        sort="x_asc",
    )
    commandes = VisualSpec(
        type="bar",
        title="Commandes à passer, semaine par semaine",
        subtitle="Quantité à commander chaque semaine pour ne pas passer sous le seuil.",
        source="horizon",
        x=d,
        series=[SeriesSpec(key="commande_prevue", label="Commande", mark="bar", color=BLUE)],
        sort="x_asc",
    )
    table = VisualSpec(
        type="table",
        title="Plan par famille",
        subtitle="Cliquer sur une famille pour filtrer le tableau de bord.",
        source="plan",
        x=f,
        columns=[
            ColumnSpec(key=f, label=f.replace("_", " ").capitalize()),
            ColumnSpec(key="stock_actuel", label="Stock actuel"),
            ColumnSpec(key="couverture_actuelle", label="Couverture", use_thresholds=True),
            ColumnSpec(key="semaines_avant_rupture", label="Semaines avant rupture sans commande",
                       use_thresholds=True, total_measure="semaines_avant_rupture_min"),
            ColumnSpec(key="commande_semaine", label="À commander cette semaine"),
            ColumnSpec(key="prochaine_commande", label="Prochaine commande (semaine)"),
            ColumnSpec(key="commandes_horizon", label=f"Commandes sur {horizon} semaines"),
            ColumnSpec(key="semaines_sous_seuil", label="Semaines sous le seuil", use_thresholds=True),
        ],
        total_row=True,
        click_filters={"famille": f},
        ignore_filters=["famille"],
        highlight_filter="famille",
    )
    hist_series = [
        SeriesSpec(key="stock_hebdo", label="Stock", mark="area", color="good"),
        SeriesSpec(key="ventes_hebdo", label="Ventes", mark="line", color=BROWN),
    ]
    if safety_column:
        hist_series.append(SeriesSpec(key="seuil_hebdo", label="Seuil de sécurité", mark="line", color="bad",
                                      dash=True))
    historique_visual = VisualSpec(
        type="combo",
        title="Historique : stock réel et ventes",
        source="stocks",
        x=d,
        series=hist_series,
        sort="x_asc",
    )

    seuil_src = f"la colonne `{safety_column}`" if safety_column else f"{_fr(safety_weeks)} semaine(s) de prévision"
    delai_src = f"la colonne `{lead_time_column}`" if lead_time_column else f"{lead_time} semaine(s)"
    seuil_txt = f"Seuil de sécurité : {seuil_src}. Délai fournisseur : {delai_src}, famille par famille."
    forecast_txt = (
        "La prévision vient du moteur th2forecast ; s'il ne répond pas, le plan utilise la moyenne des 4 dernières "
        "semaines de ventes et un avertissement l'indique."
        if with_engine_forecast
        else "La prévision est la moyenne des 4 dernières semaines de ventes."
    )
    notes = [
        VisualSpec(
            type="note",
            title="Origine des données",
            badges=[real, fc, sim],
            text=(
                f"Les ventes (`{s}`) et le stock (`{stock}`) sont les données réelles du fichier. Le stock des semaines "
                "à venir est une projection ; les commandes et les approvisionnements sont des recommandations."
            ),
        ),
        VisualSpec(
            type="note",
            title="Règle de commande",
            badges=[sim],
            text=(
                "On commande une semaine donnée si, à l'arrivée de cette commande (après le délai fournisseur), le "
                f"stock passerait sous le seuil de sécurité. Quantité : de quoi revenir au seuil plus {target_cover} "
                f"semaines de prévision. {seuil_txt} {forecast_txt}"
            ),
        ),
        VisualSpec(
            type="note",
            title="Limites",
            text=(
                "Les commandes déjà passées ne sont pas connues : rien n'arrive pendant le premier délai. Une famille "
                "« à risque » passe sous le seuil avant qu'une commande puisse arriver : il faut une action immédiate "
                "(commande urgente, transfert)."
            ),
        ),
    ]

    grid: list[tuple[VisualSpec, dict[str, int]]] = [(synthese, dict(row=0, col=0, width=12, height=2))]
    for i, k in enumerate(kpis):
        grid.append((k, dict(row=2, col=3 * i, width=3, height=3)))
    grid += [
        (combo, dict(row=5, col=0, width=12, height=6)),
        (table, dict(row=11, col=0, width=12, height=5)),
        (commandes, dict(row=16, col=0, width=6, height=5)),
        (historique_visual, dict(row=16, col=6, width=6, height=5)),
        (notes[0], dict(row=21, col=0, width=4, height=4)),
        (notes[1], dict(row=21, col=4, width=4, height=4)),
        (notes[2], dict(row=21, col=8, width=4, height=4)),
    ]
    return {
        "sources": [_dump(historique), _dump(plan), _dump(horizon_src)],
        "filters": [_dump(flt) for flt in filters],
        "components": [{"visual": _dump(v), "position": pos} for v, pos in grid],
    }
