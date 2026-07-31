import os
import re
import datetime
import time as _time
from decimal import Decimal
import threading
from dataclasses import dataclass, field
import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
import litellm
from typing import Dict, Any, List, Optional
from logging import getLogger
from apowerb.sqlgen.generator import extract_sql
from apowerb.sqlgen.safety import validate_sql_safety
from apowerb.sqlgen.schema_format import build_schema_prompt

logger = getLogger(__name__)

# ── Tables to exclude from schema introspection ──
_EXCLUDED_TABLE_PREFIXES = (
    "admin_event_entity",
    "associated_policy",
    "authentication_",
    "broker_link",
    "client",
    "component",
    "composite_role",
    "credential",
    "databasechangelog",
    "default_client_scope",
    "event_entity",
    "fed_",
    "federated_",
    "group_",
    "identity_provider",
    "keycloak_",
    "migration_model",
    "offline_",
    "policy_config",
    "protocol_mapper",
    "realm",
    "redirect_uris",
    "required_action",
    "resource_attribute",
    "resource_policy",
    "resource_scope",
    "resource_server",
    "resource_uris",
    "revoked_token",
    "role_attribute",
    "scope_mapping",
    "scope_policy",
    "user_attribute",
    "user_consent",
    "user_entity",
    "user_federation",
    "user_group_membership",
    "user_required_action",
    "user_role_mapping",
    "username_login_failure",
    "web_origins",
)

_EXCLUDED_TABLES_EXACT = {
    "pwd_mngt",
    "th2dive_cookies_sessions",
    "th2dive_with_cookies",
}


def _is_excluded_table(table_name: str) -> bool:
    if table_name in _EXCLUDED_TABLES_EXACT:
        return True
    return any(table_name.startswith(p) for p in _EXCLUDED_TABLE_PREFIXES)



def _json_safe(obj):
    if isinstance(obj, (datetime.date, datetime.datetime)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj) if obj.is_finite() else str(obj)
    if isinstance(obj, bytes):
        return "<binary>"
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _norm(rows) -> list:
    """Normalise all column-name keys to lowercase (MySQL returns them in uppercase)."""
    return [{k.lower(): v for k, v in dict(row).items()} for row in rows]


def _validate_sql_safety(sql_query: str) -> tuple[bool, Optional[str]]:
    """Backward-compat wrapper; logic lives in apowerb.sqlgen.safety."""
    return validate_sql_safety(sql_query)


def _get_db_connection():
    """Get a DB connection using os.getenv credentials (backward compat)."""
    host     = os.getenv("DB_HOST", "localhost")
    port     = int(os.getenv("DB_PORT", "5432"))
    database = os.getenv("DB_NAME")
    user     = os.getenv("DB_USER")
    password = os.getenv("DB_PASSWORD")
    if not all([database, user, password]):
        raise ValueError("Missing required database credentials (DB_NAME, DB_USER, DB_PASSWORD)")
    return psycopg2.connect(host=host, port=port, database=database, user=user, password=password)


def _get_database_schema() -> Dict[str, Any]:
    """Fetch DB schema using os.getenv credentials (backward compat)."""
    db_schema          = os.getenv("DB_SCHEMA", "public")
    include_tables_env = os.getenv("DB_INCLUDE_TABLES", "")
    conn               = _get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = %s AND table_type = 'BASE TABLE'
            ORDER BY table_name;
        """, (db_schema,))
        tables = _norm(cur.fetchall())
        if include_tables_env.strip():
            wl = {t.strip() for t in include_tables_env.split(",") if t.strip()}
            tables = [t for t in tables if t["table_name"] in wl]
        else:
            tables = [t for t in tables if not _is_excluded_table(t["table_name"])]
        schema_info: Dict[str, Any] = {"tables": {}, "schema": db_schema}
        for tbl in tables:
            tn = tbl["table_name"]
            cur.execute("""
                SELECT column_name, data_type, is_nullable, column_default
                FROM information_schema.columns
                WHERE table_schema = %s AND table_name = %s
                ORDER BY ordinal_position;
            """, (db_schema, tn))
            columns = _norm(cur.fetchall())
            cur.execute("""
                SELECT kcu.column_name
                FROM information_schema.table_constraints tc
                JOIN information_schema.key_column_usage kcu
                    ON tc.constraint_name = kcu.constraint_name
                    AND tc.table_schema = kcu.table_schema
                WHERE tc.table_schema = %s AND tc.table_name = %s
                    AND tc.constraint_type = 'PRIMARY KEY';
            """, (db_schema, tn))
            primary_keys = [r["column_name"] for r in _norm(cur.fetchall())]
            cur.execute("""
                SELECT DISTINCT kcu.column_name,
                    ccu.table_name AS foreign_table_name,
                    ccu.column_name AS foreign_column_name
                FROM information_schema.table_constraints AS tc
                JOIN information_schema.key_column_usage AS kcu
                    ON tc.constraint_name = kcu.constraint_name
                    AND tc.table_schema = kcu.table_schema
                JOIN information_schema.constraint_column_usage AS ccu
                    ON ccu.constraint_name = tc.constraint_name
                    AND ccu.table_schema = tc.table_schema
                WHERE tc.table_schema = %s AND tc.table_name = %s
                    AND tc.constraint_type = 'FOREIGN KEY';
            """, (db_schema, tn))
            foreign_keys = _norm(cur.fetchall())
            try:
                cur.execute(f'SELECT * FROM "{db_schema}"."{tn}" LIMIT 3;')
                sample_data = [_json_safe(dict(r)) for r in cur.fetchall()]
            except Exception:
                sample_data = []
            schema_info["tables"][tn] = {
                "columns":      [dict(c) for c in columns],
                "primary_keys": primary_keys,
                "foreign_keys": [dict(fk) for fk in foreign_keys],
                "sample_data":  sample_data,
            }
        cur.close()
        logger.info("[SCHEMA] Module-level: %d tables in '%s'", len(schema_info["tables"]), db_schema)
        return schema_info
    except Exception as e:
        logger.error("[SCHEMA] Module-level error: %s", e)
        raise
    finally:
        conn.close()


def invalidate_schema_cache():
    """Flush every registered agent's cached schema.

    Call after a DDL change or a DB tool-config update so the next query
    re-introspects. Per-agent refresh is also available via
    tool_get_database_schema(refresh=True).

    Note: the legacy _get_database_schema() path used by rag/index_db.py does
    NOT use this registry — it re-introspects on every call — so it is
    unaffected (and was already, when this was a no-op).
    """
    for st in _state_registry.values():
        with st.lock:
            st.cache = {}
            st.cache_ts = 0.0


def _get_cached_schema() -> Dict[str, Any]:
    """Fetch schema using os.getenv credentials (backward compat for rag.py)."""
    return _get_database_schema()


def _execute_sql_query(sql_query: str, limit: int = 100) -> List[Dict[str, Any]]:
    """Execute SQL using os.getenv credentials (backward compat for rag.py)."""
    conn = _get_db_connection()
    try:
        cur = conn.cursor(cursor_factory=RealDictCursor)
        if "LIMIT" not in sql_query.upper():
            sql_query = f"{sql_query.rstrip(';')} LIMIT {limit}"
        cur.execute(sql_query)
        results = cur.fetchall()
        cur.close()
        return [_json_safe(dict(r)) for r in results]
    except Exception as e:
        logger.error("[SQL] _execute_sql_query error: %s", e)
        raise
    finally:
        conn.close()


def _generate_sql_with_llm(question: str, schema_info: Dict[str, Any]) -> str:
    """Generate SQL via LLM (backward compat for rag.py)."""
    if not _state_registry:
        raise RuntimeError("No agent state registered yet.")
    agent_name = next(iter(_state_registry))
    return _agent_generate_sql(agent_name, question, schema_info)


# ── Tool Box param discovery (scan-time only, never at query time) ────────────
_TOOL_BOX_PARAM_DISCOVERY = [
    os.getenv("DB_HOST", "localhost"),
    os.getenv("DB_PORT", "5432"),
    os.getenv("DB_NAME"),
    os.getenv("DB_USER"),
    os.getenv("DB_PASSWORD"),
    os.getenv("DB_SCHEMA", "public"),
    os.getenv("DB_INCLUDE_TABLES", ""),
]


# ── Agent state dataclass ─────────────────────────────────────────────────────

@dataclass
class _AgentState:
    model:       str
    api_base:    Optional[str]
    api_key:     Optional[str]
    agent_name:  str
    db_ok:       bool
    db_type:     str
    db_host:     str
    db_port:     int
    db_name:     str
    db_user:     str
    db_password: str
    db_schema:   str
    db_include:  str
    schema_samples: bool
    cache:       Dict[str, Any]
    cache_ts:    float
    lock:        threading.Lock = field(default_factory=threading.Lock)


_CACHE_TTL = int(os.getenv("SQL_SCHEMA_CACHE_TTL_S", "3600"))  # seconds

# ── Persistent registry — survives across ALL async tasks and requests ────────
_state_registry: Dict[str, _AgentState] = {}


def _get_state(agent_name: str) -> _AgentState:
    s = _state_registry.get(agent_name)
    if s is None:
        raise RuntimeError(
            f"No state registered for {agent_name}. "
            "Ensure make_text_to_sql_tools() was called during agent init."
        )
    return s


# ── Agent-level DB + schema helpers ──────────────────────────────────────────

# ── Optional connection pooling (opt-in via SQL_POOL_ENABLED) ─────────────────
# The shared OVH Postgres caps connections (max_connections=100, split across
# th2dev/th2scei/th2prod). The default per-query open/close has NO ceiling, so a
# burst of concurrent queries can spike connections and saturate the shared DB.
# A bounded ThreadedConnectionPool both reuses connections AND acts as a hard
# cap (maxconn) — a semaphore that protects the shared DB. PostgreSQL only.
#
# OFF by default. Enable with SQL_POOL_ENABLED=1 and tune SQL_POOL_MAX, then
# load-test OFF-PEAK while watching pg_stat_activity before relying on it.

_PG_POOLS: Dict[tuple, "ThreadedConnectionPool"] = {}
_PG_POOLS_LOCK = threading.Lock()


def _pool_enabled() -> bool:
    return os.getenv("SQL_POOL_ENABLED", "0").strip().lower() in ("1", "true", "yes")


def _pool_max() -> int:
    try:
        return max(1, int(os.getenv("SQL_POOL_MAX", "4")))
    except ValueError:
        return 4


def _pg_pool_key(s: "_AgentState") -> tuple:
    """One pool per distinct (host, port, db, user) — agents sharing a DB share
    a pool instead of each holding its own connections."""
    return (s.db_host, s.db_port, s.db_name, s.db_user)


def _get_or_create_pool(key: tuple, creator):
    """Return the cached pool for ``key``, creating it exactly once (thread-safe,
    double-checked). ``creator`` is a no-arg callable returning the pool."""
    pool = _PG_POOLS.get(key)
    if pool is None:
        with _PG_POOLS_LOCK:
            pool = _PG_POOLS.get(key)
            if pool is None:
                pool = creator()
                _PG_POOLS[key] = pool
    return pool


def _get_pg_pool(s: "_AgentState"):
    maxconn = _pool_max()
    return _get_or_create_pool(
        _pg_pool_key(s),
        lambda: ThreadedConnectionPool(
            1, maxconn,
            host=s.db_host, port=s.db_port, database=s.db_name,
            user=s.db_user, password=s.db_password,
        ),
    )


# A semaphore per pool gives the pool QUEUEING semantics. psycopg2's
# ThreadedConnectionPool.getconn() raises "connection pool exhausted" the moment
# all maxconn are checked out — it does NOT wait. Without this, enabling the
# pool would turn concurrency > maxconn into user-facing errors. The semaphore
# makes excess callers BLOCK until a connection frees (bounded by
# SQL_POOL_ACQUIRE_TIMEOUT_S), so a burst serialises through maxconn slots
# instead of failing.
_PG_POOL_SEMAS: Dict[tuple, threading.Semaphore] = {}


def _acquire_timeout() -> int:
    try:
        return max(1, int(os.getenv("SQL_POOL_ACQUIRE_TIMEOUT_S", "30")))
    except ValueError:
        return 30


def _get_pg_sema(s: "_AgentState") -> threading.Semaphore:
    """One semaphore(maxconn) per pool key, created once (thread-safe)."""
    key = _pg_pool_key(s)
    sema = _PG_POOL_SEMAS.get(key)
    if sema is None:
        with _PG_POOLS_LOCK:
            sema = _PG_POOL_SEMAS.get(key)
            if sema is None:
                sema = threading.Semaphore(_pool_max())
                _PG_POOL_SEMAS[key] = sema
    return sema


def _use_pool(s: "_AgentState") -> bool:
    return _pool_enabled() and s.db_type == "postgresql" and s.db_ok


def _acquire_conn(agent_name: str):
    """Get a connection — from the pool when enabled (Postgres), else a fresh one.

    Pooled path waits (up to SQL_POOL_ACQUIRE_TIMEOUT_S) for a free slot via a
    semaphore, so concurrency beyond maxconn queues instead of raising
    'connection pool exhausted'."""
    s = _get_state(agent_name)
    if _use_pool(s):
        sema = _get_pg_sema(s)
        if not sema.acquire(timeout=_acquire_timeout()):
            raise RuntimeError(
                f"SQL connection pool busy: all {_pool_max()} connections in use; "
                f"timed out after {_acquire_timeout()}s waiting for a free slot."
            )
        try:
            conn = _get_pg_pool(s).getconn()
            conn.autocommit = True  # SELECT-only: avoid idle-in-transaction on return
            return conn
        except Exception:
            sema.release()  # never got a usable connection — give the slot back
            raise
    return _agent_connection(agent_name)


def _release_conn(agent_name: str, conn) -> None:
    """Return a pooled connection to its pool, or close a non-pooled one.

    Never raises: a release failure must not mask the caller's result/error.
    """
    s = _get_state(agent_name)
    if _use_pool(s):
        pool = _get_pg_pool(s)
        try:
            try:
                pool.putconn(conn)
            except Exception:
                # Plain putconn failed. Retry with close=True so the pool FREES
                # the slot (a bare close leaves the slot 'checked out' forever,
                # which eventually exhausts the pool). If even that fails, hard
                # close so the connection isn't leaked.
                try:
                    pool.putconn(conn, close=True)
                except Exception:
                    try:
                        conn.close()
                    except Exception:
                        pass
        finally:
            # Release the queue slot regardless of how the connection came back,
            # so a waiting caller can proceed. Never raises (Semaphore.release).
            _get_pg_sema(s).release()
        return
    try:
        conn.close()
    except Exception:
        pass


def _agent_connection(agent_name: str):
    s = _get_state(agent_name)
    if not s.db_ok:
        raise RuntimeError(
            "No database configured for this agent. "
            "Add a database connection in the agent's Tool settings."
        )
    if s.db_type == "mysql":
        import pymysql
        return pymysql.connect(
            host=s.db_host, port=s.db_port, database=s.db_name,
            user=s.db_user, password=s.db_password,
            cursorclass=pymysql.cursors.DictCursor,
            ssl_disabled=True,
        )
    else:
        return psycopg2.connect(
            host=s.db_host, port=s.db_port, database=s.db_name,
            user=s.db_user, password=s.db_password,
        )


def _agent_cursor(conn, db_type: str):
    """Return a dict-cursor regardless of DB type."""
    if db_type == "mysql":
        return conn.cursor()  # DictCursor already set at connect time
    else:
        return conn.cursor(cursor_factory=RealDictCursor)


def _agent_fetch_schema(agent_name: str) -> Dict[str, Any]:
    """Introspect the agent's DB schema with grouped queries (no N+1).

    3 round-trips total: tables, all columns, all PK+FK constraints — grouped by
    table in Python. Sample rows are fetched only when SQL_SCHEMA_SAMPLES is on
    (one extra query per table), off by default.
    """
    s = _get_state(agent_name)
    conn = _acquire_conn(agent_name)
    is_mysql = s.db_type == "mysql"
    # For MySQL, schema == database name; db_schema param is unused
    effective_schema = s.db_name if is_mysql else s.db_schema
    include_samples = s.schema_samples
    try:
        cur = _agent_cursor(conn, s.db_type)

        # 1) Tables
        cur.execute("""
            SELECT table_name FROM information_schema.tables
            WHERE table_schema = %s AND table_type = 'BASE TABLE'
            ORDER BY table_name;
        """, (effective_schema,))
        tables = _norm(cur.fetchall())
        if s.db_include.strip():
            wl = {t.strip() for t in s.db_include.split(",") if t.strip()}
            tables = [t for t in tables if t["table_name"] in wl]
            logger.info("[SCHEMA][%s] Whitelist: %d tables", agent_name, len(tables))
        else:
            before = len(tables)
            tables = [t for t in tables if not _is_excluded_table(t["table_name"])]
            logger.info("[SCHEMA][%s] Excluded %d system tables, %d remain",
                        agent_name, before - len(tables), len(tables))
        table_names = {t["table_name"] for t in tables}

        # 2) All columns for the schema, grouped by table in Python (1 query)
        cur.execute("""
            SELECT table_name, column_name, data_type, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema = %s
            ORDER BY table_name, ordinal_position;
        """, (effective_schema,))
        cols_by_table: Dict[str, list] = {}
        for row in _norm(cur.fetchall()):
            tn = row["table_name"]
            if tn in table_names:
                cols_by_table.setdefault(tn, []).append({
                    "column_name":   row["column_name"],
                    "data_type":     row["data_type"],
                    "is_nullable":   row["is_nullable"],
                    "column_default": row["column_default"],
                })

        # 3) All PK + FK constraints for the schema, grouped in Python (1 query)
        if is_mysql:
            cur.execute("""
                SELECT tc.constraint_type, kcu.table_name AS tbl, kcu.column_name,
                    kcu.referenced_table_name AS foreign_table_name,
                    kcu.referenced_column_name AS foreign_column_name
                FROM information_schema.key_column_usage kcu
                JOIN information_schema.table_constraints tc
                    ON tc.constraint_name = kcu.constraint_name
                    AND tc.table_schema = kcu.table_schema
                    AND tc.table_name = kcu.table_name
                WHERE kcu.table_schema = %s
                    AND tc.constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY');
            """, (effective_schema,))
        else:
            cur.execute("""
                SELECT tc.constraint_type, kcu.table_name AS tbl, kcu.column_name,
                    ccu.table_name AS foreign_table_name,
                    ccu.column_name AS foreign_column_name
                FROM information_schema.table_constraints AS tc
                JOIN information_schema.key_column_usage AS kcu
                    ON tc.constraint_name = kcu.constraint_name
                    AND tc.table_schema = kcu.table_schema
                LEFT JOIN information_schema.constraint_column_usage AS ccu
                    ON ccu.constraint_name = tc.constraint_name
                    AND ccu.table_schema = tc.table_schema
                WHERE tc.table_schema = %s
                    AND tc.constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY');
            """, (effective_schema,))
        pks_by_table: Dict[str, list] = {}
        fks_by_table: Dict[str, list] = {}
        fk_seen: Dict[str, set] = {}
        for row in _norm(cur.fetchall()):
            tn = row.get("tbl")
            if tn not in table_names:
                continue
            if row.get("constraint_type") == "PRIMARY KEY":
                pk = pks_by_table.setdefault(tn, [])
                if row["column_name"] not in pk:
                    pk.append(row["column_name"])
            elif row.get("constraint_type") == "FOREIGN KEY":
                key = (row["column_name"], row.get("foreign_table_name"),
                       row.get("foreign_column_name"))
                seen = fk_seen.setdefault(tn, set())
                if key not in seen and row.get("foreign_table_name"):
                    seen.add(key)
                    fks_by_table.setdefault(tn, []).append({
                        "column_name":         row["column_name"],
                        "foreign_table_name":  row["foreign_table_name"],
                        "foreign_column_name": row["foreign_column_name"],
                    })

        # 4) Assemble; sample rows only when explicitly enabled
        schema_info: Dict[str, Any] = {"tables": {}, "schema": effective_schema, "db_type": s.db_type}
        for tbl in tables:
            tn = tbl["table_name"]
            sample_data: list = []
            if include_samples:
                try:
                    if is_mysql:
                        cur.execute(f"SELECT * FROM `{tn}` LIMIT 3;")
                    else:
                        cur.execute(f'SELECT * FROM "{effective_schema}"."{tn}" LIMIT 3;')
                    sample_data = [_json_safe(dict(r)) for r in cur.fetchall()]
                except Exception:
                    sample_data = []
            schema_info["tables"][tn] = {
                "columns":      cols_by_table.get(tn, []),
                "primary_keys": pks_by_table.get(tn, []),
                "foreign_keys": fks_by_table.get(tn, []),
                "sample_data":  sample_data,
            }
        cur.close()
        logger.info("[SCHEMA][%s] %d tables in '%s' (%s), grouped introspection (samples=%s)",
                    agent_name, len(schema_info["tables"]), effective_schema, s.db_type, include_samples)
        return schema_info
    except Exception as e:
        logger.error("[SCHEMA][%s] %s", agent_name, e)
        raise
    finally:
        _release_conn(agent_name, conn)


def _agent_cached_schema(agent_name: str, refresh: bool = False) -> Dict[str, Any]:
    s = _get_state(agent_name)
    now = _time.time()
    if not refresh and s.cache and (now - s.cache_ts) < _CACHE_TTL:
        logger.info("[SCHEMA][%s] Cache hit (age %.0fs)", agent_name, now - s.cache_ts)
        return s.cache
    # Double-checked locking: only one thread introspects; the rest reuse it.
    with s.lock:
        now = _time.time()
        if not refresh and s.cache and (now - s.cache_ts) < _CACHE_TTL:
            return s.cache
        schema = _agent_fetch_schema(agent_name)
        s.cache = schema
        s.cache_ts = now
        return schema


def _agent_execute_sql(agent_name: str, sql_query: str, limit: int = 100) -> List[Dict[str, Any]]:
    # Defense in depth: safety is invariant, not dependent on the caller.
    # run_investigation already validates before calling, but a future caller
    # must not be able to bypass the SELECT-only guarantee.
    is_safe, err = validate_sql_safety(sql_query)
    if not is_safe:
        raise ValueError(f"unsafe SQL rejected: {err}")
    s = _get_state(agent_name)
    conn = _acquire_conn(agent_name)
    try:
        cur = _agent_cursor(conn, s.db_type)
        if "LIMIT" not in sql_query.upper():
            sql_query = f"{sql_query.rstrip(';')} LIMIT {limit}"
        if s.db_type == "postgresql":
            cur.execute(f'SET search_path TO "{s.db_schema}", public')
        cur.execute(sql_query)
        results = cur.fetchall()
        cur.close()
        return [_json_safe(dict(r)) for r in results]
    except Exception as e:
        logger.error("[SQL][%s] %s", agent_name, e)
        raise
    finally:
        _release_conn(agent_name, conn)


def _agent_generate_sql(agent_name: str, question: str, schema_info: Dict[str, Any]) -> str:
    s = _get_state(agent_name)
    max_chars = int(os.getenv("SQL_SCHEMA_MAX_CHARS", "16000"))
    include_samples = s.schema_samples
    schema_description = build_schema_prompt(
        schema_info, question=question, max_chars=max_chars,
        include_samples=include_samples, sample_rows=3,
    )
    logger.info("[SQL][%s] schema_chars=%d (samples=%s)",
                agent_name, len(schema_description), include_samples)
    db_schema = schema_info.get("schema", "public")

    db_type = schema_info.get("db_type", "postgresql")
    if db_type == "mysql":
        dialect_name = "MySQL"
        dialect_rules = (
            "2. Use proper MySQL syntax\n"
            "8. Use backtick quoting for table/column names if needed (e.g. `table`.`column`)\n"
            "9. Do NOT qualify table names with a schema prefix"
        )
        dialect_hint = f"Generate a MySQL SELECT query."
    else:
        dialect_name = "PostgreSQL"
        dialect_rules = (
            "2. Use proper PostgreSQL syntax\n"
            f"8. Always qualify table names with the schema: {db_schema}.table_name"
        )
        dialect_hint = f"Generate a PostgreSQL SELECT query using the {db_schema} schema."

    system_prompt = f"""You are an expert SQL query generator. Convert natural language to valid {dialect_name} queries.

Rules:
1. Generate ONLY the SQL query, no explanations
{dialect_rules}
3. Use appropriate JOINs when needed
4. Always use explicit column names
5. Generate ONLY SELECT queries
6. Use table/column names exactly as in schema
7. Add meaningful aliases

Output ONLY the SQL query without markdown."""

    user_content = (
        f"{schema_description}\n\n"
        f"Question: {question}\n\n"
        f"{dialect_hint}"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_content},
    ]

    def _ask(msgs):
        resp = litellm.completion(
            model=s.model,
            api_base=s.api_base,
            api_key=s.api_key,
            messages=msgs,
            temperature=0.0,
            max_tokens=1000,
            timeout=60,
        )
        return resp.choices[0].message.content or ""

    raw = _ask(messages)
    sql = extract_sql(raw)
    is_safe, err = validate_sql_safety(sql) if sql else (False, "empty output")
    if not is_safe:
        # One corrective retry with the failure as feedback. Local models
        # (Mistral on OVH) often wrap SQL in prose or emit a non-SELECT on
        # the first try; a single retry recovers most of these cheaply.
        logger.warning("[SQL][%s] invalid first attempt (%s); retrying", agent_name, err)
        retry_messages = messages + [
            {"role": "assistant", "content": raw},
            {"role": "user", "content": (
                f"That was not a valid single SELECT query ({err}). "
                "Output ONLY the SQL query: a single SELECT statement, no prose, no markdown."
            )},
        ]
        raw = _ask(retry_messages)
        sql = extract_sql(raw)

    logger.info("[SQL][%s] Generated: %s", agent_name, sql)
    return sql


# ── Business Analyst: multi-step investigation + interpretation ───────────────

def _agent_llm(agent_name: str, messages: list, max_tokens: int = 1200) -> str:
    """Single chat completion using the agent's configured model."""
    s = _get_state(agent_name)
    resp = litellm.completion(
        model=s.model, api_base=s.api_base, api_key=s.api_key,
        messages=messages, temperature=0.0, max_tokens=max_tokens, timeout=90,
    )
    return resp.choices[0].message.content or ""


def _tool_business_analyst_impl(
    agent_name: str,
    question: str,
    max_steps: int = 4,
    max_rows: int = 100,
) -> dict:
    """Answer a business question through a chained SQL investigation.

    Unlike tool_text_to_sql (one question -> one query), this runs several
    queries that build on each other, then interprets the evidence into a
    narrative + findings + recommendations + an optional chart spec. The model
    that plans and interprets is the agent's configured model; SQL safety is
    enforced deterministically regardless of the model.
    """
    from apowerb.sqlgen.analyst import run_investigation
    from apowerb.sqlgen.analyst_prompts import (
        build_interpreter_messages,
        build_planner_messages,
        parse_interpreter_response,
        parse_planner_response,
    )

    try:
        logger.info("[BIZ_ANALYST][%s] %s", agent_name, question)
        schema_info = _agent_cached_schema(agent_name)
        schema_prompt = build_schema_prompt(
            schema_info, question=question,
            max_chars=int(os.getenv("SQL_SCHEMA_MAX_CHARS", "16000")),
            include_samples=_get_state(agent_name).schema_samples, sample_rows=3,
        )

        def plan_next(q, _schema, steps):
            raw = _agent_llm(
                agent_name,
                build_planner_messages(q, schema_prompt, steps, max_steps),
                max_tokens=800,
            )
            plan = parse_planner_response(raw)
            logger.info("[BIZ_ANALYST][%s] plan step %d: done=%s sub=%s",
                        agent_name, len(steps) + 1, plan.get("done"),
                        plan.get("sub_question"))
            return plan

        def execute(sql):
            return _agent_execute_sql(agent_name, sql, limit=max_rows)

        def interpret(q, steps):
            raw = _agent_llm(
                agent_name, build_interpreter_messages(q, steps), max_tokens=1200,
            )
            return parse_interpreter_response(raw)

        result = run_investigation(
            question, schema_info,
            plan_next=plan_next, execute=execute, interpret=interpret,
            max_steps=max_steps,
        )

        return {
            "success": result.ok,
            "question": question,
            "schema": schema_info.get("schema", "public"),
            "steps": [
                {
                    "sub_question": st.sub_question,
                    "sql_query": st.sql,
                    "row_count": st.row_count,
                    "data": st.rows,
                    "error": st.error,
                }
                for st in result.steps
            ],
            "narrative": result.narrative,
            "findings": result.findings,
            "recommendations": result.recommendations,
            "chart": result.chart,
            "message": (
                "Investigation complete. Present the narrative, then the "
                "findings and recommendations. If a chart spec is present, build "
                "it with tool_create_chart using the relevant step's sql_query."
            ),
        }
    except Exception as e:
        logger.error("[BIZ_ANALYST][%s] %s", agent_name, e)
        return {"success": False, "error": str(e)}


# ── Module-level tool implementations ────────────────────────────────────────

def _tool_text_to_sql_impl(
    agent_name: str,
    question: str,
    max_rows: int = 100,
    include_sql: bool = True,
) -> dict:
    try:
        logger.info("[TEXT_TO_SQL][%s] %s", agent_name, question)
        schema_info = _agent_cached_schema(agent_name)
        sql_query   = _agent_generate_sql(agent_name, question, schema_info)

        is_safe, error_msg = _validate_sql_safety(sql_query)
        if not is_safe:
            return {
                "success":   False,
                "error":     f"Safety check failed: {error_msg}",
                "sql_query": sql_query if include_sql else None,
            }

        results = _agent_execute_sql(agent_name, sql_query, limit=max_rows)
        resp = {
            "success":   True,
            "question":  question,
            "schema":    schema_info.get("schema", "public"),
            "row_count": len(results),
            "data":      results,
            "message":   "Data retrieved successfully. Present the results clearly to the user.",
        }
        if include_sql:
            resp["sql_query"] = sql_query
        return resp
    except Exception as e:
        logger.error("[TEXT_TO_SQL][%s] %s", agent_name, e)
        return {"success": False, "error": str(e)}


def _tool_get_database_schema_impl(agent_name: str, refresh: bool = False) -> dict:
    try:
        s = _get_state(agent_name)
        schema_info = _agent_cached_schema(agent_name, refresh=refresh)
        return {
            "success":     True,
            "database":    s.db_name,
            "schema":      schema_info.get("schema", "public"),
            "table_count": len(schema_info["tables"]),
            "tables":      schema_info["tables"],
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def _tool_text_to_sql_explain_impl(agent_name: str, question: str) -> dict:
    try:
        s = _get_state(agent_name)
        schema_info = _agent_cached_schema(agent_name)
        sql_query   = _agent_generate_sql(agent_name, question, schema_info)
        is_safe, error_msg = _validate_sql_safety(sql_query)

        expl_resp = litellm.completion(
            model=s.model,
            api_base=s.api_base,
            api_key=s.api_key,
            messages=[{
                "role":    "user",
                "content": f"Explain this SQL query in simple terms:\n\n{sql_query}",
            }],
            temperature=0.3,
            max_tokens=300,
            timeout=60,
        )
        explanation = expl_resp.choices[0].message.content.strip()
        return {
            "success":        True,
            "question":       question,
            "schema":         schema_info.get("schema", "public"),
            "sql_query":      sql_query,
            "is_safe":        is_safe,
            "safety_message": error_msg if not is_safe else "Query passed safety checks",
            "explanation":    explanation,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


# ── Module-level placeholders ─────────────────────────────────────────────────
# REQUIRED: load_agent_tools_functions() detects these by name to set
# has_text_to_sql=True, which triggers make_text_to_sql_tools() in agent_helpers.
# After the factory runs, these are replaced in tools_funcs with proper
# agent-bound closures — these placeholders are never actually called.

def tool_text_to_sql(question: str, max_rows: int = 100, include_sql: bool = True) -> dict:
    """Converts natural language to SQL and executes it against the configured database."""
    return {"success": False, "error": "Tool not yet bound to agent. make_text_to_sql_tools() must run first."}


def tool_get_database_schema() -> dict:
    """Retrieves the database schema including tables, columns, and relationships."""
    return {"success": False, "error": "Tool not yet bound to agent. make_text_to_sql_tools() must run first."}


def tool_text_to_sql_explain(question: str) -> dict:
    """Generates SQL from natural language with explanation, without executing it."""
    return {"success": False, "error": "Tool not yet bound to agent. make_text_to_sql_tools() must run first."}


def tool_business_analyst(question: str, max_steps: int = 4, max_rows: int = 100) -> dict:
    """Answers a business question through a multi-step SQL investigation.

    Runs several chained SELECT queries, then returns a narrative analysis with
    findings, recommendations and an optional chart spec. Use for open
    analytical questions; use tool_text_to_sql for a single direct lookup.
    """
    return {"success": False, "error": "Tool not yet bound to agent. make_text_to_sql_tools() must run first."}


# ── Factory ───────────────────────────────────────────────────────────────────

def make_text_to_sql_tools(
    agent_name: str,
    db_params: Optional[Dict[str, Any]] = None,
) -> list:
    """
    Register agent credentials in the persistent registry and return
    thin tool wrappers bound to this agent.

    State is stored in _state_registry (a plain module-level dict) which
    persists across ALL async tasks and requests — no ContextVar loss between requests.

    Args:
        agent_name: Agent name string, e.g. "agent42".
        db_params:  Decrypted DB tool-config params. Pass None if no DB configured.

    Returns:
        [tool_text_to_sql, tool_get_database_schema, tool_text_to_sql_explain]
    """
    from apowerb.core.agent_helpers import load_agent_model_params

    agent_model_name, model_params = load_agent_model_params(
        int(agent_name.replace("agent", ""))
    )
    api_key  = model_params.get("model_api_key")
    api_base = model_params.get("model_api_base")

    if not api_base and agent_model_name.startswith("mistral/"):
        api_base = (
            "https://mistral-small-3-2-24b-instruct-2506.endpoints.kepler.ai.cloud.ovh.net"
            "/api/openai_compat/v1"
        )

    if db_params and db_params.get("DB_NAME"):
        db_ok       = True
        db_type     = db_params.get("DB_TYPE", "postgresql").lower()
        db_host     = db_params.get("DB_HOST", "localhost")
        db_port     = int(db_params.get("DB_PORT", 3306 if db_type == "mysql" else 5432))
        db_name     = db_params["DB_NAME"]
        db_user     = db_params.get("DB_USER", "")
        db_password = db_params.get("DB_PASSWORD", "")
        db_schema   = db_params.get("DB_SCHEMA", "public")
        db_include  = db_params.get("DB_INCLUDE_TABLES", "")
        db_status   = f"db={db_name!r} schema={db_schema!r} type={db_type!r}"
    else:
        db_ok = False
        db_type = "postgresql"
        db_host = db_name = db_user = db_password = db_schema = db_include = ""
        db_port = 5432
        db_status = "db=<not configured>"

    # Schema samples: per-agent opt-in via the DB tool config (SCHEMA_SAMPLES),
    # falling back to the global SQL_SCHEMA_SAMPLES env. Sending sample rows to
    # the model is a data-governance decision, so it is OFF unless explicitly
    # enabled — per agent, never globally by accident.
    _samples_raw = str(
        (db_params or {}).get("SCHEMA_SAMPLES", os.getenv("SQL_SCHEMA_SAMPLES", "0"))
    ).strip().lower()
    schema_samples = _samples_raw in ("1", "true", "yes")

    # Register in persistent dict — survives across all async tasks/requests
    _state_registry[agent_name] = _AgentState(
        model=agent_model_name, api_base=api_base, api_key=api_key,
        agent_name=agent_name,
        db_ok=db_ok, db_type=db_type, db_host=db_host, db_port=db_port, db_name=db_name,
        db_user=db_user, db_password=db_password, db_schema=db_schema,
        db_include=db_include, schema_samples=schema_samples,
        cache={}, cache_ts=0.0,
    )

    logger.info(
        "[TEXT_TO_SQL] agent=%s model=%s api_base=%s %s schema_samples=%s",
        agent_name, agent_model_name, api_base or "not set", db_status, schema_samples,
    )

    # Thin wrappers — capture agent_name, delegate to module-level _impl functions
    def tool_text_to_sql(question: str, max_rows: int = 100, include_sql: bool = True) -> dict:
        """Converts natural language to SQL and executes it against the configured database."""
        return _tool_text_to_sql_impl(agent_name, question, max_rows, include_sql)

    def tool_get_database_schema(refresh: bool = False) -> dict:
        """Retrieves the database schema including tables, columns, and relationships.

        Pass refresh=True to bypass the cache and re-introspect the database.
        """
        return _tool_get_database_schema_impl(agent_name, refresh=refresh)

    def tool_text_to_sql_explain(question: str) -> dict:
        """Generates SQL from natural language with explanation, without executing it."""
        return _tool_text_to_sql_explain_impl(agent_name, question)

    def tool_business_analyst(question: str, max_steps: int = 4, max_rows: int = 100) -> dict:
        """Answers a business question through a multi-step SQL investigation.

        Runs several chained SELECT queries (not just one), then returns a
        narrative analysis with findings, recommendations and an optional chart
        spec. Use this for open analytical questions ('why did X drop?', 'compare
        A vs B', 'what are the trends in Y?'); use tool_text_to_sql for a single
        direct lookup.
        """
        return _tool_business_analyst_impl(agent_name, question, max_steps, max_rows)

    return [
        tool_text_to_sql,
        tool_get_database_schema,
        tool_text_to_sql_explain,
        tool_business_analyst,
    ]