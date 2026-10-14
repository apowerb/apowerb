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

from apowerb.bi.spec.formatting import format_plural
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
from apowerb.bi.spec.templates_common import DEFAULT_LANG, check_lang, translator

_IDENT_RE = re.compile(IDENT)

BROWN = "#9a6b3f"
BLUE = "#3b6fd8"

_DEFAULT_LOT = InventorySimStep.model_fields["default_lot"].default


def _fr(value: float, digits: int = 2, lang: str = DEFAULT_LANG) -> str:
    """Number without trailing zeros: decimal comma in French, point in English."""
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return text if lang == "en" else text.replace(".", ",")


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
    lang: str = DEFAULT_LANG,
) -> dict:
    """Build the "Pilotage stocks" spec for a weekly sales dataset, in ``lang``.

    ``date``/``family``/``sales`` name the dataset columns; ``site`` is the
    optional column to sum over when the data has one row per site and week.
    ``stock`` names the real stock column: the dashboard then plans the
    orders from it (``safety_column``/``lead_time_column`` per family, else
    ``safety_factor`` weeks of forecast and ``lead_time``) instead of
    simulating the stock.
    """
    lang = check_lang(lang)
    T = translator(lang)
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
            lang=lang,
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
        FieldSpec(name=d, label=T(d.replace("_", " ").capitalize(), "Week"), type="date", role="date"),
        FieldSpec(name=f, label=T(f.replace("_", " ").capitalize(), "Product family"), role="dimension"),
        FieldSpec(name=s, label=T("Ventes", "Sales"), type="number", role="measure", provenance=Provenance.REAL, format="compact"),
        FieldSpec(name="prevision", label=T("Prévision", "Forecast"), type="number", role="measure", provenance=Provenance.FORECAST, format="compact"),
        FieldSpec(name="stock_debut", label=T("Stock début de semaine", "Start-of-week stock"), type="number", role="measure", provenance=sim, format="compact"),
        FieldSpec(name="appro", label=T("Approvisionnements reçus", "Replenishments received"), type="number", role="measure", provenance=sim, format="compact"),
        FieldSpec(name="ventes_servies", label=T("Ventes servies", "Sales served"), type="number", role="measure", provenance=sim, format="compact"),
        FieldSpec(name="demande_non_servie", label=T("Demande non servie", "Unserved demand"), type="number", role="measure", provenance=sim, format="compact"),
        FieldSpec(name="stock_fin", label=T("Stock fin de semaine", "End-of-week stock"), type="number", role="measure", provenance=sim, format="compact"),
        FieldSpec(name="stock_securite", label=T("Stock de sécurité", "Safety stock"), type="number", role="measure", provenance=sim, format="compact"),
        FieldSpec(name="commande", label=T("Commande", "Order"), type="number", role="measure", provenance=sim, format="compact"),
        FieldSpec(name="ecart", label=T("Écart ventes - prévision", "Sales minus forecast"), type="number", role="measure", provenance=Provenance.FORECAST, format="compact"),
        FieldSpec(name="couverture_semaine", label=T("Couverture", "Coverage"), type="number", role="measure", provenance=sim, format="weeks"),
        FieldSpec(name="sous_secu", label=T("Sous le stock de sécurité", "Below safety stock"), type="boolean", role="measure", provenance=sim),
        FieldSpec(name="mois", label=T("Mois", "Month"), type="string", role="dimension", provenance=Provenance.REAL),
    ]

    # ---- measures ----------------------------------------------------
    cov_thr = ThresholdSpec(direction="higher_is_better", good=1, warn=0.5)
    svc_thr = ThresholdSpec(direction="higher_is_better", good=0.99, warn=0.97)
    rel_thr = ThresholdSpec(direction="higher_is_better", good=0.9, warn=0.8)
    risk_thr = ThresholdSpec(direction="lower_is_better", good=0, warn=2)
    stock_last = f"SUM(stock_fin) FILTER (WHERE {last_week})"
    prev_week = f"{d} = (SELECT MAX({d}) FROM frame WHERE {d} < (SELECT MAX({d}) FROM frame))"
    measures = [
        MeasureSpec(name="stock_fin_semaine", label=T("Stock fin de semaine", "End-of-week stock"), expr=stock_last, format="compact", unit=T("unités", "units"),
                    description=T("Stock de la dernière semaine de la période.", "Stock of the last week of the period.")),
        MeasureSpec(name="stock_semaine_precedente", label=T("Stock semaine précédente", "Previous week stock"),
                    expr=f"SUM(stock_fin) FILTER (WHERE {prev_week})", format="compact", unit=T("unités", "units")),
        MeasureSpec(name="variation_stock", label=T("Variation du stock", "Stock change"),
                    expr=f"{stock_last} / NULLIF(SUM(stock_fin) FILTER (WHERE {prev_week}), 0) - 1",
                    format="percent", description=T("Stock de la dernière semaine par rapport à la semaine précédente.", "Stock of the last week compared with the previous week.")),
        MeasureSpec(name="couverture", label=T("Couverture", "Coverage"),
                    expr=f"{stock_last} / NULLIF(SUM(prevision) FILTER (WHERE {last_week}), 0)",
                    format="weeks", thresholds=cov_thr,
                    description=T("Stock fin de semaine ÷ prévision de cette semaine.", "End-of-week stock ÷ this week's forecast.")),
        MeasureSpec(name="taux_service", label=T("Taux de service", "Service level"),
                    expr=f"1 - SUM(demande_non_servie) / NULLIF(SUM({s}), 0)",
                    format="percent", thresholds=svc_thr,
                    description=T("Part de la demande servie sur la période.", "Share of demand served over the period.")),
        MeasureSpec(name="fiabilite", label=T("Fiabilité de la prévision", "Forecast accuracy"),
                    expr=f"1 - SUM(ABS({s} - prevision)) / NULLIF(SUM({s}), 0)",
                    format="percent", thresholds=rel_thr,
                    description=T("1 moins l'erreur absolue totale rapportée aux ventes.", "1 minus the total absolute error relative to sales.")),
        MeasureSpec(name="biais", label=T("Biais de prévision", "Forecast bias"),
                    expr=f"(SUM({s}) - SUM(prevision)) / NULLIF(SUM(prevision), 0)",
                    format="percent",
                    description=T("Positif : la prévision est inférieure aux ventes.", "Positive: the forecast is below sales.")),
        MeasureSpec(name="semaines_sous_secu", label=T("Semaines sous le stock de sécurité", "Weeks below safety stock"),
                    expr=(
                        f"COUNT(DISTINCT {d}) FILTER (WHERE {d} IN "
                        f"(SELECT {d} FROM frame GROUP BY {d} HAVING SUM(stock_fin) < SUM(stock_securite)))"
                    ),
                    format="integer", unit=T("semaines", "weeks"), thresholds=risk_thr,
                    description=T("Semaines où le stock total est inférieur au stock de sécurité total.", "Weeks when total stock is below total safety stock.")),
        MeasureSpec(name="semaines_sous_secu_famille", label=T("Semaines sous sécu", "Weeks below safety"),
                    expr=f"COUNT(DISTINCT {d}) FILTER (WHERE stock_fin < stock_securite)",
                    format="integer", unit=T("semaines", "weeks"), thresholds=risk_thr,
                    description=T("Semaines où le stock de la famille est inférieur à son stock de sécurité.", "Weeks when the product family's stock is below its safety stock.")),
        MeasureSpec(name="appro_recus", label=T("Approvisionnements reçus", "Replenishments received"), expr="SUM(appro)", format="compact", unit=T("unités", "units")),
        MeasureSpec(name="ventes_totales", label=T("Ventes", "Sales"), expr=f"SUM({s})", format="compact", unit=T("unités", "units")),
        MeasureSpec(name="stock_debut_periode", label=T("Stock début de période", "Opening stock of the period"),
                    expr=f"SUM(stock_debut) FILTER (WHERE {first_week})", format="compact", unit=T("unités", "units")),
        MeasureSpec(name="ventes_servies_total", label=T("Ventes servies", "Sales served"), expr="SUM(ventes_servies)", format="compact", unit=T("unités", "units")),
        MeasureSpec(name="erreur_abs", label=T("Erreur absolue de prévision", "Absolute forecast error"), expr=f"SUM(ABS({s} - prevision))", format="compact", unit=T("unités", "units")),
        MeasureSpec(name="stock_total", label=T("Stock fin de semaine", "End-of-week stock"), expr="SUM(stock_fin)", format="compact", unit=T("unités", "units")),
        MeasureSpec(name="prevision_totale", label=T("Prévision", "Forecast"), expr="SUM(prevision)", format="compact", unit=T("unités", "units")),
        MeasureSpec(name="appro_total", label=T("Approvisionnements", "Replenishments"), expr="SUM(appro)", format="compact", unit=T("unités", "units")),
        MeasureSpec(name="securite_totale", label=T("Stock de sécurité", "Safety stock"), expr="SUM(stock_securite)", format="compact", unit=T("unités", "units")),
        MeasureSpec(name="ecart_total", label=T("Écart ventes - prévision", "Sales minus forecast"), expr="SUM(ecart)", format="compact", unit=T("unités", "units")),
        MeasureSpec(name="couverture_hebdo", label=T("Couverture par semaine", "Coverage by week"),
                    expr="SUM(stock_fin) / NULLIF(SUM(prevision), 0)", format="weeks", thresholds=cov_thr),
    ]

    stocks = SourceSpec(
        name="stocks",
        label=T("Stocks et prévisions", "Stock and forecasts"),
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
                label=T("Prévision moteur (périodes futures)", "Engine forecast (future periods)"),
                kind="source",
                parent="stocks",
                transforms=[ForecastStep(date=d, value=s, by=[f], horizon=horizon, output="prevision_moteur")],
                fields=[
                    FieldSpec(name="prevision_moteur", label=T("Prévision moteur", "Engine forecast"), type="number", role="measure",
                              provenance=Provenance.FORECAST, format="compact"),
                ],
                measures=[
                    MeasureSpec(name="prevision_moteur_totale", label=T("Prévision moteur", "Engine forecast"), expr="SUM(prevision_moteur)",
                                format="compact", unit=T("unités", "units")),
                ],
            )
        )

    # ---- filters -----------------------------------------------------
    filters = [
        FilterSpec(id="famille", label=T("Famille", "Product family"), field=f, type="select", default=None,
                   applies_to=None),
        FilterSpec(id="periode", label=T("Période", "Period"), field=d, type="relative_period", default=default_period,
                   applies_to=["stocks"] if with_engine_forecast else None,
                   presets=["13w", "26w", "52w", "all"]),
    ]

    # ---- visuals -----------------------------------------------------
    def kpi(title: str, measure: str, **kw: Any) -> VisualSpec:
        return VisualSpec(type="kpi", title=title, source="stocks", kpi=KpiSpec(measure=measure, **kw))

    synthese = VisualSpec(
        type="text",
        source="stocks",
        text=T(
            "{{filters.famille}}, semaine du {{period.last}} : stock de {{stock_fin_semaine|compact}} unités, "
            "soit {{couverture|weeks}} de couverture. Sur la période, la demande a été servie à "
            "{{taux_service|percent}}, la prévision est fiable à {{fiabilite|percent}} et le stock est passé "
            "{{semaines_sous_secu|integer}} fois sous le stock de sécurité.",
            "{{filters.famille}}, week of {{period.last}}: stock of {{stock_fin_semaine|compact}} units, "
            "i.e. {{couverture|weeks}} of coverage. Over the period, {{taux_service|percent}} of demand was served, "
            "the forecast is {{fiabilite|percent}} accurate and the stock fell below safety stock "
            "{{semaines_sous_secu|integer}} times.",
        ),
    )
    combo = VisualSpec(
        type="combo",
        title=T("Stock, prévision et approvisionnements par semaine", "Stock, forecast and replenishments by week"),
        source="stocks",
        x=d,
        series=[
            SeriesSpec(key="stock_total", label=T("Stock fin de semaine", "End-of-week stock"), mark="area", color="good"),
            SeriesSpec(key="prevision_totale", label=T("Prévision", "Forecast"), mark="line", color=BROWN),
            SeriesSpec(key="appro_total", label=T("Approvisionnements", "Replenishments"), mark="bar", color=BLUE),
            SeriesSpec(key="securite_totale", label=T("Stock de sécurité", "Safety stock"), mark="line", color="muted", dash=True),
        ],
        sort="x_asc",
    )
    ecart = VisualSpec(
        type="diverging_bar",
        title=T("Écart ventes - prévision", "Sales minus forecast"),
        subtitle=T("Positif : les ventes dépassent la prévision.", "Positive: sales exceed the forecast."),
        source="stocks",
        x=d,
        series=[
            SeriesSpec(
                key="ecart_total",
                label=T("Écart", "Gap"),
                mark="bar",
                color_rules=[ColorRule(op=">", value=0, color="good"), ColorRule(op="<", value=0, color=BROWN)],
            )
        ],
        sort="x_asc",
    )
    couverture = VisualSpec(
        type="bar",
        title=T("Couverture par semaine", "Coverage by week"),
        subtitle=T("Semaines de ventes couvertes par le stock.", "Weeks of sales covered by the stock."),
        source="stocks",
        x=d,
        series=[SeriesSpec(key="couverture_hebdo", label=T("Couverture", "Coverage"), mark="bar", use_thresholds=True)],
        reference_lines=[
            ReferenceLine(value=1, label=T("1 semaine", "1 week"), color="good"),
            ReferenceLine(value=0.5, label=T("0,5 semaine", "0.5 week"), color="bad"),
        ],
        sort="x_asc",
    )
    table = VisualSpec(
        type="table",
        title=T("Comparatif par famille", "Comparison by product family"),
        subtitle=T("Cliquer sur une famille pour filtrer le tableau de bord.", "Click a product family to filter the dashboard."),
        source="stocks",
        x=f,
        columns=[
            ColumnSpec(key=f, label=T(f.replace("_", " ").capitalize(), "Product family")),
            ColumnSpec(key="stock_fin_semaine", label=T("Stock fin", "End stock")),
            ColumnSpec(key="couverture", label=T("Couverture", "Coverage"), use_thresholds=True),
            ColumnSpec(key="taux_service", label=T("Taux de service", "Service level"), use_thresholds=True),
            ColumnSpec(key="fiabilite", label=T("Fiabilité", "Accuracy"), use_thresholds=True),
            ColumnSpec(key="biais", label=T("Biais", "Bias")),
            ColumnSpec(key="semaines_sous_secu_famille", label=T("Semaines sous sécu", "Weeks below safety"), use_thresholds=True,
                       total_measure="semaines_sous_secu"),
        ],
        total_row=True,
        click_filters={"famille": f},
        ignore_filters=["famille"],
        highlight_filter="famille",
    )
    waterfall = VisualSpec(
        type="waterfall",
        title=T("Pourquoi le stock a bougé", "Why the stock moved"),
        subtitle=T("De la première à la dernière semaine de la période.", "From the first to the last week of the period."),
        source="stocks",
        steps=[
            WaterfallStep(label=T("Stock début", "Opening stock"), measure="stock_debut_periode", kind="start"),
            WaterfallStep(label=T("Approvisionnements", "Replenishments"), measure="appro_recus", kind="increase", color=BLUE),
            WaterfallStep(label=T("Ventes servies", "Sales served"), measure="ventes_servies_total", kind="decrease", color=BROWN),
            WaterfallStep(label=T("Stock fin", "End stock"), measure="stock_fin_semaine", kind="end"),
        ],
    )
    poids = VisualSpec(
        type="hbar",
        title=T("Qui pèse dans l'erreur de prévision", "Who drives the forecast error"),
        subtitle=T("Somme des écarts absolus entre ventes et prévision.", "Sum of absolute gaps between sales and forecast."),
        source="stocks",
        x=f,
        series=[SeriesSpec(key="erreur_abs", label=T("Erreur absolue", "Absolute error"), mark="bar", color=BROWN)],
        sort="value_desc",
        ignore_filters=["famille"],
    )
    biais_mois = VisualSpec(
        type="bar",
        title=T("Quand la prévision se trompe", "When the forecast misses"),
        subtitle=T("Biais moyen par mois : positif quand les ventes dépassent la prévision.", "Average bias by month: positive when sales exceed the forecast."),
        source="stocks",
        x="mois",
        series=[SeriesSpec(key="biais", label=T("Biais", "Bias"), mark="bar", color="muted")],
        sort="x_asc",
    )

    lot_text = (
        T("Les commandes sont arrondies au lot de chaque famille (", "Orders are rounded to each product family's lot (")
        + ", ".join(T(f"{k} : {v}", f"{k}: {v}") for k, v in lots.items()) + ")."
        if lots
        else T(f"Les commandes sont arrondies au lot de {_DEFAULT_LOT} unités.",
               f"Orders are rounded to lots of {_DEFAULT_LOT} units.")
    )
    notes = [
        VisualSpec(
            type="note",
            title=T("Origine des données", "Data origin"),
            badges=[Provenance.REAL, Provenance.FORECAST, Provenance.SIMULATED],
            text=T(
                f"Les ventes (`{s}`) sont les données réelles du fichier importé. La prévision est calculée à partir "
                "de ces ventes. Les stocks, approvisionnements et commandes sont simulés : ils n'ont pas été mesurés "
                "en entrepôt.",
                f"Sales (`{s}`) are the actual data of the imported file. The forecast is computed from these sales. "
                "Stock, replenishments and orders are simulated: they were not measured in the warehouse.",
            ),
        ),
        VisualSpec(
            type="note",
            title=T("Méthode de prévision", "Forecast method"),
            badges=[Provenance.FORECAST],
            text=T(
                "La prévision est volontairement simple : au début de l'historique, moyenne des 4 semaines "
                "précédentes ; ensuite, ventes de la même semaine l'an dernier, corrigées de la tendance récente "
                "(bornée entre 0,8 et 1,25).",
                "The forecast is deliberately simple: at the start of the history, the average of the previous 4 "
                "weeks; then the sales of the same week last year, adjusted by the recent trend "
                "(capped between 0.8 and 1.25).",
            ),
        ),
        VisualSpec(
            type="note",
            title=T("Règles de réapprovisionnement simulées", "Simulated replenishment rules"),
            badges=[Provenance.SIMULATED],
            text=T(
                f"Stock de sécurité : {_fr(safety_factor)} × la prévision de la semaine. Commande : "
                f"{_fr(order_factor)} × la prévision de la semaine suivante + stock de sécurité - stock en fin de "
                f"semaine. Délai de livraison : {lead_time} semaine{'s' if lead_time > 1 else ''}. {lot_text}",
                f"Safety stock: {_fr(safety_factor, lang=lang)} × the week's forecast. Order: "
                f"{_fr(order_factor, lang=lang)} × next week's forecast + safety stock - end-of-week stock. "
                f"Delivery lead time: {lead_time} week{'s' if lead_time > 1 else ''}. {lot_text}",
            ),
        ),
        VisualSpec(
            type="note",
            title=T("Définition des KPI", "KPI definitions"),
            text=T(
                "**Couverture** : stock de fin de semaine ÷ prévision de la semaine. **Taux de service** : part de la "
                "demande servie. **Fiabilité** : 1 - erreur absolue ÷ ventes. **Biais** : (ventes - prévision) ÷ "
                "prévision. **Semaines sous sécu** : semaines où le stock total passe sous le stock de sécurité total.",
                "**Coverage**: end-of-week stock ÷ the week's forecast. **Service level**: share of demand served. "
                "**Accuracy**: 1 - absolute error ÷ sales. **Bias**: (sales - forecast) ÷ forecast. "
                "**Weeks below safety**: weeks when total stock falls below total safety stock.",
            ),
        ),
        VisualSpec(
            type="note",
            title=T("Limites", "Limitations"),
            text=T(
                "Les stocks sont simulés : ils illustrent la méthode, ils ne remplacent pas l'inventaire réel. La "
                "prévision ne connaît ni les promotions ni les ruptures passées. Les chiffres dépendent des "
                "paramètres de simulation ci-contre.",
                "Stock is simulated: it illustrates the method and does not replace an actual inventory count. The "
                "forecast knows neither promotions nor past stock-outs. The figures depend on the simulation "
                "settings shown alongside.",
            ),
        ),
    ]

    grid: list[tuple[VisualSpec, dict[str, int]]] = [(synthese, dict(row=0, col=0, width=12, height=2))]
    kpis = [
        kpi(T("Stock fin de semaine", "End-of-week stock"), "stock_fin_semaine", sparkline="stock_total", secondary=T("{{variation_stock|percent}} vs semaine précédente", "{{variation_stock|percent}} vs previous week")),
        kpi(T("Couverture", "Coverage"), "couverture", sparkline="couverture_hebdo", secondary=T("Stock ÷ prévision de la semaine", "Stock ÷ this week's forecast")),
        kpi(T("Taux de service", "Service level"), "taux_service", sparkline="taux_service", target_label=T("cible ≥ 99 %", "target ≥ 99%")),
        kpi(T("Fiabilité de la prévision", "Forecast accuracy"), "fiabilite", secondary=T("Biais : {{biais|percent}}", "Bias: {{biais|percent}}"), target_label=T("cible ≥ 90 %", "target ≥ 90%")),
        kpi(T("Semaines sous sécu", "Weeks below safety"), "semaines_sous_secu", secondary=T("risque de rupture", "stock-out risk")),
        kpi(T("Approvisionnements reçus", "Replenishments received"), "appro_recus", sparkline="appro_total", secondary=T("Ventes : {{ventes_totales|compact}}", "Sales: {{ventes_totales|compact}}")),
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
    lang: str = DEFAULT_LANG,
) -> dict:
    """Real stock + orders to place over the horizon (see ``stock_pilotage``)."""
    T = translator(lang)
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
        FieldSpec(name=d, label=T(d.replace("_", " ").capitalize(), "Week"), type="date", role="date"),
        FieldSpec(name=f, label=T(f.replace("_", " ").capitalize(), "Product family"), role="dimension"),
        FieldSpec(name=s, label=T("Ventes", "Sales"), type="number", role="measure", provenance=real, format="compact"),
        FieldSpec(name=stock, label="Stock", type="number", role="measure", provenance=real, format="compact"),
    ]
    hist_measures = [
        MeasureSpec(name="stock_hebdo", label="Stock", expr=f"SUM({stock})", format="compact", unit=T("unités", "units")),
        MeasureSpec(name="ventes_hebdo", label=T("Ventes", "Sales"), expr=f"SUM({s})", format="compact", unit=T("unités", "units")),
    ]
    if safety_column:
        hist_fields.append(FieldSpec(name=safety_column, label=T("Seuil de sécurité", "Safety stock"), type="number", role="measure",
                                     provenance=real, format="compact"))
        hist_measures.append(MeasureSpec(name="seuil_hebdo", label=T("Seuil de sécurité", "Safety stock"), expr=f"SUM({safety_column})",
                                         format="compact", unit=T("unités", "units")))
    historique = SourceSpec(name="stocks", label=T("Historique des stocks", "Stock history"), kind="dataset", dataset_id=dataset_id,
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
        FieldSpec(name="prevision_moteur", label=T("Prévision", "Forecast"), type="number", role="measure", provenance=fc,
                  format="compact"),
        FieldSpec(name="stock_projete", label=T("Stock (réel puis projeté)", "Stock (actual, then projected)"), type="number", role="measure",
                  provenance=fc, format="compact"),
        FieldSpec(name="stock_sans_commande", label=T("Stock sans commande", "Stock without orders"), type="number", role="measure",
                  provenance=fc, format="compact"),
        FieldSpec(name="reception", label=T("Approvisionnements", "Replenishments"), type="number", role="measure", provenance=sim,
                  format="compact"),
        FieldSpec(name="commande", label=T("Commande à passer", "Order to place"), type="number", role="measure", provenance=sim,
                  format="compact"),
        FieldSpec(name="seuil_plan", label=T("Seuil de sécurité", "Safety stock"), type="number", role="measure",
                  provenance=real if safety_column else sim, format="compact"),
        FieldSpec(name="delai_plan", label=T("Délai fournisseur", "Supplier lead time"), type="number", role="measure",
                  provenance=real if lead_time_column else sim, format="integer"),
        FieldSpec(name="risque", label=T("Risque", "Risk"), type="number", role="measure", provenance=fc, format="integer"),
        FieldSpec(name="semaine_plan", label=T("Semaine du plan", "Plan week"), type="number", role="dimension", format="integer"),
    ]
    no_risk = ThresholdSpec(direction="lower_is_better", good=0, warn=0)
    plan_measures = [
        MeasureSpec(name="stock_actuel", label=T("Stock actuel", "Current stock"), expr=stock_now, format="compact", unit=T("unités", "units"),
                    description=T("Stock de la dernière semaine connue.", "Stock of the last known week.")),
        MeasureSpec(name="couverture_actuelle", label=T("Couverture", "Coverage"),
                    expr=f"{stock_now} / NULLIF(SUM(prevision_moteur) FILTER (WHERE {future}) / NULLIF({weeks}, 0), 0)",
                    format="weeks", thresholds=ThresholdSpec(direction="higher_is_better", good=4, warn=2),
                    description=T("Stock actuel ÷ prévision moyenne d'une semaine de l'horizon.", "Current stock ÷ average weekly forecast over the horizon.")),
        MeasureSpec(name="prevision_semaine", label=T("Prévision de la semaine", "This week's forecast"),
                    expr=f"SUM(prevision_moteur) FILTER (WHERE {first})", format="compact", unit=T("unités", "units")),
        MeasureSpec(name="prevision_horizon", label=T("Prévision sur l'horizon", "Forecast over the horizon"),
                    expr=f"SUM(prevision_moteur) FILTER (WHERE {future})", format="compact", unit=T("unités", "units")),
        MeasureSpec(name="commande_semaine", label=T("À commander cette semaine", "To order this week"),
                    expr=f"COALESCE(SUM(commande) FILTER (WHERE {first}), 0)", format="compact", unit=T("unités", "units")),
        MeasureSpec(name="familles_a_commander", label=T("Familles à commander cette semaine", "Product families to order this week"),
                    expr=f"COUNT(DISTINCT {f}) FILTER (WHERE {first} AND commande > 0)", format="integer"),
        MeasureSpec(name="commandes_horizon", label=T("Commandes sur l'horizon", "Orders over the horizon"),
                    expr=f"COALESCE(SUM(commande) FILTER (WHERE {future}), 0)", format="compact", unit=T("unités", "units")),
        MeasureSpec(name="prochaine_commande", label=T("Prochaine commande", "Next order"),
                    expr=f"MIN(semaine_plan) FILTER (WHERE {future} AND commande > 0)", format="integer",
                    unit=T("semaine", "week"), description=T("Semaine du plan où passer la prochaine commande (1 = cette semaine).", "Plan week in which to place the next order (1 = this week).")),
        MeasureSpec(name="familles_a_risque", label=T("Familles à risque", "Product families at risk"),
                    expr=f"COUNT(DISTINCT {f}) FILTER (WHERE risque = 2)", format="integer", thresholds=no_risk,
                    description=T("Familles qui passent sous le seuil avant qu'une commande puisse arriver.", "Product families that fall below the threshold before an order can arrive.")),
        MeasureSpec(name="semaines_sous_seuil", label=T("Semaines sous le seuil", "Weeks below threshold"),
                    expr="COUNT(*) FILTER (WHERE risque = 2)", format="integer", unit=T("semaines", "weeks"), thresholds=no_risk,
                    description=T("Semaines de l'horizon sous le seuil malgré les commandes.", "Horizon weeks below the threshold despite the orders.")),
        MeasureSpec(name="semaines_avant_rupture", label=T("Semaines avant rupture sans commande", "Weeks before stock-out without orders"),
                    expr=f"COUNT(*) FILTER (WHERE {future} AND stock_sans_commande > 0)", format="integer",
                    unit=T("semaines", "weeks"), thresholds=ThresholdSpec(direction="higher_is_better", good=4, warn=2),
                    description=T(f"Semaines couvertes par le stock actuel sans commande (au plus {horizon}).",
                                  f"Weeks covered by the current stock without orders (at most {horizon}).")),
        MeasureSpec(name="semaines_avant_rupture_min", label=T("Semaines avant rupture sans commande", "Weeks before stock-out without orders"),
                    expr=(f"(SELECT MIN(n) FROM (SELECT COUNT(*) FILTER (WHERE {future} AND stock_sans_commande > 0) "
                          f"AS n FROM frame GROUP BY {f}))"),
                    format="integer", unit=T("semaines", "weeks")),
        MeasureSpec(name="stock_plan", label=T("Stock (réel puis projeté)", "Stock (actual, then projected)"), expr="SUM(stock_projete)",
                    format="compact", unit=T("unités", "units")),
        MeasureSpec(name="prevision_plan", label=T("Prévision", "Forecast"), expr="SUM(prevision_moteur)", format="compact",
                    unit=T("unités", "units")),
        MeasureSpec(name="ventes_plan", label=T("Ventes", "Sales"), expr=f"SUM({s})", format="compact", unit=T("unités", "units")),
        MeasureSpec(name="appro_plan", label=T("Approvisionnements", "Replenishments"), expr="SUM(reception)", format="compact",
                    unit=T("unités", "units")),
        MeasureSpec(name="seuil_total", label=T("Seuil de sécurité", "Safety stock"), expr="SUM(seuil_plan)", format="compact",
                    unit=T("unités", "units")),
    ]
    plan = SourceSpec(name="plan", label=T("Plan d'approvisionnement", "Replenishment plan"), kind="source", parent="stocks",
                      transforms=plan_transforms, fields=plan_fields, measures=plan_measures)
    # the plan weeks only, so the orders chart does not show the kept history as empty weeks
    horizon_src = SourceSpec(
        name="horizon", label=T(f"Plan sur {horizon} semaines", f"Plan over {horizon} weeks"), kind="source", parent="plan",
        transforms=[FilterStep(where="semaine_plan >= 1")],
        measures=[MeasureSpec(name="commande_prevue", label=T("Commande", "Order"), expr="COALESCE(SUM(commande), 0)",
                              format="compact", unit=T("unités", "units"))],
    )

    filters = [
        FilterSpec(id="famille", label=T("Famille", "Product family"), field=f, type="select", default=None, applies_to=None),
        FilterSpec(id="periode", label=T("Période", "Period"), field=d, type="relative_period", default=default_period,
                   applies_to=["stocks"], presets=["13w", "26w", "52w", "all"]),
    ]

    def kpi(title: str, measure: str, **kw: Any) -> VisualSpec:
        return VisualSpec(type="kpi", title=title, source="plan", kpi=KpiSpec(measure=measure, **kw))

    synthese = VisualSpec(
        type="text",
        source="plan",
        text=T(
            "{{filters.famille}} : stock actuel de {{stock_actuel|compact}} unités, soit {{couverture_actuelle|weeks}} "
            "de ventes prévues. À commander cette semaine : {{commande_semaine|compact}} unités "
            "({{familles_a_commander|plural:famille:familles}}). {{familles_a_risque|plural:famille passe:familles passent}} "
            "sous le seuil de sécurité avant qu'une commande puisse arriver.",
            "{{filters.famille}}: current stock of {{stock_actuel|compact}} units, i.e. {{couverture_actuelle|weeks}} "
            "of forecast sales. To order this week: {{commande_semaine|compact}} units "
            "({{familles_a_commander|plural:product family:product families}}). "
            "{{familles_a_risque|plural:product family falls:product families fall}} below safety stock before an order can arrive.",
        ),
    )
    kpis = [
        kpi(T("Stock actuel", "Current stock"), "stock_actuel", secondary=T("Couverture : {{couverture_actuelle|weeks}}", "Coverage: {{couverture_actuelle|weeks}}")),
        kpi(T("Prévision de la semaine", "This week's forecast"), "prevision_semaine",
            secondary=T(f"{{{{prevision_horizon|compact}}}} sur {horizon} semaines",
                        f"{{{{prevision_horizon|compact}}}} over {horizon} weeks")),
        kpi(T("À commander cette semaine", "To order this week"), "commande_semaine",
            secondary=T("{{familles_a_commander|plural:famille concernée:familles concernées}}",
                        "{{familles_a_commander|plural:product family affected:product families affected}}")),
        kpi(T("Familles à risque", "Product families at risk"), "familles_a_risque", secondary=T("sous le seuil avant l'arrivée d'une commande", "below threshold before an order can arrive")),
    ]
    combo = VisualSpec(
        type="combo",
        title=T("Stock, prévision et approvisionnements", "Stock, forecast and replenishments"),
        subtitle=T(f"Stock réel jusqu'à aujourd'hui, puis projeté sur {horizon} semaines avec les commandes recommandées.",
                   f"Actual stock up to today, then projected over {horizon} weeks with the recommended orders."),
        source="plan",
        x=d,
        series=[
            SeriesSpec(key="stock_plan", label="Stock", mark="area", color="good"),
            SeriesSpec(key="ventes_plan", label=T("Ventes", "Sales"), mark="line", color="muted"),
            SeriesSpec(key="prevision_plan", label=T("Prévision", "Forecast"), mark="line", color=BROWN),
            SeriesSpec(key="appro_plan", label=T("Approvisionnements", "Replenishments"), mark="bar", color=BLUE),
            SeriesSpec(key="seuil_total", label=T("Seuil de sécurité", "Safety stock"), mark="line", color="bad", dash=True),
        ],
        sort="x_asc",
    )
    commandes = VisualSpec(
        type="bar",
        title=T("Commandes à passer, semaine par semaine", "Orders to place, week by week"),
        subtitle=T("Quantité à commander chaque semaine pour ne pas passer sous le seuil.", "Quantity to order each week to stay above the threshold."),
        source="horizon",
        x=d,
        series=[SeriesSpec(key="commande_prevue", label=T("Commande", "Order"), mark="bar", color=BLUE)],
        sort="x_asc",
    )
    table = VisualSpec(
        type="table",
        title=T("Plan par famille", "Plan by product family"),
        subtitle=T("Cliquer sur une famille pour filtrer le tableau de bord.", "Click a product family to filter the dashboard."),
        source="plan",
        x=f,
        columns=[
            ColumnSpec(key=f, label=T(f.replace("_", " ").capitalize(), "Product family")),
            ColumnSpec(key="stock_actuel", label=T("Stock actuel", "Current stock")),
            ColumnSpec(key="couverture_actuelle", label=T("Couverture", "Coverage"), use_thresholds=True),
            ColumnSpec(key="semaines_avant_rupture", label=T("Semaines avant rupture sans commande", "Weeks before stock-out without orders"),
                       use_thresholds=True, total_measure="semaines_avant_rupture_min"),
            ColumnSpec(key="commande_semaine", label=T("À commander cette semaine", "To order this week")),
            ColumnSpec(key="prochaine_commande", label=T("Prochaine commande (semaine)", "Next order (week)")),
            ColumnSpec(key="commandes_horizon", label=T(f"Commandes sur {horizon} semaines", f"Orders over {horizon} weeks")),
            ColumnSpec(key="semaines_sous_seuil", label=T("Semaines sous le seuil", "Weeks below threshold"), use_thresholds=True),
        ],
        total_row=True,
        click_filters={"famille": f},
        ignore_filters=["famille"],
        highlight_filter="famille",
    )
    hist_series = [
        SeriesSpec(key="stock_hebdo", label="Stock", mark="area", color="good"),
        SeriesSpec(key="ventes_hebdo", label=T("Ventes", "Sales"), mark="line", color=BROWN),
    ]
    if safety_column:
        hist_series.append(SeriesSpec(key="seuil_hebdo", label=T("Seuil de sécurité", "Safety stock"), mark="line", color="bad",
                                      dash=True))
    historique_visual = VisualSpec(
        type="combo",
        title=T("Historique : stock réel et ventes", "History: actual stock and sales"),
        source="stocks",
        x=d,
        series=hist_series,
        sort="x_asc",
    )

    seuil_src = (
        T(f"la colonne `{safety_column}`", f"the `{safety_column}` column") if safety_column
        else T(format_plural(safety_weeks, "semaine de prévision", "semaines de prévision"),
               format_plural(safety_weeks, "week of forecast", "weeks of forecast", lang="en"))
    )
    delai_src = (
        T(f"la colonne `{lead_time_column}`", f"the `{lead_time_column}` column") if lead_time_column
        else T(format_plural(lead_time, "semaine", "semaines"), format_plural(lead_time, "week", "weeks", lang="en"))
    )
    seuil_txt = T(f"Seuil de sécurité : {seuil_src}. Délai fournisseur : {delai_src}, famille par famille.",
                  f"Safety stock: {seuil_src}. Supplier lead time: {delai_src}, product family by product family.")
    forecast_txt = (
        T("La prévision vient du moteur th2forecast ; s'il ne répond pas, le plan utilise la moyenne des 4 dernières "
          "semaines de ventes et un avertissement l'indique.",
          "The forecast comes from the th2forecast engine; if it does not answer, the plan uses the average of the "
          "last 4 weeks of sales and a warning says so.")
        if with_engine_forecast
        else T("La prévision est la moyenne des 4 dernières semaines de ventes.",
               "The forecast is the average of the last 4 weeks of sales.")
    )
    notes = [
        VisualSpec(
            type="note",
            title=T("Origine des données", "Data origin"),
            badges=[real, fc, sim],
            text=T(
                f"Les ventes (`{s}`) et le stock (`{stock}`) sont les données réelles du fichier. Le stock des semaines "
                "à venir est une projection ; les commandes et les approvisionnements sont des recommandations.",
                f"Sales (`{s}`) and stock (`{stock}`) are the actual data of the file. The stock of the coming weeks "
                "is a projection; orders and replenishments are recommendations.",
            ),
        ),
        VisualSpec(
            type="note",
            title=T("Règle de commande", "Ordering rule"),
            badges=[sim],
            text=T(
                "On commande une semaine donnée si, à l'arrivée de cette commande (après le délai fournisseur), le "
                f"stock passerait sous le seuil de sécurité. Quantité : de quoi revenir au seuil plus {target_cover} "
                f"semaines de prévision. {seuil_txt} {forecast_txt}",
                "An order is placed in a given week if, when it arrives (after the supplier lead time), the stock "
                f"would fall below safety stock. Quantity: enough to get back to the threshold plus {target_cover} "
                f"weeks of forecast. {seuil_txt} {forecast_txt}",
            ),
        ),
        VisualSpec(
            type="note",
            title=T("Limites", "Limitations"),
            text=T(
                "Les commandes déjà passées ne sont pas connues : rien n'arrive pendant le premier délai. Une famille "
                "« à risque » passe sous le seuil avant qu'une commande puisse arriver : il faut une action immédiate "
                "(commande urgente, transfert).",
                "Orders already placed are not known: nothing arrives during the first lead time. A product family "
                "“at risk” falls below the threshold before an order can arrive: it needs immediate action "
                "(rush order, transfer).",
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
