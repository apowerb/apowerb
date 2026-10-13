"""
bi/spec/templates_forecast.py
-----------------------------
"Prévision des ventes" dashboard (spec v2) for weekly sales per product family.

The history carries a naive one-step-ahead forecast (``NaiveForecastStep``), used
to measure how reliable a simple method is on the past. The weeks ahead come
from the th2forecast engine (``ForecastStep``); families the engine did not
answer for (service unavailable, partial answer, engine disabled) fall back on
the same naive method, extended over the horizon. The table says which method
produced each family's figure.

The series is assumed weekly: the fallback weeks are 7 days apart and "same week
last year" is 364 days earlier.
"""

from __future__ import annotations

from typing import Any

from apowerb.bi.spec.duck import quote_ident
from apowerb.bi.spec.models import (
    AggregateStep,
    ColorRule,
    ColumnSpec,
    DeriveStep,
    FieldSpec,
    FilterSpec,
    ForecastStep,
    KpiSpec,
    MeasureSpec,
    NaiveForecastStep,
    Provenance,
    SeriesSpec,
    SourceSpec,
    SqlStep,
    ThresholdSpec,
    VisualSpec,
)
from apowerb.bi.spec.templates import BLUE, BROWN, _check_ident, _dump

_MAX_HORIZON = 52
_DEFAULT_HORIZON = 8
_ENGINE = "Moteur th2forecast"
_NAIVE = "Prévision naïve"
_NAIVE_FALLBACK = "Prévision naïve (repli)"
_RELIABLE, _ACCEPTABLE = 0.85, 0.7

DESCRIPTOR: dict[str, Any] = {
    "id": "sales_forecast",
    "label": "Prévision des ventes",
    "description": (
        "Tableau de bord de prévision des ventes hebdomadaires par famille : total prévu sur l'horizon, "
        "écart avec la même période de l'an dernier, fiabilité de la méthode sur le passé, semaine de pic "
        "et détail par famille. Prévision du moteur th2forecast, avec repli sur une prévision naïve."
    ),
    "version": 1,
    "columns": [
        {"key": "date", "label": "Semaine", "type": "date", "required": True, "default": "semaine"},
        {"key": "family", "label": "Famille", "type": "string", "required": True, "default": "famille"},
        {"key": "sales", "label": "Ventes", "type": "number", "required": True, "default": "ventes"},
    ],
    "params": [
        {"key": "horizon", "label": "Horizon de prévision (semaines)", "type": "integer",
         "default": _DEFAULT_HORIZON, "min": 1, "max": 26},
    ],
}


def build(dataset_id: str, columns: dict[str, str | None], params: dict[str, Any]) -> dict:
    defaults = {c["key"]: c["default"] for c in DESCRIPTOR["columns"]}
    return sales_forecast(
        dataset_id,
        date=columns.get("date") or defaults["date"],
        family=columns.get("family") or defaults["family"],
        sales=columns.get("sales") or defaults["sales"],
        horizon=params.get("horizon") or _DEFAULT_HORIZON,
    )


def _missing_weeks_query(d: str, f: str, s: str, horizon: int) -> str:
    """Rows for the weeks ahead of every family the engine returned nothing for."""
    weeks = ", ".join(f"({n})" for n in range(1, horizon + 1))
    return (
        "SELECT * FROM input UNION ALL BY NAME "
        f"SELECT g.{f} AS {f}, CAST(g.last_week + k.n * INTERVAL 7 DAY AS TIMESTAMP) AS {d} "
        f"FROM (SELECT {f}, MAX({d}) FILTER (WHERE {s} IS NOT NULL) AS last_week FROM input "
        f"GROUP BY {f} HAVING COUNT(prevision_moteur) = 0) AS g "
        f"CROSS JOIN (VALUES {weeks}) AS k(n)"
    )


def sales_forecast(
    dataset_id: str,
    *,
    date: str = "semaine",
    family: str = "famille",
    sales: str = "ventes",
    horizon: int = _DEFAULT_HORIZON,
    with_engine_forecast: bool = True,
) -> dict:
    """Build the "Prévision des ventes" spec for a weekly sales dataset.

    ``date``/``family``/``sales`` name the dataset columns, ``horizon`` the
    number of weeks to forecast. Without ``with_engine_forecast`` every figure
    comes from the naive method.
    """
    if not dataset_id:
        raise ValueError("dataset_id is required.")
    d = _check_ident("date", date)
    f = _check_ident("family", family)
    s = _check_ident("sales", sales)
    if not isinstance(horizon, int) or isinstance(horizon, bool) or not 1 <= horizon <= _MAX_HORIZON:
        raise ValueError(f"horizon={horizon!r} must be a number of weeks between 1 and {_MAX_HORIZON}.")
    qd, qf, qs = quote_ident(d), quote_ident(f), quote_ident(s)
    label = f.replace("_", " ").capitalize()
    noun = label.lower()
    fc = Provenance.FORECAST

    # ---- source -------------------------------------------------------
    transforms: list[Any] = [AggregateStep(group_by=[d, f], aggregates={s: f"SUM({s})"})]
    if with_engine_forecast:
        transforms.append(ForecastStep(date=d, value=s, by=[f], horizon=horizon, output="prevision_moteur"))
    else:
        transforms.append(DeriveStep(column="prevision_moteur", expr="CAST(NULL AS DOUBLE)", provenance=fc))
    fallback = _NAIVE_FALLBACK if with_engine_forecast else _NAIVE
    transforms += [
        SqlStep(query=_missing_weeks_query(qd, qf, qs, horizon)),
        NaiveForecastStep(date=d, value=s, by=[f], output="prevision_naive"),
        SqlStep(query=(
            "SELECT t.*, p." + qs + " AS ventes_n1 FROM input AS t LEFT JOIN input AS p "
            f"ON p.{qf} = t.{qf} AND p.{qd} = t.{qd} - INTERVAL 364 DAY"
        )),
        DeriveStep(column="futur", expr=f"{qd} > (SELECT MAX({qd}) FROM input WHERE {qs} IS NOT NULL)"),
        DeriveStep(column="prevision", provenance=fc,
                   expr="CASE WHEN futur THEN COALESCE(prevision_moteur, prevision_naive) ELSE prevision_naive END"),
        DeriveStep(column="methode", provenance=fc,
                   expr=(f"CASE WHEN NOT futur THEN NULL WHEN prevision_moteur IS NOT NULL THEN '{_ENGINE}' "
                         f"ELSE '{fallback}' END")),
    ]
    fields = [
        FieldSpec(name=d, label=d.replace("_", " ").capitalize(), type="date", role="date"),
        FieldSpec(name=f, label=label, role="dimension"),
        FieldSpec(name=s, label="Ventes", type="number", role="measure", provenance=Provenance.REAL, format="compact"),
        FieldSpec(name="prevision_moteur", label="Prévision du moteur", type="number", role="measure",
                  provenance=fc, format="compact"),
        FieldSpec(name="prevision_naive", label="Prévision naïve", type="number", role="measure",
                  provenance=fc, format="compact"),
        FieldSpec(name="prevision", label="Prévision", type="number", role="measure", provenance=fc, format="compact"),
        # filled on the weeks ahead too: tagged forecast so that it does not count as a real value
        # and the last real date stays the last week of sales
        FieldSpec(name="ventes_n1", label="Même semaine N-1", type="number", role="measure",
                  provenance=fc, format="compact"),
        FieldSpec(name="methode", label="Méthode", type="string", role="dimension", provenance=fc),
    ]

    # ---- measures -----------------------------------------------------
    weekly_peak = f"SELECT {qd} AS d, SUM(prevision) AS v FROM frame WHERE futur GROUP BY {qd}"
    reliability_thr = ThresholdSpec(direction="higher_is_better", good=_RELIABLE, warn=_ACCEPTABLE)
    comparable = "SUM(prevision) FILTER (WHERE futur AND ventes_n1 IS NOT NULL)"
    measures = [
        MeasureSpec(name="prevu_horizon", label=f"Prévu sur {horizon} semaines",
                    expr="SUM(prevision) FILTER (WHERE futur)", format="compact", unit="unités",
                    description="Somme des ventes prévues sur les semaines à venir."),
        MeasureSpec(name="ventes_n1_horizon", label="Mêmes semaines N-1",
                    expr="SUM(ventes_n1) FILTER (WHERE futur)", format="compact", unit="unités",
                    description="Ventes réelles des mêmes semaines un an plus tôt."),
        MeasureSpec(name="evolution_n1", label="Écart vs même période N-1",
                    expr=f"{comparable} / NULLIF(SUM(ventes_n1) FILTER (WHERE futur), 0) - 1", format="percent",
                    description="Prévu ÷ ventes des mêmes semaines un an plus tôt, moins 1."),
        MeasureSpec(name="fiabilite", label="Fiabilité (test sur le passé)",
                    expr=(f"1 - SUM(ABS({qs} - prevision_naive)) "
                          f"/ NULLIF(SUM({qs}) FILTER (WHERE prevision_naive IS NOT NULL), 0)"),
                    format="percent", thresholds=reliability_thr,
                    description="1 moins l'erreur absolue de la prévision naïve sur les ventes de la période."),
        MeasureSpec(name="semaine_pic", label="Semaine de pic",
                    expr=(f"MAX((SELECT weekofyear(d) FROM ({weekly_peak}) ORDER BY v DESC, d LIMIT 1))"),
                    format="integer", description="Numéro ISO de la semaine la plus chargée de l'horizon."),
        MeasureSpec(name="pic_volume", label="Volume du pic", expr=f"MAX((SELECT MAX(v) FROM ({weekly_peak})))",
                    format="compact", unit="unités"),
        MeasureSpec(name="methode_prevision", label="Méthode",
                    expr="CASE WHEN COUNT(DISTINCT methode) > 1 THEN 'Mixte' ELSE MAX(methode) END",
                    description="Méthode qui a produit la prévision : moteur ou prévision naïve."),
        MeasureSpec(name="ventes_hebdo", label="Ventes", expr=f"SUM({qs})", format="compact", unit="unités"),
        MeasureSpec(name="prevision_hebdo", label="Prévision", expr="SUM(prevision)", format="compact", unit="unités"),
        MeasureSpec(name="ventes_n1_hebdo", label="Même semaine N-1", expr="SUM(ventes_n1)", format="compact",
                    unit="unités"),
    ]
    source = SourceSpec(name="ventes", label="Ventes et prévisions", kind="dataset", dataset_id=dataset_id,
                        transforms=transforms, fields=fields, measures=measures)
    filters = [
        FilterSpec(id="famille", label=label, field=f, type="select", default=None),
        FilterSpec(id="periode", label="Période", field=d, type="relative_period", default="52w",
                   presets=["13w", "26w", "52w", "all"]),
    ]

    # ---- visuals ------------------------------------------------------
    def kpi(title: str, measure: str, **kw: Any) -> VisualSpec:
        return VisualSpec(type="kpi", title=title, source="ventes", kpi=KpiSpec(measure=measure, **kw))

    synthese = VisualSpec(
        type="text",
        source="ventes",
        text=(
            f"{{{{filters.famille}}}} : {{{{prevu_horizon|compact}}}} unités prévues sur les {horizon} prochaines "
            "semaines, soit {{evolution_n1|percent}} par rapport aux mêmes semaines de l'an dernier. Pic en "
            "semaine {{semaine_pic|integer}} ({{pic_volume|compact}} unités). Fiabilité de la méthode de référence "
            "sur la période : {{fiabilite|percent}}."
        ),
    )
    kpis = [
        kpi("Total prévu sur l'horizon", "prevu_horizon", secondary=f"sur {horizon} semaines"),
        kpi("Écart vs même période N-1", "evolution_n1", secondary="N-1 : {{ventes_n1_horizon|compact}}"),
        kpi("Fiabilité (test sur le passé)", "fiabilite", secondary="Prévision naïve appliquée à la période",
            target_label=f"cible ≥ {round(_RELIABLE * 100)} %"),
        kpi("Semaine de pic", "semaine_pic", secondary="{{pic_volume|compact}} unités prévues"),
    ]
    combo = VisualSpec(
        type="combo",
        title="Ventes, prévision et même semaine N-1",
        subtitle=("Avant la dernière semaine connue, la prévision est celle de la méthode naïve (test sur le "
                  f"passé) ; sur les {horizon} semaines à venir, celle de la méthode indiquée dans le tableau."),
        source="ventes",
        x=d,
        series=[
            SeriesSpec(key="ventes_hebdo", label="Ventes", mark="area", color="good"),
            SeriesSpec(key="prevision_hebdo", label="Prévision", mark="line", color=BROWN),
            SeriesSpec(key="ventes_n1_hebdo", label="Même semaine N-1", mark="line", color="muted", dash=True),
        ],
        sort="x_asc",
    )
    par_famille = VisualSpec(
        type="hbar",
        title=f"Prévu par {noun}",
        subtitle=f"Cliquer sur une {noun} pour filtrer le tableau de bord.",
        source="ventes",
        x=f,
        series=[SeriesSpec(key="prevu_horizon", label="Prévu", mark="bar", color=BLUE)],
        sort="value_desc",
        click_filters={"famille": f},
        ignore_filters=["famille"],
        highlight_filter="famille",
    )
    ecart = VisualSpec(
        type="diverging_bar",
        title=f"Écart vs même période N-1, par {noun}",
        subtitle="Positif : la prévision dépasse les ventes des mêmes semaines l'an dernier.",
        source="ventes",
        x=f,
        series=[SeriesSpec(key="evolution_n1", label="Écart", mark="bar",
                           color_rules=[ColorRule(op=">=", value=0, color="good"),
                                        ColorRule(op="<", value=0, color=BROWN)])],
        sort="value_desc",
        ignore_filters=["famille"],
    )
    table = VisualSpec(
        type="table",
        title=f"Détail par {noun}",
        subtitle=f"Cliquer sur une {noun} pour filtrer le tableau de bord.",
        source="ventes",
        x=f,
        columns=[
            ColumnSpec(key=f, label=label),
            ColumnSpec(key="prevu_horizon", label="Prévu"),
            ColumnSpec(key="ventes_n1_horizon", label="N-1"),
            ColumnSpec(key="evolution_n1", label="Évolution vs N-1"),
            ColumnSpec(key="fiabilite", label="Fiabilité", use_thresholds=True),
            ColumnSpec(key="methode_prevision", label="Méthode"),
        ],
        sort="value_desc",
        total_row=True,
        click_filters={"famille": f},
        ignore_filters=["famille"],
        highlight_filter="famille",
    )
    engine_text = (
        f"Les {horizon} semaines à venir viennent du moteur th2forecast. Pour une famille sans réponse du moteur "
        f"(service indisponible ou réponse partielle), la prévision naïve la remplace : la colonne « Méthode » du "
        "tableau l'indique."
        if with_engine_forecast
        else f"Les {horizon} semaines à venir viennent de la prévision naïve : le moteur th2forecast n'est pas utilisé."
    )
    notes = [
        VisualSpec(
            type="note",
            title="Origine des données",
            badges=[Provenance.REAL, fc],
            text=(
                f"Les ventes (`{s}`) sont les données réelles du fichier importé, additionnées par semaine et par "
                f"{noun} (`{f}`). Tout le reste est calculé : prévisions et écarts sont des estimations."
            ),
        ),
        VisualSpec(
            type="note",
            title="Méthode de prévision",
            badges=[fc],
            text=(
                f"{engine_text} La prévision naïve reprend les ventes de la même semaine l'an dernier, corrigées de "
                "la tendance récente (moyenne des 4 dernières semaines, bornée entre 0,8 et 1,25) ; au début de "
                "l'historique, c'est la moyenne des 4 semaines précédentes."
            ),
        ),
        VisualSpec(
            type="note",
            title="Définition des indicateurs",
            text=(
                f"**Écart vs N-1** : prévu sur {horizon} semaines ÷ ventes des mêmes semaines un an plus tôt (364 jours), "
                f"moins 1. **Fiabilité** : 1 - erreur absolue de la prévision naïve ÷ ventes, sur la période ; vert "
                f"à partir de {round(_RELIABLE * 100)} %, orange à partir de {round(_ACCEPTABLE * 100)} %. "
                "**Semaine de pic** : numéro ISO de la semaine au plus gros volume prévu."
            ),
        ),
        VisualSpec(
            type="note",
            title="Limites",
            text=(
                "La série est supposée hebdomadaire, sans semaine manquante. La prévision naïve demande plus d'un an "
                "d'historique par famille : sinon seules les premières semaines à venir sont chiffrées. Ni les "
                "promotions ni les ruptures passées ne sont connues. La fiabilité mesure la méthode naïve, pas le "
                "moteur."
            ),
        ),
    ]

    grid: list[tuple[VisualSpec, dict[str, int]]] = [(synthese, dict(row=0, col=0, width=12, height=2))]
    for i, tile in enumerate(kpis):
        grid.append((tile, dict(row=2, col=3 * i, width=3, height=3)))
    grid += [
        (combo, dict(row=5, col=0, width=12, height=6)),
        (par_famille, dict(row=11, col=0, width=6, height=5)),
        (ecart, dict(row=11, col=6, width=6, height=5)),
        (table, dict(row=16, col=0, width=12, height=5)),
    ]
    for i, note in enumerate(notes):
        grid.append((note, dict(row=21, col=3 * i, width=3, height=6)))

    return {
        "sources": [_dump(source)],
        "filters": [_dump(flt) for flt in filters],
        "components": [{"visual": _dump(v), "position": pos} for v, pos in grid],
    }
