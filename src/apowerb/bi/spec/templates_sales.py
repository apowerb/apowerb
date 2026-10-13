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
from apowerb.bi.spec.templates_common import DEFAULT_LANG, translator

_PERIOD_RE = re.compile(r"^(all|\d{1,3}[wm])$")
_RESERVED = ("mois", "ca", "ca_n1", "quantite", "n1_dispo")
_PRESETS = ["3m", "12m", "24m", "all"]
_MAX_PERIOD_MONTHS = 120
_TOP_CUSTOMERS = 10

DESCRIPTOR: dict[str, Any] = {
    "id": "sales_tracking",
    "label": {"fr": "Suivi des ventes", "en": "Sales tracking"},
    "description": {
        "fr": (
            "Tableau de bord de suivi des ventes à partir de lignes de vente : chiffre d'affaires et évolution "
            "par rapport à la même période de l'an dernier, courbe mensuelle, familles, clients et régions. "
            "Les données sont agrégées par mois."
        ),
        "en": (
            "Sales tracking dashboard built from sales lines: revenue and change against the same period "
            "last year, monthly curve, product families, customers and regions. "
            "Data is aggregated by month."
        ),
    },
    "version": 1,
    "columns": [
        {"key": "date", "label": "Date", "type": "date", "required": True, "default": "date"},
        {"key": "amount", "label": {"fr": "Montant", "en": "Amount"}, "type": "number", "required": True, "default": "montant"},
        {"key": "quantity", "label": {"fr": "Quantité", "en": "Quantity"}, "type": "number", "required": False, "default": None},
        {"key": "family", "label": {"fr": "Famille", "en": "Product family"}, "type": "string", "required": True, "default": "famille"},
        {"key": "customer", "label": {"fr": "Client", "en": "Customer"}, "type": "string", "required": False, "default": None},
        {"key": "region", "label": {"fr": "Région", "en": "Region"}, "type": "string", "required": False, "default": None},
    ],
    "params": [
        {"key": "period_months", "label": {"fr": "Période affichée par défaut (mois)", "en": "Default displayed period (months)"},
         "type": "integer",
         "default": 12, "min": 1, "max": _MAX_PERIOD_MONTHS},
    ],
}


def build(dataset_id: str, columns: dict[str, str | None], params: dict[str, Any], lang: str = DEFAULT_LANG) -> dict:
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
        lang=lang,
    )


def _split(total: int, parts: int) -> list[int]:
    """``parts`` widths that add up to ``total`` columns, the first ones wider."""
    base, extra = divmod(total, parts)
    return [base + (1 if i < extra else 0) for i in range(parts)]


def _period_text(period: str, lang: str = DEFAULT_LANG) -> str:
    T = translator(lang)
    if period == "all":
        return T("toute la période", "whole period")
    count, unit = int(period[:-1]), period[-1]
    word = T("mois", "month") if unit == "m" else T("semaine", "week")
    if count == 1:
        return T(f"dernier {word}", f"last {word}")
    if unit == "m":
        return T(f"{count} derniers mois", f"last {count} months")
    return T(f"{count} dernières semaines", f"last {count} weeks")


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
    lang: str = DEFAULT_LANG,
) -> dict:
    """Build the "Suivi des ventes" spec for a dataset of sales lines, in ``lang``.

    ``date``/``amount``/``family`` name the dataset columns; ``quantity``,
    ``customer`` and ``region`` are optional and each adds its indicators.
    ``default_period`` is a relative period (``12m``, ``26w``, ``all``...).
    """
    T = translator(lang)
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
    def dim_label(column: str, default: str, en: str) -> str:
        """Label derived from the column name; the default French names read in ``lang``."""
        return T(column.replace("_", " ").capitalize(), en) if column == default else column.replace("_", " ").capitalize()

    label = dim_label(f, "famille", "Product family")
    noun = label.lower()

    # ---- source -------------------------------------------------------
    fields = [
        FieldSpec(name="mois", label=T("Mois", "Month"), type="date", role="date"),
        FieldSpec(name=f, label=label, role="dimension"),
        *([FieldSpec(name=c, label=dim_label(c, "client", "Customer"), role="dimension")] if c else []),
        *([FieldSpec(name=r, label=dim_label(r, "region", "Region"), role="dimension")] if r else []),
        FieldSpec(name="ca", label=T("Chiffre d'affaires", "Revenue"), type="number", role="measure", format="compact"),
        FieldSpec(name="ca_n1", label=T("CA même mois N-1", "Revenue same month PY"), type="number", role="measure", format="compact"),
        *([FieldSpec(name="quantite", label=T("Quantité", "Quantity"), type="number", role="measure", format="compact")] if q else []),
    ]
    ca_n1 = "SUM(ca_n1)"
    comparable = "SUM(ca) FILTER (WHERE n1_dispo = 1)"
    evolution_thr = ThresholdSpec(direction="higher_is_better", good=0, warn=-0.05)
    measures = [
        MeasureSpec(name="ca_total", label=T("Chiffre d'affaires", "Revenue"), expr="SUM(ca)", format="compact"),
        MeasureSpec(name="ca_n1_total", label=T("CA même période N-1", "Revenue same period PY"), expr=ca_n1,
                    format="compact",
                    description=T("Chiffre d'affaires des mêmes mois un an plus tôt.",
                                  "Revenue of the same months one year earlier.")),
        MeasureSpec(name="ca_comparable", label=T("CA des mois comparables", "Revenue of comparable months"), expr=comparable,
                    format="compact",
                    description=T("Chiffre d'affaires des mois qui ont un équivalent un an plus tôt.",
                                  "Revenue of the months that have an equivalent one year earlier.")),
        MeasureSpec(name="evolution_n1", label=T("Évolution vs N-1", "Change vs PY"),
                    expr=f"{comparable} / NULLIF({ca_n1}, 0) - 1",
                    format="percent", thresholds=evolution_thr,
                    description=T("CA des mois comparables ÷ CA des mêmes mois un an plus tôt, moins 1.",
                                  "Revenue of comparable months ÷ revenue of the same months one year earlier, minus 1.")),
        MeasureSpec(name="ca_n1_mensuel", label=T("Même mois N-1", "Same month PY"), expr="NULLIF(SUM(ca_n1) FILTER (WHERE n1_dispo = 1), 0)",
                    format="compact"),
        MeasureSpec(name="part_ca", label=T("Part du CA", "Revenue share"),
                    expr="SUM(ca) / NULLIF((SELECT SUM(ca) FROM frame), 0)",
                    format="percent",
                    description=T("Part de la ligne dans le CA de la période, toutes familles confondues.",
                                  "Share of the row in the revenue of the period, all product families combined.")),
    ]
    if q:
        measures += [
            MeasureSpec(name="quantite_totale", label=T("Quantité", "Quantity"), expr="SUM(quantite)", format="compact"),
            MeasureSpec(name="prix_moyen", label=T("Prix moyen", "Average price"),
                        expr="SUM(ca) / NULLIF(SUM(quantite), 0)",
                        format="decimal2",
                        description=T("Chiffre d'affaires ÷ quantité vendue.", "Revenue ÷ quantity sold.")),
        ]
    if c:
        measures.append(MeasureSpec(name="nb_clients", label=T("Clients actifs", "Active customers"),
                                    expr=f"COUNT(DISTINCT {quote_ident(c)}) FILTER (WHERE ca <> 0)", format="integer",
                                    description=T("Clients avec au moins un achat sur la période.",
                                                  "Customers with at least one purchase in the period.")))
    source = SourceSpec(
        name="ventes",
        label=T("Ventes par mois", "Sales by month"),
        kind="dataset",
        dataset_id=dataset_id,
        transforms=[SqlStep(query=_monthly_query(d, a, q, dims))],
        fields=fields,
        measures=measures,
    )

    presets = _PRESETS if default_period in _PRESETS else [*_PRESETS[:-1], default_period, "all"]
    filters = [
        FilterSpec(id="famille", label=label, field=f, type="select", default=None),
        FilterSpec(id="periode", label=T("Période", "Period"), field="mois", type="relative_period", default=default_period,
                   presets=presets),
    ]

    # ---- visuals ---------------------------------------------------------
    def kpi(title: str, measure: str, **kw: Any) -> VisualSpec:
        return VisualSpec(type="kpi", title=title, source="ventes", kpi=KpiSpec(measure=measure, **kw))

    clients_text = T(" {{nb_clients|integer}} clients actifs.", " {{nb_clients|integer}} active customers.") if c else ""
    synthese = VisualSpec(
        type="text",
        source="ventes",
        text=T(
            (
                "{{filters.famille}} · {{filters.periode}} (jusqu'à {{period.last}}) : chiffre d'affaires de "
                "{{ca_total|compact}}, en évolution de {{evolution_n1|percent}} par rapport à la même période "
                f"de l'an dernier.{clients_text}"
            ),
            (
                "{{filters.famille}} · {{filters.periode}} (up to {{period.last}}): revenue of "
                "{{ca_total|compact}}, changing by {{evolution_n1|percent}} against the same period "
                f"last year.{clients_text}"
            ),
        ),
    )
    kpis = [
        kpi(T("Chiffre d'affaires", "Revenue"), "ca_total", sparkline="ca_total",
            secondary=T("N-1 : {{ca_n1_total|compact}}", "PY: {{ca_n1_total|compact}}")),
        kpi(T("Évolution vs même période N-1", "Change vs same period PY"), "evolution_n1",
            secondary=T("{{ca_comparable|compact}} contre {{ca_n1_total|compact}}",
                        "{{ca_comparable|compact}} against {{ca_n1_total|compact}}"),
            target_label=T("alerte sous -5 %", "alert below -5%")),
    ]
    if q:
        kpis += [
            kpi(T("Quantité", "Quantity"), "quantite_totale", sparkline="quantite_totale"),
            kpi(T("Prix moyen", "Average price"), "prix_moyen", sparkline="prix_moyen",
                secondary=T("CA ÷ quantité", "Revenue ÷ quantity")),
        ]
    if c:
        kpis.append(kpi(T("Clients actifs", "Active customers"), "nb_clients",
                        secondary=T("au moins un achat sur la période", "at least one purchase in the period")))

    combo = VisualSpec(
        type="combo",
        title=T("CA mensuel vs même mois N-1", "Monthly revenue vs same month PY"),
        subtitle=T("Les mois sans équivalent un an plus tôt n'ont pas de courbe N-1.",
                   "Months with no equivalent one year earlier have no PY curve."),
        source="ventes",
        x="mois",
        series=[
            SeriesSpec(key="ca_total", label=T("Chiffre d'affaires", "Revenue"), mark="bar", color=BLUE),
            SeriesSpec(key="ca_n1_mensuel", label=T("Même mois N-1", "Same month PY"), mark="line", color="muted", dash=True),
        ],
        sort="x_asc",
    )
    top_families = VisualSpec(
        type="hbar",
        title=T(f"CA par {noun}", f"Revenue by {noun}"),
        subtitle=T(f"Cliquer sur une {noun} pour filtrer le tableau de bord.",
                   "Click a bar to filter the dashboard."),
        source="ventes",
        x=f,
        series=[SeriesSpec(key="ca_total", label=T("Chiffre d'affaires", "Revenue"), mark="bar", color=BLUE)],
        sort="value_desc",
        click_filters={"famille": f},
        ignore_filters=["famille"],
        highlight_filter="famille",
    )
    charts = [top_families]
    if c:
        charts.append(VisualSpec(
            type="hbar",
            title=T(f"Top {_TOP_CUSTOMERS} clients", f"Top {_TOP_CUSTOMERS} customers"),
            source="ventes",
            x=c,
            series=[SeriesSpec(key="ca_total", label=T("Chiffre d'affaires", "Revenue"), mark="bar", color=BLUE)],
            sort="value_desc",
            top_n=_TOP_CUSTOMERS,
        ))
    if r:
        charts.append(VisualSpec(
            type="pie",
            title=T("Répartition par région", "Breakdown by region"),
            source="ventes",
            x=r,
            series=[SeriesSpec(key="ca_total", label=T("Chiffre d'affaires", "Revenue"))],
        ))
    columns = [
        ColumnSpec(key=f, label=label),
        ColumnSpec(key="ca_total", label=T("CA", "Revenue")),
        ColumnSpec(key="part_ca", label=T("Part du CA", "Revenue share")),
        ColumnSpec(key="ca_n1_total", label=T("CA N-1", "Revenue PY")),
        ColumnSpec(key="evolution_n1", label=T("Évolution vs N-1", "Change vs PY"), use_thresholds=True),
    ]
    if q:
        columns.append(ColumnSpec(key="quantite_totale", label=T("Quantité", "Quantity")))
    table = VisualSpec(
        type="table",
        title=T(f"Comparatif par {noun}", f"Comparison by {noun}"),
        subtitle=T(f"Cliquer sur une {noun} pour filtrer le tableau de bord.",
                   "Click a row to filter the dashboard."),
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
            title=T("Origine des données", "Data source"),
            badges=[Provenance.REAL],
            text=T(
                (
                    f"Toutes les valeurs viennent des lignes de vente importées (colonnes {used}), additionnées par "
                    "mois. Aucune valeur n'est simulée ni prévue. La période initiale est les "
                    f"{_period_text(default_period)}, modifiable en haut du tableau de bord."
                ),
                (
                    f"All values come from the imported sales lines (columns {used}), summed by "
                    "month. No value is simulated or forecast. The initial period is the "
                    f"{_period_text(default_period, lang)}, adjustable at the top of the dashboard."
                ),
            ),
        ),
        VisualSpec(
            type="note",
            title=T("Définition des indicateurs", "Indicator definitions"),
            text=T(
                (
                    "**Évolution vs N-1** : chiffre d'affaires des mois de la période, divisé par celui des mêmes mois "
                    "un an plus tôt, moins 1 ; seuls les mois qui ont un équivalent un an plus tôt comptent. Le voyant "
                    "passe à l'orange sous 0 % et au rouge sous -5 %. **Part du CA** : CA de la ligne ÷ CA de toutes "
                    f"les {noun}s de la période."
                    + (" **Prix moyen** : CA ÷ quantité." if q else "")
                    + (" **Clients actifs** : clients avec au moins une vente." if c else "")
                ),
                (
                    "**Change vs PY**: revenue of the months of the period, divided by that of the same months "
                    "one year earlier, minus 1; only months that have an equivalent one year earlier count. The "
                    "indicator turns orange below 0% and red below -5%. **Revenue share**: row revenue ÷ total "
                    "revenue of the period."
                    + (" **Average price**: revenue ÷ quantity." if q else "")
                    + (" **Active customers**: customers with at least one sale." if c else "")
                ),
            ),
        ),
        VisualSpec(
            type="note",
            title=T("Limites", "Limitations"),
            text=T(
                (
                    "Le dernier mois est partiel si les données s'arrêtent en cours de mois : son CA paraît alors "
                    "inférieur à celui du même mois l'an dernier. Les retours et avoirs, s'ils sont saisis en "
                    "négatif, viennent en déduction du CA."
                ),
                (
                    "The last month is partial if the data stops mid-month: its revenue then looks "
                    "lower than that of the same month last year. Returns and credit notes, if entered as "
                    "negatives, are deducted from revenue."
                ),
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
