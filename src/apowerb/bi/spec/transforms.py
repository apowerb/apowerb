"""
bi/spec/transforms.py
---------------------
Execution of the ordered ``transforms`` of a source.

SQL steps (filter / derive / aggregate / sql) run in the sandboxed DuckDB of
``duck.py`` after the SQL guard; ``join`` is a pandas merge;
``naive_forecast`` and ``inventory_sim`` are deterministic pandas / numpy
computations; ``forecast`` calls the th2forecast engine and degrades to an
empty output column (with a warning) when the engine is unavailable.

Each step returns a new frame (inputs are never mutated: the frames are shared
through the cache) and updates the column provenance.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import zlib
from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np
import pandas as pd

from apowerb.bi.spec import sql_guard
from apowerb.bi.spec.cache import TTLCache
from apowerb.bi.spec.duck import SqlRunner, quote_ident
from apowerb.bi.spec.errors import SpecError
from apowerb.bi.spec.frames import MatSource, combine, referenced_columns
from apowerb.bi.spec.models import (
    AggregateStep,
    DeriveStep,
    FilterStep,
    ForecastStep,
    InventorySimStep,
    JoinStep,
    NaiveForecastStep,
    Provenance,
    SqlStep,
    TransformStep,
)

logger = logging.getLogger(__name__)

FORECAST_TIMEOUT_S = 90.0
FORECAST_TTL_S = 1800.0

_forecast_cache = TTLCache(maxsize=64, ttl=FORECAST_TTL_S)

INVENTORY_COLUMNS = (
    "stock_debut",
    "appro",
    "ventes_servies",
    "demande_non_servie",
    "stock_fin",
    "stock_securite",
    "commande",
)


def clear_cache() -> None:
    _forecast_cache.clear()


class ForecastUnavailable(RuntimeError):
    """The th2forecast engine could not produce the requested periods."""


class TransformContext(Protocol):
    owner: str

    async def get_source(self, name: str) -> MatSource: ...


@dataclass
class TransformState:
    df: pd.DataFrame
    prov: dict[str, Provenance]
    warnings: list[str] = field(default_factory=list)
    failed_columns: dict[str, str] = field(default_factory=dict)


def _need(df: pd.DataFrame, columns: list[str], what: str) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise SpecError(
            f"{what} : colonne(s) introuvable(s) {', '.join(missing)} "
            f"(colonnes disponibles : {', '.join(map(str, df.columns))})."
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def apply_transforms(
    state: TransformState, steps: list[TransformStep], ctx: TransformContext
) -> TransformState:
    for i, step in enumerate(steps, start=1):
        what = f"transform {i} ({step.op})"
        if isinstance(step, FilterStep):
            state = await asyncio.to_thread(_filter_step, state, step, what)
        elif isinstance(step, DeriveStep):
            state = await asyncio.to_thread(_derive_step, state, step, what)
        elif isinstance(step, AggregateStep):
            state = await asyncio.to_thread(_aggregate_step, state, step, what)
        elif isinstance(step, SqlStep):
            state = await asyncio.to_thread(_sql_step, state, step, what)
        elif isinstance(step, JoinStep):
            other = await ctx.get_source(step.source)
            state = _join_step(state, step, other, what)
        elif isinstance(step, NaiveForecastStep):
            state = await asyncio.to_thread(_naive_forecast_step, state, step, what)
        elif isinstance(step, InventorySimStep):
            state = await asyncio.to_thread(_inventory_sim_step, state, step, what)
        elif isinstance(step, ForecastStep):
            state = await _forecast_step(state, step, what)
        else:  # pragma: no cover - the union is closed
            raise SpecError(f"{what} : opération inconnue.")
    return state


def _with(state: TransformState, df: pd.DataFrame, prov: dict[str, Provenance]) -> TransformState:
    return TransformState(
        df=df,
        prov={c: prov.get(c, Provenance.REAL) for c in df.columns},
        warnings=state.warnings,
        failed_columns=state.failed_columns,
    )


# ---------------------------------------------------------------------------
# SQL steps
# ---------------------------------------------------------------------------


def _filter_step(state: TransformState, step: FilterStep, what: str) -> TransformState:
    where = sql_guard.check_expression(step.where, what)
    with SqlRunner() as runner:
        out = runner.run(f"SELECT * FROM input WHERE ({where})", {"input": state.df})
    return _with(state, out, state.prov)


def _derive_step(state: TransformState, step: DeriveStep, what: str) -> TransformState:
    expr = sql_guard.check_expression(step.expr, what)
    col = quote_ident(step.column)
    if step.column in state.df.columns:
        sql = f"SELECT * EXCLUDE ({col}), ({expr}) AS {col} FROM input"
    else:
        sql = f"SELECT *, ({expr}) AS {col} FROM input"
    with SqlRunner() as runner:
        out = runner.run(sql, {"input": state.df})
    provenance = step.provenance or combine(
        state.prov.get(c, Provenance.REAL) for c in referenced_columns(expr, state.df.columns)
    )
    prov = dict(state.prov)
    prov[step.column] = provenance
    return _with(state, out, prov)


def _aggregate_step(state: TransformState, step: AggregateStep, what: str) -> TransformState:
    _need(state.df, step.group_by, what)
    if not step.aggregates and not step.group_by:
        raise SpecError(f"{what} : rien à agréger.")
    select = [quote_ident(g) for g in step.group_by]
    prov: dict[str, Provenance] = {g: state.prov.get(g, Provenance.REAL) for g in step.group_by}
    for name, agg in step.aggregates.items():
        if not name or not name.replace("_", "a").isalnum():
            raise SpecError(f"{what} : nom de colonne invalide « {name} ».")
        expr = sql_guard.check_expression(agg, f"{what} ({name})")
        select.append(f"({expr}) AS {quote_ident(name)}")
        prov[name] = combine(
            state.prov.get(c, Provenance.REAL) for c in referenced_columns(expr, state.df.columns)
        )
    group = ""
    if step.group_by:
        cols = ", ".join(quote_ident(g) for g in step.group_by)
        group = f" GROUP BY {cols} ORDER BY {cols}"
    with SqlRunner() as runner:
        out = runner.run(f"SELECT {', '.join(select)} FROM input{group}", {"input": state.df})
    return _with(state, out, prov)


def _sql_step(state: TransformState, step: SqlStep, what: str) -> TransformState:
    query = sql_guard.check_query(step.query, what)
    with SqlRunner() as runner:
        out = runner.run(query, {"input": state.df})
    used = referenced_columns(query, state.df.columns)
    derived = combine(state.prov.get(c, Provenance.REAL) for c in used)
    prov = {c: state.prov.get(c, derived) for c in out.columns}
    return _with(state, out, prov)


# ---------------------------------------------------------------------------
# join (pandas)
# ---------------------------------------------------------------------------


def _join_step(state: TransformState, step: JoinStep, other: MatSource, what: str) -> TransformState:
    _need(state.df, step.on, what)
    _need(other.df, step.on, f"{what} (source « {step.source} »)")
    right_cols = step.columns
    if right_cols is None:
        right_cols = [c for c in other.df.columns if c not in step.on]
    _need(other.df, right_cols, f"{what} (source « {step.source} »)")
    clash = [c for c in right_cols if c in state.df.columns and c not in step.on]
    if clash:
        raise SpecError(
            f"{what} : la colonne « {clash[0]} » existe déjà des deux côtés ; "
            "précisez `columns` pour choisir celles à ramener."
        )
    right = other.df[[*step.on, *[c for c in right_cols if c not in step.on]]]
    right = right.drop_duplicates(subset=step.on) if step.how == "left" else right
    try:
        out = state.df.merge(right, on=step.on, how=step.how)
    except (ValueError, TypeError) as exc:
        raise SpecError(f"{what} : jointure impossible ({exc}).") from None
    prov = dict(state.prov)
    for c in right_cols:
        prov[c] = other.prov.get(c, Provenance.REAL)
    return _with(state, out.reset_index(drop=True), prov)


# ---------------------------------------------------------------------------
# naive_forecast
# ---------------------------------------------------------------------------


def _window_means(values: np.ndarray, starts: np.ndarray, ends: np.ndarray) -> np.ndarray:
    """NaN-aware mean of ``values[start:end]`` for each (start, end) pair."""
    valid = ~np.isnan(values)
    sums = np.concatenate([[0.0], np.cumsum(np.where(valid, values, 0.0))])
    counts = np.concatenate([[0], np.cumsum(valid)])
    cnt = counts[ends] - counts[starts]
    total = sums[ends] - sums[starts]
    return np.where(cnt > 0, total / np.maximum(cnt, 1), np.nan)


def naive_forecast_series(
    values: np.ndarray, *, window: int, season_length: int, trend_bounds: tuple[float, float]
) -> np.ndarray:
    """One-step-ahead forecast of an ordered series (see ``NaiveForecastStep``)."""
    n = len(values)
    idx = np.arange(n)
    prev_mean = _window_means(values, np.maximum(idx - window, 0), idx)
    last_year = np.full(n, np.nan)
    if n > season_length:
        last_year[season_length:] = values[: n - season_length]
    cur_mean = prev_mean  # mean of the `window` periods right before t
    ly_mean = _window_means(
        values, np.maximum(idx - season_length - window, 0), np.maximum(idx - season_length, 0)
    )
    enough = (idx - window >= 0) & (idx - season_length - window >= 0)
    usable = enough & np.isfinite(cur_mean) & np.isfinite(ly_mean) & (ly_mean != 0)
    trend = np.where(usable, cur_mean / np.where(ly_mean == 0, 1.0, ly_mean), 1.0)
    trend = np.clip(trend, trend_bounds[0], trend_bounds[1])
    seasonal = last_year * trend
    use_seasonal = (idx >= season_length) & np.isfinite(last_year)
    return np.where(use_seasonal, seasonal, prev_mean)


def _sorted_groups(df: pd.DataFrame, by: list[str], date: str) -> tuple[pd.DataFrame, list[tuple[str, np.ndarray]]]:
    """Frame sorted by (by, date) and the positional indices of each group."""
    out = df.sort_values([*by, date], kind="stable").reset_index(drop=True)
    if not by:
        return out, [("", np.arange(len(out)))]
    groups = out.groupby(by, sort=False, dropna=False).indices
    named = []
    for key, idx in groups.items():
        parts = key if isinstance(key, tuple) else (key,)
        named.append(("|".join(str(p) for p in parts), np.asarray(idx)))
    return out, named


def _naive_forecast_step(state: TransformState, step: NaiveForecastStep, what: str) -> TransformState:
    _need(state.df, [step.date, step.value, *step.by], what)
    out, groups = _sorted_groups(state.df, step.by, step.date)
    values = pd.to_numeric(out[step.value], errors="coerce").to_numpy(dtype=float)
    result = np.full(len(out), np.nan)
    for _, idx in groups:
        result[idx] = naive_forecast_series(
            values[idx],
            window=step.window,
            season_length=step.season_length,
            trend_bounds=step.trend_bounds,
        )
    out[step.output] = result
    prov = dict(state.prov)
    prov[step.output] = Provenance.FORECAST
    return _with(state, out, prov)


# ---------------------------------------------------------------------------
# inventory_sim
# ---------------------------------------------------------------------------


def _group_lot(step: InventorySimStep, key: str, lot_key: str | None) -> int:
    for k in (lot_key, key):
        if k is not None and k in step.lot_sizes and step.lot_sizes[k] >= 1:
            return int(step.lot_sizes[k])
    return step.default_lot


def simulate_inventory(
    demand: np.ndarray, forecast: np.ndarray, step: InventorySimStep, *, key: str, lot_key: str | None
) -> dict[str, np.ndarray]:
    """Period-by-period stock simulation of one ordered group (``InventorySimStep``)."""
    n = len(demand)
    cols = {name: np.full(n, np.nan) for name in INVENTORY_COLUMNS}
    lot = _group_lot(step, key, lot_key)
    lag = step.lead_time
    rng = np.random.default_rng([abs(step.seed), zlib.crc32(key.encode("utf-8"))])
    half_delivery = rng.random(n) < step.partial_rate
    fc = np.where(np.isfinite(forecast), forecast, demand)
    orders = np.zeros(n)
    stock_prev: float | None = None
    for t in range(n):
        d = demand[t]
        if not math.isfinite(d):
            continue
        f = fc[t] if math.isfinite(fc[t]) else d
        safety = step.safety_factor * f
        begin = step.initial_cover * f if stock_prev is None else stock_prev
        source = t - lag
        received = 0.0 if source < 0 else orders[source] * (0.5 if half_delivery[t] else 1.0)
        served = min(d, begin + received)
        end = begin + received - served
        nxt = fc[t + 1] if t + 1 < n and math.isfinite(fc[t + 1]) else f
        need = step.order_factor * nxt + safety - end
        order = math.ceil(need / lot - 1e-12) * lot if need > 1e-12 else 0
        orders[t] = order
        cols["stock_debut"][t] = begin
        cols["appro"][t] = received
        cols["ventes_servies"][t] = served
        cols["demande_non_servie"][t] = d - served
        cols["stock_fin"][t] = end
        cols["stock_securite"][t] = safety
        cols["commande"][t] = order
        stock_prev = end
    return cols


def _inventory_sim_step(state: TransformState, step: InventorySimStep, what: str) -> TransformState:
    needed = [step.date, step.demand, step.forecast, *step.by]
    if step.lot_column:
        needed.append(step.lot_column)
    _need(state.df, needed, what)
    out, groups = _sorted_groups(state.df, step.by, step.date)
    demand = pd.to_numeric(out[step.demand], errors="coerce").to_numpy(dtype=float)
    forecast = pd.to_numeric(out[step.forecast], errors="coerce").to_numpy(dtype=float)
    results = {name: np.full(len(out), np.nan) for name in INVENTORY_COLUMNS}
    for key, idx in groups:
        lot_key = str(out[step.lot_column].iloc[idx[0]]) if step.lot_column and len(idx) else None
        group = simulate_inventory(demand[idx], forecast[idx], step, key=key, lot_key=lot_key)
        for name, arr in group.items():
            results[name][idx] = arr
    prov = dict(state.prov)
    for name, arr in results.items():
        out[name] = arr
        prov[name] = Provenance.SIMULATED
    return _with(state, out, prov)


# ---------------------------------------------------------------------------
# forecast (th2forecast engine)
# ---------------------------------------------------------------------------


def _call_forecast_engine(body: dict[str, Any]) -> dict[str, Any]:
    """Synchronous call to th2forecast; raises :class:`ForecastUnavailable`."""
    from apowerb.integrations.th2forecast_client import (
        Th2forecastAPIError,
        Th2forecastClient,
        Th2forecastNotConfigured,
        Th2forecastUnavailable,
    )

    try:
        client = Th2forecastClient()
    except Th2forecastNotConfigured:
        raise ForecastUnavailable("service de prévision non configuré (TH2FORECAST_URL)") from None
    try:
        return client.forecast(body)
    except Th2forecastAPIError as exc:
        raise ForecastUnavailable(f"le service de prévision a refusé la requête ({exc.status_code})") from None
    except Th2forecastUnavailable as exc:
        raise ForecastUnavailable(f"service de prévision indisponible : {exc}") from None


def _group_label(row: pd.Series, by: list[str]) -> str:
    return " | ".join(str(row[b]) for b in by)


def _forecast_frame(df: pd.DataFrame, step: ForecastStep) -> pd.DataFrame:
    """``df`` with the engine's future periods appended (output column filled)."""
    if not pd.api.types.is_datetime64_any_dtype(df[step.date]):
        raise ForecastUnavailable(f"la colonne « {step.date} » n'est pas une date")
    hist = df[[step.date, step.value, *step.by]].copy()
    hist[step.value] = pd.to_numeric(hist[step.value], errors="coerce")
    hist = hist.dropna(subset=[step.date, step.value])
    if hist.empty:
        raise ForecastUnavailable("aucune donnée historique à prévoir")

    from apowerb.schema.forecast_schema import MAX_DATA_ROWS, ForecastRequestSchema

    if len(hist) > MAX_DATA_ROWS:
        raise ForecastUnavailable(f"plus de {MAX_DATA_ROWS} lignes d'historique")

    group_var: str | None = None
    if len(step.by) == 1:
        group_var = step.by[0]
    elif len(step.by) > 1:
        group_var = "_group"
        hist[group_var] = hist.apply(lambda r: _group_label(r, step.by), axis=1)
    send = hist[[step.date, step.value] + ([group_var] if group_var else [])].copy()
    send[step.date] = send[step.date].dt.strftime("%Y-%m-%d")
    if group_var:
        send[group_var] = send[group_var].astype(str)
    rows = json.loads(send.to_json(orient="records", date_format="iso"))

    try:
        request = ForecastRequestSchema(
            data=rows,
            date_var=step.date,
            target_var=step.value,
            group_var=group_var,
            horizon=step.horizon,
            frequency=step.frequency,
            models=["prophet"],
        )
    except Exception as exc:  # noqa: BLE001 - pydantic errors, reported as unavailable
        raise ForecastUnavailable(f"requête de prévision invalide : {exc}") from None
    body = request.model_dump(exclude_none=False)
    cache_key = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    response = _forecast_cache.get(cache_key)
    if response is None:
        response = _call_forecast_engine(body)
        _forecast_cache.set(cache_key, response)

    series = response.get("series") or []
    if not series:
        raise ForecastUnavailable("réponse du service de prévision sans série")

    # group label sent to the engine -> values of the `by` columns
    last_dates: dict[str, pd.Timestamp] = {}
    by_values: dict[str, dict[str, Any]] = {}
    if group_var:
        for _, grp in hist.groupby(group_var, dropna=False):
            first = grp.iloc[0]
            label = str(first[group_var])
            by_values[label] = {b: first[b] for b in step.by}
            last_dates[label] = grp[step.date].max()
    else:
        by_values[""] = {}
        last_dates[""] = hist[step.date].max()

    new_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in series:
        label = "" if not group_var else str(item.get("group"))
        if label in seen or label not in by_values:
            continue
        seen.add(label)
        for point in item.get("forecast") or []:
            ts = pd.to_datetime(point.get("date"), errors="coerce")
            value = point.get("value")
            if pd.isna(ts) or not isinstance(value, (int, float)):
                continue
            if ts <= last_dates[label]:
                continue
            new_rows.append({step.date: ts, **by_values[label], step.output: float(value)})
    if not new_rows:
        raise ForecastUnavailable("le service de prévision n'a renvoyé aucune période future")

    base = df.copy()
    base[step.output] = np.nan
    future = pd.DataFrame(new_rows)
    future[step.date] = pd.to_datetime(future[step.date])
    return pd.concat([base, future], ignore_index=True, sort=False)


async def _forecast_step(state: TransformState, step: ForecastStep, what: str) -> TransformState:
    _need(state.df, [step.date, step.value, *step.by], what)
    prov = dict(state.prov)
    prov[step.output] = Provenance.FORECAST
    try:
        out = await asyncio.wait_for(
            asyncio.to_thread(_forecast_frame, state.df, step), timeout=FORECAST_TIMEOUT_S
        )
    except (ForecastUnavailable, asyncio.TimeoutError) as exc:
        message = str(exc) or "délai dépassé"
        logger.warning("[BiSpec] forecast unavailable: %s", message)
        empty = state.df.copy()
        empty[step.output] = np.nan
        warnings = [*state.warnings, f"Prévision indisponible : {message}."]
        failed = {**state.failed_columns, step.output: message}
        return TransformState(
            df=empty,
            prov={c: prov.get(c, Provenance.REAL) for c in empty.columns},
            warnings=warnings,
            failed_columns=failed,
        )
    return _with(state, out, prov)
