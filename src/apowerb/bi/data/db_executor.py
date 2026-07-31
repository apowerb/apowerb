"""
bi/data/db_executor.py
----------------------
DatabaseQueryExecutor — runs SELECT queries against PostgreSQL, MySQL
or Microsoft SQL Server databases using credentials fetched from a tool_config
entry.

Implements the QueryExecutor protocol defined in ``apowerb.bi.data.service``.

Security
--------
- Only SELECT statements are allowed.
- SQL comments are stripped before validation.
- Dangerous keywords (INSERT, UPDATE, DELETE, DROP, ALTER, CREATE, TRUNCATE,
  EXEC, EXECUTE, GRANT, REVOKE, CALL, SET, REPLACE, LOAD, COPY, VACUUM,
  ANALYZE, EXPLAIN) are rejected before execution.
- Row limit is capped at 10 000.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

import asyncpg

from apowerb.bi.charts.core import DataSource
from apowerb.tools_store.tools_helpers import fetch_tool_configs

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional asyncpg connection pooling (opt-in via SQL_POOL_ENABLED) — same flag
# as the text-to-sql psycopg2 pool. The shared OVH Postgres caps connections;
# the default connect/close-per-query has no ceiling. A bounded pool reuses
# connections and caps them (max_size). OFF by default; load-test off-peak
# before enabling. Pools are keyed by (event loop, dsn) so they stay bound to
# the loop that created them.
# ---------------------------------------------------------------------------

_ASYNCPG_POOLS: dict[tuple, asyncpg.Pool] = {}
# One lock PER event loop. A module-level asyncio.Lock() binds to whatever loop
# is current at import time and raises "got Future attached to a different loop"
# from any other loop (tests, multi-loop). Created lazily; the get/set pair has
# no await between them, so it is race-free within a single loop.
_ASYNCPG_LOCKS: dict[int, asyncio.Lock] = {}


def _get_asyncpg_lock() -> asyncio.Lock:
    loop_id = id(asyncio.get_running_loop())
    lock = _ASYNCPG_LOCKS.get(loop_id)
    if lock is None:
        lock = asyncio.Lock()
        _ASYNCPG_LOCKS[loop_id] = lock
    return lock


def _bi_pool_enabled() -> bool:
    return os.getenv("SQL_POOL_ENABLED", "0").strip().lower() in ("1", "true", "yes")


def _bi_pool_max() -> int:
    try:
        return max(1, int(os.getenv("SQL_POOL_MAX", "4")))
    except ValueError:
        return 4


async def _get_asyncpg_pool(dsn: str) -> asyncpg.Pool:
    """Get-or-create a pool for ``dsn`` on the current event loop (once)."""
    key = (id(asyncio.get_running_loop()), dsn)
    pool = _ASYNCPG_POOLS.get(key)
    if pool is None:
        async with _get_asyncpg_lock():
            pool = _ASYNCPG_POOLS.get(key)
            if pool is None:
                pool = await asyncpg.create_pool(
                    dsn, min_size=1, max_size=_bi_pool_max(),
                )
                _ASYNCPG_POOLS[key] = pool
    return pool

# ---------------------------------------------------------------------------
# SQL security — forbidden keywords and comment stripping
# ---------------------------------------------------------------------------

_FORBIDDEN_KEYWORDS: set[str] = {
    "INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE",
    "EXEC", "EXECUTE", "GRANT", "REVOKE", "CALL", "SET",
    "REPLACE", "LOAD", "COPY", "VACUUM", "ANALYZE", "EXPLAIN",
}

_FORBIDDEN_RE = re.compile(
    r"\b(" + "|".join(_FORBIDDEN_KEYWORDS) + r")\b",
    re.IGNORECASE,
)

# Patterns to strip SQL comments before validation
_LINE_COMMENT_RE = re.compile(r"--[^\n]*")
_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)

MAX_ROWS = 10_000


def _strip_comments(query: str) -> str:
    """Remove SQL comments to prevent keyword-hiding tricks."""
    query = _BLOCK_COMMENT_RE.sub(" ", query)
    query = _LINE_COMMENT_RE.sub(" ", query)
    return query


def validate_query(query: str) -> None:
    """Raise ``ValueError`` if the query contains forbidden statements.

    Comments are stripped first to prevent bypass tricks like:
    ``SELECT * FROM t; --DROP TABLE t``
    """
    cleaned = _strip_comments(query)

    # Must start with SELECT (after stripping whitespace and comments)
    if not cleaned.strip().upper().startswith("SELECT"):
        raise ValueError("Query rejected: only SELECT queries are permitted.")

    # Reject forbidden keywords (on comment-stripped version)
    match = _FORBIDDEN_RE.search(cleaned)
    if match:
        raise ValueError(
            f"Query rejected: '{match.group()}' statements are not allowed. "
            "Only SELECT queries are permitted."
        )

    # Reject multiple statements (semicolon followed by non-whitespace)
    # Allow trailing semicolons but not multiple statements
    parts = [p.strip() for p in cleaned.split(";") if p.strip()]
    if len(parts) > 1:
        raise ValueError(
            "Query rejected: multiple statements are not allowed. "
            "Only a single SELECT query is permitted."
        )


# ---------------------------------------------------------------------------
# DatabaseQueryExecutor — supports PostgreSQL and MySQL
# ---------------------------------------------------------------------------


class DatabaseQueryExecutor:
    """
    Async database query executor that resolves connection credentials
    from a ``tool_config_id``.

    Supports:
    - PostgreSQL (via asyncpg)
    - MySQL (via pymysql in a thread)
    - SQL Server (via pyodbc in a thread)

    Parameters
    ----------
    tool_config_id : str
        The tool_config identifier whose decrypted params are expected to
        contain ``host``, ``port``, ``database``, ``user``, and ``password``.
    owner_id : str
        Email of the requesting user. Scopes the tool_config lookup so a
        chart cannot leak another tenant's DB credentials.
    """

    def __init__(self, tool_config_id: str, owner_id: str) -> None:
        self._tool_config_id = tool_config_id
        self._owner_id = owner_id

    async def run(self, source: DataSource) -> list[dict[str, Any]]:
        query = source.query.strip()
        validate_query(query)

        limit = min(source.limit or MAX_ROWS, MAX_ROWS)
        config = self._get_config()
        db_type = self._detect_db_type(config)

        if db_type == "mysql":
            return await self._run_mysql(query, limit, config)
        if db_type == "mssql":
            return await self._run_mssql(query, limit, config)
        return await self._run_postgres(query, limit, config)

    # ------------------------------------------------------------------
    # PostgreSQL executor
    # ------------------------------------------------------------------

    async def _run_postgres(
        self, query: str, limit: int, config: dict
    ) -> list[dict[str, Any]]:
        dsn = self._build_dsn(config, "postgresql")
        clean_query = query.rstrip().rstrip(";")
        limited_query = f"SELECT * FROM ({clean_query}) AS _q LIMIT {limit}"

        conn: asyncpg.Connection | None = None
        pool: asyncpg.Pool | None = None
        params = config["tool_config_params"]
        schema = params.get("schema") or params.get("DB_SCHEMA", "")
        try:
            if _bi_pool_enabled():
                pool = await _get_asyncpg_pool(dsn)
                conn = await pool.acquire()
            else:
                conn = await asyncpg.connect(dsn)
            if schema:
                await conn.execute(f"SET search_path TO {schema}, public")
            elif pool is not None:
                # A reused pooled connection may carry a prior request's
                # search_path; reset it so an unqualified query is not resolved
                # against the wrong schema.
                await conn.execute("SET search_path TO public")
            records = await conn.fetch(limited_query)
            return [dict(r) for r in records]
        except asyncpg.PostgresError as exc:
            logger.error(
                "PostgreSQL query failed (config=%s): %s",
                self._tool_config_id, exc,
            )
            raise RuntimeError(f"PostgreSQL query error: {exc}") from exc
        except OSError as exc:
            logger.error(
                "PostgreSQL connection failed (config=%s): %s",
                self._tool_config_id, exc,
            )
            raise RuntimeError(
                f"Could not connect to PostgreSQL (config={self._tool_config_id}): {exc}"
            ) from exc
        finally:
            if conn is not None:
                try:
                    if pool is not None:
                        await pool.release(conn)
                    else:
                        await conn.close()
                except Exception as rel_exc:  # never mask the real result/error
                    logger.warning(
                        "PostgreSQL connection release failed (config=%s): %s",
                        self._tool_config_id, rel_exc,
                    )

    # ------------------------------------------------------------------
    # MySQL executor
    # ------------------------------------------------------------------

    async def _run_mysql(
        self, query: str, limit: int, config: dict
    ) -> list[dict[str, Any]]:
        import asyncio
        import pymysql

        params = config["tool_config_params"]
        host = params.get("host") or params.get("DB_HOST", "localhost")
        port = int(params.get("port") or params.get("DB_PORT", "3306"))
        database = params.get("database") or params.get("dbname") or params.get("DB_NAME", "")
        user = params.get("user") or params.get("username") or params.get("DB_USER", "")
        password = params.get("password") or params.get("DB_PASSWORD", "")
        ssl = params.get("sslmode") or params.get("ssl") or params.get("DB_SSLMODE", "")

        clean_query = query.rstrip().rstrip(";")
        limited_query = f"SELECT * FROM ({clean_query}) AS _q LIMIT {limit}"

        def _sync_query():
            connect_kwargs = dict(
                host=host,
                port=port,
                user=user,
                password=password,
                database=database,
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                connect_timeout=10,
            )
            if ssl and ssl not in ("disable", "false", "none"):
                connect_kwargs["ssl"] = {"ssl": True}

            conn = pymysql.connect(**connect_kwargs)
            try:
                with conn.cursor() as cursor:
                    cursor.execute(limited_query)
                    return cursor.fetchall()
            finally:
                conn.close()

        try:
            return await asyncio.to_thread(_sync_query)
        except pymysql.MySQLError as exc:
            logger.error(
                "MySQL query failed (config=%s): %s",
                self._tool_config_id, exc,
            )
            raise RuntimeError(f"MySQL query error: {exc}") from exc
        except OSError as exc:
            logger.error(
                "MySQL connection failed (config=%s): %s",
                self._tool_config_id, exc,
            )
            raise RuntimeError(
                f"Could not connect to MySQL (config={self._tool_config_id}): {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_config(self) -> dict:
        """Fetch and validate tool config."""
        config = fetch_tool_configs(self._tool_config_id, owner_id=self._owner_id)
        if not config or "tool_config_params" not in config:
            raise RuntimeError(
                f"Tool config '{self._tool_config_id}' not found or has no parameters."
            )
        params = config["tool_config_params"]
        if not isinstance(params, dict):
            raise RuntimeError(
                f"Tool config '{self._tool_config_id}' params are not a valid dict."
            )
        return config

    def _detect_db_type(self, config: dict) -> str:
        """Detect database type from tool config params."""
        params = config["tool_config_params"]

        # Explicit db_type field (supports lowercase 'db_type' and uppercase 'DB_TYPE')
        db_type = (params.get("db_type") or params.get("DB_TYPE") or "").lower()
        if db_type:
            if db_type in ("mysql", "mariadb"):
                return "mysql"
            if db_type in ("mssql", "sqlserver", "sql_server"):
                return "mssql"
            return "postgresql"

        # Infer from port (lowercase 'port' or uppercase 'DB_PORT')
        port = str(params.get("port") or params.get("DB_PORT") or "")
        if port == "3306":
            return "mysql"
        if port == "1433":
            return "mssql"

        # Also check tool_category or tool_name from config metadata
        category = config.get("tool_category", "").lower()
        tool_name = config.get("tool_name", "").lower()
        if "mysql" in category or "mysql" in tool_name:
            return "mysql"
        if "mssql" in category or "sqlserver" in category or "mssql" in tool_name:
            return "mssql"

        return "postgresql"


    # ------------------------------------------------------------------
    # SQL Server executor
    # ------------------------------------------------------------------

    async def _run_mssql(
        self, query: str, limit: int, config: dict
    ) -> list[dict[str, Any]]:
        """Run a SELECT on a Microsoft SQL Server database via pyodbc.

        Uses asyncio.to_thread() because pyodbc is synchronous. The query is
        wrapped with SELECT TOP <limit> * FROM (...) AS _q to enforce the
        row cap, mirroring the pattern used for PostgreSQL/MySQL.
        """
        import asyncio
        try:
            import pyodbc  # noqa: WPS433 — lazy import; pyodbc is optional
        except ImportError as exc:  # pragma: no cover — env-dependent
            raise RuntimeError(
                f"pyodbc is required for MSSQL but is not installed "
                f"(config={self._tool_config_id}): {exc}"
            ) from exc

        params = config["tool_config_params"]
        host = params.get("host") or params.get("DB_HOST", "localhost")
        port = int(params.get("port") or params.get("DB_PORT", "1433"))
        database = (
            params.get("database")
            or params.get("dbname")
            or params.get("DB_NAME", "")
        )
        user = (
            params.get("user")
            or params.get("username")
            or params.get("DB_USER", "")
        )
        password = params.get("password") or params.get("DB_PASSWORD", "")
        driver = (
            params.get("driver")
            or params.get("DB_ODBC_DRIVER")
            or "ODBC Driver 18 for SQL Server"
        )
        encrypt = params.get("encrypt") or params.get("DB_ENCRYPT", "no")
        trust = (
            params.get("trust_server_certificate")
            or params.get("DB_TRUST_SERVER_CERTIFICATE", "yes")
        )

        clean_query = query.rstrip().rstrip(";").strip()

        # T-SQL refuses ORDER BY inside subqueries unless they also have TOP / OFFSET / FOR XML.
        # So we cannot blindly wrap user queries with ORDER BY in "SELECT * FROM (...) AS _q".
        # Three branches:
        #   1) User query already starts with SELECT TOP <n>  -> leave untouched (user controls cap).
        #   2) CTE (WITH ...) followed by ORDER BY at top level -> append OFFSET 0 ROWS FETCH NEXT.
        #   3) Plain SELECT -> inject "SELECT TOP {limit}" after the first SELECT keyword (no wrap).
        if re.match(r"^\s*SELECT\s+TOP\b", clean_query, re.IGNORECASE):
            limited_query = clean_query
        elif re.match(r"^\s*WITH\b", clean_query, re.IGNORECASE):
            if re.search(r"\bORDER\s+BY\b[^()]*$", clean_query, re.IGNORECASE):
                limited_query = (
                    f"{clean_query} OFFSET 0 ROWS FETCH NEXT {limit} ROWS ONLY"
                )
            else:
                limited_query = (
                    f"SELECT TOP {limit} * FROM ({clean_query}) AS _q"
                )
        else:
            limited_query = re.sub(
                r"\bSELECT\b",
                f"SELECT TOP {limit}",
                clean_query,
                count=1,
                flags=re.IGNORECASE,
            )

        conn_str = (
            f"DRIVER={{{driver}}};"
            f"SERVER={host},{port};DATABASE={database};"
            f"UID={user};PWD={password};"
            f"Encrypt={encrypt};TrustServerCertificate={trust};"
        )

        def _sync_query() -> list[dict[str, Any]]:
            conn = pyodbc.connect(conn_str, timeout=10)
            try:
                cursor = conn.cursor()
                cursor.execute(limited_query)
                cols = [d[0] for d in cursor.description]
                return [dict(zip(cols, row)) for row in cursor.fetchall()]
            finally:
                conn.close()

        try:
            return await asyncio.to_thread(_sync_query)
        except pyodbc.Error as exc:
            logger.error(
                "MSSQL query failed (config=%s): %s",
                self._tool_config_id, exc,
            )
            raise RuntimeError(
                f"MSSQL query error (config={self._tool_config_id}): {exc}"
            ) from exc

    @staticmethod
    def _build_dsn(config: dict, driver: str) -> str:
        """Build a DSN string for the given driver."""
        params = config["tool_config_params"]
        # Support both lowercase (host) and uppercase/prefixed (DB_HOST) keys
        host = params.get("host") or params.get("DB_HOST", "localhost")
        port = params.get("port") or params.get("DB_PORT", "5432")
        database = params.get("database") or params.get("dbname") or params.get("DB_NAME", "")
        user = params.get("user") or params.get("username") or params.get("DB_USER", "")
        password = params.get("password") or params.get("DB_PASSWORD", "")

        if not database or not user:
            raise RuntimeError(
                "Missing required database connection parameters (database, user)."
            )

        dsn = f"{driver}://{user}:{password}@{host}:{port}/{database}"
        sslmode = params.get("sslmode") or params.get("DB_SSLMODE", "")
        if sslmode and sslmode not in ("disable", "false", "none"):
            dsn += f"?sslmode={sslmode}"
        return dsn
