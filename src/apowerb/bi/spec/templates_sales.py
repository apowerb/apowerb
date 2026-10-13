"""
bi/spec/templates_sales.py
--------------------------
"Suivi des ventes" dashboard (spec v2) for a dataset of sales lines.

Whatever the granularity of the date column, the data is aggregated by month
(and family, customer, region). Last year's amount of each month rides on the
same row (``ca_n1``), so the period filter and the "vs same period last year"
comparison stay consistent for any period, not only 12 months.
"""

from __future__ import annotations

import re
from typing import Any

from apowerb.bi.spec.duck import quote_ident
from apowerb.bi.spec.models import (
    ColumnSpec,
    FieldSpec,
    FilterSpec,
    KpiSpec,
    MeasureSpec,
    Provenance,
    SeriesSpec,
    SourceSpec,
    SqlStep,
    ThresholdSpec,
    VisualSpec,
)
from apowerb.bi.spec.templates import BLUE, _check_ident, _dump

_PERIOD_RE = re.compile(r"^(all|\d{1,3}[wm])$")
_RESERVED = ("mois", "ca", "ca_n1", "quantite", "n1_dispo")
_PRESETS = ["3m", "12m", "24m", "all"]
_MAX_PERIOD_MONTHS = 120
_TOP_CUSTOMERS = 10

DESCRIPTOR: dict[str, Any] = {
    "id": "sales_tracking",
    "label": "Suivi des ventes",
    "description": (
        "Tableau de bord de suivi des ventes à partir de lignes de vente : chiffre d'affaires et évolution "
        "par rapport à la même période de l'an dernier, courbe mensuelle, familles, clients et régions. "
        "Les données sont agrégées par mois."
    ),
    "version": 1,
    "columns": [
        {"key": "date", "label": "Date", "type": "date", "required": True, "default": "date"},
        {"key": "amount", "label": "Montant", "type": "number", "required": True, "default": "montant"},
        {"key": "quantity", "label": "Quantité", "type": "number", "required": False, "default": None},
        {"key": "family", "label": "Famille", "type": "string", "required": True, "default": "famille"},
        {"key": "customer", "label": "Client", "type": "string", "required": False, "default": None},
        {"key": "region", "label": "Région", "type": "string", "required": False, "default": None},
    ],
    "params": [
        {"key": "period_months", "label": "Période affichée par défaut (mois)", "type": "integer",
         "default": 12, "min": 1, "max": _MAX_PERIOD_MONTHS},
    ],
}


def build(dataset_id: str, columns: dict[str, str | None], params: dict[str, Any]) -> dict:
    defaults = {c["key"]: c["default"] for c in DESCRIPTOR["columns"]}
    months = params.get("period_months") or DESCRIPTOR["params"][0]["default"]
    return sales_tracking(
        dataset_id,
        date=columns.get("date") or defaults["date"],
        amount=columns.get("amount") or defaults["amount"],
        quantity=columns.get("quantity") or None,
        family=columns.get("family") or defaults["family"],
        customer=columns.get("customer") or None,
        region=columns.get("region") or None,
        default_period=f"{months}m",
    )


def _split(total: int, parts: int) -> list[int]:
    """``parts`` widths that add up to ``total`` columns, the first ones wider."""
    base, extra = divmod(total, parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]


def _period_text(period: str) -> str:
    if period == "all":
        return "toute la période"
    count, unit = int(period[:-1]), period[-1]
    word = "mois" if unit == "m" else "semaine"
    if count == 1:
        return f"dernier {word}"
    return f"{count} derniers mois" if unit == "m" else f"{count} dernières semaines"


def _monthly_query(date: str, amount: str, quantity: str | None, dims: list[str]) -> str:
    """Month x dimensions, with last year's amount of each month on the same row."""
    d, a = quote_ident(date), quote_ident(amount)
    cols = ", ".join(quote_ident(x) for x in dims)
    qty_base = f", SUM({quote_ident(quantity)}) AS quantite" if quantity else ""
    qty_cur = ", quantite" if quantity else ""
    qty_out = ", SUM(quantite) AS quantite" if quantity else ""
    qty_prev = ", 0.0 AS quantite" if quantity else ""
    positions = ", ".join(str(i) for i in range(1, len(dims) + 2))
    return (
        f"WITH base AS (SELECT date_trunc('month', CAST({d} AS DATE)) AS mois, {cols}, SUM({a}) AS ca{qty_base} "
        f"FROM input WHERE {d} IS NOT NULL GROUP BY {positions}), "
        "edge AS (SELECT MIN(mois) AS first_m, MAX(mois) AS last_m FROM base) "
        f"SELECT mois, {cols}, SUM(ca) AS ca{qty_out}, SUM(ca_n1) AS ca_n1, "
        "CASE WHEN mois - INTERVAL 12 MONTH >= (SELECT first_m FROM edge) THEN 1 ELSE 0 END AS n1_dispo "
        f"FROM (SELECT mois, {cols}, ca{qty_cur}, 0.0 AS ca_n1 FROM base "
        f"UNION ALL SELECT CAST(mois + INTERVAL 12 MONTH AS DATE) AS mois, {cols}, 0.0 AS ca{qty_prev}, ca AS ca_n1 "
        "FROM base WHERE mois + INTERVAL 12 MONTH <= (SELECT last_m FROM edge)) GROUP BY ALL"
    )


def sales_tracking(
    dataset_id: str,
    *,
    date: str = "date",
    amount: str = "montant",
    quantity: str | None = None,
    family: str = "famille",
    customer: str | None = None,
    region: str | None = None,
    default_period: str = "12m",
) -> dict:
    """Build the "Suivi des ventes" spec for a dataset of sales lines.

    ``date``/``amount``/``family`` name the dataset columns; ``quantity``,
    ``customer`` and ``region`` are optional and each adds its indicators.
    ``default_period`` is a relative period (``12m``, ``26w``, ``all``...).
    """
    if not dataset_id:
        raise ValueError("dataset_id is required.")
    d = _check_ident("date", date)
    a = _check_ident("amount", amount)
    f = _check_ident("family", family)
    q = _check_ident("quantity", quantity) if quantity else None
    c = _check_ident("customer", customer) if customer else None
    r = _check_ident("region", region) if region else None
    if not _PERIOD_RE.match(default_period or ""):
        raise ValueError(f"default_period='{default_period}' is not a period: use 13w, 26w, 3m, 12m or all.")
    dims = [f, *([c] if c else []), *([r] if r else [])]
    if len(set(dims)) != len(dims) or set(dims) & set(_RESERVED):
        raise ValueError(
            f"family, customer and region must be distinct columns and not one of the reserved names {_RESERVED}."
        )
    label = f.replace("_", " ").capitalize()
    noun = label.lower()

    # ---- source -------------------------------------------------------
    fields = [
        FieldSpec(name="mois", label="Mois", type="date", role="date"),
        FieldSpec(name=f, label=label, role="dimension"),
        *([FieldSpec(name=c, label=c.replace("_", " ").capitalize(), role="dimension")] if c else []),
        *([FieldSpec(name=r, label=r.replace("_", " ").capitalize(), role="dimension")] if r else []),
        FieldSpec(name="ca", label="Chiffre d'affaires", type="number", role="measure", format="compact"),
        FieldSpec(name="ca_n1", label="CA même mois N-1", type="number", role="measure", format="compact"),
        *([FieldSpec(name="quantite", label="Quantité", type="number", role="measure", format="compact")] if q else []),
    ]
    ca_n1 = "SUM(ca_n1)"
    comparable = "SUM(ca) FILTER (WHERE n1_dispo = 1)"
    evolution_thr = ThresholdSpec(direction="higher_is_better", good=0, warn=-0.05)
    measures = [
        MeasureSpec(name="ca_total", label="Chiffre d'affaires", expr="SUM(ca)", format="compact"),
        MeasureSpec(name="ca_n1_total", label="CA même période N-1", expr=ca_n1, format="compact",
                    description="Chiffre d'affaires des mêmes mois un an plus tôt."),
        MeasureSpec(name="ca_comparable", label="CA des mois comparables", expr=comparable, format="compact",
                    description="Chiffre d'affaires des mois qui ont un équivalent un an plus tôt."),
        MeasureSpec(name="evolution_n1", label="Évolution vs N-1", expr=f"{comparable} / NULLIF({ca_n1}, 0) - 1",
                    format="percent", thresholds=evolution_thr,
                    description="CA des mois comparables ÷ CA des mêmes mois un an plus tôt, moins 1."),
        MeasureSpec(name="ca_n1_mensuel", label="Même mois N-1", expr="NULLIF(SUM(ca_n1) FILTER (WHERE n1_dispo = 1), 0)",
                    format="compact"),
        MeasureSpec(name="part_ca", label="Part du CA", expr="SUM(ca) / NULLIF((SELECT SUM(ca) FROM frame), 0)",
                    format="percent", description="Part de la ligne dans le CA de la période, toutes familles confondues."),
    ]
    if q:
        measures += [
            MeasureSpec(name="quantite_totale", label="Quantité", expr="SUM(quantite)", format="compact"),
            MeasureSpec(name="prix_moyen", label="Prix moyen", expr="SUM(ca) / NULLIF(SUM(quantite), 0)",
                        format="decimal2", description="Chiffre d'affaires ÷ quantité vendue."),
        ]
    if c:
        measures.append(MeasureSpec(name="nb_clients", label="Clients actifs",
                                    expr=f"COUNT(DISTINCT {quote_ident(c)}) FILTER (WHERE ca <> 0)", format="integer",
                                    description="Clients avec au moins un achat sur la période."))
    source = SourceSpec(
        name="ventes",
        label="Ventes par mois",
        kind="dataset",
        dataset_id=dataset_id,
        transforms=[SqlStep(query=_monthly_query(d, a, q, dims))],
        fields=fields,
        measures=measures,
    )

    presets = _PRESETS if default_period in _PRESETS else [*_PRESETS[:-1], default_period, "all"]
    filters = [
        FilterSpec(id="famille", label=label, field=f, type="select", default=None),
        FilterSpec(id="periode", label="Période", field="mois", type="relative_period", default=default_period,
                   presets=presets),
    ]

    # ---- visuals ---------------------------------------------------------
    def kpi(title: str, measure: str, **kw: Any) -> VisualSpec:
        return VisualSpec(type="kpi", title=title, source="ventes", kpi=KpiSpec(measure=measure, **kw))

    clients_text = " {{nb_clients|integer}} clients actifs." if c else ""
    synthese = VisualSpec(
        type="text",
        source="ventes",
        text=(
            "{{filters.famille}} · {{filters.periode}} (jusqu'à {{period.last}}) : chiffre d'affaires de "
            "{{ca_total|compact}}, en évolution de {{evolution_n1|percent}} par rapport à la même période "
            f"de l'an dernier.{clients_text}"
        ),
    )
    kpis = [
        kpi("Chiffre d'affaires", "ca_total", sparkline="ca_total", secondary="N-1 : {{ca_n1_total|compact}}"),
        kpi("Évolution vs même période N-1", "evolution_n1",
            secondary="{{ca_comparable|compact}} contre {{ca_n1_total|compact}}", target_label="alerte sous -5 %"),
    ]
    if q:
        kpis += [
            kpi("Quantité", "quantite_totale", sparkline="quantite_totale"),
            kpi("Prix moyen", "prix_moyen", sparkline="prix_moyen", secondary="CA ÷ quantité"),
        ]
    if c:
        kpis.append(kpi("Clients actifs", "nb_clients", secondary="au moins un achat sur la période"))

    combo = VisualSpec(
        type="combo",
        title="CA mensuel vs même mois N-1",
        subtitle="Les mois sans équivalent un an plus tôt n'ont pas de courbe N-1.",
        source="ventes",
        x="mois",
        series=[
            SeriesSpec(key="ca_total", label="Chiffre d'affaires", mark="bar", color=BLUE),
            SeriesSpec(key="ca_n1_mensuel", label="Même mois N-1", mark="line", color="muted", dash=True),
        ],
        sort="x_asc",
    )
    top_families = VisualSpec(
        type="hbar",
        title=f"CA par {noun}",
        subtitle=f"Cliquer sur une {noun} pour filtrer le tableau de bord.",
        source="ventes",
        x=f,
        series=[SeriesSpec(key="ca_total", label="Chiffre d'affaires", mark="bar", color=BLUE)],
        sort="value_desc",
        click_filters={"famille": f},
        ignore_filters=["famille"],
        highlight_filter="famille",
    )
    charts = [top_families]
    if c:
        charts.append(VisualSpec(
            type="hbar",
            title=f"Top {_TOP_CUSTOMERS} clients",
            source="ventes",
            x=c,
            series=[SeriesSpec(key="ca_total", label="Chiffre d'affaires", mark="bar", color=BLUE)],
            sort="value_desc",
            top_n=_TOP_CUSTOMERS,
        ))
    if r:
        charts.append(VisualSpec(
            type="pie",
            title="Répartition par région",
            source="ventes",
            x=r,
            series=[SeriesSpec(key="ca_total", label="Chiffre d'affaires")],
        ))
    columns = [
        ColumnSpec(key=f, label=label),
        ColumnSpec(key="ca_total", label="CA"),
        ColumnSpec(key="part_ca", label="Part du CA"),
        ColumnSpec(key="ca_n1_total", label="CA N-1"),
        ColumnSpec(key="evolution_n1", label="Évolution vs N-1", use_thresholds=True),
    ]
    if q:
        columns.append(ColumnSpec(key="quantite_totale", label="Quantité"))
    table = VisualSpec(
        type="table",
        title=f"Comparatif par {noun}",
        subtitle=f"Cliquer sur une {noun} pour filtrer le tableau de bord.",
        source="ventes",
        x=f,
        columns=columns,
        sort="value_desc",
        total_row=True,
        click_filters={"famille": f},
        ignore_filters=["famille"],
        highlight_filter="famille",
    )

    used = f"`{d}`, `{a}`, `{f}`" + "".join(f", `{x}`" for x in (q, c, r) if x)
    notes = [
        VisualSpec(
            type="note",
            title="Origine des données",
            badges=[Provenance.REAL],
            text=(
                f"Toutes les valeurs viennent des lignes de vente importées (colonnes {used}), additionnées par "
                "mois. Aucune valeur n'est simulée ni prévue. La période initiale est les "
                f"{_period_text(default_period)}, modifiable en haut du tableau de bord."
            ),
        ),
        VisualSpec(
            type="note",
            title="Définition des indicateurs",
            text=(
                "**Évolution vs N-1** : chiffre d'affaires des mois de la période, divisé par celui des mêmes mois "
                "un an plus tôt, moins 1 ; seuls les mois qui ont un équivalent un an plus tôt comptent. Le voyant "
                "passe à l'orange sous 0 % et au rouge sous -5 %. **Part du CA** : CA de la ligne ÷ CA de toutes "
                f"les {noun}s de la période."
                + (" **Prix moyen** : CA ÷ quantité." if q else "")
                + (" **Clients actifs** : clients avec au moins une vente." if c else "")
            ),
        ),
        VisualSpec(
            type="note",
            title="Limites",
            text=(
                "Le dernier mois est partiel si les données s'arrêtent en cours de mois : son CA paraît alors "
                "inférieur à celui du même mois l'an dernier. Les retours et avoirs, s'ils sont saisis en "
                "négatif, viennent en déduction du CA."
            ),
        ),
    ]

    grid: list[tuple[VisualSpec, dict[str, int]]] = [(synthese, dict(row=0, col=0, width=12, height=2))]
    row = 2
    col = 0
    for tile, width in zip(kpis, _split(12, len(kpis))):
        grid.append((tile, dict(row=row, col=col, width=width, height=3)))
        col += width
    row += 3
    grid.append((combo, dict(row=row, col=0, width=12, height=6)))
    row += 6
    col = 0
    for chart, width in zip(charts, _split(12, len(charts))):
        grid.append((chart, dict(row=row, col=col, width=width, height=6)))
        col += width
    row += 6
    grid.append((table, dict(row=row, col=0, width=12, height=6)))
    row += 6
    col = 0
    for note, width in zip(notes, _split(12, len(notes))):
        grid.append((note, dict(row=row, col=col, width=width, height=5)))
        col += width

    return {
        "sources": [_dump(source)],
        "filters": [_dump(flt) for flt in filters],
        "components": [{"visual": _dump(v), "position": pos} for v, pos in grid],
    }
