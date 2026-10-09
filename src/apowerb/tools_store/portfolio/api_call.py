from datetime import date, timedelta
from typing import Any

from apowerb.integrations.th2forecast_client import (
    Th2forecastAPIError,
    Th2forecastClient,
    Th2forecastNotConfigured,
    Th2forecastTimeout,
    Th2forecastUnavailable,
)
from apowerb.schema.forecast_schema import (
    ALLOWED_MODELS,
    MAX_DATA_ROWS,
    MAX_HORIZON,
    ForecastRequestSchema,
)
from apowerb.tools_store.portfolio.bi_datasets import (
    _agent_owner,
    _load_agent_sql_rows,
    _load_owned_dataset_rows,
    _load_uploaded_file_rows,
    _run_async,
    validate_forecast_columns,
)

# A row count an LLM can reasonably paste inline (`rows=`) without truncating
# its own context. Larger datasets must go through `sql`, which is executed
# server-side and never round-trips through the model.
_MAX_INLINE_ROWS = 2000

# How many forecast points to keep per series in the tool's answer. The full
# series (up to `horizon` points, possibly per group/model) can be large;
# an agent needs a handful of representative points, not the whole curve.
_MAX_SAMPLE_POINTS = 6
_MAX_CORRECTIONS_SHOWN = 5

# Metrics worth surfacing to the LLM as-is; th2forecast returns more, but the
# rest is redundant for a summary (mase already tells beats_baseline's story).
_KEY_METRICS = ("mape", "smape", "mase", "rmse")


def tool_api_call(api_url: str, payload: dict, token: str) -> dict:
    """Placeholder function for API call tool."""
    return {"status": "success", "data": {"api_url": api_url, "payload": payload}}


def _sample_points(points: list[dict]) -> list[dict]:
    """Keep a handful of representative forecast points: first + last, plus
    evenly spaced ones in between, capped at `_MAX_SAMPLE_POINTS`."""
    if len(points) <= _MAX_SAMPLE_POINTS:
        return points
    step = (len(points) - 1) / (_MAX_SAMPLE_POINTS - 1)
    idx = sorted({round(i * step) for i in range(_MAX_SAMPLE_POINTS)})
    return [points[i] for i in idx]


def _interval_width(point: dict) -> float | None:
    """Widest confidence interval present on a forecast point (upper_XX -
    lower_XX), so the summary can say how uncertain the forecast is without
    assuming which confidence_levels were requested."""
    widths = []
    for key, value in point.items():
        if key.startswith("upper_"):
            suffix = key[len("upper_"):]
            lower_key = f"lower_{suffix}"
            lower_value = point.get(lower_key)
            if isinstance(value, (int, float)) and isinstance(lower_value, (int, float)):
                widths.append(value - lower_value)
    return max(widths) if widths else None


def _parse_date(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _same_period_last_year(series: dict, values: list[float]) -> float | None:
    """Mean of the history points dated one year before the horizon, or None
    when the history does not cover at least half of that window. Comparing
    the horizon with its first point instead reads a seasonal peak as a
    decline (forecast starting right after the autumn peak)."""
    dates = [_parse_date(p.get("date")) for p in series.get("forecast") or []]
    dates = [d for d in dates if d is not None]
    if not dates:
        return None
    # 4 days of slack: 52 weeks are 364 days, not 365.
    low = min(dates) - timedelta(days=369)
    high = max(dates) - timedelta(days=361)
    past = [
        p["value"]
        for p in series.get("history") or []
        if isinstance(p.get("value"), (int, float))
        and (d := _parse_date(p.get("date"))) is not None
        and low <= d <= high
    ]
    if len(past) * 2 < len(values):
        return None
    return sum(past) / len(past)


def _summarize_series(series: dict) -> dict:
    """Turn one th2forecast `series` entry into a compact, factual summary:
    trend, min/max forecast, interval width — no claim beyond what the
    numbers show."""
    forecast = series.get("forecast") or []
    values = [p.get("value") for p in forecast if isinstance(p.get("value"), (int, float))]

    trend = "inconnue"
    trend_basis = None
    change_pct = None
    if len(values) >= 2:
        last_year = _same_period_last_year(series, values)
        if last_year:
            trend_basis = "même période l'an dernier"
            ref, current = last_year, sum(values) / len(values)
        else:
            trend_basis = "début et fin de l'horizon"
            ref, current = values[0], values[-1]
        change_pct = (current - ref) / (abs(ref) or 1.0) * 100
        # A flat threshold avoids calling noise-level drift a trend.
        if abs(change_pct) < 2:
            trend = "stable"
        else:
            trend = "hausse" if change_pct > 0 else "baisse"

    widths = [w for w in (_interval_width(p) for p in forecast) if w is not None]

    metrics = series.get("metrics") or {}
    key_metrics = {k: metrics[k] for k in _KEY_METRICS if k in metrics}

    summary = {
        "group": series.get("group"),
        "model": series.get("model"),
        "reliability": series.get("reliability", "unknown"),
        "beats_baseline": series.get("beats_baseline"),
        "metrics": key_metrics,
        "forecast_sample": _sample_points(forecast),
        "summary": {
            "trend": trend,
            "trend_basis": trend_basis,
            "change_pct": round(change_pct, 1) if change_pct is not None else None,
            "forecast_min": min(values) if values else None,
            "forecast_max": max(values) if values else None,
            "avg_interval_width": (sum(widths) / len(widths)) if widths else None,
        },
        "warnings": series.get("warnings") or [],
    }
    preprocessing = series.get("preprocessing")
    if isinstance(preprocessing, dict):
        summary["preprocessing"] = {
            "outliers_corrected": preprocessing.get("outliers_corrected"),
            "anomalies_corrected": preprocessing.get("anomalies_corrected"),
            "corrections": (preprocessing.get("corrections") or [])[:_MAX_CORRECTIONS_SHOWN],
        }
    return summary


def _compact_response(th2forecast_response: dict) -> dict:
    series = th2forecast_response.get("series") or []
    return {
        "status": th2forecast_response.get("status", "success"),
        "frequency": th2forecast_response.get("frequency"),
        "warnings": th2forecast_response.get("warnings") or [],
        "series": [_summarize_series(s) for s in series],
    }


def _too_large_message(what: str) -> str:
    return (
        f"{what} dépasse {MAX_DATA_ROWS} lignes, au-delà du plafond "
        "supporté pour la prévision. Il ne peut pas être chargé en entier."
    )


def tool_thaink2_forecast(
    date_var: str,
    target_var: str,
    horizon: int,
    sql: str | None = None,
    rows: list[dict] | None = None,
    dataset_id: str | None = None,
    file_id: str | None = None,
    sheet: str | None = None,
    models: list[str] | None = None,
    group_var: str | None = None,
    frequency: str | None = None,
    correct_outliers: bool = False,
) -> dict[str, Any]:
    """
    Generate a time-series forecast via the th2forecast service.

    Provide the historical data as exactly one of: a SQL query (`sql`,
    executed server-side against the connected database), inline `rows`
    (capped, for small ad-hoc series already in context), `dataset_id`
    (a CSV dataset previously imported via the BI upload, owner-scoped —
    see tool_list_datasets / tool_describe_dataset), or `file_id` (a file the
    user attached to the conversation).

    Args:
        date_var (str): Name of the date column in the data.
        target_var (str): Name of the column to forecast.
        horizon (int): Number of future periods to forecast (1-366).
        sql (str): A single SELECT query returning the historical rows, run
            on this agent's database connection (its database tool_config;
            refused when the agent has none). Preferred for a real database
            dataset; write it with tool_text_to_sql. A query starting with
            WITH is refused.
        rows (list[dict]): Historical rows, provided inline. Capped at
            2000 rows — use `sql` or `dataset_id` for anything larger.
        dataset_id (str): Identifier of an imported CSV dataset (see
            tool_list_datasets). Preferred for an imported dataset.
        file_id (str): Name of a file attached to the chat, exactly as shown
            in the `[Uploaded files: ...]` line of the user's message (csv,
            tsv, txt, xlsx, xlsm, xls, ods, json, parquet). Read server-side,
            never copy its content into `rows`. Preferred whenever the user
            attached the data.
        sheet (str): Sheet name for a spreadsheet `file_id` (first sheet when
            omitted). Ignored for other formats.
        models (list[str]): Forecast models to try. Default: ["prophet"].
            Always available: prophet, arima, ets, snaive, naive, auto.
            R engine (the default) adds linear, mars, random_forest, xgboost
            and ensemble; the Python engine adds croston, tsb and imapa.
        group_var (str): Column to forecast independently per group
            (e.g. one forecast per store). Optional.
        frequency (str): Series frequency: day, week, month, quarter, year.
            Optional — detected automatically when omitted.
        correct_outliers (bool): R engine only. Detect and correct outliers
            in the history before training. The returned history keeps the
            original values; corrections are listed in each series'
            `preprocessing` block. Default: False.

    Returns:
        dict: On success, {"status": "success", "frequency", "warnings",
            "series": [{"group", "model", "reliability", "beats_baseline",
            "metrics", "forecast_sample", "summary", "warnings"}, ...]} — one
            entry per (group, model) pair, with a compact forecast sample and
            a factual summary (trend, forecast min/max, average interval
            width) rather than the full curve. When outliers were corrected
            (R engine), each series also carries "preprocessing":
            {"outliers_corrected", "anomalies_corrected", "corrections"
            (the first 5: date, original, corrected, kind)}.
            On failure, {"status": "error", "errors": [{"field", "message"}]}
            or {"status": "error", "message": "..."} for a local validation
            error (e.g. bad sql, too many rows, unknown column).
    """
    sources_given = sum(1 for s in (sql, rows, dataset_id, file_id) if s)
    if sources_given != 1:
        return {
            "status": "error",
            "message": "Fournir exactement une source de données : sql, rows, dataset_id ou file_id.",
        }

    if sql or dataset_id or file_id:
        owner = _agent_owner()
        if not owner:
            return {"status": "error", "message": "Aucun contexte propriétaire (agent hors contexte BI)."}
        if sql:
            # The agent's own database connection, never the process DB_*
            # variables (they point at the platform's database).
            loaded = _run_async(_load_agent_sql_rows(sql, owner, MAX_DATA_ROWS))
        elif file_id:
            loaded = _load_uploaded_file_rows(file_id, owner, MAX_DATA_ROWS, sheet or None)
        else:
            loaded = _run_async(_load_owned_dataset_rows(dataset_id, owner, MAX_DATA_ROWS))
        if not loaded["success"]:
            return {"status": "error", "message": loaded["error"]}
        if loaded["truncated"]:
            what = "Le résultat de la requête" if sql else "Le jeu de données"
            return {"status": "error", "message": _too_large_message(what)}
        column_error = validate_forecast_columns(
            loaded["columns"], date_var, target_var, group_var or ""
        )
        if column_error:
            return {"status": "error", "message": column_error}
        data_rows = loaded["rows"]
    else:
        data_rows = rows or []
        if len(data_rows) > _MAX_INLINE_ROWS:
            return {
                "status": "error",
                "message": (
                    f"rows contient {len(data_rows)} lignes, au-delà du plafond de "
                    f"{_MAX_INLINE_ROWS} pour des données passées en ligne. "
                    f"Utilisez `sql` ou `dataset_id` pour un jeu de données plus large."
                ),
            }

    return _execute_forecast(
        date_var=date_var, target_var=target_var, horizon=horizon,
        data_rows=data_rows, group_var=group_var, frequency=frequency,
        models=models,
        preprocessing={"outliers": True} if correct_outliers else None,
    )


def _execute_forecast(
    date_var: str,
    target_var: str,
    horizon: int,
    data_rows: list[dict],
    group_var: str | None = None,
    frequency: str | None = None,
    models: list[str] | None = None,
    preprocessing: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Shared tail of tool_thaink2_forecast, once ``data_rows`` is resolved:
    validation, th2forecast call, compact response. Reused as-is by
    business_intelligence.tool_create_forecast_chart (same path, no
    chart_id) so a forecast chart's summary is computed exactly like a
    standalone forecast call."""
    if not data_rows:
        return {"status": "error", "message": "Aucune donnée historique à prévoir."}

    if not (1 <= horizon <= MAX_HORIZON):
        return {
            "status": "error",
            "message": f"horizon doit être entre 1 et {MAX_HORIZON}, reçu {horizon}.",
        }

    chosen_models = models or ["prophet"]
    unknown = sorted(set(chosen_models) - ALLOWED_MODELS)
    if unknown:
        return {
            "status": "error",
            "message": (
                f"modèle(s) inconnu(s) : {', '.join(unknown)} ; autorisés : "
                f"{', '.join(sorted(ALLOWED_MODELS))}"
            ),
        }

    try:
        request = ForecastRequestSchema(
            data=data_rows,
            date_var=date_var,
            target_var=target_var,
            group_var=group_var,
            horizon=horizon,
            frequency=frequency,
            models=chosen_models,
            preprocessing=preprocessing,
        )
    except Exception as exc:  # noqa: BLE001 -- pydantic ValidationError plus any construction error, all reported the same way
        return {"status": "error", "message": f"Requête de prévision invalide : {exc}"}

    try:
        client = Th2forecastClient()
    except Th2forecastNotConfigured:
        return {
            "status": "error",
            "message": "Service de prévision non configuré (TH2FORECAST_URL).",
        }

    try:
        response = client.forecast(request.model_dump(exclude_none=False))
    except Th2forecastAPIError as exc:
        body = exc.body if isinstance(exc.body, dict) else {"message": str(exc.body)}
        return {"status": "error", "errors": body.get("errors", [{"field": None, "message": str(body)}])}
    except Th2forecastTimeout:
        return {
            "status": "error",
            "message": "Le service de prévision n'a pas répondu dans le délai imparti.",
        }
    except Th2forecastUnavailable as exc:
        return {"status": "error", "message": f"Service de prévision indisponible : {exc}"}

    return _compact_response(response)
