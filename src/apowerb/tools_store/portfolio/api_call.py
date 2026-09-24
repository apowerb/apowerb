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
    MAX_HORIZON,
    ForecastRequestSchema,
)
from apowerb.tools_store.portfolio.database import tool_run_sql

# A row count an LLM can reasonably paste inline (`rows=`) without truncating
# its own context. Larger datasets must go through `sql`, which is executed
# server-side and never round-trips through the model.
_MAX_INLINE_ROWS = 2000

# How many forecast points to keep per series in the tool's answer. The full
# series (up to `horizon` points, possibly per group/model) can be large;
# an agent needs a handful of representative points, not the whole curve.
_MAX_SAMPLE_POINTS = 6

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


def _summarize_series(series: dict) -> dict:
    """Turn one th2forecast `series` entry into a compact, factual summary:
    trend, min/max forecast, interval width — no claim beyond what the
    numbers show."""
    forecast = series.get("forecast") or []
    values = [p.get("value") for p in forecast if isinstance(p.get("value"), (int, float))]

    trend = "inconnue"
    if len(values) >= 2:
        delta = values[-1] - values[0]
        # A flat threshold relative to the starting value avoids calling
        # noise-level drift a trend.
        ref = abs(values[0]) or 1.0
        if abs(delta) / ref < 0.02:
            trend = "stable"
        else:
            trend = "hausse" if delta > 0 else "baisse"

    widths = [w for w in (_interval_width(p) for p in forecast) if w is not None]

    metrics = series.get("metrics") or {}
    key_metrics = {k: metrics[k] for k in _KEY_METRICS if k in metrics}

    return {
        "group": series.get("group"),
        "model": series.get("model"),
        "reliability": series.get("reliability", "unknown"),
        "beats_baseline": series.get("beats_baseline"),
        "metrics": key_metrics,
        "forecast_sample": _sample_points(forecast),
        "summary": {
            "trend": trend,
            "forecast_min": min(values) if values else None,
            "forecast_max": max(values) if values else None,
            "avg_interval_width": (sum(widths) / len(widths)) if widths else None,
        },
        "warnings": series.get("warnings") or [],
    }


def _compact_response(th2forecast_response: dict) -> dict:
    series = th2forecast_response.get("series") or []
    return {
        "status": th2forecast_response.get("status", "success"),
        "frequency": th2forecast_response.get("frequency"),
        "warnings": th2forecast_response.get("warnings") or [],
        "series": [_summarize_series(s) for s in series],
    }


def _rows_from_sql(sql: str) -> dict[str, Any]:
    result = tool_run_sql(sql)
    if not result.get("success"):
        return {"error": f"La requête SQL a échoué : {result.get('error')}"}
    return {"rows": result.get("data") or []}


def tool_thaink2_forecast(
    date_var: str,
    target_var: str,
    horizon: int,
    sql: str | None = None,
    rows: list[dict] | None = None,
    models: list[str] | None = None,
    group_var: str | None = None,
    frequency: str | None = None,
) -> dict[str, Any]:
    """
    Generate a time-series forecast via the th2forecast service.

    Provide the historical data either as a SQL query (`sql`, executed
    server-side against the connected database — preferred for any dataset
    the agent did not type by hand) or as inline `rows` (capped, for small
    ad-hoc series the agent already has in context).

    Args:
        date_var (str): Name of the date column in the data.
        target_var (str): Name of the column to forecast.
        horizon (int): Number of future periods to forecast (1-366).
        sql (str): SELECT query returning the historical rows (run via the
            database tool). Preferred over `rows` for any real dataset.
        rows (list[dict]): Historical rows, provided inline. Capped at
            2000 rows — use `sql` for anything larger.
        models (list[str]): Forecast models to try. Default: ["prophet"].
            Allowed: prophet, arima, ets, snaive, naive, auto.
        group_var (str): Column to forecast independently per group
            (e.g. one forecast per store). Optional.
        frequency (str): Series frequency: day, week, month, quarter, year.
            Optional — detected automatically when omitted.

    Returns:
        dict: On success, {"status": "success", "frequency", "warnings",
            "series": [{"group", "model", "reliability", "beats_baseline",
            "metrics", "forecast_sample", "summary", "warnings"}, ...]} — one
            entry per (group, model) pair, with a compact forecast sample and
            a factual summary (trend, forecast min/max, average interval
            width) rather than the full curve.
            On failure, {"status": "error", "errors": [{"field", "message"}]}
            or {"status": "error", "message": "..."} for a local validation
            error (e.g. bad sql, too many rows).
    """
    if bool(sql) == bool(rows):
        return {
            "status": "error",
            "message": "Fournir exactement une source de données : sql OU rows.",
        }

    if sql:
        sourced = _rows_from_sql(sql)
        if "error" in sourced:
            return {"status": "error", "message": sourced["error"]}
        data_rows = sourced["rows"]
    else:
        data_rows = rows or []
        if len(data_rows) > _MAX_INLINE_ROWS:
            return {
                "status": "error",
                "message": (
                    f"rows contient {len(data_rows)} lignes, au-delà du plafond de "
                    f"{_MAX_INLINE_ROWS} pour des données passées en ligne. "
                    f"Utilisez `sql` pour un jeu de données plus large."
                ),
            }

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
