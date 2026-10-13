"""
bi/spec/templates_finance.py
----------------------------
Spec v2 template « Pilotage financier »: a monthly finance dashboard built on a
FEC dataset (Fichier des Écritures Comptables, one row per accounting line).

Only five FEC columns are read: ``ecrituredate``, ``comptenum``, ``debit`` and
``credit`` (required), plus ``comptelib`` (optional, labels the accounts). The
other FEC columns are accepted in the mapping so that a full FEC maps as is.

Sign convention, per account prefix of ``comptenum`` (debit − credit = « solde »):

====================  ===============================================  =========
Indicator             Accounts                                         Sign
====================  ===============================================  =========
Trésorerie            512                                              d − c
CA                    70                                               c − d
Produits exploitation 70 to 75                                         c − d
Achats consommés      60 (603 variation de stock and 609 RRR included) d − c
Services extérieurs   61, 62                                           d − c
Impôts et taxes       63                                               d − c
Personnel             64                                               d − c
Autres charges        65                                               d − c
Résultat financier    76 − 66                                          c − d
Résultat              class 7 − class 6                                c − d
Stocks                class 3                                          d − c
Clients               411                                              d − c
Fournisseurs          401                                              c − d
====================  ===============================================  =========

Balances (trésorerie, stocks, clients, fournisseurs) are cumulated from the first
entry of the file (opening « à-nouveaux » included) and read at the end of the
selected period: the period filter only cuts the months shown, never the opening
balance. Flows (CA, marge, EBE...) are summed over the selected months.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel

from apowerb.bi.spec.models import (
    IDENT,
    ColumnSpec,
    FieldSpec,
    FilterSpec,
    KpiSpec,
    MeasureSpec,
    Provenance,
    ReferenceLine,
    SeriesSpec,
    SourceSpec,
    SqlStep,
    ThresholdSpec,
    VisualSpec,
    WaterfallStep,
)
from apowerb.bi.spec.templates_common import DEFAULT_LANG, translator

_IDENT_RE = re.compile(IDENT)

BLUE = "#3b6fd8"
BROWN = "#9a6b3f"
GREEN = "#2f8f6b"

_FEC_COLUMNS = [
    ("journalcode", "Code journal", "Journal code", "string"),
    ("journallib", "Libellé journal", "Journal label", "string"),
    ("ecriturenum", "Numéro d'écriture", "Entry number", "string"),
    ("ecrituredate", "Date d'écriture", "Entry date", "date"),
    ("comptenum", "Numéro de compte", "Account number", "string"),
    ("comptelib", "Libellé de compte", "Account label", "string"),
    ("compauxnum", "Compte auxiliaire", "Subsidiary account", "string"),
    ("compauxlib", "Libellé auxiliaire", "Subsidiary label", "string"),
    ("pieceref", "Référence de pièce", "Document reference", "string"),
    ("piecedate", "Date de pièce", "Document date", "date"),
    ("ecriturelib", "Libellé d'écriture", "Entry label", "string"),
    ("debit", "Débit", "Debit", "number"),
    ("credit", "Crédit", "Credit", "number"),
    ("ecriturelet", "Lettrage", "Reconciliation code", "string"),
    ("datelet", "Date de lettrage", "Reconciliation date", "date"),
    ("validdate", "Date de validation", "Validation date", "date"),
    ("montantdevise", "Montant en devise", "Foreign currency amount", "number"),
    ("idevise", "Devise", "Currency", "string"),
]
_REQUIRED = ["ecrituredate", "comptenum", "debit", "credit"]

DESCRIPTOR: dict[str, Any] = {
    "id": "finance_pilotage",
    "label": {"fr": "Pilotage financier", "en": "Financial management"},
    "description": {
        "fr": (
            "Tableau de bord financier mensuel à partir d'un FEC : trésorerie, chiffre d'affaires, marge brute, "
            "EBE approché, charges de personnel, seuil de rentabilité et BFR approché, avec la cascade du chiffre "
            "d'affaires au résultat et les principales charges par compte."
        ),
        "en": (
            "Monthly financial dashboard built on a FEC (French accounting entries file): cash, revenue, gross "
            "margin, approximate EBITDA, payroll costs, break-even point and approximate working capital "
            "requirement, with the waterfall from revenue to net result and the main expenses by account."
        ),
    },
    "version": 1,
    "columns": [
        {"key": key, "label": {"fr": label_fr, "en": label_en}, "type": kind, "required": key in _REQUIRED,
         "default": key}
        for key, label_fr, label_en, kind in _FEC_COLUMNS
    ],
    "params": [
        {"key": "periode_defaut_mois", "label": {"fr": "Période affichée par défaut (mois)", "en": "Default period shown (months)"}, "type": "integer",
         "default": 12, "min": 1, "max": 120},
        {"key": "tresorerie_min", "label": {"fr": "Trésorerie minimale (€)", "en": "Minimum cash (€)"}, "type": "integer",
         "default": 0, "min": 0, "max": 1_000_000_000},
        {"key": "marge_brute_cible_pct", "label": {"fr": "Taux de marge brute cible (%)", "en": "Target gross margin rate (%)"}, "type": "integer",
         "default": 30, "min": 0, "max": 100},
        {"key": "part_variable_achats_pct",
         "label": {"fr": "Part variable des achats consommés, comptes 60 (%)",
                   "en": "Variable share of consumed purchases, accounts 60 (%)"},
         "type": "integer", "default": 100, "min": 0, "max": 100},
        {"key": "part_variable_services_pct",
         "label": {"fr": "Part variable des services extérieurs, comptes 61-62 (%)",
                   "en": "Variable share of external services, accounts 61-62 (%)"},
         "type": "integer", "default": 0, "min": 0, "max": 100},
        {"key": "part_variable_personnel_pct",
         "label": {"fr": "Part variable des charges de personnel, comptes 64 (%)",
                   "en": "Variable share of payroll costs, accounts 64 (%)"},
         "type": "integer", "default": 0, "min": 0, "max": 100},
    ],
}
_PARAMS = {p["key"]: p for p in DESCRIPTOR["params"]}


def _check_ident(label: str, value: str | None) -> str:
    if not value:
        raise ValueError(f"{label} is required: map the FEC column '{label}'.")
    if not _IDENT_RE.match(value):
        raise ValueError(
            f"{label}='{value}' is not a valid column name: use lowercase letters, "
            "digits and underscores (start with a letter), e.g. 'ecrituredate'."
        )
    return value


def _param(params: dict[str, Any], key: str) -> int:
    spec = _PARAMS[key]
    value = params.get(key)
    if value is None:
        value = spec["default"]
    if isinstance(value, bool) or not isinstance(value, int) or not spec["min"] <= value <= spec["max"]:
        raise ValueError(f"{key} must be an integer between {spec['min']} and {spec['max']}.")
    return value


def _dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True)


def _q(column: str) -> str:
    return f'"{column}"'


def _spread(count: int) -> list[int]:
    base, extra = divmod(12, count)
    return [base + (1 if i < extra else 0) for i in range(count)]


def _money(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _line_sql(ed: str, cn: str, db: str, cr: str, cl: str | None) -> str:
    """One row per entry line: month, normalised account, and every indicator as a signed amount."""
    lib = f"COALESCE(CAST({_q(cl)} AS VARCHAR), '')" if cl else "''"
    return f"""
WITH l AS (
  SELECT date_trunc('month', CAST({_q(ed)} AS TIMESTAMP)) AS periode_mois,
         regexp_replace(trim(CAST({_q(cn)} AS VARCHAR)), '\\.0+$', '') AS compte,
         {lib} AS lib,
         COALESCE(CAST({_q(db)} AS DOUBLE), 0) - COALESCE(CAST({_q(cr)} AS DOUBLE), 0) AS solde
  FROM input
  WHERE {_q(ed)} IS NOT NULL AND {_q(cn)} IS NOT NULL
), c AS (
  SELECT *, MIN(NULLIF(trim(lib), '')) OVER (PARTITION BY compte) AS libelle FROM l
)
SELECT periode_mois, compte,
  CASE WHEN compte LIKE '6%' THEN compte || COALESCE(' - ' || libelle, '') END AS compte_charge,
  CASE WHEN compte LIKE '512%' THEN solde ELSE 0 END AS tresorerie_l,
  CASE WHEN compte LIKE '70%' THEN -solde ELSE 0 END AS ca_l,
  CASE WHEN LEFT(compte, 2) IN ('70', '71', '72', '73', '74', '75') THEN -solde ELSE 0 END AS prod_expl_l,
  CASE WHEN compte LIKE '60%' THEN solde ELSE 0 END AS achats_l,
  CASE WHEN LEFT(compte, 2) IN ('61', '62') THEN solde ELSE 0 END AS services_l,
  CASE WHEN compte LIKE '63%' THEN solde ELSE 0 END AS impots_l,
  CASE WHEN compte LIKE '64%' THEN solde ELSE 0 END AS personnel_l,
  CASE WHEN compte LIKE '65%' THEN solde ELSE 0 END AS autres_l,
  CASE WHEN LEFT(compte, 2) IN ('66', '76') THEN -solde ELSE 0 END AS financier_l,
  CASE WHEN compte LIKE '7%' THEN -solde ELSE 0 END AS prod_total_l,
  CASE WHEN compte LIKE '6%' THEN solde ELSE 0 END AS ch_total_l,
  CASE WHEN compte LIKE '3%' THEN solde ELSE 0 END AS stocks_l,
  CASE WHEN compte LIKE '411%' THEN solde ELSE 0 END AS clients_l,
  CASE WHEN compte LIKE '401%' THEN -solde ELSE 0 END AS fournisseurs_l
FROM c
""".strip()


def _monthly_sql(pa: int, ps: int, pp: int) -> str:
    """One row per month: flows, variable costs (from the three shares) and cumulated balances."""
    flows = ["ca", "prod_expl", "achats", "services", "impots", "personnel", "autres", "financier", "prod_total",
             "ch_total"]
    sums = ", ".join(f"SUM({f}_l) AS {f}_m" for f in flows)
    flux = "SUM(tresorerie_l) AS tresorerie_flux, SUM(stocks_l) AS stocks_flux, SUM(clients_l) AS clients_flux, " \
           "SUM(fournisseurs_l) AS fournisseurs_flux"
    return f"""
WITH m AS (
  SELECT periode_mois, {sums}, {flux} FROM input GROUP BY periode_mois
), k AS (
  SELECT *,
    ca_m - achats_m AS marge_brute_m,
    achats_m + services_m + impots_m + personnel_m + autres_m AS charges_expl_m,
    prod_expl_m - (achats_m + services_m + impots_m + personnel_m + autres_m) AS ebe_m,
    prod_total_m - ch_total_m AS resultat_m,
    achats_m * {pa} / 100.0 + services_m * {ps} / 100.0 + personnel_m * {pp} / 100.0 AS cv_m
  FROM m
), b AS (
  SELECT *,
    charges_expl_m - cv_m AS cf_m,
    SUM(tresorerie_flux) OVER (ORDER BY periode_mois) AS tresorerie_fin,
    SUM(stocks_flux) OVER (ORDER BY periode_mois) AS stocks_fin,
    SUM(clients_flux) OVER (ORDER BY periode_mois) AS clients_fin,
    SUM(fournisseurs_flux) OVER (ORDER BY periode_mois) AS fournisseurs_fin
  FROM k
)
SELECT *, stocks_fin + clients_fin - fournisseurs_fin AS bfr_fin FROM b ORDER BY periode_mois
""".strip()


def finance_pilotage(
    dataset_id: str,
    *,
    ecrituredate: str = "ecrituredate",
    comptenum: str = "comptenum",
    debit: str = "debit",
    credit: str = "credit",
    comptelib: str | None = None,
    periode_defaut_mois: int = 12,
    tresorerie_min: int = 0,
    marge_brute_cible_pct: int = 30,
    part_variable_achats_pct: int = 100,
    part_variable_services_pct: int = 0,
    part_variable_personnel_pct: int = 0,
    lang: str = DEFAULT_LANG,
) -> dict:
    """Build the « Pilotage financier » spec for a FEC dataset, in ``lang``."""
    T = translator(lang)
    if not dataset_id:
        raise ValueError("dataset_id is required.")
    ed = _check_ident("ecrituredate", ecrituredate)
    cn = _check_ident("comptenum", comptenum)
    db = _check_ident("debit", debit)
    cr = _check_ident("credit", credit)
    cl = _check_ident("comptelib", comptelib) if comptelib else None
    given = dict(periode_defaut_mois=periode_defaut_mois, tresorerie_min=tresorerie_min,
                 marge_brute_cible_pct=marge_brute_cible_pct, part_variable_achats_pct=part_variable_achats_pct,
                 part_variable_services_pct=part_variable_services_pct,
                 part_variable_personnel_pct=part_variable_personnel_pct)
    p = {key: _param(given, key) for key in given}
    default_period = f"{p['periode_defaut_mois']}m"
    treso_min, cible = p["tresorerie_min"], p["marge_brute_cible_pct"]

    # ---- sources -----------------------------------------------------
    lines = SourceSpec(
        name="fec",
        label=T("Écritures du FEC", "FEC entries"),
        kind="dataset",
        dataset_id=dataset_id,
        transforms=[SqlStep(query=_line_sql(ed, cn, db, cr, cl))],
        fields=[
            FieldSpec(name="periode_mois", label=T("Mois", "Month"), type="date", role="date"),
            FieldSpec(name="compte", label=T("Compte", "Account"), role="dimension"),
            FieldSpec(name="compte_charge", label=T("Compte de charges", "Expense account"), role="dimension"),
        ],
        measures=[
            MeasureSpec(name="charges_par_compte", label=T("Charges (classe 6)", "Expenses (class 6)"),
                        expr="SUM(ch_total_l)", format="compact", unit="€",
                        description=T("Débit − crédit des comptes de la classe 6.",
                                      "Debit − credit of the class 6 accounts.")),
        ],
    )

    warn_floor = treso_min - abs(treso_min) // 4
    rate_thr = ThresholdSpec(direction="higher_is_better", good=cible / 100, warn=max(cible - 5, 0) / 100)
    cash_thr = ThresholdSpec(direction="higher_is_better", good=float(treso_min), warn=float(warn_floor))

    def money(name: str, label: str, expr: str, description: str, **kw: Any) -> MeasureSpec:
        return MeasureSpec(name=name, label=label, expr=expr, format="compact", unit="€", description=description,
                           **kw)

    net_other = "(SUM(prod_expl_m) - SUM(ca_m) - SUM(autres_m))"
    net_hors = "(SUM(resultat_m) - SUM(ebe_m) - SUM(financier_m))"
    ca_sr = "SUM(ca_m)"
    sr = f"CASE WHEN {ca_sr} - SUM(cv_m) > 0 THEN SUM(cf_m) * {ca_sr} / ({ca_sr} - SUM(cv_m)) END"
    measures = [
        money("tresorerie", T("Trésorerie", "Cash"), "arg_max(tresorerie_fin, periode_mois)",
              T("Solde cumulé des comptes 512 (débit − crédit) à la fin de la période.",
                "Cumulative balance of accounts 512 (debit − credit) at the end of the period."),
              thresholds=cash_thr),
        money("ca", T("Chiffre d'affaires", "Revenue"), "SUM(ca_m)",
              T("Comptes 70 (crédit − débit), cumulé sur les mois affichés.",
                "Accounts 70 (credit − debit), cumulated over the months shown.")),
        money("ca_mois", T("CA du dernier mois", "Last month revenue"), "arg_max(ca_m, periode_mois)",
              T("Comptes 70 du dernier mois affiché.", "Accounts 70 of the last month shown.")),
        money("achats", T("Achats consommés", "Consumed purchases"), "SUM(achats_m)",
              T("Comptes 60, variation de stock (603) et rabais obtenus (609) inclus.",
                "Accounts 60, including inventory change (603) and rebates obtained (609).")),
        money("marge_brute", T("Marge brute", "Gross margin"), "SUM(marge_brute_m)",
              T("CA (70) − achats consommés (60).", "Revenue (70) − consumed purchases (60).")),
        MeasureSpec(name="taux_marge_brute", label=T("Taux de marge brute", "Gross margin rate"),
                    expr="SUM(marge_brute_m) / NULLIF(SUM(ca_m), 0)", format="percent", thresholds=rate_thr,
                    description=T("Marge brute ÷ CA.", "Gross margin ÷ revenue.")),
        money("ebe", T("EBE approché", "Approx. EBITDA"), "SUM(ebe_m)",
              T("Produits d'exploitation (70-75) − charges (60-65).",
                "Operating income (70-75) − expenses (60-65).")),
        MeasureSpec(name="taux_ebe", label=T("EBE / CA", "EBITDA / revenue"),
                    expr="SUM(ebe_m) / NULLIF(SUM(ca_m), 0)", format="percent"),
        money("charges_personnel", T("Charges de personnel", "Payroll costs"), "SUM(personnel_m)",
              T("Comptes 64.", "Accounts 64.")),
        MeasureSpec(name="part_personnel", label=T("Personnel / CA", "Payroll / revenue"),
                    expr="SUM(personnel_m) / NULLIF(SUM(ca_m), 0)", format="percent"),
        money("seuil_rentabilite", T("Seuil de rentabilité", "Break-even point"), sr,
              T("Charges fixes ÷ taux de marge sur coûts variables, sur la période affichée. Vide si la marge sur "
                "coûts variables est négative ou nulle.",
                "Fixed costs ÷ variable-cost margin rate, over the period shown. Empty if the variable-cost "
                "margin is negative or zero.")),
        MeasureSpec(name="marge_securite", label=T("Marge de sécurité", "Safety margin"),
                    expr=f"CASE WHEN {ca_sr} > 0 THEN ({ca_sr} - ({sr})) / {ca_sr} END", format="percent",
                    description=T("(CA − seuil de rentabilité) ÷ CA.", "(Revenue − break-even point) ÷ revenue.")),
        money("stocks_fin", "Stocks", "arg_max(stocks_fin, periode_mois)",
              T("Comptes 3, solde de fin de période.", "Accounts 3, end-of-period balance.")),
        money("clients_fin", T("Clients", "Customers"), "arg_max(clients_fin, periode_mois)",
              T("Comptes 411, solde de fin de période.", "Accounts 411, end-of-period balance.")),
        money("fournisseurs_fin", T("Fournisseurs", "Suppliers"), "arg_max(fournisseurs_fin, periode_mois)",
              T("Comptes 401 (crédit − débit), solde de fin de période.",
                "Accounts 401 (credit − debit), end-of-period balance.")),
        money("bfr", T("BFR approché", "Approx. working capital requirement"), "arg_max(bfr_fin, periode_mois)",
              T("Stocks + clients 411 − fournisseurs 401.", "Inventory + customers 411 − suppliers 401.")),
        money("resultat", T("Résultat", "Net result"), "SUM(resultat_m)",
              T("Produits (classe 7) − charges (classe 6).", "Income (class 7) − expenses (class 6).")),
        money("services_ext", T("Services extérieurs", "External services"), "SUM(services_m)",
              T("Comptes 61 et 62.", "Accounts 61 and 62.")),
        money("impots_taxes", T("Impôts et taxes", "Taxes and duties"), "SUM(impots_m)",
              T("Comptes 63.", "Accounts 63.")),
        money("autres_exploit_pos", T("Autres produits d'exploitation nets", "Net other operating income"),
              f"GREATEST({net_other}, 0)",
              T("Comptes 71-75 moins comptes 65, si positif.", "Accounts 71-75 minus accounts 65, if positive.")),
        money("autres_exploit_neg", T("Autres charges d'exploitation nettes", "Net other operating expenses"),
              f"GREATEST(-{net_other}, 0)",
              T("Comptes 65 moins comptes 71-75, si positif.", "Accounts 65 minus accounts 71-75, if positive.")),
        money("financier_pos", T("Produits financiers nets", "Net financial income"),
              "GREATEST(SUM(financier_m), 0)",
              T("Comptes 76 moins 66, si positif.", "Accounts 76 minus 66, if positive.")),
        money("financier_neg", T("Charges financières nettes", "Net financial expenses"),
              "GREATEST(-SUM(financier_m), 0)",
              T("Comptes 66 moins 76, si positif.", "Accounts 66 minus 76, if positive.")),
        money("hors_exploit_pos", T("Autres produits nets", "Net other income"), f"GREATEST({net_hors}, 0)",
              T("Exceptionnel, dotations/reprises et impôt sur les bénéfices (67-69, 77-79), si positif.",
                "Exceptional items, provisions/reversals and income tax (67-69, 77-79), if positive.")),
        money("hors_exploit_neg", T("Autres charges nettes", "Net other expenses"), f"GREATEST(-{net_hors}, 0)",
              T("Exceptionnel, dotations/reprises et impôt sur les bénéfices (67-69, 77-79), si négatif.",
                "Exceptional items, provisions/reversals and income tax (67-69, 77-79), if negative.")),
    ]
    monthly = SourceSpec(
        name="mensuel",
        label=T("Synthèse mensuelle", "Monthly summary"),
        kind="source",
        parent="fec",
        transforms=[SqlStep(query=_monthly_sql(
            p["part_variable_achats_pct"], p["part_variable_services_pct"], p["part_variable_personnel_pct"]))],
        fields=[FieldSpec(name="periode_mois", label=T("Mois", "Month"), type="date", role="date")],
        measures=measures,
    )

    presets = ["3m", "6m", "12m", "24m", "all"]
    if default_period not in presets:
        presets.insert(0, default_period)
    filters = [FilterSpec(id="periode", label=T("Période", "Period"), field="periode_mois", type="relative_period",
                          default=default_period, presets=presets)]

    # ---- visuals -----------------------------------------------------
    def kpi(title: str, measure: str, *, spark: str | None = None, **kw: Any) -> VisualSpec:
        return VisualSpec(type="kpi", title=title, source="mensuel", x="periode_mois",
                          kpi=KpiSpec(measure=measure, sparkline=spark, **kw))

    synthese = VisualSpec(
        type="text",
        source="mensuel",
        text=T(
            "{{filters.periode}}, du {{period.from}} au {{period.to}} : chiffre d'affaires de {{ca}}, marge brute de "
            "{{taux_marge_brute|percent}} ({{marge_brute}}), EBE approché de {{ebe}} ({{taux_ebe|percent}} du CA). "
            "Trésorerie de {{tresorerie}} en fin de période et BFR approché de {{bfr}}. Les charges de personnel "
            "pèsent {{part_personnel|percent}} du CA ; le seuil de rentabilité est de {{seuil_rentabilite}}.",
            "{{filters.periode}}, {{period.from}} to {{period.to}}: revenue of {{ca}}, gross margin of "
            "{{taux_marge_brute|percent}} ({{marge_brute}}), approximate EBITDA of {{ebe}} ({{taux_ebe|percent}} of "
            "revenue). Cash of {{tresorerie}} at period end and approximate working capital requirement of {{bfr}}. "
            "Payroll costs weigh {{part_personnel|percent}} of revenue; the break-even point is "
            "{{seuil_rentabilite}}.",
        ),
    )
    kpis_top = [
        kpi(T("Trésorerie", "Cash"), "tresorerie", spark="tresorerie", compare="previous_point",
            target_label=T(f"minimum {_money(treso_min)} €", f"minimum €{_money(treso_min)}")),
        kpi(T("CA du dernier mois", "Last month revenue"), "ca_mois", spark="ca", compare="previous_point"),
        kpi(T("CA cumulé", "Cumulative revenue"), "ca", spark="ca", compare="previous_period",
            secondary=T("sur les mois affichés", "over the months shown")),
        kpi(T("Marge brute", "Gross margin"), "taux_marge_brute", spark="taux_marge_brute",
            compare="previous_period", secondary=T("{{marge_brute}} de marge brute", "{{marge_brute}} gross margin"),
            target_label=T(f"cible ≥ {cible} %", f"target ≥ {cible}%")),
    ]
    kpis_bottom = [
        kpi(T("EBE approché", "Approx. EBITDA"), "ebe", spark="ebe", compare="previous_period",
            secondary=T("{{taux_ebe|percent}} du CA", "{{taux_ebe|percent}} of revenue")),
        kpi(T("Charges de personnel", "Payroll costs"), "charges_personnel", spark="charges_personnel",
            compare="previous_period",
            secondary=T("{{part_personnel|percent}} du CA", "{{part_personnel|percent}} of revenue")),
        kpi(T("Seuil de rentabilité", "Break-even point"), "seuil_rentabilite",
            secondary=T("Marge de sécurité : {{marge_securite|percent}}",
                        "Safety margin: {{marge_securite|percent}}")),
        kpi(T("BFR approché", "Approx. working capital requirement"), "bfr", spark="bfr",
            compare="previous_point",
            secondary=T("Stocks {{stocks_fin}} + clients {{clients_fin}} − fournisseurs {{fournisseurs_fin}}",
                        "Inventory {{stocks_fin}} + customers {{clients_fin}} − suppliers {{fournisseurs_fin}}")),
    ]
    tresorerie_chart = VisualSpec(
        type="area",
        title=T("Évolution de la trésorerie", "Cash trend"),
        subtitle=T("Solde cumulé des comptes 512 à la fin de chaque mois.",
                   "Cumulative balance of accounts 512 at the end of each month."),
        source="mensuel",
        x="periode_mois",
        series=[SeriesSpec(key="tresorerie", label=T("Trésorerie", "Cash"), mark="area", color=BLUE)],
        reference_lines=[ReferenceLine(value=float(treso_min), label=T("Minimum", "Minimum"), color="bad")],
        sort="x_asc",
    )
    ca_marge = VisualSpec(
        type="combo",
        title=T("CA et marge par mois", "Revenue and margin by month"),
        subtitle=T("CA et marge brute en euros, taux de marge brute sur l'axe de droite.",
                   "Revenue and gross margin in euros, gross margin rate on the right axis."),
        source="mensuel",
        x="periode_mois",
        series=[
            SeriesSpec(key="ca", label=T("CA", "Revenue"), mark="bar", color=BLUE),
            SeriesSpec(key="marge_brute", label=T("Marge brute", "Gross margin"), mark="bar", color=GREEN),
            SeriesSpec(key="taux_marge_brute", label=T("Taux de marge", "Margin rate"), mark="line", color=BROWN, axis="right"),
        ],
        sort="x_asc",
    )
    cascade = VisualSpec(
        type="waterfall",
        title=T("Du chiffre d'affaires au résultat", "From revenue to net result"),
        subtitle=T("Cumul sur les mois affichés ; les soldes nets sont répartis en gains et pertes.",
                   "Cumulated over the months shown; net balances are split into gains and losses."),
        source="mensuel",
        steps=[
            WaterfallStep(label=T("Chiffre d'affaires", "Revenue"), measure="ca", kind="start"),
            WaterfallStep(label=T("Achats consommés", "Consumed purchases"), measure="achats", kind="decrease"),
            WaterfallStep(label=T("Services extérieurs", "External services"), measure="services_ext", kind="decrease"),
            WaterfallStep(label=T("Impôts et taxes", "Taxes and duties"), measure="impots_taxes", kind="decrease"),
            WaterfallStep(label=T("Personnel", "Payroll"), measure="charges_personnel", kind="decrease"),
            WaterfallStep(label=T("Autres produits d'exploitation", "Other operating income"), measure="autres_exploit_pos", kind="increase"),
            WaterfallStep(label=T("Autres charges d'exploitation", "Other operating expenses"), measure="autres_exploit_neg", kind="decrease"),
            WaterfallStep(label=T("EBE", "EBITDA"), measure="ebe", kind="end"),
            WaterfallStep(label=T("Produits financiers", "Financial income"), measure="financier_pos", kind="increase"),
            WaterfallStep(label=T("Charges financières", "Financial expenses"), measure="financier_neg", kind="decrease"),
            WaterfallStep(label=T("Autres produits (exc., reprises)", "Other income (exceptional, reversals)"), measure="hors_exploit_pos", kind="increase"),
            WaterfallStep(label=T("Dotations, exc. et impôt", "Provisions, exceptional and tax"), measure="hors_exploit_neg", kind="decrease"),
            WaterfallStep(label=T("Résultat", "Net result"), measure="resultat", kind="end"),
        ],
    )
    top_charges = VisualSpec(
        type="hbar",
        title=T("Principales charges par compte", "Main expenses by account"),
        subtitle=T("Les 10 comptes de charges (classe 6) les plus lourds sur la période.",
                   "The 10 heaviest expense accounts (class 6) over the period."),
        source="fec",
        x="compte_charge",
        series=[SeriesSpec(key="charges_par_compte", label=T("Charges", "Expenses"), mark="bar", color=BROWN)],
        sort="value_desc",
        top_n=10,
    )
    table = VisualSpec(
        type="table",
        title=T("Tableau mensuel", "Monthly table"),
        subtitle=T("Flux du mois ; trésorerie et BFR en solde de fin de mois.",
                   "Monthly flows; cash and working capital requirement as end-of-month balances."),
        source="mensuel",
        x="periode_mois",
        columns=[
            ColumnSpec(key="periode_mois", label=T("Mois", "Month")),
            ColumnSpec(key="ca", label=T("CA", "Revenue")),
            ColumnSpec(key="marge_brute", label=T("Marge brute", "Gross margin")),
            ColumnSpec(key="taux_marge_brute", label=T("Taux de marge", "Margin rate"), use_thresholds=True),
            ColumnSpec(key="ebe", label=T("EBE", "EBITDA")),
            ColumnSpec(key="charges_personnel", label=T("Personnel", "Payroll")),
            ColumnSpec(key="resultat", label=T("Résultat", "Net result")),
            ColumnSpec(key="tresorerie", label=T("Trésorerie", "Cash"), use_thresholds=True),
            ColumnSpec(key="bfr", label=T("BFR", "WCR")),
        ],
        total_row=True,
        sort="x_desc",
    )
    notes = [
        VisualSpec(
            type="note",
            title=T("Définitions et comptes retenus", "Definitions and accounts used"),
            badges=[Provenance.REAL],
            text=T(
                "**Trésorerie** : débit − crédit des comptes 512, cumulé depuis la première écriture (à-nouveaux "
                "compris). **CA** : comptes 70, crédit − débit. **Marge brute** : CA − achats consommés (comptes 60, "
                "dont variation de stock 603). **EBE approché** : produits d'exploitation 70-75 − charges 60-65 "
                "(les comptes 66 à 69 en sont exclus). **BFR approché** : stocks (classe 3) + clients 411 − "
                "fournisseurs 401, soldes de fin de période, hors TVA et autres comptes de tiers. **Résultat** : "
                "classe 7 − classe 6.",
                "**Cash**: debit − credit of accounts 512, cumulated from the first entry (opening balances "
                "included). **Revenue**: accounts 70, credit − debit. **Gross margin**: revenue − consumed "
                "purchases (accounts 60, including inventory change 603). **Approx. EBITDA**: operating income "
                "70-75 − expenses 60-65 (accounts 66 to 69 are excluded). **Approx. working capital requirement "
                "(WCR)**: inventory (class 3) + customers 411 − suppliers 401, end-of-period balances, excluding "
                "VAT and other third-party accounts. **Net result**: class 7 − class 6.",
            ),
        ),
        VisualSpec(
            type="note",
            title=T("Seuil de rentabilité : hypothèse fixes / variables", "Break-even point: fixed / variable assumption"),
            text=T(
                f"Coûts variables = {p['part_variable_achats_pct']} % des achats consommés (60) + "
                f"{p['part_variable_services_pct']} % des services extérieurs (61-62) + "
                f"{p['part_variable_personnel_pct']} % du personnel (64). Impôts et taxes (63) et autres charges "
                "(65) sont fixes, ainsi que le reste des comptes 61-62 et 64. Seuil = charges fixes ÷ (marge sur "
                "coûts variables ÷ CA), sur la période affichée. Parts réglables à la création du tableau de bord.",
                f"Variable costs = {p['part_variable_achats_pct']}% of consumed purchases (60) + "
                f"{p['part_variable_services_pct']}% of external services (61-62) + "
                f"{p['part_variable_personnel_pct']}% of payroll (64). Taxes and duties (63) and other expenses "
                "(65) are fixed, as is the rest of accounts 61-62 and 64. Break-even point = fixed costs ÷ "
                "(variable-cost margin ÷ revenue), over the period shown. Shares can be set when the dashboard "
                "is created.",
            ),
        ),
        VisualSpec(
            type="note",
            title=T("Limites", "Limitations"),
            text=T(
                f"Seuils : trésorerie minimale {_money(treso_min)} €, marge brute cible {cible} %. Les écritures sans "
                "date ou sans numéro de compte sont ignorées. Les écritures de clôture qui solderaient les "
                "comptes 6 et 7 doivent être exclues du FEC fourni, sinon elles annulent le résultat du mois. "
                "Ce n'est pas une balance âgée : les créances clients ne sont pas ventilées par ancienneté.",
                f"Thresholds: minimum cash €{_money(treso_min)}, target gross margin {cible}%. Entries without a "
                "date or an account number are ignored. Closing entries that would zero out accounts 6 and 7 "
                "must be excluded from the supplied FEC, otherwise they cancel the month's result. "
                "This is not an aged balance: customer receivables are not broken down by age.",
            ),
        ),
    ]

    # ---- grid --------------------------------------------------------
    grid: list[tuple[VisualSpec, dict[str, int]]] = [(synthese, dict(row=0, col=0, width=12, height=2))]
    for row, tiles in ((2, kpis_top), (5, kpis_bottom)):
        col = 0
        for tile, width in zip(tiles, _spread(len(tiles))):
            grid.append((tile, dict(row=row, col=col, width=width, height=3)))
            col += width
    grid += [
        (tresorerie_chart, dict(row=8, col=0, width=6, height=6)),
        (ca_marge, dict(row=8, col=6, width=6, height=6)),
        (cascade, dict(row=14, col=0, width=7, height=7)),
        (top_charges, dict(row=14, col=7, width=5, height=7)),
        (table, dict(row=21, col=0, width=12, height=7)),
    ]
    col = 0
    for tile, width in zip(notes, _spread(len(notes))):
        grid.append((tile, dict(row=28, col=col, width=width, height=6)))
        col += width

    return {
        "sources": [_dump(lines), _dump(monthly)],
        "filters": [_dump(f) for f in filters],
        "components": [{"visual": _dump(v), "position": pos} for v, pos in grid],
    }


def build(dataset_id: str, columns: dict, params: dict, lang: str = DEFAULT_LANG) -> dict:
    """Catalog entry point: FEC column mapping (blank optional → None) and parameters to the template."""
    mapping: dict[str, Any] = {key: columns.get(key) or "" for key in _REQUIRED}
    if columns.get("comptelib"):
        mapping["comptelib"] = columns["comptelib"]
    return finance_pilotage(
        dataset_id, **mapping, **{k: v for k, v in (params or {}).items() if k in _PARAMS}, lang=lang
    )
