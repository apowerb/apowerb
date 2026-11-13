"""Pydantic schema for POST /api/v1/forecast — mirrors the th2forecast request
shape fixed by the contract (~/tmp/th2fc/CONTRAT.md), validated here so a
malformed request never reaches th2forecast at all."""

from datetime import date
from typing import Any

from pydantic import BaseModel, Field, StrictBool, field_validator, model_validator

# th2forecast's own MAX_HORIZON default (see contract); the route validates
# against this ceiling too so an oversized horizon fails fast, locally, with
# the same limit th2forecast would enforce (it can still return 413 for a
# smaller-but-still-too-large deployment-specific limit).
MAX_HORIZON = 366
# th2forecast's own MAX_ROWS default: rejecting larger payloads here avoids
# relaying tens of megabytes only for th2forecast to answer 413.
MAX_DATA_ROWS = 100_000

# Union of both engines: croston/tsb/imapa exist only in the Python engine,
# linear/mars/random_forest/xgboost/ensemble only in the R engine. The
# th2forecast client refuses what TH2FORECAST_ENGINE cannot run.
ALLOWED_MODELS = frozenset({
    "prophet", "arima", "ets", "snaive", "naive", "auto",
    "croston", "tsb", "imapa",
    "linear", "mars", "random_forest", "xgboost", "ensemble",
})
# R engine only (th2forecast#19): cleaning steps applied before training.
ALLOWED_PREPROCESSING = frozenset({"anomalies", "outliers"})
ALLOWED_FREQUENCIES = frozenset({"day", "week", "month", "quarter", "year"})
# Étape 5 §2 : "mint" par défaut côté moteur quand hierarchy est fournie.
ALLOWED_RECONCILIATION = frozenset({"mint", "bottom_up", "none"})
# Cardinalité raisonnable pour une hiérarchie (niveaux au-dessus de group_var).
MAX_HIERARCHY_LEVELS = 10


class ForecastRequestSchema(BaseModel):
    """Body of POST /api/v1/forecast — relayed to th2forecast almost verbatim."""

    data: list[dict[str, Any]] = Field(..., min_length=1, max_length=MAX_DATA_ROWS)
    date_var: str = Field(..., min_length=1)
    target_var: str = Field(..., min_length=1)
    group_var: str | None = None
    horizon: int = Field(..., ge=1, le=MAX_HORIZON)
    frequency: str | None = None
    models: list[str] = Field(default_factory=lambda: ["prophet"], min_length=1)
    confidence_levels: list[float] | None = Field(default_factory=lambda: [0.8, 0.95])
    holidays_country: str | None = None
    # R engine only: {"anomalies": true, "outliers": true}. The engine keeps
    # the original values in `history` and lists every correction.
    preprocessing: dict[str, StrictBool] | None = None
    # Contexte métier (th2forecast : événements appris comme covariables et
    # scénarios « et si »). Sans ces champs, Pydantic les jetterait en silence ;
    # leur contenu est validé par th2forecast, qui répond 400 avec un message clair.
    events: list[dict[str, Any]] | None = Field(None, max_length=20)
    scenarios: list[dict[str, Any]] | None = Field(None, max_length=5)
    # Étape 5 §2 : hiérarchie et réconciliation MinT, relayées au moteur.
    hierarchy: list[str] | None = Field(None, max_length=MAX_HIERARCHY_LEVELS)
    reconciliation: str | None = None
    # Étape 5 §3 : identifiant du graphique BI à suivre (jamais relayé au
    # moteur — voir routers/forecast.py). Absent = comportement actuel, rien
    # n'est stocké ni suivi.
    chart_id: str | None = None

    @field_validator("models")
    @classmethod
    def _models_in_allowed_set(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - ALLOWED_MODELS)
        if unknown:
            raise ValueError(
                f"modèle(s) inconnu(s) : {', '.join(unknown)} ; autorisés : "
                f"{', '.join(sorted(ALLOWED_MODELS))}"
            )
        return value

    @field_validator("preprocessing")
    @classmethod
    def _preprocessing_in_allowed_set(cls, value: dict[str, bool] | None) -> dict[str, bool] | None:
        if value is None:
            return value
        unknown = sorted(set(value) - ALLOWED_PREPROCESSING)
        if unknown:
            raise ValueError(
                f"option(s) de prétraitement inconnue(s) : {', '.join(unknown)} ; autorisées : "
                f"{', '.join(sorted(ALLOWED_PREPROCESSING))}"
            )
        return value

    @field_validator("frequency")
    @classmethod
    def _frequency_in_allowed_set(cls, value: str | None) -> str | None:
        if value is not None and value not in ALLOWED_FREQUENCIES:
            raise ValueError(
                f"frequency invalide : {value!r} ; autorisées : "
                f"{', '.join(sorted(ALLOWED_FREQUENCIES))} ou null"
            )
        return value

    @field_validator("confidence_levels")
    @classmethod
    def _confidence_levels_in_range(cls, value: list[float] | None) -> list[float] | None:
        if value is None:
            return value
        if not value:
            raise ValueError("confidence_levels ne peut pas être une liste vide")
        for level in value:
            if not (0 < level < 1):
                raise ValueError(f"confidence_levels doit être dans ]0, 1[, reçu {level!r}")
        return value

    @field_validator("data")
    @classmethod
    def _data_rows_are_non_empty_objects(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for i, row in enumerate(value):
            if not row:
                raise ValueError(f"data[{i}] est une ligne vide")
        return value

    @field_validator("hierarchy")
    @classmethod
    def _hierarchy_requires_group_var_and_non_empty_names(
        cls, value: list[str] | None, info
    ) -> list[str] | None:
        if value is None:
            return value
        # Contrat étape 5 §2 : liste éventuellement vide (Total seul, aucun
        # niveau intermédiaire) — seul `null` signifie « pas de hiérarchie ».
        if any(not level for level in value):
            raise ValueError("hierarchy ne peut pas contenir de colonne vide")
        if not info.data.get("group_var"):
            raise ValueError("hierarchy exige group_var")
        return value

    @field_validator("reconciliation")
    @classmethod
    def _reconciliation_in_allowed_set(cls, value: str | None) -> str | None:
        if value is not None and value not in ALLOWED_RECONCILIATION:
            raise ValueError(
                f"reconciliation invalide : {value!r} ; autorisées : "
                f"{', '.join(sorted(ALLOWED_RECONCILIATION))} ou null"
            )
        return value


class ForecastInterpretSchema(BaseModel):
    """Body of POST /api/v1/forecast/interpret — free text to dated events."""

    text: str = Field(..., min_length=3, max_length=2000)
    history_start: date
    history_end: date
    horizon_end: date
    frequency: str
    groups: list[str] = Field(default_factory=list, max_length=200)
    events: list[dict[str, Any]] = Field(default_factory=list, max_length=20)

    @field_validator("frequency")
    @classmethod
    def _frequency_in_allowed_set(cls, value: str) -> str:
        if value not in ALLOWED_FREQUENCIES:
            raise ValueError(f"frequency invalide : {value!r}")
        return value

    @model_validator(mode="after")
    def _dates_in_order(self) -> "ForecastInterpretSchema":
        if not self.history_start <= self.history_end < self.horizon_end:
            raise ValueError("attendu : history_start <= history_end < horizon_end")
        return self


# Libellé de la période d'agrégation dans la note renvoyée à l'appelant.
_PERIOD_LABELS = {
    None: "date", "day": "jour", "week": "semaine", "month": "mois",
    "quarter": "trimestre", "year": "année",
}


def aggregate_forecast_rows(
    rows: list[dict[str, Any]], date_var: str, target_var: str,
    group_var: str | None, frequency: str | None, extra_keys: list[str] | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Sum ``target_var`` per (group, period) when several rows share a date.

    th2forecast rejects a series whose dates repeat ("Doublons de dates"), so
    transaction-grain data (one row per order) could not be forecast at all.
    Rows are summed per period start when ``frequency`` is given (week =
    Monday), per day otherwise. Rows with one row per date — or with a date
    or value this function cannot read, left for the engine to report — are
    returned untouched, with no note. Otherwise returns the aggregated rows
    (date, measure, group and ``extra_keys`` columns only) and a note stating
    the aggregated measure, meant for the response's ``warnings``.
    """
    import pandas as pd

    keys = [k for k in (group_var, *(extra_keys or [])) if k]
    frame = pd.DataFrame(rows)
    if any(c not in frame.columns for c in (date_var, target_var, *keys)):
        return rows, None
    days = _wall_clock_days(frame[date_var])
    values = pd.to_numeric(frame[target_var], errors="coerce")
    readable = days.notna() & values.notna()
    if not frame[readable].assign(_day=days[readable]).duplicated([*keys, "_day"]).any():
        return rows, None
    # Une ligne sans date ou sans mesure lisible ne compte pas dans la somme.
    frame, days, values = frame[readable], days[readable], values[readable]

    if frequency == "week":
        period = days - pd.to_timedelta(days.dt.weekday, unit="D")
    elif frequency in ("month", "quarter", "year"):
        period = days.dt.to_period({"month": "M", "quarter": "Q", "year": "Y"}[frequency]).dt.start_time
    else:
        period = days
    summed = (
        frame[keys].assign(**{date_var: period, target_var: values})
        .groupby([*keys, date_var], dropna=False, sort=True)[target_var].sum()
        .reset_index()
    )
    summed[date_var] = summed[date_var].dt.strftime("%Y-%m-%d")
    out = [
        {date_var: r[date_var], target_var: float(r[target_var]),
         **{k: None if pd.isna(r[k]) else r[k] for k in keys}}
        for r in summed.to_dict(orient="records")
    ]
    per = _PERIOD_LABELS.get(frequency, "date")
    by_group = f" et par {group_var}" if group_var else ""
    note = (
        f"Mesure agrégée : somme de {target_var} par {per}{by_group} "
        f"({len(rows)} lignes → {len(out)} points), plusieurs lignes partageant une même date."
    )
    if len(frame) < len(rows):
        note += f" {len(rows) - len(frame)} ligne(s) sans date ou valeur lisible ignorée(s)."
    return out, note


def _wall_clock_days(column):
    """Each value's calendar day in its own time zone (NaT when unreadable).

    Mixed UTC offsets (summer/winter time) make pandas fall back to an object
    column, so those are read one by one."""
    import warnings

    import pandas as pd

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", FutureWarning)  # fuseaux mixtes : erreur dans pandas 3
            dates = pd.to_datetime(column, errors="coerce")
    except (TypeError, ValueError):
        dates = None
    if dates is None or not pd.api.types.is_datetime64_any_dtype(dates):
        def wall(value):
            try:
                ts = pd.Timestamp(value)
            except (TypeError, ValueError):
                return pd.NaT
            return ts.tz_localize(None) if ts.tzinfo is not None else ts

        dates = pd.to_datetime(column.map(wall), errors="coerce")
    elif dates.dt.tz is not None:
        dates = dates.dt.tz_localize(None)
    return dates.dt.normalize()
