"""
bi/spec/templates_cash.py
-------------------------
Spec v2 template « Prévision de trésorerie » : solde de trésorerie projeté sur
l'horizon en trois scénarios (pessimiste / central / optimiste), à partir d'une
ligne par période (semaine ou mois), et par compte si la colonne existe, avec
le solde réel (optionnel), les encaissements et les décaissements.

Règle (étape ``cash_forecast``) : solde(t) = solde(t-1) + encaissements(t) -
décaissements(t), les flux étant prévus par le moteur th2forecast (repli : moyenne
des 4 dernières périodes, avec un avertissement). Pessimiste : encaissements ×
(1 - a), décaissements × (1 + b) ; optimiste : l'inverse.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from apowerb.bi.spec.models import (
    IDENT,
    CashForecastStep,
    ColumnSpec,
    FieldSpec,
    FilterSpec,
    FilterStep,
    ForecastStep,
    KpiSpec,
    MeasureSpec,
    Provenance,
    SeriesSpec,
    SourceSpec,
    VisualSpec,
)
from apowerb.bi.spec.templates_common import DEFAULT_LANG, pick, translator

_IDENT_RE = re.compile(IDENT)

BROWN = "#9a6b3f"
BLUE = "#3b6fd8"

DESCRIPTOR: dict[str, Any] = {
    "id": "cash_forecast",
    "label": {"fr": "Prévision de trésorerie", "en": "Cash forecast"},
    "description": {
        "fr": (
            "Prévision de trésorerie à partir d'une ligne par période (semaine ou mois, et par compte si besoin) "
            "avec le solde, les encaissements et les décaissements : solde projeté sur l'horizon en trois "
            "scénarios (pessimiste, central, optimiste), seuil de trésorerie minimale, première période sous "
            "le seuil et nombre de semaines de trésorerie. Les flux sont prévus par le moteur de prévision."
        ),
        "en": (
            "Cash forecast from one row per period (week or month, and per account if needed) "
            "with the balance, cash in and cash out: balance projected over the horizon in three "
            "scenarios (pessimistic, central, optimistic), minimum cash threshold, first period below "
            "the threshold and weeks of cash on hand. Flows are predicted by the forecast engine."
        ),
    },
    "version": 1,
    "columns": [
        {"key": "date", "label": {"fr": "Date de la période", "en": "Period date"}, "type": "date",
         "required": True, "default": "semaine"},
        {"key": "inflow", "label": {"fr": "Encaissements", "en": "Cash in"}, "type": "number", "required": True,
         "default": "encaissements"},
        {"key": "outflow", "label": {"fr": "Décaissements", "en": "Cash out"}, "type": "number", "required": True,
         "default": "decaissements"},
        {"key": "balance", "label": {"fr": "Solde réel", "en": "Actual balance"}, "type": "number",
         "required": False, "default": None},
        {"key": "account", "label": {"fr": "Compte", "en": "Account"}, "type": "string", "required": False,
         "default": None},
        {"key": "threshold", "label": {"fr": "Seuil de trésorerie minimale", "en": "Minimum cash threshold"},
         "type": "number", "required": False, "default": None},
    ],
    "params": [
        {"key": "horizon", "label": {"fr": "Horizon de prévision (périodes)", "en": "Forecast horizon (periods)"},
         "type": "integer", "default": 13, "min": 1, "max": 26},
        {"key": "inflow_shock_pct",
         "label": {"fr": "Écart sur les encaissements, a (%)", "en": "Deviation on cash in, a (%)"},
         "type": "integer", "default": 10, "min": 0, "max": 50},
        {"key": "outflow_shock_pct",
         "label": {"fr": "Écart sur les décaissements, b (%)", "en": "Deviation on cash out, b (%)"},
         "type": "integer", "default": 10, "min": 0, "max": 50},
        {"key": "min_cash",
         "label": {"fr": "Seuil de trésorerie minimale (par compte)", "en": "Minimum cash threshold (per account)"},
         "type": "integer", "default": 0, "min": -1_000_000_000, "max": 1_000_000_000},
        {"key": "opening_balance",
         "label": {"fr": "Solde d'ouverture (sans colonne de solde)",
                   "en": "Opening balance (without a balance column)"},
         "type": "integer", "default": 0, "min": -1_000_000_000, "max": 1_000_000_000},
        {"key": "monthly",
         "label": {"fr": "Données mensuelles (0 = hebdomadaires, 1 = mensuelles)",
                   "en": "Monthly data (0 = weekly, 1 = monthly)"},
         "type": "integer", "default": 0, "min": 0, "max": 1},
    ],
}
_REQUIRED = [c["key"] for c in DESCRIPTOR["columns"] if c["required"]]
_OPTIONAL = [c["key"] for c in DESCRIPTOR["columns"] if not c["required"]]


def _check_ident(label: str, value: str) -> str:
    if not _IDENT_RE.match(value or ""):
        raise ValueError(
            f"{label}='{value}' is not a valid column name: use lowercase letters, "
            "digits and underscores (start with a letter), e.g. 'encaissements'."
        )
    return value


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True)


def _fr(value: float, digits: int = 1) -> str:
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return text.replace(".", ",")


def cash_forecast(
    dataset_id: str,
    *,
    date: str = "semaine",
    inflow: str = "encaissements",
    outflow: str = "decaissements",
    balance: str | None = None,
    account: str | None = None,
    threshold_column: str | None = None,
    horizon: int = 13,
    inflow_shock: float = 0.10,
    outflow_shock: float = 0.10,
    min_cash: float = 0.0,
    opening_balance: float = 0.0,
    monthly: bool = False,
    keep_history: int | None = None,
    default_period: str | None = None,
    lang: str = DEFAULT_LANG,
) -> dict:
    """Build the « Prévision de trésorerie » spec for a dataset of cash flows, in ``lang``."""
    T = translator(lang)
    if not dataset_id:
        raise ValueError("dataset_id is required.")
    d = _check_ident("date", date)
    enc = _check_ident("inflow", inflow)
    dec = _check_ident("outflow", outflow)
    bal = _check_ident("balance", balance) if balance else None
    acc = _check_ident("account", account) if account else None
    thr = _check_ident("threshold_column", threshold_column) if threshold_column else None
    if not 0 <= inflow_shock <= 1 or not 0 <= outflow_shock <= 1:
        raise ValueError("inflow_shock and outflow_shock must be between 0 and 1.")
    if not 1 <= horizon <= 104:
        raise ValueError("horizon must be between 1 and 104.")

    unit_one, unit_many = (T("mois", "month"), T("mois", "months")) if monthly else (
        T("semaine", "week"), T("semaines", "weeks"))
    unit_short = T("mois", "mo.") if monthly else T("sem.", "wk")
    frequency = "month" if monthly else None  # weekly: inferred by the engine
    history = keep_history if keep_history is not None else (12 if monthly else 26)
    period = default_period or ("24m" if monthly else "52w")
    presets = ["12m", "24m", "36m", "all"] if monthly else ["26w", "52w", "104w", "all"]
    if period not in presets:
        presets.insert(0, period)
    by = [acc] if acc else []
    real, fc, sim = Provenance.REAL, Provenance.FORECAST, Provenance.SIMULATED
    a_txt = T(f"{_fr(inflow_shock * 100)} %", f"{_fr(inflow_shock * 100).replace(',', '.')}%")
    b_txt = T(f"{_fr(outflow_shock * 100)} %", f"{_fr(outflow_shock * 100).replace(',', '.')}%")

    # ---- history: the dataset as imported ----------------------------
    hist_fields = [FieldSpec(name=d, label=T("Période", "Period"), type="date", role="date")]
    if acc:
        hist_fields.append(FieldSpec(name=acc, label=T("Compte", "Account"), role="dimension"))
    hist_fields += [
        FieldSpec(name=enc, label=T("Encaissements", "Cash in"), type="number", role="measure", provenance=real,
                  format="compact"),
        FieldSpec(name=dec, label=T("Décaissements", "Cash out"), type="number", role="measure", provenance=real,
                  format="compact"),
    ]
    if bal:
        hist_fields.append(FieldSpec(name=bal, label=T("Solde", "Balance"), type="number", role="measure", provenance=real,
                                     format="compact"))
    if thr:
        hist_fields.append(FieldSpec(name=thr, label=T("Seuil de trésorerie minimale", "Minimum cash threshold"),
                                     type="number", role="measure", provenance=real, format="compact"))
    historique = SourceSpec(name="tresorerie", label=T("Historique de trésorerie", "Cash history"), kind="dataset",
                            dataset_id=dataset_id, fields=hist_fields)

    # ---- plan: engine forecast of both flows, then the scenarios -------
    plan_transforms: list[Any] = [
        ForecastStep(date=d, value=enc, by=by, horizon=horizon, output="encaissements_prevus", frequency=frequency),
        ForecastStep(date=d, value=dec, by=by, horizon=horizon, output="decaissements_prevus", frequency=frequency),
        CashForecastStep(
            date=d, by=by, balance=bal, inflow=enc, outflow=dec,
            forecast_inflow="encaissements_prevus", forecast_outflow="decaissements_prevus",
            inflow_shock=inflow_shock, outflow_shock=outflow_shock, min_cash=min_cash, min_cash_column=thr,
            opening_balance=opening_balance, horizon=horizon, keep_history=history,
        ),
    ]
    now, future = "semaine_plan = 0", "semaine_plan >= 1"
    end = f"semaine_plan = {horizon}"
    solde_now = "SUM(solde_projete_central) FILTER (WHERE " + now + ")"
    periods = f"COUNT(DISTINCT {d}) FILTER (WHERE {future})"
    money = dict(type="number", role="measure", format="compact")
    plan_fields = [
        FieldSpec(name="encaissements_prevus", label=T("Encaissements prévus (moteur)", "Forecast cash in (engine)"),
                  provenance=fc, **money),
        FieldSpec(name="decaissements_prevus", label=T("Décaissements prévus (moteur)", "Forecast cash out (engine)"),
                  provenance=fc, **money),
        FieldSpec(name="solde_projete_central", label=T("Solde (réel puis central)", "Balance (actual then central)"),
                  provenance=fc, **money),
        FieldSpec(name="solde_projete_pessimiste", label=T("Solde pessimiste", "Pessimistic balance"),
                  provenance=sim, **money),
        FieldSpec(name="solde_projete_optimiste", label=T("Solde optimiste", "Optimistic balance"),
                  provenance=sim, **money),
        FieldSpec(name="encaissements_projetes", label=T("Encaissements projetés", "Projected cash in"),
                  provenance=fc, **money),
        FieldSpec(name="decaissements_projetes", label=T("Décaissements projetés", "Projected cash out"),
                  provenance=fc, **money),
        FieldSpec(name="flux_net_projete", label=T("Flux net projeté", "Projected net flow"), provenance=fc, **money),
        FieldSpec(name="seuil_tresorerie", label=T("Seuil de trésorerie minimale", "Minimum cash threshold"),
                  provenance=real if thr else sim, **money),
        FieldSpec(name="sous_seuil_central", label=T("Sous le seuil (central)", "Below threshold (central)"),
                  type="number", role="measure", provenance=fc, format="integer"),
        FieldSpec(name="sous_seuil_pessimiste",
                  label=T("Sous le seuil (pessimiste)", "Below threshold (pessimistic)"), type="number",
                  role="measure", provenance=sim, format="integer"),
        FieldSpec(name="sous_seuil_optimiste",
                  label=T("Sous le seuil (optimiste)", "Below threshold (optimistic)"), type="number",
                  role="measure", provenance=sim, format="integer"),
        FieldSpec(name="semaine_plan", label=T("Période du plan", "Plan period"), type="number", role="dimension",
                  format="integer"),
    ]
    runway_fmt = dict(format="decimal1", unit=T("mois", "months")) if monthly else dict(format="weeks")
    plan_measures = [
        MeasureSpec(name="solde_actuel", label=T("Solde actuel", "Current balance"), expr=solde_now, format="compact",
                    unit="€", description=T("Solde de la dernière période réelle.", "Balance of the last actual period.")),
        MeasureSpec(name="solde_horizon_central", label=T("Solde à l'horizon (central)", "Balance at horizon (central)"),
                    expr=f"SUM(solde_projete_central) FILTER (WHERE {end})", format="compact", unit="€"),
        MeasureSpec(name="solde_horizon_pessimiste", label=T("Solde à l'horizon (pessimiste)", "Balance at horizon (pessimistic)"),
                    expr=f"SUM(solde_projete_pessimiste) FILTER (WHERE {end})", format="compact", unit="€"),
        MeasureSpec(name="solde_horizon_optimiste", label=T("Solde à l'horizon (optimiste)", "Balance at horizon (optimistic)"),
                    expr=f"SUM(solde_projete_optimiste) FILTER (WHERE {end})", format="compact", unit="€"),
        MeasureSpec(name="premiere_sous_seuil", label=T("Première période sous le seuil", "First period below threshold"),
                    expr="MIN(semaine_plan) FILTER (WHERE sous_seuil_central = 1)", format="integer", unit=unit_short,
                    description=T("Période du plan (1 = prochaine période) où un compte passe sous son seuil, "
                                  "scénario central ; vide si jamais.",
                                  "Plan period (1 = next period) in which an account falls below its threshold, "
                                  "central scenario; empty if never.")),
        MeasureSpec(name="premiere_sous_seuil_pessimiste", label=T("Première période sous le seuil (pessimiste)",
                                                                     "First period below threshold (pessimistic)"),
                    expr="MIN(semaine_plan) FILTER (WHERE sous_seuil_pessimiste = 1)", format="integer",
                    unit=unit_short),
        MeasureSpec(name="premiere_sous_seuil_optimiste", label=T("Première période sous le seuil (optimiste)",
                                                                   "First period below threshold (optimistic)"),
                    expr="MIN(semaine_plan) FILTER (WHERE sous_seuil_optimiste = 1)", format="integer",
                    unit=unit_short),
        MeasureSpec(name="semaines_tresorerie", label=T(f"{unit_many.capitalize()} de trésorerie", f"{unit_many.capitalize()} of cash"),
                    expr=(f"{solde_now} / NULLIF(SUM(decaissements_projetes) FILTER (WHERE {future}) "
                          f"/ NULLIF({periods}, 0), 0)"),
                    description=T("Solde actuel ÷ décaissements moyens par période de l'horizon.",
                                  "Current balance ÷ average cash out per period over the horizon."),
                    **runway_fmt),
        MeasureSpec(name="encaissements_horizon", label=T("Encaissements prévus sur l'horizon", "Forecast cash in over the horizon"),
                    expr=f"SUM(encaissements_projetes) FILTER (WHERE {future})", format="compact", unit="€"),
        MeasureSpec(name="decaissements_horizon", label=T("Décaissements prévus sur l'horizon", "Forecast cash out over the horizon"),
                    expr=f"SUM(decaissements_projetes) FILTER (WHERE {future})", format="compact", unit="€"),
        MeasureSpec(name="solde_central_plan", label=T("Central", "Central"), expr="SUM(solde_projete_central)",
                    format="compact", unit="€"),
        MeasureSpec(name="solde_pessimiste_plan", label=T("Pessimiste", "Pessimistic"), expr="SUM(solde_projete_pessimiste)",
                    format="compact", unit="€"),
        MeasureSpec(name="solde_optimiste_plan", label=T("Optimiste", "Optimistic"), expr="SUM(solde_projete_optimiste)",
                    format="compact", unit="€"),
        MeasureSpec(name="seuil_total", label=T("Seuil de trésorerie minimale", "Minimum cash threshold"), expr="SUM(seuil_tresorerie)",
                    format="compact", unit="€"),
    ]
    plan = SourceSpec(name="plan", label=T("Prévision de trésorerie", "Cash forecast"), kind="source", parent="tresorerie",
                      transforms=plan_transforms, fields=plan_fields, measures=plan_measures)
    # the horizon periods only, so the flows chart does not show the kept history as empty periods
    horizon_src = SourceSpec(
        name="horizon", label=T(f"Flux sur {horizon} {unit_many}", f"Flows over {horizon} {unit_many}"), kind="source", parent="plan",
        transforms=[FilterStep(where="semaine_plan >= 1")],
        measures=[
            MeasureSpec(name="encaissements_prevus_m", label=T("Encaissements", "Cash in"),
                        expr="COALESCE(SUM(encaissements_projetes), 0)", format="compact", unit="€"),
            MeasureSpec(name="decaissements_prevus_m", label=T("Décaissements", "Cash out"),
                        expr="COALESCE(SUM(decaissements_projetes), 0)", format="compact", unit="€"),
            MeasureSpec(name="flux_net_m", label=T("Flux net", "Net flow"), expr="COALESCE(SUM(flux_net_projete), 0)",
                        format="compact", unit="€"),
        ],
    )

    filters: list[FilterSpec] = []
    if acc:
        filters.append(FilterSpec(id="compte", label=T("Compte", "Account"), field=acc, type="select", default=None,
                                  applies_to=None))
    filters.append(FilterSpec(id="periode", label=T("Période", "Period"), field=d, type="relative_period", default=period,
                              applies_to=["tresorerie"], presets=presets))

    # ---- visuals -----------------------------------------------------
    def kpi(title: str, measure: str, **kw: Any) -> VisualSpec:
        return VisualSpec(type="kpi", title=title, source="plan", kpi=KpiSpec(measure=measure, **kw))

    synthese = VisualSpec(
        type="text",
        source="plan",
        text=T(
            f"Solde actuel de {{{{solde_actuel|compact}}}} €. À {horizon} {unit_many} : "
            "{{solde_horizon_central|compact}} € dans le scénario central, "
            "{{solde_horizon_pessimiste|compact}} € dans le pessimiste et {{solde_horizon_optimiste|compact}} € "
            "dans l'optimiste. Trésorerie couvrant {{semaines_tresorerie|decimal1}} "
            f"{unit_many} de décaissements.",
            f"Current balance of {{{{solde_actuel|compact}}}} €. In {horizon} {unit_many}: "
            "{{solde_horizon_central|compact}} € in the central scenario, "
            "{{solde_horizon_pessimiste|compact}} € in the pessimistic one and {{solde_horizon_optimiste|compact}} € "
            "in the optimistic one. Cash covering {{semaines_tresorerie|decimal1}} "
            f"{unit_many} of cash out.",
        ),
    )
    kpis = [
        kpi(T("Solde actuel", "Current balance"), "solde_actuel",
            secondary=T("Dernière période réelle", "Last actual period")),
        kpi(T(f"Solde à {horizon} {unit_many}", f"Balance in {horizon} {unit_many}"), "solde_horizon_central",
            secondary=T("Pessimiste : {{solde_horizon_pessimiste|compact}} € · optimiste : "
                        "{{solde_horizon_optimiste|compact}} €",
                        "Pessimistic: {{solde_horizon_pessimiste|compact}} € · optimistic: "
                        "{{solde_horizon_optimiste|compact}} €")),
        kpi(T("Première période sous le seuil", "First period below threshold"), "premiere_sous_seuil",
            secondary=T("Pessimiste : {{premiere_sous_seuil_pessimiste|integer}} · optimiste : "
                        "{{premiere_sous_seuil_optimiste|integer}} (— : jamais)",
                        "Pessimistic: {{premiere_sous_seuil_pessimiste|integer}} · optimistic: "
                        "{{premiere_sous_seuil_optimiste|integer}} (— : never)")),
        kpi(T(f"{unit_many.capitalize()} de trésorerie", f"{unit_many.capitalize()} of cash"), "semaines_tresorerie",
            secondary=T("Solde ÷ décaissements moyens prévus", "Balance ÷ average forecast cash out")),
    ]
    combo = VisualSpec(
        type="combo",
        title=T("Solde de trésorerie : trois scénarios", "Cash balance: three scenarios"),
        subtitle=T(f"Solde réel jusqu'à aujourd'hui, puis projeté sur {horizon} {unit_many}. Pessimiste : "
                   f"encaissements -{a_txt}, décaissements +{b_txt}. Optimiste : l'inverse.",
                   f"Actual balance up to today, then projected over {horizon} {unit_many}. Pessimistic: "
                   f"cash in -{a_txt}, cash out +{b_txt}. Optimistic: the reverse."),
        source="plan",
        x=d,
        series=[
            SeriesSpec(key="solde_central_plan", label=T("Central", "Central"), mark="line", color=BLUE),
            SeriesSpec(key="solde_pessimiste_plan", label=T("Pessimiste", "Pessimistic"), mark="line", color="bad",
                       dash=True),
            SeriesSpec(key="solde_optimiste_plan", label=T("Optimiste", "Optimistic"), mark="line", color="good",
                       dash=True),
            SeriesSpec(key="seuil_total", label=T("Seuil de trésorerie minimale", "Minimum cash threshold"),
                       mark="line", color="muted", dash=True),
        ],
        sort="x_asc",
    )
    flux = VisualSpec(
        type="combo",
        title=T(f"Flux prévus, {unit_one} par {unit_one}", f"Forecast flows, {unit_one} by {unit_one}"),
        subtitle=T("Encaissements et décaissements prévus (scénario central) et flux net.",
                   "Forecast cash in and cash out (central scenario) and net flow."),
        source="horizon",
        x=d,
        series=[
            SeriesSpec(key="encaissements_prevus_m", label=T("Encaissements", "Cash in"), mark="bar", color=BLUE),
            SeriesSpec(key="decaissements_prevus_m", label=T("Décaissements", "Cash out"), mark="bar", color=BROWN),
            SeriesSpec(key="flux_net_m", label=T("Flux net", "Net flow"), mark="line", color="muted"),
        ],
        sort="x_asc",
    )
    table = None
    if acc:
        table = VisualSpec(
            type="table",
            title=T("Trésorerie par compte", "Cash by account"),
            subtitle=T("Cliquer sur un compte pour filtrer le tableau de bord.",
                       "Click an account to filter the dashboard."),
            source="plan",
            x=acc,
            columns=[
                ColumnSpec(key=acc, label=T("Compte", "Account")),
                ColumnSpec(key="solde_actuel", label=T("Solde actuel", "Current balance")),
                ColumnSpec(key="solde_horizon_central", label=T("Central à l'horizon", "Central at horizon")),
                ColumnSpec(key="solde_horizon_pessimiste",
                           label=T("Pessimiste à l'horizon", "Pessimistic at horizon")),
                ColumnSpec(key="solde_horizon_optimiste",
                           label=T("Optimiste à l'horizon", "Optimistic at horizon")),
                ColumnSpec(key="premiere_sous_seuil",
                           label=T("Première période sous le seuil", "First period below threshold")),
            ],
            total_row=True,
            click_filters={"compte": acc},
            ignore_filters=["compte"],
            highlight_filter="compte",
        )
    seuil_src = T(f"la colonne `{thr}`", f"the `{thr}` column") if thr else f"{_fr(min_cash, 0)} €"
    solde_src = (T(f"le solde (`{bal}`)", f"the balance (`{bal}`)") if bal else
                 T(f"un solde reconstitué à partir d'un solde d'ouverture de {_fr(opening_balance, 0)} € et des flux",
                   f"a balance rebuilt from an opening balance of {_fr(opening_balance, 0)} € and the flows"))
    notes = [
        VisualSpec(
            type="note",
            title=T("Origine des données", "Data origin"),
            badges=[real, fc, sim],
            text=T(
                f"Les encaissements (`{enc}`), les décaissements (`{dec}`) et {solde_src} sont les données réelles "
                "du fichier. Les flux à venir sont prévus par le moteur de prévision ; les scénarios pessimiste et "
                "optimiste sont des hypothèses.",
                f"Cash in (`{enc}`), cash out (`{dec}`) and {solde_src} are the actual data "
                "of the file. Future flows are predicted by the forecast engine; the pessimistic and "
                "optimistic scenarios are assumptions.",
            ),
        ),
        VisualSpec(
            type="note",
            title=T("Règle de calcul et scénarios", "Calculation rule and scenarios"),
            badges=[fc, sim],
            text=T(
                f"Solde = solde de la période précédente + encaissements - décaissements. Central : flux "
                f"prévus. Pessimiste : encaissements × (1 - {a_txt}) et décaissements × (1 + {b_txt}). Optimiste : "
                f"encaissements × (1 + {a_txt}) et décaissements × (1 - {b_txt}). Seuil de trésorerie minimale : "
                f"{seuil_src}, par compte ; une période est « sous le seuil » si le solde lui est strictement "
                "inférieur. Si le moteur ne répond pas, les flux valent la moyenne des 4 dernières périodes et un "
                "avertissement l'indique.",
                f"Balance = balance of the previous period + cash in - cash out. Central: forecast flows. "
                f"Pessimistic: cash in × (1 - {a_txt}) and cash out × (1 + {b_txt}). Optimistic: "
                f"cash in × (1 + {a_txt}) and cash out × (1 - {b_txt}). Minimum cash threshold: "
                f"{seuil_src}, per account; a period is “below the threshold” if the balance is strictly "
                "lower. If the engine does not respond, flows equal the average of the last 4 periods and a "
                "warning says so.",
            ),
        ),
        VisualSpec(
            type="note",
            title=T("Limites", "Limitations"),
            text=T(
                "Une ligne par période (et par compte). Les encaissements et décaissements sont des montants "
                "positifs. Les échéances connues (loyers, emprunts, salaires, factures à venir) ne sont pas "
                "intégrées : la prévision extrapole l'historique. Sans filtre de compte, le seuil affiché est la "
                "somme des seuils des comptes.",
                "One row per period (and per account). Cash in and cash out are positive "
                "amounts. Known due dates (rent, loans, salaries, upcoming invoices) are not "
                "included: the forecast extrapolates the history. Without an account filter, the threshold shown is "
                "the sum of the account thresholds.",
            ),
        ),
    ]

    grid: list[tuple[VisualSpec, dict[str, int]]] = [(synthese, dict(row=0, col=0, width=12, height=2))]
    for i, k in enumerate(kpis):
        grid.append((k, dict(row=2, col=3 * i, width=3, height=3)))
    grid.append((combo, dict(row=5, col=0, width=12, height=6)))
    row = 11
    if table is not None:
        grid.append((table, dict(row=row, col=0, width=12, height=5)))
        row += 5
    grid.append((flux, dict(row=row, col=0, width=12, height=5)))
    row += 5
    for i, n in enumerate(notes):
        grid.append((n, dict(row=row, col=4 * i, width=4, height=4)))

    return {
        "sources": [_dump(historique), _dump(plan), _dump(horizon_src)],
        "filters": [_dump(f) for f in filters],
        "components": [{"visual": _dump(v), "position": pos} for v, pos in grid],
    }


def build(dataset_id: str, columns: dict, params: dict, lang: str = DEFAULT_LANG) -> dict:
    """Catalog entry point: column mapping (blank optional → None) and parameters to the template, in ``lang``."""
    T = translator(lang)
    mapping: dict[str, Any] = {key: columns.get(key) or "" for key in _REQUIRED}
    mapping.update({key: columns.get(key) or None for key in _OPTIONAL})
    missing = [pick(c["label"], lang) for c in DESCRIPTOR["columns"] if c["required"] and not mapping[c["key"]]]
    if missing:
        raise ValueError(
            (T("Colonnes obligatoires manquantes : ", "Missing required columns: ") if len(missing) > 1
             else T("Colonne obligatoire manquante : ", "Missing required column: ")) + ", ".join(missing) + "."
        )
    defaults = {p["key"]: p["default"] for p in DESCRIPTOR["params"]}
    values = {k: (params.get(k) if params.get(k) is not None else v) for k, v in defaults.items()}
    return cash_forecast(
        dataset_id,
        date=mapping["date"],
        inflow=mapping["inflow"],
        outflow=mapping["outflow"],
        balance=mapping["balance"],
        account=mapping["account"],
        threshold_column=mapping["threshold"],
        horizon=int(values["horizon"]),
        inflow_shock=int(values["inflow_shock_pct"]) / 100,
        outflow_shock=int(values["outflow_shock_pct"]) / 100,
        min_cash=float(values["min_cash"]),
        opening_balance=float(values["opening_balance"]),
        monthly=bool(values["monthly"]),
        lang=lang,
    )
