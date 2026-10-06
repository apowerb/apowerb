"""Shared helpers for the data warehouse tools (Snowflake, Databricks, BigQuery,
Redshift).

No ``tool_*`` function lives here: the module only provides the read-only SQL
guard and the common result shape. The guard is a first line of defence; the
credentials configured for each warehouse should themselves be read-only.
"""

from __future__ import annotations

import re
from typing import Any

# Statements an agent may run against a warehouse. Everything else is refused.
_READ_ONLY_FIRST_TOKENS = frozenset(
    {"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"}
)

# Refused anywhere in the statement, so a CTE or a script cannot smuggle a
# write (``WITH x AS (...) DELETE ...``, BigQuery scripting, ``COPY INTO``).
_FORBIDDEN_KEYWORDS = (
    "INSERT",
    "UPDATE",
    "DELETE",
    "MERGE",
    "UPSERT",
    "COPY",
    "UNLOAD",
    "PUT",
    "REMOVE",
    "DROP",
    "TRUNCATE",
    "ALTER",
    "CREATE",
    "GRANT",
    "REVOKE",
    "EXEC",
    "EXECUTE",
    "CALL",
    "DECLARE",
    "SET",
    "USE",
)

DEFAULT_MAX_ROWS = 100
MAX_ROWS_CAP = 1000
POLL_BUDGET_S = 120
POLL_INTERVAL_S = 1.0


def _strip_literals_and_comments(sql: str) -> str:
    """Blank out string literals, quoted identifiers and comments.

    Keywords inside ``'...'``, ``"..."`` or `` `...` `` or after ``--`` /
    inside ``/* */`` are data, not statements, so they must not trigger the
    keyword check (``WHERE note = 'delete me'``).
    """
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return re.sub(r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|`[^`]*`", "''", sql)


def validate_read_only_sql(sql: str | None) -> str:
    """Return the stripped statement if it is a single read-only query.

    Raises ValueError with a message meant for the agent otherwise.
    """
    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("`sql` is required.")
    statement = sql.strip().rstrip(";").strip()
    code = _strip_literals_and_comments(statement).upper()
    if ";" in code:
        raise ValueError("Only one SQL statement is allowed per call.")
    tokens = code.split()
    if not tokens or tokens[0] not in _READ_ONLY_FIRST_TOKENS:
        first = tokens[0] if tokens else ""
        raise ValueError(
            f"Operation '{first}' is not allowed: these tools are read-only "
            f"({', '.join(sorted(_READ_ONLY_FIRST_TOKENS))})."
        )
    for keyword in _FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{keyword}\b", code):
            raise ValueError(
                f"Query contains forbidden keyword: {keyword}. These tools are "
                "read-only."
            )
    return statement


def clamp_rows(max_rows: int | None) -> int:
    if not isinstance(max_rows, int) or max_rows < 1:
        return DEFAULT_MAX_ROWS
    return min(max_rows, MAX_ROWS_CAP)


def rows_result(
    columns: list[str], rows: list[list[Any]], max_rows: int, total: int | None
) -> dict:
    """Build the common success payload: rows as dicts, capped at max_rows."""
    kept = rows[:max_rows]
    truncated = len(rows) > max_rows or (total is not None and total > len(kept))
    return {
        "status": "success",
        "columns": columns,
        "rows": [dict(zip(columns, row)) for row in kept],
        "row_count": len(kept),
        "total_rows": total,
        "truncated": truncated,
    }


def error_result(exc: Exception, service: str, token_hint: str, logger) -> dict:
    """Map an exception to the tools' error dict, keeping HTTP status if any."""
    import httpx

    if isinstance(exc, (EnvironmentError, ValueError, TimeoutError)):
        return {"status": "error", "error_message": str(exc)}
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        detail = exc.response.text[:500]
        if code in (401, 403):
            detail = f"Authentication failed (check {token_hint}). {detail}"
        logger.warning("[%s] HTTP %s: %s", service.upper(), code, detail)
        return {
            "status": "error",
            "http_status": code,
            "error_message": f"{service} error (HTTP {code}). {detail}",
        }
    logger.warning("[%s] request failed: %s", service.upper(), exc)
    return {"status": "error", "error_message": f"Request to {service} failed: {exc}"}
