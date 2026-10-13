"""
bi/spec/templates_ecommerce.py
------------------------------
Spec v2 template « E-commerce »: one row per order (Shopify, WooCommerce,
PrestaShop export), optionally with the ad spend of the period.

Cancelled and refunded orders (status starting with « annul », « rembours »,
« cancel » or « refund ») are left out. Every ratio is a ratio of sums, so a
week, a channel and the whole period share the same definition. Ad spend is
summed as it comes: each expense must appear once, either on a dedicated row
(no order id) or allocated to an order row.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from apowerb.bi.spec.models import (
    IDENT,
    DeriveStep,
    FieldSpec,
    FilterSpec,
    FilterStep,
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

BROWN = "#9a6b3f"
BLUE = "#3b6fd8"

ROAS_THRESHOLD = 2

DESCRIPTOR: dict[str, Any] = {
    "id": "ecommerce",
    "label": "E-commerce",
    "description": (
        "Tableau de bord d'une boutique en ligne à partir d'une ligne par commande (export Shopify, WooCommerce, "
        "PrestaShop) : chiffre d'affaires, commandes, panier moyen, marge de contribution, ROAS avec alerte sous "
        "un seuil, coût d'acquisition client et part de clients récurrents. Les commandes annulées ou remboursées "
        "sont exclues. Les dépenses publicitaires peuvent figurer sur des lignes dédiées du même fichier."
    ),
    "version": 1,
    "columns": [
        {"key": "date", "label": "Date de commande", "type": "date", "required": True, "default": "date"},
        {"key": "order_id", "label": "Numéro de commande", "type": "string", "required": True,
         "default": "commande_id"},
        {"key": "customer_id", "label": "Client", "type": "string", "required": False, "default": None},
        {"key": "amount", "label": "Montant de la commande", "type": "number", "required": True,
         "default": "montant"},
        {"key": "product_cost", "label": "Coût des produits", "type": "number", "required": False, "default": None},
        {"key": "channel", "label": "Canal", "type": "string", "required": False, "default": None},
        {"key": "ad_spend", "label": "Dépense publicitaire", "type": "number", "required": False, "default": None},
        {"key": "new_customer", "label": "Nouveau client (0/1)", "type": "number", "required": False,
         "default": None},
        {"key": "status", "label": "Statut de la commande", "type": "string", "required": False, "default": None},
    ],
    "params": [
        {"key": "roas_threshold", "label": "Seuil d'alerte du ROAS", "type": "integer",
         "default": ROAS_THRESHOLD, "min": 1, "max": 20},
    ],
}
_REQUIRED = [c["key"] for c in DESCRIPTOR["columns"] if c["required"]]
_OPTIONAL = [c["key"] for c in DESCRIPTOR["columns"] if not c["required"]]
_PARAMS = {p["key"]: p for p in DESCRIPTOR["params"]}

_EXCLUDED_STATUS = ("annul", "rembours", "cancel", "refund")
_TRUE_FLAGS = ("1", "1.0", "true", "vrai", "oui", "yes", "y", "o")


def _check_ident(label: str, value: str) -> str:
    if not _IDENT_RE.match(value or ""):
        raise ValueError(
            f"{label}='{value}' is not a valid column name: use lowercase letters, "
            "digits and underscores (start with a letter), e.g. 'commande_id'."
        )
    return value


def _check_params(params: dict[str, Any]) -> dict[str, int]:
    """Parameters with defaults filled in; ValueError when out of range."""
    clean: dict[str, int] = {}
    for key, spec in _PARAMS.items():
        value = params.get(key)
        value = spec["default"] if value is None else value
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"Parameter '{key}' must be an integer.")
        if not spec["min"] <= value <= spec["max"]:
            raise ValueError(f"Parameter '{key}' must be between {spec['min']} and {spec['max']}.")
        clean[key] = value
    return clean


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True)


def _q(column: str) -> str:
    return f'"{column}"'


def _split(count: int, per_row: int = 4) -> list[int]:
    """Tiles per row, as even as possible, at most ``per_row``."""
    rows = -(-count // per_row)
    base, extra = divmod(count, rows)
    return [base + (1 if i < extra else 0) for i in range(rows)]


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


def ecommerce(
    dataset_id: str,
    *,
    date: str = "date",
    order_id: str = "commande_id",
    amount: str = "montant",
    customer_id: str | None = None,
    product_cost: str | None = None,
    channel: str | None = None,
    ad_spend: str | None = None,
    new_customer: str | None = None,
    status: str | None = None,
    roas_threshold: int = ROAS_THRESHOLD,
    default_period: str = "26w",
) -> dict:
    """Build the « E-commerce » spec for a dataset of orders."""
    if not dataset_id:
        raise ValueError("dataset_id is required.")
    d = _check_ident("date", date)
    oid = _check_ident("order_id", order_id)
    am = _check_ident("amount", amount)
    cu = _check_ident("customer_id", customer_id) if customer_id else None
    pc = _check_ident("product_cost", product_cost) if product_cost else None
    ch = _check_ident("channel", channel) if channel else None
    ad = _check_ident("ad_spend", ad_spend) if ad_spend else None
    nc = _check_ident("new_customer", new_customer) if new_customer else None
    st = _check_ident("status", status) if status else None

    bucket = "periode_semaine"
    ORD, A = _q(oid), _q(am)
    new_flag = "client_nouveau"

    # ---- transforms --------------------------------------------------
    transforms: list[Any] = []
    if st:
        S = f"LOWER(TRIM(CAST({_q(st)} AS VARCHAR)))"
        excluded = " OR ".join(f"{S} LIKE '{prefix}%'" for prefix in _EXCLUDED_STATUS)
        transforms.append(FilterStep(where=f"{_q(st)} IS NULL OR NOT ({excluded})"))
    transforms.append(DeriveStep(column=bucket, expr=f"date_trunc('week', CAST({_q(d)} AS TIMESTAMP))",
                                 provenance=Provenance.REAL))
    if nc:
        flags = ", ".join(f"'{f}'" for f in _TRUE_FLAGS)
        transforms.append(DeriveStep(
            column=new_flag,
            expr=(f"CASE WHEN {_q(nc)} IS NULL THEN NULL "
                  f"WHEN LOWER(TRIM(CAST({_q(nc)} AS VARCHAR))) IN ({flags}) THEN 1 ELSE 0 END"),
        ))

    # ---- fields ------------------------------------------------------
    fields = [
        FieldSpec(name=d, label="Date de commande", type="date", role="date"),
        FieldSpec(name=oid, label="Commande", role="dimension"),
        FieldSpec(name=am, label="Montant", type="number", role="measure", format="compact"),
    ]
    if cu:
        fields.append(FieldSpec(name=cu, label="Client", role="dimension"))
    if pc:
        fields.append(FieldSpec(name=pc, label="Coût des produits", type="number", role="measure", format="compact"))
    if ch:
        fields.append(FieldSpec(name=ch, label="Canal", role="dimension"))
    if ad:
        fields.append(FieldSpec(name=ad, label="Dépense publicitaire", type="number", role="measure",
                                format="compact"))
    if nc:
        fields.append(FieldSpec(name=new_flag, label="Nouveau client", type="number", role="measure",
                                format="integer"))
    fields.append(FieldSpec(name=bucket, label="Semaine", type="date", role="dimension"))

    # ---- measures ----------------------------------------------------
    revenue = f"SUM({A})"
    orders = f"COUNT(DISTINCT {ORD})"
    measures = [
        MeasureSpec(name="ca", label="Chiffre d'affaires", expr=revenue, format="compact", unit="€",
                    description="Somme des montants des commandes (annulées et remboursées exclues)."),
        MeasureSpec(name="commandes", label="Commandes", expr=orders, format="integer", unit="commandes",
                    description="Nombre de commandes distinctes."),
        MeasureSpec(name="panier_moyen", label="Panier moyen", expr=f"{revenue} / NULLIF({orders}, 0)",
                    format="decimal1", unit="€", description="Chiffre d'affaires ÷ nombre de commandes."),
    ]
    if ad:
        spend = f"SUM({_q(ad)})"
        measures += [
            MeasureSpec(name="depense_pub", label="Dépense publicitaire", expr=spend, format="compact", unit="€"),
            MeasureSpec(name="roas", label="ROAS", expr=f"{revenue} / NULLIF({spend}, 0)", format="decimal1",
                        thresholds=ThresholdSpec(direction="higher_is_better", good=roas_threshold,
                                                 warn=roas_threshold),
                        description="Return on ad spend : chiffre d'affaires ÷ dépense publicitaire."),
        ]
    if pc:
        margin = f"{revenue} - SUM({_q(pc)})" + (f" - COALESCE(SUM({_q(ad)}), 0)" if ad else "")
        measures += [
            MeasureSpec(name="marge_contribution", label="Marge de contribution", expr=margin, format="compact",
                        unit="€",
                        description="Chiffre d'affaires - coût des produits" + (" - dépense publicitaire." if ad else ".")),
            MeasureSpec(name="taux_marge", label="Taux de marge de contribution",
                        expr=f"({margin}) / NULLIF({revenue}, 0)", format="percent",
                        description="Marge de contribution ÷ chiffre d'affaires."),
        ]
    if nc:
        if cu:
            new_customers = f"COUNT(DISTINCT CASE WHEN {new_flag} = 1 THEN {_q(cu)} END)"
        else:
            new_customers = f"SUM(CASE WHEN {new_flag} = 1 THEN 1 ELSE 0 END)"
        measures.append(MeasureSpec(name="nouveaux_clients", label="Nouveaux clients", expr=new_customers,
                                    format="integer", unit="clients"))
        if ad:
            measures.append(MeasureSpec(
                name="cac", label="Coût d'acquisition client", expr=f"SUM({_q(ad)}) / NULLIF({new_customers}, 0)",
                format="decimal1", unit="€", description="Dépense publicitaire ÷ nouveaux clients."))
    if cu:
        C = _q(cu)
        buyers = f"(SELECT COUNT(DISTINCT {C}) FROM frame WHERE {C} IS NOT NULL AND {ORD} IS NOT NULL)"
        repeaters = (f"(SELECT COUNT(*) FROM (SELECT {C} FROM frame WHERE {C} IS NOT NULL AND {ORD} IS NOT NULL "
                     f"GROUP BY {C} HAVING COUNT(DISTINCT {ORD}) >= 2))")
        measures += [
            MeasureSpec(name="clients_actifs", label="Clients actifs", expr=buyers, format="integer",
                        unit="clients", description="Clients ayant passé au moins une commande sur la période."),
            MeasureSpec(name="clients_recurrents", label="Clients récurrents",
                        expr=f"{repeaters} / NULLIF({buyers}, 0)", format="percent",
                        description="Clients ayant passé au moins deux commandes sur la période ÷ clients actifs."),
        ]
    source = SourceSpec(
        name="commandes",
        label="Commandes",
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
    filters = [FilterSpec(id="periode", label="Période", field=d, type="relative_period", default=default_period,
                          presets=presets)]
    if ch:
        filters.append(FilterSpec(id="canal", label="Canal", field=ch, type="select", default=None))

    # ---- visuals -----------------------------------------------------
    def kpi(title: str, measure: str, sparkline: bool = True, **kw: Any) -> VisualSpec:
        return VisualSpec(type="kpi", title=title, source="commandes", x=bucket,
                          kpi=KpiSpec(measure=measure, sparkline=measure if sparkline else None, **kw))

    scope = "{{filters.canal}}, d" if ch else "D"
    sentence = (
        f"{scope}u {{{{period.from}}}} au {{{{period.to}}}} : {{{{ca|compact}}}} € de chiffre d'affaires sur "
        "{{commandes|integer}} commandes, panier moyen de {{panier_moyen|decimal1}} €."
    )
    if pc:
        sentence += " Marge de contribution de {{marge_contribution|compact}} € ({{taux_marge|percent}})."
    if ad:
        sentence += " ROAS de {{roas|decimal1}} pour {{depense_pub|compact}} € de publicité."
    synthese = VisualSpec(type="text", source="commandes", text=sentence)

    kpis = [
        kpi("Chiffre d'affaires", "ca"),
        kpi("Commandes", "commandes"),
        kpi("Panier moyen", "panier_moyen"),
    ]
    if pc:
        kpis.append(kpi("Marge de contribution", "marge_contribution",
                        secondary="{{taux_marge|percent}} du chiffre d'affaires"))
    if ad:
        kpis.append(kpi("ROAS", "roas", target_label=f"alerte sous {roas_threshold}",
                        secondary="{{depense_pub|compact}} € de publicité"))
        if nc:
            kpis.append(kpi("Coût d'acquisition client", "cac",
                            secondary="{{nouveaux_clients|integer}} nouveaux clients"))
    if cu:
        kpis.append(kpi("Clients récurrents", "clients_recurrents", sparkline=False,
                        secondary="{{clients_actifs|integer}} clients actifs"))

    evolution = VisualSpec(
        type="combo",
        title="CA et commandes par semaine",
        subtitle="Chiffre d'affaires en barres, nombre de commandes en ligne.",
        source="commandes",
        x=bucket,
        series=[
            SeriesSpec(key="ca", label="Chiffre d'affaires", mark="bar", color=BLUE),
            SeriesSpec(key="commandes", label="Commandes", mark="line", color=BROWN, axis="right"),
        ],
        sort="x_asc",
    )
    charts: list[VisualSpec] = []
    if ad:
        charts.append(VisualSpec(
            type="combo",
            title="ROAS par semaine",
            subtitle=f"Rouge sous le seuil d'alerte de {roas_threshold}.",
            source="commandes",
            x=bucket,
            series=[SeriesSpec(key="roas", label="ROAS", mark="bar", use_thresholds=True)],
            reference_lines=[ReferenceLine(value=roas_threshold, label=f"Seuil {roas_threshold}", color="bad")],
            sort="x_asc",
        ))
    if ch:
        charts.append(VisualSpec(
            type="hbar",
            title="CA par canal",
            subtitle="Cliquer pour filtrer le tableau de bord.",
            source="commandes",
            x=ch,
            series=[SeriesSpec(key="ca", label="Chiffre d'affaires", mark="bar", color=BLUE)],
            sort="value_desc",
            click_filters={"canal": ch},
            ignore_filters=["canal"],
        ))
    if cu:
        charts.append(VisualSpec(
            type="hbar",
            title="Top clients",
            subtitle="Les 10 clients au plus fort chiffre d'affaires sur la période.",
            source="commandes",
            x=cu,
            series=[SeriesSpec(key="ca", label="Chiffre d'affaires", mark="bar", color=BROWN)],
            sort="value_desc",
            top_n=10,
        ))

    definitions = (
        "**CA** : somme des montants, commandes annulées et remboursées exclues. **Panier moyen** : CA ÷ commandes."
    )
    if pc:
        definitions += (" **Marge de contribution** : CA - coût des produits"
                        + (" - dépense publicitaire." if ad else "."))
    if ad:
        definitions += " **ROAS** : CA ÷ dépense publicitaire."
        if nc:
            definitions += " **Coût d'acquisition** : dépense publicitaire ÷ nouveaux clients."
    if cu:
        definitions += " **Clients récurrents** : clients ayant au moins deux commandes sur la période."
    notes = [VisualSpec(type="note", title="Définitions", badges=[Provenance.REAL], text=definitions)]
    reading = ("La période se règle en haut du tableau de bord. Chaque ratio est calculé sur les sommes, jamais "
               "comme une moyenne de ratios.")
    if ad:
        reading = (f"Le ROAS passe en alerte sous {roas_threshold} (seuil réglable à la création). Les dépenses "
                   "publicitaires doivent figurer une seule fois dans le fichier. " + reading)
    notes.append(VisualSpec(type="note", title="Seuils et lecture", text=reading))

    # ---- grid --------------------------------------------------------
    grid: list[tuple[VisualSpec, dict[str, int]]] = [(synthese, dict(row=0, col=0, width=12, height=2))]
    row, index = 2, 0
    for count in _split(len(kpis)):
        col = 0
        for width in _spread(count):
            grid.append((kpis[index], dict(row=row, col=col, width=width, height=3)))
            col += width
            index += 1
        row += 3
    grid.append((evolution, dict(row=row, col=0, width=12, height=6)))
    placed, row = _flow(charts, row + 6, 6)
    grid += placed
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
    return ecommerce(dataset_id, **mapping, **_check_params(params))
