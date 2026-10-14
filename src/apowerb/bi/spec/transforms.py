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
    CashForecastStep,
    DeriveStep,
    FilterStep,
    ForecastStep,
    InventorySimStep,
    JoinStep,
    NaiveForecastStep,
    Provenance,
    ReplenishmentPlanStep,
    SqlStep,
    TransformStep,
)
from apowerb.bi.spec.templates_common import translator

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

PLAN_COLUMNS = (
    "stock_projete",
    "stock_sans_commande",
    "reception",
    "commande",
    "seuil_plan",
    "delai_plan",
    "risque",
    "semaine_plan",
)

CASH_COLUMNS = (
    "solde_projete_central",
    "solde_projete_pessimiste",
    "solde_projete_optimiste",
    "encaissements_projetes",
    "decaissements_projetes",
    "flux_net_projete",
    "seuil_tresorerie",
    "sous_seuil_central",
    "sous_seuil_pessimiste",
    "sous_seuil_optimiste",
    "semaine_plan",
)


def clear_cache() -> None:
    _forecast_cache.clear()


class ForecastUnavailable(RuntimeError):
    """The th2forecast engine could not produce the requested periods (``en``: the English reason)."""

    def __init__(self, message: str, *, en: str | None = None) -> None:
        super().__init__(message)
        self.en = en

    def text(self, lang: str | None) -> str:
        return self.en if lang == "en" and self.en else str(self)


class TransformContext(Protocol):
    owner: str

    async def get_source(self, name: str) -> MatSource: ...


@dataclass
class TransformState:
    df: pd.DataFrame
    prov: dict[str, Provenance]
    warnings: list[str] = field(default_factory=list)
    failed_columns: dict[str, str] = field(default_factory=dict)
    lang: str | None = None  # dashboard language of the warnings; None = French


def _need(df: pd.DataFrame, columns: list[str], what: str) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        listed, available = ", ".join(missing), ", ".join(map(str, df.columns))
        plural = len(missing) > 1
        raise SpecError(
            f"{what} : {'colonnes introuvables' if plural else 'colonne introuvable'} {listed} "
            f"(colonnes disponibles : {available}).",
            en=f"{what}: {'columns' if plural else 'column'} not found: {listed} (available columns: {available}).",
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
        elif isinstance(step, ReplenishmentPlanStep):
            state = await asyncio.to_thread(_replenishment_plan_step, state, step, what)
        elif isinstance(step, CashForecastStep):
            state = await asyncio.to_thread(_cash_forecast_step, state, step, what)
        else:  # pragma: no cover - the union is closed
            raise SpecError(f"{what} : opération inconnue.", en=f"{what}: unknown operation.")
    return state


def _with(state: TransformState, df: pd.DataFrame, prov: dict[str, Provenance]) -> TransformState:
    return TransformState(
        df=df,
        prov={c: prov.get(c, Provenance.REAL) for c in df.columns},
        warnings=state.warnings,
        failed_columns=state.failed_columns,
        lang=state.lang,
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
        raise SpecError(f"{what} : rien à agréger.", en=f"{what}: nothing to aggregate.")
    select = [quote_ident(g) for g in step.group_by]
    prov: dict[str, Provenance] = {g: state.prov.get(g, Provenance.REAL) for g in step.group_by}
    for name, agg in step.aggregates.items():
        if not name or not name.replace("_", "a").isalnum():
            raise SpecError(f"{what} : nom de colonne invalide « {name} ».", en=f"{what}: invalid column name “{name}”.")
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
            "précisez `columns` pour choisir celles à ramener.",
            en=f"{what}: column “{clash[0]}” exists on both sides; "
            "specify `columns` to choose the ones to bring over.",
        )
    right = other.df[[*step.on, *[c for c in right_cols if c not in step.on]]]
    right = right.drop_duplicates(subset=step.on) if step.how == "left" else right
    try:
        out = state.df.merge(right, on=step.on, how=step.how)
    except (ValueError, TypeError) as exc:
        raise SpecError(f"{what} : jointure impossible ({exc}).", en=f"{what}: join failed ({exc}).") from None
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
# replenishment_plan
# ---------------------------------------------------------------------------


def plan_replenishment(
    stock0: float, forecast: np.ndarray, *, seuil: float, lead_time: int, target_cover: int, lot: int
) -> dict[str, np.ndarray]:
    """Projected stock and orders of one group over its future periods."""
    h = len(forecast)
    fc = np.where(np.isfinite(forecast), np.maximum(forecast, 0.0), 0.0)

    def cover(start: int) -> float:
        # forecast after the horizon: the last period is repeated
        last = fc[-1] if h else 0.0
        return float(sum(fc[k] if k < h else last for k in range(start, start + target_cover)))

    reception = np.zeros(h)
    orders = np.zeros(h)

    def project() -> np.ndarray:
        out = np.zeros(h)
        prev = stock0
        for k in range(h):
            prev = max(0.0, prev + reception[k] - fc[k])
            out[k] = prev
        return out

    for k in range(h):
        arrival = k + lead_time
        if arrival >= h:
            break
        at_arrival = project()[arrival]
        if at_arrival < seuil:
            need = seuil + cover(arrival + 1) - at_arrival
            qty = math.ceil(need / lot - 1e-9) * lot if need > 1e-9 else 0
            orders[k] = qty
            reception[arrival] += qty

    stock = project()
    without = np.maximum(0.0, stock0 - np.cumsum(fc))
    risque = np.where(stock < seuil, 2, np.where(orders > 0, 1, 0))
    return {
        "stock_projete": stock,
        "stock_sans_commande": without,
        "reception": reception,
        "commande": orders,
        "risque": risque.astype(float),
    }


def _last_finite(values: pd.Series) -> float | None:
    numeric = pd.to_numeric(values, errors="coerce").dropna()
    return float(numeric.iloc[-1]) if len(numeric) else None


def _future_dates(dates: pd.Series, n: int) -> list[pd.Timestamp]:
    known = pd.to_datetime(dates, errors="coerce").dropna().sort_values()
    steps = known.diff().dropna()
    step = steps.median() if len(steps) else pd.Timedelta(weeks=1)
    if pd.isna(step) or step <= pd.Timedelta(0):
        step = pd.Timedelta(weeks=1)
    return [known.iloc[-1] + step * (i + 1) for i in range(n)]


def _replenishment_plan_step(state: TransformState, step: ReplenishmentPlanStep, what: str) -> TransformState:
    needed = [step.date, step.stock, step.demand, *step.by]
    needed += [c for c in (step.safety_column, step.lead_time_column) if c]
    _need(state.df, needed, what)
    df = state.df.copy()
    if step.forecast not in df.columns:
        df[step.forecast] = np.nan
    df[step.date] = pd.to_datetime(df[step.date], errors="coerce")
    out, groups = _sorted_groups(df, step.by, step.date)
    stock = pd.to_numeric(out[step.stock], errors="coerce")
    fc_all = pd.to_numeric(out[step.forecast], errors="coerce")

    keep: list[np.ndarray] = []
    appended: list[dict[str, Any]] = []
    fallback: list[str] = []
    results = {name: np.full(len(out), np.nan) for name in PLAN_COLUMNS}
    future_rows: list[tuple[str, np.ndarray, np.ndarray, int]] = []  # key, idx, forecast, real position

    lots = {k: v for k, v in step.lot_sizes.items() if v >= 1}

    def settings(last: int, fc: np.ndarray) -> tuple[float, int]:
        seuil = _last_finite(out[step.safety_column].iloc[[last]]) if step.safety_column else None
        if seuil is None:
            seuil = step.safety_weeks * (float(np.nanmean(fc)) if len(fc) else 0.0)
        delai = _last_finite(out[step.lead_time_column].iloc[[last]]) if step.lead_time_column else None
        lead = step.lead_time if delai is None else int(min(12, max(1, round(delai))))
        return float(seuil), lead

    for key, idx in groups:
        real = idx[stock.iloc[idx].notna().to_numpy()]
        if not len(real):
            continue
        last = int(real[-1])
        history = real[-step.keep_history:] if step.keep_history else real[-1:]
        keep.append(history)
        after = idx[idx > last]
        future = after[fc_all.iloc[after].notna().to_numpy()][: step.horizon]
        if len(future):
            fc = fc_all.iloc[future].to_numpy(dtype=float)
            future_rows.append((key, future, fc, last))
        else:
            recent = pd.to_numeric(out[step.demand].iloc[idx[idx <= last]], errors="coerce").dropna().tail(4)
            mean = float(recent.mean()) if len(recent) else 0.0
            fc = np.full(step.horizon, mean)
            base = {b: out[b].iloc[last] for b in step.by}
            fallback.append(key or "total")
            for ts in _future_dates(out[step.date].iloc[idx[idx <= last]], step.horizon):
                appended.append({step.date: ts, **base, step.forecast: mean, "_key": key, "_last": last})
        results["stock_projete"][history] = stock.iloc[history].to_numpy(dtype=float)
        # threshold and lead time on the kept history too: the threshold line spans the whole chart
        results["seuil_plan"][history], results["delai_plan"][history] = settings(last, fc)
        results["semaine_plan"][last] = 0

    def fill(target: dict[str, np.ndarray], pos: np.ndarray, key: str, fc: np.ndarray, last: int) -> None:
        seuil, lead = settings(last, fc)
        lot = lots.get(key, step.default_lot)
        plan = plan_replenishment(float(stock.iloc[last]), fc, seuil=seuil, lead_time=lead,
                                  target_cover=step.target_cover, lot=lot)
        for name, arr in plan.items():
            target[name][pos] = arr
        target["seuil_plan"][pos] = seuil
        target["delai_plan"][pos] = lead
        target["semaine_plan"][pos] = np.arange(1, len(pos) + 1)

    for key, future, fc, last in future_rows:
        keep.append(future)
        fill(results, future, key, fc, last)

    kept = np.sort(np.concatenate(keep)) if keep else np.array([], dtype=int)
    frame = out.iloc[kept].copy()
    for name, arr in results.items():
        frame[name] = arr[kept]

    if appended:
        extra = pd.DataFrame(appended)
        extra_results = {name: np.full(len(extra), np.nan) for name in PLAN_COLUMNS}
        for key, part in extra.groupby("_key", sort=False):
            pos = part.index.to_numpy()
            fill(extra_results, pos, key, part[step.forecast].to_numpy(dtype=float), int(part["_last"].iloc[0]))
        extra = extra.drop(columns=["_key", "_last"])
        for name, arr in extra_results.items():
            extra[name] = arr
        frame = pd.concat([frame, extra], ignore_index=True, sort=False)
        frame = frame.sort_values([*step.by, step.date], kind="stable")
    frame = frame.reset_index(drop=True)

    prov = dict(state.prov)
    for name in ("stock_projete", "stock_sans_commande", "risque"):
        prov[name] = Provenance.FORECAST
    for name in ("reception", "commande"):
        prov[name] = Provenance.SIMULATED
    prov["seuil_plan"] = Provenance.REAL if step.safety_column else Provenance.SIMULATED
    prov["delai_plan"] = Provenance.REAL if step.lead_time_column else Provenance.SIMULATED
    prov["semaine_plan"] = Provenance.REAL
    if fallback:
        prov[step.forecast] = Provenance.FORECAST
    new = _with(state, frame, prov)
    if fallback:
        T = translator(state.lang)
        names = ", ".join(fallback)
        message = T(
            "plan calculé avec la moyenne des 4 dernières semaines de ventes",
            "plan computed with the average of the last 4 weeks of sales",
        )
        new.warnings = [
            *new.warnings,
            T(f"Prévision moteur absente pour {names} : {message}.", f"Engine forecast missing for {names}: {message}."),
        ]
        if step.forecast in new.failed_columns:
            # the column is filled now: the visuals say how, not that it is missing
            new.failed_columns = {**new.failed_columns, step.forecast: message}
    return new


# ---------------------------------------------------------------------------
# cash_forecast
# ---------------------------------------------------------------------------

_CASH_SCENARIOS = (
    # name, inflow factor sign, outflow factor sign (applied to the shocks)
    ("central", 0, 0),
    ("pessimiste", -1, 1),
    ("optimiste", 1, -1),
)


def project_cash(
    balance0: float,
    inflow: np.ndarray,
    outflow: np.ndarray,
    *,
    inflow_shock: float,
    outflow_shock: float,
    threshold: float,
) -> dict[str, np.ndarray]:
    """Projected balance of one group over its future periods, in three scenarios.

    balance(t) = balance(t-1) + inflow(t) x fi - outflow(t) x fo with
    (fi, fo) = (1, 1) central, (1 - a, 1 + b) pessimistic, (1 + a, 1 - b)
    optimistic. Flows are floored at 0; the balance is never floored.
    """
    enc = np.where(np.isfinite(inflow), np.maximum(inflow, 0.0), 0.0)
    dec = np.where(np.isfinite(outflow), np.maximum(outflow, 0.0), 0.0)
    out: dict[str, np.ndarray] = {
        "encaissements_projetes": enc,
        "decaissements_projetes": dec,
        "flux_net_projete": enc - dec,
    }
    for name, si, so in _CASH_SCENARIOS:
        balance = balance0 + np.cumsum(enc * (1 + si * inflow_shock) - dec * (1 + so * outflow_shock))
        out[f"solde_projete_{name}"] = balance
        out[f"sous_seuil_{name}"] = (balance < threshold - 1e-9).astype(float)
    return out


def _cash_future_dates(dates: pd.Series, n: int) -> list[pd.Timestamp]:
    """The next ``n`` periods: calendar months for monthly data, else the median step."""
    known = pd.to_datetime(dates, errors="coerce").dropna().sort_values().drop_duplicates()
    steps = known.diff().dropna()
    if len(known) and len(steps) and pd.Timedelta(days=27) <= steps.median() <= pd.Timedelta(days=32):
        last = known.iloc[-1]
        if last.is_month_end:
            return [last + pd.offsets.MonthEnd(i + 1) for i in range(n)]
        return [last + pd.DateOffset(months=i + 1) for i in range(n)]
    return _future_dates(known, n)


def _cash_forecast_step(state: TransformState, step: CashForecastStep, what: str) -> TransformState:
    needed = [step.date, step.inflow, step.outflow, *step.by]
    needed += [c for c in (step.balance, step.min_cash_column) if c]
    _need(state.df, needed, what)
    df = state.df.copy()
    for column in (step.forecast_inflow, step.forecast_outflow):
        if column not in df.columns:
            df[column] = np.nan
    df[step.date] = pd.to_datetime(df[step.date], errors="coerce")
    out, groups = _sorted_groups(df, step.by, step.date)

    def num(column: str) -> np.ndarray:
        return pd.to_numeric(out[column], errors="coerce").to_numpy(dtype=float)

    inflow, outflow = num(step.inflow), num(step.outflow)
    fc_in, fc_out = num(step.forecast_inflow), num(step.forecast_outflow)
    balance = num(step.balance) if step.balance else None
    dated = out[step.date].notna().to_numpy()

    parts: list[pd.DataFrame] = []
    fallback: list[str] = []
    window = step.fallback_window

    for key, idx in groups:
        idx = idx[dated[idx]]
        if balance is not None:
            real = idx[np.isfinite(balance[idx])]
        else:
            real = idx[np.isfinite(inflow[idx]) | np.isfinite(outflow[idx])]
        if not len(real):
            continue
        last = int(real[-1])
        if balance is not None:
            real_balance = balance[real]
        else:
            real_balance = step.opening_balance + np.cumsum(
                np.nan_to_num(inflow[real]) - np.nan_to_num(outflow[real])
            )
        threshold = None
        if step.min_cash_column:
            threshold = _last_finite(out[step.min_cash_column].iloc[real])
        threshold = step.min_cash if threshold is None else float(threshold)
        keep = real[-step.keep_history:] if step.keep_history else real[-1:]
        keep_balance = real_balance[-len(keep):]

        hist = out.iloc[keep].copy()
        for name in ("central", "pessimiste", "optimiste"):
            hist[f"solde_projete_{name}"] = keep_balance
        hist["seuil_tresorerie"] = threshold
        hist["semaine_plan"] = np.where(keep == last, 0.0, np.nan)
        # the last real period carries the flags too: a balance already under the threshold counts as period 0
        for name in ("central", "pessimiste", "optimiste"):
            hist[f"sous_seuil_{name}"] = np.where(keep == last, float(real_balance[-1] < threshold - 1e-9), np.nan)
        parts.append(hist)

        # future periods: one row per date, merging the rows of several forecast steps
        after = idx[idx > last]
        future = pd.DataFrame({
            step.date: out[step.date].to_numpy()[after], "i": fc_in[after], "o": fc_out[after],
        })
        future = future.groupby(step.date, sort=True).first()
        future = future[future["i"].notna() | future["o"].notna()].head(step.horizon)
        if len(future):
            dates = list(future.index)
            f_in = future["i"].to_numpy(dtype=float, copy=True)
            f_out = future["o"].to_numpy(dtype=float, copy=True)
        else:
            dates = _cash_future_dates(out[step.date].iloc[real], step.horizon)
            f_in = np.full(len(dates), np.nan)
            f_out = np.full(len(dates), np.nan)
        used_mean = False
        for arr, source in ((f_in, inflow), (f_out, outflow)):
            missing = ~np.isfinite(arr)
            if missing.any():
                recent = pd.Series(source[real]).dropna().tail(window)
                arr[missing] = float(recent.mean()) if len(recent) else 0.0
                used_mean = True
        if used_mean:
            fallback.append(key or "total")

        plan = project_cash(
            float(real_balance[-1]), f_in, f_out,
            inflow_shock=step.inflow_shock, outflow_shock=step.outflow_shock, threshold=threshold,
        )
        n = len(dates)
        fut = pd.DataFrame({step.date: dates})
        for b in step.by:
            fut[b] = out[b].iloc[last]
        fut[step.forecast_inflow], fut[step.forecast_outflow] = f_in, f_out
        for name, arr in plan.items():
            fut[name] = arr
        fut["seuil_tresorerie"] = threshold
        fut["semaine_plan"] = np.arange(1, n + 1, dtype=float)
        parts.append(fut)

    if parts:
        frame = pd.concat(parts, ignore_index=True, sort=False)
        frame = frame.sort_values([*step.by, step.date], kind="stable")
    else:
        frame = out.iloc[0:0].copy()
    for name in CASH_COLUMNS:
        if name not in frame.columns:
            frame[name] = np.nan
    frame = frame.reset_index(drop=True)

    prov = dict(state.prov)
    for name in ("solde_projete_central", "encaissements_projetes", "decaissements_projetes", "flux_net_projete",
                 "sous_seuil_central", step.forecast_inflow, step.forecast_outflow):
        prov[name] = Provenance.FORECAST
    for name in ("solde_projete_pessimiste", "solde_projete_optimiste", "sous_seuil_pessimiste",
                 "sous_seuil_optimiste"):
        prov[name] = Provenance.SIMULATED
    prov["seuil_tresorerie"] = Provenance.REAL if step.min_cash_column else Provenance.SIMULATED
    prov["semaine_plan"] = Provenance.REAL
    new = _with(state, frame, prov)
    if fallback:
        T = translator(state.lang)
        names = ", ".join(fallback)
        message = T(
            f"flux calculés avec la moyenne des {window} dernières périodes",
            f"flows computed with the average of the last {window} periods",
        )
        new.warnings = [
            *new.warnings,
            T(f"Prévision moteur absente pour {names} : {message}.", f"Engine forecast missing for {names}: {message}."),
        ]
        new.failed_columns = {
            **new.failed_columns,
            **{c: message for c in (step.forecast_inflow, step.forecast_outflow) if c in new.failed_columns},
        }
    return new


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
        raise ForecastUnavailable(
            "service de prévision non configuré (TH2FORECAST_URL)",
            en="forecasting service not configured (TH2FORECAST_URL)",
        ) from None
    try:
        return client.forecast(body)
    except Th2forecastAPIError as exc:
        raise ForecastUnavailable(
            f"le service de prévision a refusé la requête ({exc.status_code})",
            en=f"the forecasting service rejected the request ({exc.status_code})",
        ) from None
    except Th2forecastUnavailable as exc:
        raise ForecastUnavailable(
            f"service de prévision indisponible : {exc}", en=f"forecasting service unavailable: {exc}"
        ) from None


def _group_label(row: pd.Series, by: list[str]) -> str:
    return " | ".join(str(row[b]) for b in by)


def _forecast_frame(df: pd.DataFrame, step: ForecastStep) -> pd.DataFrame:
    """``df`` with the engine's future periods appended (output column filled)."""
    if not pd.api.types.is_datetime64_any_dtype(df[step.date]):
        raise ForecastUnavailable(f"la colonne « {step.date} » n'est pas une date", en=f"column “{step.date}” is not a date")
    hist = df[[step.date, step.value, *step.by]].copy()
    hist[step.value] = pd.to_numeric(hist[step.value], errors="coerce")
    hist = hist.dropna(subset=[step.date, step.value])
    if hist.empty:
        raise ForecastUnavailable("aucune donnée historique à prévoir", en="no historical data to forecast")

    from apowerb.schema.forecast_schema import MAX_DATA_ROWS, ForecastRequestSchema

    if len(hist) > MAX_DATA_ROWS:
        raise ForecastUnavailable(f"plus de {MAX_DATA_ROWS} lignes d'historique", en=f"more than {MAX_DATA_ROWS} rows of history")

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
        raise ForecastUnavailable(f"requête de prévision invalide : {exc}", en=f"invalid forecast request: {exc}") from None
    body = request.model_dump(exclude_none=False)
    cache_key = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    response = _forecast_cache.get(cache_key)
    if response is None:
        response = _call_forecast_engine(body)
        _forecast_cache.set(cache_key, response)

    series = response.get("series") or []
    if not series:
        raise ForecastUnavailable("réponse du service de prévision sans série", en="forecasting service response without series")

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
        raise ForecastUnavailable(
            "le service de prévision n'a renvoyé aucune période future",
            en="the forecasting service returned no future period",
        )

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
        T = translator(state.lang)
        message = (exc.text(state.lang) if isinstance(exc, ForecastUnavailable) else str(exc)) or T("délai dépassé", "timed out")
        logger.warning("[BiSpec] forecast unavailable: %s", message)
        empty = state.df.copy()
        empty[step.output] = np.nan
        warnings = [*state.warnings, T(f"Prévision indisponible : {message}.", f"Forecast unavailable: {message}.")]
        failed = {**state.failed_columns, step.output: message}
        return TransformState(
            df=empty,
            prov={c: prov.get(c, Provenance.REAL) for c in empty.columns},
            warnings=warnings,
            failed_columns=failed,
            lang=state.lang,
        )
    return _with(state, out, prov)
