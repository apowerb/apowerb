"""
bi/spec/duck.py
---------------
Sandboxed in-memory DuckDB used by the spec engine.

- ``enable_external_access=False``: no file, no network, no extension load;
  the configuration is then locked so no expression can undo it.
- Only pandas DataFrames registered for the call are visible; nothing is ever
  written to disk.
- Every query has a time limit (``conn.interrupt()`` from a timer) and a row
  cap; both raise :class:`SpecError`.
"""

from __future__ import annotations

import re
import threading
import time
from types import TracebackType
from typing import Any

import duckdb
import pandas as pd

from apowerb.bi.spec.errors import SpecError

QUERY_TIMEOUT_S = 10.0
MAX_RESULT_ROWS = 200_000
MEMORY_LIMIT = "512MB"
MAX_CONCURRENT_QUERIES = 4
_slots = threading.BoundedSemaphore(MAX_CONCURRENT_QUERIES)

_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


def quote_ident(name: str) -> str:
    """Double-quoted SQL identifier."""
    return '"' + str(name).replace('"', '""') + '"'


def _short_error(exc: Exception) -> str:
    lines = [ln.strip() for ln in str(exc).splitlines() if ln.strip()]
    # DuckDB appends "LINE 1: ..." context after the message: keep the message.
    head = [ln for ln in lines if not ln.startswith(("LINE ", "^"))][:2]
    return " ".join(head)[:400] or exc.__class__.__name__


class SqlRunner:
    """One sandboxed connection; use as a context manager."""

    def __init__(
        self, *, timeout_s: float | None = None, max_rows: int | None = None, budget_s: float | None = None
    ) -> None:
        """``budget_s``: total time all queries of this runner may use together."""
        self._budget_s = budget_s
        self._deadline: float | None = None
        self._timeout_s = timeout_s
        self._max_rows = max_rows
        self._conn: duckdb.DuckDBPyConnection | None = None

    def __enter__(self) -> "SqlRunner":
        conn = duckdb.connect(
            config={
                "enable_external_access": False,
                "memory_limit": MEMORY_LIMIT,
                "threads": 2,
            }
        )
        conn.execute("SET lock_configuration=true")
        self._conn = conn
        if self._budget_s is not None:
            self._deadline = time.monotonic() + self._budget_s
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def run(self, sql: str, relations: dict[str, pd.DataFrame] | None = None) -> pd.DataFrame:
        """Run one SELECT over the given relations and return a DataFrame."""
        assert self._conn is not None, "SqlRunner must be used as a context manager"
        conn = self._conn
        timeout = self._timeout_s if self._timeout_s is not None else QUERY_TIMEOUT_S
        cap = self._max_rows if self._max_rows is not None else MAX_RESULT_ROWS
        relations = relations or {}
        if self._deadline is not None:
            remaining = self._deadline - time.monotonic()
            if remaining <= 0:
                raise SpecError(
                    "Délai de calcul dépassé (budget global épuisé).",
                    en="Computation time exceeded (overall budget used up).",
                )
            timeout = min(timeout, remaining)
        try:
            statements = duckdb.extract_statements(sql)
        except duckdb.Error as exc:
            raise SpecError(f"Erreur SQL : {_short_error(exc)}", en=f"SQL error: {_short_error(exc)}") from None
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            raise SpecError("Une seule requête SELECT est permise.", en="Only one SELECT query is allowed.")

        for name, frame in relations.items():
            if not _NAME_RE.match(name):
                raise SpecError(f"Nom de relation invalide : {name}", en=f"Invalid relation name: {name}")
        # Global cap on concurrent executions; waiting counts against the time limit.
        if not _slots.acquire(timeout=timeout):
            raise SpecError(
                f"Délai de calcul dépassé ({timeout:g} s, serveur occupé).",
                en=f"Computation time exceeded ({timeout:g} s, server busy).",
            )
        started = time.monotonic()
        timer = threading.Timer(max(timeout - (time.monotonic() - started), 0.01), conn.interrupt)
        timer.daemon = True
        timer.start()
        try:
            for name, frame in relations.items():
                conn.register(name, frame)
            result = conn.execute(f"SELECT * FROM ({sql}) AS _q LIMIT {cap + 1}").df()
        except duckdb.InterruptException:
            raise SpecError(
                f"Délai de calcul dépassé ({timeout:g} s).", en=f"Computation time exceeded ({timeout:g} s)."
            ) from None
        except duckdb.Error as exc:
            raise SpecError(f"Erreur SQL : {_short_error(exc)}", en=f"SQL error: {_short_error(exc)}") from None
        finally:
            timer.cancel()
            _slots.release()
            for name in relations:
                try:
                    conn.unregister(name)
                except duckdb.Error:  # pragma: no cover - already gone
                    pass
        if len(result) > cap:
            raise SpecError(
                f"Résultat trop volumineux (plus de {cap} lignes).", en=f"Result too large (more than {cap} rows)."
            )
        return result


def scalar_row(frame: pd.DataFrame) -> dict[str, Any]:
    """First row of a result as ``{column: python value}`` (NaN → None)."""
    if frame.empty:
        return {c: None for c in frame.columns}
    return {c: to_python(frame.iloc[0][c]) for c in frame.columns}


def to_python(value: Any) -> Any:
    """JSON-safe python value: numpy → python, NaN/NaT → None, dates → ISO."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d") if value == value.normalize() else value.isoformat()
    if hasattr(value, "item"):
        value = value.item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value
