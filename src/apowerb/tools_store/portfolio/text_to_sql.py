import json
import os
import re
import datetime
import time as _time
from decimal import Decimal
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
import psycopg2
import psycopg2.errors
from psycopg2.extras import RealDictCursor
from psycopg2.pool import ThreadedConnectionPool
import litellm
from typing import Dict, Any, List, Optional
from logging import getLogger
from apowerb.sqlgen.generator import extract_sql
from apowerb.sqlgen.safety import validate_sql_safety
from apowerb.sqlgen.readonly import read_only_transaction
from apowerb.tools_store.tool_env import config_env
from apowerb.sqlgen.schema_format import build_schema_prompt

logger = getLogger(__name__)

# ── Tables to exclude from schema introspection ──
# Keycloak/Liquibase tables are matched by exact name: a bare prefix such as
# "client" or "component" also hid business tables (clients, components...).
# Names come from Keycloak's Liquibase changelogs (26.8.1, incl. renamed and
# dropped tables), limited to those the former prefixes already covered: newer
# tables with generic names (org, user_session, workflow_state...) are left
# visible on purpose.
_EXCLUDED_TABLE_PREFIXES = (
    "databasechangelog",
    "keycloak_",
)

_KEYCLOAK_TABLES = frozenset({
    "admin_event_entity", "associated_policy", "authentication_execution",
    "authentication_flow", "broker_link", "client", "client_attributes",
    "client_auth_flow_bindings", "client_default_roles",
    "client_identity_prov_mapping", "client_initial_access",
    "client_node_registrations", "client_scope", "client_scope_attributes",
    "client_scope_client", "client_scope_role_mapping", "client_session",
    "client_session_auth_status", "client_session_note",
    "client_session_prot_mapper", "client_session_role", "client_template",
    "client_template_attributes", "client_user_session_note", "component",
    "component_config", "composite_role", "credential", "credential_attribute",
    "default_client_scope", "event_entity", "fed_credential_attribute",
    "fed_issued_ver_credential", "fed_providers", "fed_user_attribute",
    "fed_user_consent", "fed_user_consent_cl_scope", "fed_user_consent_prot_mapper",
    "fed_user_consent_role", "fed_user_credential", "fed_user_group_membership",
    "fed_user_required_action", "fed_user_role_mapping", "fed_user_ver_credential",
    "federated_identity", "federated_user", "group_attribute", "group_role_mapping",
    "identity_provider", "identity_provider_config", "identity_provider_mapper",
    "migration_model", "offline_client_session", "offline_user_session",
    "policy_config", "protocol_mapper", "protocol_mapper_config", "realm",
    "realm_application", "realm_attribute", "realm_client", "realm_client_template",
    "realm_default_groups", "realm_default_roles", "realm_enabled_event_types",
    "realm_events_listeners", "realm_localizations", "realm_required_credential",
    "realm_smtp_config", "realm_social_config", "realm_supported_locales",
    "redirect_uris", "required_action_config", "required_action_provider",
    "resource_attribute", "resource_policy", "resource_scope", "resource_server",
    "resource_server_perm_ticket", "resource_server_policy",
    "resource_server_resource", "resource_server_scope", "resource_uris",
    "revoked_token", "role_attribute", "scope_mapping", "scope_policy",
    "user_attribute", "user_consent", "user_consent_client_scope",
    "user_consent_prot_mapper", "user_consent_role", "user_entity",
    "user_federation_config", "user_federation_mapper",
    "user_federation_mapper_config", "user_federation_provider",
    "user_group_membership", "user_required_action", "user_role_mapping",
    "username_login_failure", "web_origins",
})

_EXCLUDED_TABLES_EXACT = _KEYCLOAK_TABLES | {
    "pwd_mngt",
    "th2dive_cookies_sessions",
    "th2dive_with_cookies",
}


def _is_excluded_table(table_name: str) -> bool:
    if table_name in _EXCLUDED_TABLES_EXACT:
        return True
    return table_name.startswith(_EXCLUDED_TABLE_PREFIXES)



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
    """Connection for the /rag/index-db routes: DB_* of the active tool_config only."""
    host     = config_env("DB_HOST", "localhost")
    port     = int(config_env("DB_PORT", "5432"))
    database = config_env("DB_NAME")
    user     = config_env("DB_USER")
    password = config_env("DB_PASSWORD")
    if not all([database, user, password]):
        raise ValueError(
            "No database configured: attach a database tool_config "
            "(DB_NAME, DB_USER, DB_PASSWORD) to this tool"
        )
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
        with read_only_transaction(conn, cur, "postgresql"):
            cur.execute(sql_query)
            results = cur.fetchall()
        cur.close()
        return [_json_safe(dict(r)) for r in results]
    except Exception as e:
        logger.error("[SQL] _execute_sql_query error: %s", e)
        raise
    finally:
        conn.close()


def _generate_sql_with_llm(question: str, schema_info: Dict[str, Any], agent_name: str) -> str:
    """Generate SQL via LLM with *agent_name*'s model (/rag/index-db-nl).

    Uses a transient state built from the agent's model params: the state
    registered by make_text_to_sql_tools (DB params, schema cache) is
    neither read nor replaced, and the agent need not be built in this worker.
    """
    model, api_base, api_key, api_version = _agent_llm_endpoint(
        int(agent_name.replace("agent", ""))
    )
    s = _AgentState(
        model=model, api_base=api_base, api_key=api_key, api_version=api_version,
        agent_name=agent_name,
        db_ok=False, db_type="postgresql", db_host="", db_port=5432, db_name="",
        db_user="", db_password="", db_schema="", db_include="",
        schema_samples=os.getenv("SQL_SCHEMA_SAMPLES", "0").strip().lower() in ("1", "true", "yes"),
        cache={}, cache_ts=0.0,
    )
    # Not recorded in llm_usage: the route runs in a worker thread with no
    # event loop, where record_completion_usage drops the row anyway.
    return _state_generate_sql(s, question, schema_info, record_usage=False)


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
    # Usage accounting of the tools' own LLM calls (see _record_llm_usage):
    # the agent this state is bound to, as the usage recorder names it.
    agent_id:          int = 0
    usage_agent_name:  str = ""
    owner_id:          Optional[str] = None
    billed_to_thaink2: bool = False
    # Azure api_version of the agent's model params (model_api_version).
    api_version:       Optional[str] = None


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


# ── Usage accounting of the tools' own LLM calls ─────────────────────────────
# These calls go to litellm directly, not through ADK: the agent's
# after_model_callback never sees them. Each one is recorded here, against
# the agent the tools are bound to and the session that called the tool.

# (session_id, invocation_id) of the tool call in progress. Set by the bound
# wrappers from ADK's tool_context; sync, same thread as the LLM calls.
_tool_call_ids: ContextVar[tuple] = ContextVar("text_to_sql_tool_call", default=(None, None))


@contextmanager
def _tool_call_scope(tool_context):
    try:
        session_id = tool_context.session.id
    except Exception:
        session_id = None
    invocation_id = getattr(tool_context, "invocation_id", None)
    token = _tool_call_ids.set((session_id, invocation_id))
    try:
        yield
    finally:
        _tool_call_ids.reset(token)


def _record_llm_usage(s: _AgentState, response: Any) -> None:
    from apowerb.core.agent_helpers.usage_recorder import record_completion_usage

    session_id, invocation_id = _tool_call_ids.get()
    record_completion_usage(
        response,
        agent_id=s.agent_id,
        agent_name=s.usage_agent_name or s.agent_name,
        owner_id=s.owner_id,
        model_name=s.model,
        billed_to_thaink2=s.billed_to_thaink2,
        session_id=session_id,
        invocation_id=invocation_id,
    )


def _load_agent_usage_identity(agent_id: int) -> tuple:
    """(business name, owner_id, billed_to_thaink2) of the agent, as the
    usage recorder attached by to_agent() names them."""
    from apowerb.core.agent_helpers.agent_utils import get_agent_details
    from apowerb.core.agent_helpers.default_llm import is_default_llm_model

    details = get_agent_details(agent_id=agent_id) or {}
    return (
        details.get("agent_name") or "",
        details.get("owner_id"),
        is_default_llm_model(details.get("agent_model")),
    )


# ── Agent-level DB + schema helpers ──────────────────────────────────────────

# ── Optional connection pooling (opt-in via SQL_POOL_ENABLED) ─────────────────
# The shared OVH Postgres caps connections (max_connections=100, split across
# one database per environment). The default per-query open/close has NO ceiling, so a
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
        # The database refuses writes and cancels a query past the timeout,
        # whatever the gate above let through.
        with read_only_transaction(conn, cur, s.db_type):
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


def _agent_generate_sql(
    agent_name: str, question: str, schema_info: Dict[str, Any], record_usage: bool = True,
    rejected: Optional[tuple] = None,
) -> str:
    return _state_generate_sql(_get_state(agent_name), question, schema_info, record_usage, rejected)


def _state_generate_sql(
    s: _AgentState, question: str, schema_info: Dict[str, Any], record_usage: bool = True,
    rejected: Optional[tuple] = None,
) -> str:
    """``rejected`` = (sql, database error) of a previous attempt to correct."""
    agent_name = s.agent_name
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
    if rejected:
        bad_sql, db_error = rejected
        messages += [
            {"role": "assistant", "content": bad_sql},
            {"role": "user", "content": (
                f"The database rejected that query: {db_error}\n"
                "Fix it using only the tables and columns of the schema above. "
                "Output ONLY the corrected SQL query."
            )},
        ]

    def _ask(msgs):
        resp = litellm.completion(
            **_llm_endpoint_kwargs(s),
            messages=msgs,
            temperature=0.0,
            # A ceiling, not a target: reasoning models (Gemini 2.5, gpt-5)
            # spend completion tokens thinking before the SQL; at 1000 they
            # could stop mid-query.
            max_tokens=4096,
            timeout=60,
        )
        if record_usage:
            _record_llm_usage(s, resp)
        choice = resp.choices[0]
        return choice.message.content or "", getattr(choice, "finish_reason", None) == "length"

    raw, cut = _ask(messages)
    sql = extract_sql(raw)
    is_safe, err = validate_sql_safety(sql) if sql else (False, "empty output")
    if cut:
        is_safe, err = False, "output cut off before the end of the query"
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
        raw, _ = _ask(retry_messages)
        sql = extract_sql(raw)

    logger.info("[SQL][%s] Generated: %s", agent_name, sql)
    return sql


# ── Business Analyst: multi-step investigation + interpretation ───────────────

def _agent_llm(agent_name: str, messages: list, max_tokens: int = 1200) -> str:
    """Single chat completion using the agent's configured model."""
    s = _get_state(agent_name)
    resp = litellm.completion(
        **_llm_endpoint_kwargs(s),
        messages=messages, temperature=0.0, max_tokens=max_tokens, timeout=90,
    )
    _record_llm_usage(s, resp)
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

    max_steps = max(1, min(int(max_steps or 4), _ANALYST_MAX_STEPS))
    max_rows = max(1, min(int(max_rows or _ANALYST_MAX_ROWS), _ANALYST_MAX_ROWS))
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
            "steps": [
                {
                    "sub_question": st.sub_question,
                    "sql_query": st.sql,
                    "row_count": st.row_count,
                    "data": st.rows[:_ANALYST_STEP_ROWS_SHOWN],
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


# Model-facing bounds for the tool responses.
_TEXT_TO_SQL_MAX_ROWS = 200
_ANALYST_MAX_STEPS = 6
_ANALYST_MAX_ROWS = 50
_ANALYST_STEP_ROWS_SHOWN = 20
_SCHEMA_TOOL_MAX_CHARS = 16000

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
        max_rows = max(1, min(int(max_rows or 100), _TEXT_TO_SQL_MAX_ROWS))
        rejected = None
        for attempt in range(2):
            sql_query = _agent_generate_sql(agent_name, question, schema_info, rejected=rejected)

            is_safe, error_msg = _validate_sql_safety(sql_query)
            if not is_safe:
                return {
                    "success":   False,
                    "error":     f"Safety check failed: {error_msg}",
                    "sql_query": sql_query if include_sql else None,
                }
            try:
                # Fetch one extra row so we can tell the model the result was cut.
                results = _agent_execute_sql(agent_name, sql_query, limit=max_rows + 1)
                break
            except Exception as e:
                # A query the database rejects (wrong column, join, type) is
                # regenerated once here with its error: far cheaper than sending
                # the error back to the agent, whose retry loop re-sends the
                # whole prompt and tool list on every step.
                if attempt or not _is_query_error(e):
                    logger.error("[TEXT_TO_SQL][%s] %s", agent_name, e)
                    return {
                        "success":   False,
                        "error":     str(e),
                        "sql_query": sql_query if include_sql else None,
                    }
                logger.warning("[TEXT_TO_SQL][%s] query rejected (%s); regenerating", agent_name, e)
                rejected = (sql_query, str(e).strip())
        truncated = len(results) > max_rows
        results = results[:max_rows]
        resp = {
            "success":   True,
            "row_count": len(results),
            "data":      results,
            "message":   "Data retrieved successfully. Present the results clearly to the user.",
        }
        if truncated:
            resp["truncated"] = True
            resp["message"] += (
                f" Only the first {max_rows} rows are shown; more exist. "
                "Refine the question (filter/aggregate) if the rest is needed."
            )
        if include_sql:
            resp["sql_query"] = sql_query
        return resp
    except Exception as e:
        logger.error("[TEXT_TO_SQL][%s] %s", agent_name, e)
        return {"success": False, "error": str(e)}


# MySQL server errors caused by the query itself (bad column, table, syntax,
# grouping), as opposed to connection, permission or timeout failures.
_MYSQL_QUERY_ERRORS = {1052, 1054, 1055, 1064, 1066, 1140, 1146, 1248, 1305, 1582}


def _is_query_error(exc: Exception) -> bool:
    """True when the database rejected the query itself, so regenerating it can help."""
    if isinstance(exc, psycopg2.errors.InsufficientPrivilege):
        return False
    if isinstance(exc, (psycopg2.ProgrammingError, psycopg2.DataError)):
        return True
    try:
        import pymysql
    except ImportError:  # pragma: no cover - MySQL driver optional
        return False
    if isinstance(exc, pymysql.err.MySQLError):
        return bool(exc.args) and exc.args[0] in _MYSQL_QUERY_ERRORS
    return False


def _compact_table(info: dict) -> dict:
    """Compact one table: ``columns`` as "name type" strings, PK, FK as "col->tbl.col"."""
    out: dict = {
        "columns": [
            f"{c.get('column_name')} {c.get('data_type')}"
            for c in info.get("columns", [])
        ],
    }
    if info.get("primary_keys"):
        out["pk"] = list(info["primary_keys"])
    fks = [
        f"{fk.get('column_name')}->{fk.get('foreign_table_name')}.{fk.get('foreign_column_name')}"
        for fk in info.get("foreign_keys", [])
    ]
    if fks:
        out["fk"] = fks
    if info.get("sample_data"):
        out["sample_data"] = info["sample_data"]
    return out


def _tool_get_database_schema_impl(
    agent_name: str,
    refresh: bool = False,
    tables: Optional[List[str]] = None,
) -> dict:
    try:
        s = _get_state(agent_name)
        schema_info = _agent_cached_schema(agent_name, refresh=refresh)
        all_tables = schema_info["tables"]
        unknown: list = []
        if tables:
            wanted = {str(t).strip().lower() for t in tables if str(t).strip()}
            selected = {n: t for n, t in all_tables.items() if n.lower() in wanted}
            known = {n.lower() for n in all_tables}
            unknown = sorted(w for w in wanted if w not in known)
        else:
            selected = all_tables

        compact: dict = {}
        omitted: list = []
        used = 0
        for name, info in selected.items():
            entry = _compact_table(info)
            size = len(json.dumps(entry, default=str)) + len(name)
            if used + size > _SCHEMA_TOOL_MAX_CHARS and compact:
                omitted.append(name)
                continue
            compact[name] = entry
            used += size
        resp = {
            "success":     True,
            "database":    s.db_name,
            "schema":      schema_info.get("schema", "public"),
            "table_count": len(all_tables),
            "tables":      compact,
        }
        if omitted:
            resp["truncated"] = True
            resp["omitted_tables"] = omitted
            resp["message"] = (
                "Output capped; call again with tables=[...] to get the omitted tables."
            )
        if unknown:
            resp["unknown_tables"] = unknown
        return resp
    except Exception as e:
        return {"success": False, "error": str(e)}


def _tool_text_to_sql_explain_impl(agent_name: str, question: str) -> dict:
    try:
        s = _get_state(agent_name)
        schema_info = _agent_cached_schema(agent_name)
        sql_query   = _agent_generate_sql(agent_name, question, schema_info)
        is_safe, error_msg = _validate_sql_safety(sql_query)

        expl_resp = litellm.completion(
            **_llm_endpoint_kwargs(s),
            messages=[{
                "role":    "user",
                "content": f"Explain this SQL query in simple terms:\n\n{sql_query}",
            }],
            temperature=0.3,
            max_tokens=300,
            timeout=60,
        )
        _record_llm_usage(s, expl_resp)
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


def tool_get_database_schema(refresh: bool = False, tables: list[str] | None = None) -> dict:
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

def _llm_endpoint_kwargs(s: _AgentState) -> dict:
    """litellm model/endpoint kwargs, routed as build_litellm_model routes the agent.

    With an api_base the agent calls ``openai/<model>`` at that endpoint; the
    raw ``azure/<model>`` took litellm's Azure deployment route instead and
    failed with ``NotFoundError - Resource not found``.
    """
    from apowerb.helpers.model_names import openai_compat_model_name

    kwargs = {
        "model": openai_compat_model_name(s.model) if s.api_base else s.model,
        "api_base": s.api_base,
        "api_key": s.api_key,
    }
    if s.api_version:
        kwargs["api_version"] = s.api_version
    return kwargs


def _agent_llm_endpoint(agent_id: int) -> tuple:
    """(model, api_base, api_key, api_version) of the agent's model for the SQL tools."""
    from apowerb.core.agent_helpers import load_agent_model_params

    from apowerb.helpers.model_names import default_api_base

    model, model_params = load_agent_model_params(agent_id)
    api_base = model_params.get("model_api_base") or default_api_base(model)
    api_version = model_params.get("model_api_version") or model_params.get("api_version")
    return model, api_base, model_params.get("model_api_key"), api_version


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
        [tool_text_to_sql, tool_get_database_schema, tool_text_to_sql_explain,
         tool_business_analyst]
    """
    agent_id = int(agent_name.replace("agent", ""))
    agent_model_name, api_base, api_key, api_version = _agent_llm_endpoint(agent_id)
    # Best-effort: a missing identity costs the usage row's labels, never
    # the tools themselves.
    try:
        usage_agent_name, owner_id, billed_to_thaink2 = _load_agent_usage_identity(agent_id)
    except Exception as e:
        logger.warning("[TEXT_TO_SQL] usage identity of %s not loaded: %s", agent_name, e)
        usage_agent_name, owner_id, billed_to_thaink2 = "", None, False
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
        api_version=api_version, agent_name=agent_name,
        db_ok=db_ok, db_type=db_type, db_host=db_host, db_port=db_port, db_name=db_name,
        db_user=db_user, db_password=db_password, db_schema=db_schema,
        db_include=db_include, schema_samples=schema_samples,
        cache={}, cache_ts=0.0,
        agent_id=agent_id, usage_agent_name=usage_agent_name,
        owner_id=owner_id, billed_to_thaink2=billed_to_thaink2,
    )

    logger.info(
        "[TEXT_TO_SQL] agent=%s model=%s api_base=%s %s schema_samples=%s",
        agent_name, agent_model_name, api_base or "not set", db_status, schema_samples,
    )

    # Thin wrappers — capture agent_name, delegate to module-level _impl functions.
    # ``tool_context`` is injected by ADK and hidden from the model; it gives
    # the session the tools' own LLM calls are recorded against.
    def tool_text_to_sql(
        question: str, max_rows: int = 100, include_sql: bool = True, tool_context=None
    ) -> dict:
        """Converts natural language to SQL and executes it against the configured database.

        max_rows is capped at 200; ``truncated`` is true when more rows exist.
        """
        with _tool_call_scope(tool_context):
            return _tool_text_to_sql_impl(agent_name, question, max_rows, include_sql)

    def tool_get_database_schema(refresh: bool = False, tables: list[str] | None = None) -> dict:
        """Retrieves the database schema including tables, columns, and relationships.

        Compact form per table: ``columns`` ("name type"), ``pk``, ``fk``
        ("col->table.col"). Pass ``tables`` (list of table names) to get only
        those tables; the output is capped and lists ``omitted_tables`` when cut.
        Pass refresh=True to bypass the cache and re-introspect the database.
        """
        return _tool_get_database_schema_impl(agent_name, refresh=refresh, tables=tables)

    def tool_text_to_sql_explain(question: str, tool_context=None) -> dict:
        """Generates SQL from natural language with explanation, without executing it."""
        with _tool_call_scope(tool_context):
            return _tool_text_to_sql_explain_impl(agent_name, question)

    def tool_business_analyst(
        question: str, max_steps: int = 4, max_rows: int = 100, tool_context=None
    ) -> dict:
        """Answers a business question through a multi-step SQL investigation.

        Runs several chained SELECT queries (not just one), then returns a
        narrative analysis with findings, recommendations and an optional chart
        spec. max_steps is capped at 6, max_rows at 50; each step returns at
        most 20 rows (``row_count`` gives the real count). Use this for open analytical questions ('why did X drop?', 'compare
        A vs B', 'what are the trends in Y?'); use tool_text_to_sql for a single
        direct lookup.
        """
        with _tool_call_scope(tool_context):
            return _tool_business_analyst_impl(agent_name, question, max_steps, max_rows)

    return [
        tool_text_to_sql,
        tool_get_database_schema,
        tool_text_to_sql_explain,
        tool_business_analyst,
    ]