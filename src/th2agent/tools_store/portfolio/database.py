import os
import re
from decimal import Decimal
from datetime import datetime, date
from logging import getLogger

logger = getLogger(__name__)


class _PyodbcDictCursor:
    """Adapt a pyodbc cursor so ``fetchall``/``fetchmany`` return ``list[dict]``,
    matching the semantics of ``psycopg2.RealDictCursor`` and
    ``pymysql.cursors.DictCursor``. Lets the rest of this module treat all
    three back-ends uniformly.
    """

    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, sql, *args, **kwargs):
        return self._cursor.execute(sql, *args, **kwargs)

    def _to_dicts(self, rows):
        if not rows:
            return []
        cols = [d[0] for d in self._cursor.description]
        return [dict(zip(cols, row)) for row in rows]

    def fetchall(self):
        return self._to_dicts(self._cursor.fetchall())

    def fetchmany(self, size):
        return self._to_dicts(self._cursor.fetchmany(size))

    def fetchone(self):
        row = self._cursor.fetchone()
        if row is None:
            return None
        cols = [d[0] for d in self._cursor.description]
        return dict(zip(cols, row))

    def close(self):
        return self._cursor.close()

    @property
    def description(self):
        return self._cursor.description

    @property
    def rowcount(self):
        return self._cursor.rowcount


def _serialize_row(row: dict) -> dict:
    """Convert non-JSON-serializable types to strings."""
    result = {}
    for key, value in row.items():
        if isinstance(value, Decimal):
            result[key] = str(value)
        elif isinstance(value, (datetime, date)):
            result[key] = value.isoformat()
        elif value is None:
            result[key] = None
        else:
            result[key] = value
    return result


def _get_db_type() -> str:
    return os.getenv("DB_TYPE", "postgresql").lower()


def _get_connection():
    host = os.getenv("DB_HOST", "localhost")
    database = os.getenv("DB_NAME")
    user = os.getenv("DB_USER")
    password = os.getenv("DB_PASSWORD")

    if not all([database, user, password]):
        raise ValueError("Missing required database credentials (DB_NAME, DB_USER, DB_PASSWORD)")

    db_type = _get_db_type()

    if db_type == "mysql":
        import pymysql
        port = int(os.getenv("DB_PORT", "3306"))
        return pymysql.connect(
            host=host, port=port, database=database,
            user=user, password=password,
            cursorclass=pymysql.cursors.DictCursor,
        )
    elif db_type in ("mssql", "sqlserver"):
        import pyodbc
        port = int(os.getenv("DB_PORT", "1433"))
        driver = os.getenv("DB_ODBC_DRIVER", "ODBC Driver 18 for SQL Server")
        encrypt = os.getenv("DB_ENCRYPT", "no")  # LAN/VPN by default
        trust = os.getenv("DB_TRUST_SERVER_CERTIFICATE", "yes")
        conn_str = (
            f"DRIVER={{{driver}}};"
            f"SERVER={host},{port};DATABASE={database};"
            f"UID={user};PWD={password};"
            f"Encrypt={encrypt};TrustServerCertificate={trust};"
        )
        return pyodbc.connect(conn_str)
    else:
        # Default: PostgreSQL
        import psycopg2
        from psycopg2.extras import RealDictCursor
        port = int(os.getenv("DB_PORT", "5432"))
        conn = psycopg2.connect(
            host=host, port=port, database=database, user=user, password=password
        )
        conn._cursor_factory = RealDictCursor  # store for later use
        return conn


def _get_cursor(conn):
    """Return a dict-cursor regardless of DB type."""
    db_type = _get_db_type()
    if db_type == "mysql":
        return conn.cursor()  # DictCursor already set at connect time
    elif db_type in ("mssql", "sqlserver"):
        return _PyodbcDictCursor(conn.cursor())
    else:
        from psycopg2.extras import RealDictCursor
        return conn.cursor(cursor_factory=RealDictCursor)


# ---------------------------------------------------------------------------
# SQL safety gate
# ---------------------------------------------------------------------------

# Operations a config can opt into via DB_ALLOWED_OPS.
_OPT_IN_OPS: frozenset[str] = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"})

# Operations rejected unconditionally — no config switch can re-enable them.
# Schema-altering / privilege-altering / arbitrary code execution stays out
# of every tool_run_sql call, full stop.
_NEVER_ALLOWED_OPS: tuple[str, ...] = (
    "DROP", "TRUNCATE", "ALTER", "CREATE",
    "GRANT", "REVOKE", "EXEC", "EXECUTE",
)


def _execute_and_format(conn, cursor, sql_stripped: str, db_type: str | None) -> dict:
    """Execute the prepared statement and return a tool-friendly dict.

    Splits on the first SQL token:
      - ``SELECT``      → ``cursor.fetchall()`` then return the rows.
      - everything else (``INSERT`` / ``UPDATE`` / ``DELETE``) →
        ``conn.commit()`` and return ``rows_affected``. Calling
        ``fetchall()`` on a write would raise *"No results. Previous SQL
        was not a query"* (live regression on agent6 2026-05-07 17:29 UTC,
        27 tool calls every one of them failing on this exact pyodbc
        message). The commit is mandatory for SQL Server / psycopg2 /
        pymysql which default to ``autocommit=False`` — without it the
        INSERT/UPDATE never lands even when the cursor accepts it.
    """
    cursor.execute(sql_stripped)
    op = sql_stripped.lstrip().upper().split(None, 1)[0] if sql_stripped.strip() else ""

    if op == "SELECT":
        rows = cursor.fetchall()
        _sql_max_rows_raw = os.getenv("SQL_MAX_ROWS", "150")
        try:
            max_rows = int(_sql_max_rows_raw)
        except (ValueError, TypeError):
            logger.warning(
                "[SQL] SQL_MAX_ROWS=%r invalide, fallback sur 150", _sql_max_rows_raw
            )
            max_rows = 150
        if len(rows) > max_rows:
            logger.warning("[SQL] result truncated %d->%d rows", len(rows), max_rows)
            rows = rows[:max_rows]
        cursor.close()
        serialized = [_serialize_row(dict(r)) for r in rows]
        return {
            "success": True,
            "sql": sql_stripped,
            "db_type": db_type,
            "row_count": len(serialized),
            "data": serialized,
        }

    affected = cursor.rowcount
    cursor.close()
    conn.commit()
    return {
        "success": True,
        "sql": sql_stripped,
        "db_type": db_type,
        "operation": op,
        "rows_affected": affected,
    }


def _parse_allowed_ops(raw: str | None) -> frozenset[str]:
    """Parse a DB_ALLOWED_OPS string ('SELECT,INSERT,UPDATE') into a set.

    Defaults to {SELECT} when the value is empty / malformed. Tokens not in
    ``_OPT_IN_OPS`` are dropped silently — destructive ops cannot be opted in
    via config, and unknown tokens are user typos.
    """
    if not raw:
        return frozenset({"SELECT"})
    parts = (p.strip().upper() for p in raw.split(","))
    allowed = frozenset(p for p in parts if p in _OPT_IN_OPS)
    return allowed or frozenset({"SELECT"})


def _validate_sql_against_whitelist(
    sql: str,
    allowed_ops: frozenset[str],
) -> dict | None:
    """Return None if the query is allowed, or an error dict to short-circuit.

    Two-stage check:
      1. The first SQL token must be in ``allowed_ops``.
      2. ``_NEVER_ALLOWED_OPS`` keywords are forbidden anywhere in the
         statement (defends against ``WITH x AS (DROP ...)`` and similar).
    """
    sql_upper = sql.strip().upper()
    if not sql_upper:
        return {"success": False, "sql": sql, "error": "Empty SQL query."}

    first_token = sql_upper.split(None, 1)[0]
    if first_token not in allowed_ops:
        ops_str = ", ".join(sorted(allowed_ops))
        return {
            "success": False,
            "sql": sql,
            "error": (
                f"Operation '{first_token}' is not allowed for this tool. "
                f"Allowed: {ops_str}."
            ),
        }

    for never in _NEVER_ALLOWED_OPS:
        if re.search(rf"\b{never}\b", sql_upper):
            return {
                "success": False,
                "sql": sql,
                "error": (
                    f"Query contains forbidden keyword: {never}. "
                    "Schema-altering and privilege-altering operations "
                    "are never allowed, regardless of configuration."
                ),
            }
    return None


def tool_run_sql(sql: str) -> dict:
    """
    Executes any read-only (SELECT) SQL query against the database.
    This is the primary tool for answering data questions accurately.
    Use it for: COUNT, GROUP BY, JOINs, filters, ORDER BY, aggregations, subqueries, etc.

    For PostgreSQL: DB_SCHEMA env var sets the default search_path.
    For MySQL: DB_NAME is used as the target database.

    Args:
        sql (str): A valid SELECT query (PostgreSQL or MySQL depending on DB_TYPE).

    Environment Variables:
        DB_TYPE     : 'postgresql' (default) or 'mysql'
        DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
        DB_SCHEMA   : PostgreSQL only — sets search_path (default: public)

    Returns:
        dict: {
            "success": bool,
            "sql": str,
            "row_count": int,
            "data": list[dict],
            "error": str  # only on failure
        }
    """
    try:
        sql_stripped = sql.strip()
        db_type = _get_db_type()

        allowed_ops = _parse_allowed_ops(os.getenv("DB_ALLOWED_OPS"))
        err = _validate_sql_against_whitelist(sql_stripped, allowed_ops)
        if err is not None:
            return err

        conn = _get_connection()
        try:
            cursor = _get_cursor(conn)

            if db_type == "postgresql":
                db_schema = os.getenv("DB_SCHEMA", "public")
                cursor.execute(f'SET search_path TO "{db_schema}", public')

            return _execute_and_format(conn, cursor, sql_stripped, db_type)
        finally:
            conn.close()

    except ValueError as e:
        return {"success": False, "sql": sql, "error": str(e)}
    except Exception as e:
        return {"success": False, "sql": sql, "error": f"Unexpected error: {str(e)}"}


def tool_db(table_name: str, limit: int = 10):
    """
    Connects to the configured database and retrieves a sample of rows from a target table.
    Supports PostgreSQL and MySQL via the DB_TYPE environment variable.

    Args:
        table_name (str): Name of the table to query
        limit (int): Number of items to retrieve (default: 10)

    Environment Variables:
        DB_TYPE     : 'postgresql' (default) or 'mysql'
        DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

    Returns:
        dict: Success status with data or error message
    """
    try:
        conn = _get_connection()
        cursor = _get_cursor(conn)

        query = f"SELECT * FROM {table_name} LIMIT %s"
        cursor.execute(query, (limit,))
        results = cursor.fetchall()

        cursor.close()
        conn.close()

        return {
            "success": True,
            "table": table_name,
            "count": len(results),
            "limit": limit,
            "data": [dict(row) for row in results],
        }

    except Exception as e:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


_SLUG_INVALID = re.compile(r"[^a-z0-9_]+")


def _slugify_db_name(name: str) -> str:
    """Turn a DB name into a Python identifier-safe suffix (max 32 chars)."""
    s = (name or "db").strip().lower().replace("-", "_").replace(" ", "_")
    s = _SLUG_INVALID.sub("", s)
    s = s.strip("_") or "db"
    return s[:32]


def make_database_tools(
    agent_name: str,
    db_params: dict | None = None,
    name_suffix: str = "",
) -> list:
    """Return tool_run_sql and tool_db bound to specific DB credentials.

    If db_params is provided, the tools use those credentials directly
    (no os.environ dependency). If db_params is None, falls back to os.environ.

    When ``name_suffix`` is non-empty, the returned closures are renamed to
    ``tool_run_sql_<suffix>`` / ``tool_db_<suffix>``. This is required when an
    agent has more than one DB config attached: every Python ``__name__`` ADK
    pushes to Gemini must be unique, otherwise Vertex AI rejects the request
    with ``Duplicate function declaration``. The suffix is also surfaced in
    the docstring so the LLM knows which database each tool targets.
    """
    if db_params:
        bound_db_type = db_params.get("DB_TYPE", "postgresql").lower()
        bound_db_schema = db_params.get("DB_SCHEMA", "public")
        bound_allowed_ops = _parse_allowed_ops(db_params.get("DB_ALLOWED_OPS"))

        def _bound_get_connection():
            host = db_params.get("DB_HOST", "localhost")
            database = db_params.get("DB_NAME")
            user = db_params.get("DB_USER")
            password = db_params.get("DB_PASSWORD")
            if not all([database, user, password]):
                raise ValueError(
                    "Missing required database credentials (DB_NAME, DB_USER, DB_PASSWORD)"
                )
            if bound_db_type == "mysql":
                import pymysql
                port = int(db_params.get("DB_PORT", "3306"))
                return pymysql.connect(
                    host=host, port=port, database=database,
                    user=user, password=password,
                    cursorclass=pymysql.cursors.DictCursor,
                )
            elif bound_db_type in ("mssql", "sqlserver"):
                import pyodbc
                port = int(db_params.get("DB_PORT", "1433"))
                driver = db_params.get("DB_ODBC_DRIVER", "ODBC Driver 18 for SQL Server")
                encrypt = db_params.get("DB_ENCRYPT", "no")
                trust = db_params.get("DB_TRUST_SERVER_CERTIFICATE", "yes")
                conn_str = (
                    f"DRIVER={{{driver}}};"
                    f"SERVER={host},{port};DATABASE={database};"
                    f"UID={user};PWD={password};"
                    f"Encrypt={encrypt};TrustServerCertificate={trust};"
                )
                return pyodbc.connect(conn_str)
            else:
                import psycopg2
                from psycopg2.extras import RealDictCursor
                port = int(db_params.get("DB_PORT", "5432"))
                conn = psycopg2.connect(
                    host=host, port=port, database=database,
                    user=user, password=password,
                )
                conn._cursor_factory = RealDictCursor
                return conn

        def _bound_get_cursor(conn):
            if bound_db_type == "mysql":
                return conn.cursor()
            elif bound_db_type in ("mssql", "sqlserver"):
                return _PyodbcDictCursor(conn.cursor())
            else:
                from psycopg2.extras import RealDictCursor
                return conn.cursor(cursor_factory=RealDictCursor)

        db_status = f"db={db_params.get('DB_NAME')!r} type={bound_db_type!r}"
    else:
        _bound_get_connection = _get_connection
        _bound_get_cursor = _get_cursor
        bound_db_type = None
        bound_db_schema = None
        bound_allowed_ops = _parse_allowed_ops(os.getenv("DB_ALLOWED_OPS"))
        db_status = "no DB configured — using env vars"

    logger.info(
        "[DATABASE] make_database_tools agent=%s %s allowed_ops=%s",
        agent_name, db_status, sorted(bound_allowed_ops),
    )

    def tool_run_sql(sql: str) -> dict:
        """
        Executes any read-only (SELECT) SQL query against the database.
        This is the primary tool for answering data questions accurately.
        Use it for: COUNT, GROUP BY, JOINs, filters, ORDER BY, aggregations, subqueries, etc.

        For PostgreSQL: DB_SCHEMA env var sets the default search_path.
        For MySQL: DB_NAME is used as the target database.

        Args:
            sql (str): A valid SELECT query (PostgreSQL or MySQL depending on DB_TYPE).

        Environment Variables:
            DB_TYPE     : 'postgresql' (default) or 'mysql'
            DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
            DB_SCHEMA   : PostgreSQL only — sets search_path (default: public)

        Returns:
            dict: {
                "success": bool,
                "sql": str,
                "row_count": int,
                "data": list[dict],
                "error": str  # only on failure
            }
        """
        try:
            sql_stripped = sql.strip()
            db_type = bound_db_type or _get_db_type()

            # Log the query for observability (incident 2026-05-13 ORDER_NOT_FOUND
            # false positives — needed to see exactly what the LLM is sending).
            db_label = (db_params or {}).get("DB_NAME") if db_params else None
            tool_label = f"tool_run_sql_{name_suffix}" if name_suffix else "tool_run_sql"
            logger.info(
                "[DATABASE] %s exec db=%r type=%r sql=%r",
                tool_label, db_label, db_type, sql_stripped[:1000],
            )

            err = _validate_sql_against_whitelist(sql_stripped, bound_allowed_ops)
            if err is not None:
                return err

            conn = _bound_get_connection()
            try:
                cursor = _bound_get_cursor(conn)

                if db_type == "postgresql":
                    db_schema = bound_db_schema or os.getenv("DB_SCHEMA", "public")
                    cursor.execute(f'SET search_path TO "{db_schema}", public')

                return _execute_and_format(conn, cursor, sql_stripped, db_type)
            finally:
                conn.close()

        except ValueError as e:
            return {"success": False, "sql": sql, "error": str(e)}
        except Exception as e:
            return {"success": False, "sql": sql, "error": f"Unexpected error: {str(e)}"}

    def tool_db(table_name: str, limit: int = 10) -> dict:
        """
        Connects to the configured database and retrieves a sample of rows from a target table.
        Supports PostgreSQL and MySQL via the DB_TYPE environment variable.

        Args:
            table_name (str): Name of the table to query
            limit (int): Number of items to retrieve (default: 10)

        Environment Variables:
            DB_TYPE     : 'postgresql' (default) or 'mysql'
            DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD

        Returns:
            dict: Success status with data or error message
        """
        try:
            conn = _bound_get_connection()
            cursor = _bound_get_cursor(conn)
            db_type = bound_db_type or _get_db_type()

            if db_type in ("mssql", "sqlserver"):
                # SQL Server uses TOP n, not LIMIT, and the value must be a literal.
                safe_limit = max(1, int(limit))
                query = f"SELECT TOP {safe_limit} * FROM {table_name}"
                cursor.execute(query)
            else:
                query = f"SELECT * FROM {table_name} LIMIT %s"
                cursor.execute(query, (limit,))
            results = cursor.fetchall()

            cursor.close()
            conn.close()

            return {
                "success": True,
                "table": table_name,
                "count": len(results),
                "limit": limit,
                "data": [dict(row) for row in results],
            }

        except Exception as e:
            return {"success": False, "error": f"Unexpected error: {str(e)}"}

    if name_suffix:
        db_label = (db_params or {}).get("DB_NAME", name_suffix)
        tool_run_sql.__name__ = f"tool_run_sql_{name_suffix}"
        tool_db.__name__ = f"tool_db_{name_suffix}"
        ops_label = "/".join(sorted(bound_allowed_ops))
        # Rewrite the first sentence of the docstring so the LLM
        # disambiguates which database each tool targets and which
        # operations it is allowed to send.
        tool_run_sql.__doc__ = (
            f"Execute a SQL query against the `{db_label}` database "
            f"({bound_db_type or 'postgresql'}). "
            f"Allowed operations: {ops_label}.\n"
            f"\n"
            f"Use this tool when the question is about data stored in "
            f"`{db_label}`. Other DB tools on this agent target different "
            f"databases — pick the right one based on the table you need.\n"
            + (tool_run_sql.__doc__ or "")
        )
        tool_db.__doc__ = (
            f"Sample rows from a table in the `{db_label}` database "
            f"({bound_db_type or 'postgresql'}).\n"
            + (tool_db.__doc__ or "")
        )

    return [tool_run_sql, tool_db]
