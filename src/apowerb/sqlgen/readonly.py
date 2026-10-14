"""Run generated SQL inside a read-only transaction with a statement timeout.

``validate_sql_safety`` narrows what a generated query may contain, but a
deny-list cannot prove a query harmless. The database can: in a read-only
transaction it refuses any write, and a statement timeout cancels a query that
would hold the connection. This is the second line, and the one that does not
depend on the gate being complete.

The connection's own state is restored on exit, because a pooled connection is
handed to the next caller as it was left.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from logging import getLogger
from typing import Any, Iterator

logger = getLogger(__name__)

DEFAULT_STATEMENT_TIMEOUT_S = 30


def statement_timeout_s() -> int:
    """Seconds a generated query may run (``SQL_STATEMENT_TIMEOUT_S``, 0 = off)."""
    raw = os.getenv("SQL_STATEMENT_TIMEOUT_S", "")
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_STATEMENT_TIMEOUT_S
    return value if value >= 0 else DEFAULT_STATEMENT_TIMEOUT_S


@contextmanager
def read_only_transaction(
    conn: Any, cur: Any, db_type: str, timeout_s: int | None = None
) -> Iterator[None]:
    """Open a read-only transaction on ``conn``; roll it back on exit.

    Supports PostgreSQL (psycopg2) and MySQL / MariaDB (pymysql). Statements
    the body runs through ``cur`` happen inside the transaction.

    Precondition: no transaction is open on ``conn`` (a pooled connection in
    autocommit mode, or a fresh one). psycopg2 refuses to leave autocommit
    inside a transaction.

    MySQL limits: a read-only transaction still allows ``SELECT ... INTO
    OUTFILE`` and ``INTO @var``, and ``max_execution_time`` covers SELECT only.
    ``validate_sql_safety`` refuses INTO before the query gets here.
    """
    if timeout_s is None:
        timeout_s = statement_timeout_s()
    db_type = (db_type or "postgresql").lower()
    previous_autocommit = getattr(conn, "autocommit", False)
    try:
        if db_type == "mysql":
            if timeout_s:
                _mysql_timeout(cur, timeout_s)
            cur.execute("START TRANSACTION READ ONLY")
        else:
            # A pooled connection comes in autocommit mode, where each statement
            # is its own transaction and a SET TRANSACTION would not carry over.
            conn.autocommit = False
            cur.execute("SET TRANSACTION READ ONLY")
            if timeout_s:
                cur.execute("SET LOCAL statement_timeout = %s", (timeout_s * 1000,))
        yield
    finally:
        # Never raise from here: on a dropped connection both calls fail, and
        # that must not replace the error the query itself raised.
        try:
            conn.rollback()
        except Exception as exc:
            logger.warning("Rollback of the read-only transaction failed: %s", exc)
        if db_type != "mysql":
            try:
                conn.autocommit = previous_autocommit
            except Exception as exc:
                logger.warning("Could not restore autocommit: %s", exc)


def _mysql_timeout(cur: Any, timeout_s: int) -> None:
    # MySQL 5.7.8+ takes milliseconds and applies to SELECT; MariaDB has its own
    # variable, in seconds. The connection is not pooled, so SESSION ends with it.
    try:
        cur.execute("SET SESSION max_execution_time = %s", (timeout_s * 1000,))
    except Exception:
        try:
            cur.execute("SET SESSION max_statement_time = %s", (timeout_s,))
        except Exception as exc:
            logger.warning(
                "No statement timeout available on this MySQL server: %s", exc
            )
