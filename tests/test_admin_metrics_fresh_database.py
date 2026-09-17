"""The dashboard is the first admin screen, and it must survive a fresh database.

``sessions`` belongs to ADK, not to us: its session service creates it lazily,
in ``prepare_tables()``, just before its first database operation -- so on the
first conversation. None of our ``ensure_*`` boot migrations declares it. Until
somebody chats, the table does not exist, and a metrics route that counts it
unconditionally answers HTTP 500 on the very first screen of a new install
(observed on a quickstart stack, 2026-09-17: asyncpg UndefinedTableError,
relation "public.sessions" does not exist).

Zero sessions is the truthful answer there, so these pin both halves: absent
table counts as zero without ever querying it, present table is still counted.
"""

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ENCRYPT_KEY", "test-only-key-not-used-anywhere-else")

from apowerb.admin import router as router_module  # noqa: E402


def _admin(email="admin@example.com"):
    u = MagicMock()
    u.role = "ADMIN"
    u.email = email
    return u


def _db(statements, *, sessions_table_exists):
    """A db that answers like Postgres does about a table it does not have.

    ``to_regclass`` is the probe under test: NULL for an unknown relation,
    its name otherwise. Every statement is recorded so a test can assert on
    what was *not* sent.
    """
    db = AsyncMock()

    async def execute(stmt, params=None):
        sql = str(stmt)
        statements.append((sql, params or {}))
        result = MagicMock()
        if "to_regclass" in sql:
            result.scalar.return_value = "sessions" if sessions_table_exists else None
            return result
        if ".sessions" in sql and not sessions_table_exists:
            # What asyncpg actually does, and the whole point of the guard.
            raise AssertionError(f"la table absente a quand meme ete lue : {sql}")
        result.first.return_value = (0, 0, 0, 0)
        result.scalar.return_value = 0
        result.all.return_value = []
        result.scalars.return_value.all.return_value = ["a@example.com"]
        return result

    db.execute = AsyncMock(side_effect=execute)
    return db


@pytest.mark.asyncio
async def test_a_database_without_the_adk_table_reports_zero_sessions():
    statements = []
    db = _db(statements, sessions_table_exists=False)

    with patch(
        "apowerb.admin.router.administered_user_ids",
        new=AsyncMock(return_value=None),
    ):
        out = await router_module.platform_metrics(days=30, db=db, _=_admin())

    assert out.totals.sessions == 0
    assert all(point.sessions == 0 for point in out.daily)
    # The other panels still have to be answered: a fresh install shows an
    # empty dashboard, not a broken one.
    assert out.window_days == 30
    assert len(out.daily) == 31
    assert any("llm_usage" in sql for sql, _ in statements)


@pytest.mark.asyncio
async def test_the_probe_is_asked_before_any_session_query():
    """Asked once, up front -- not caught per query.

    A failed statement aborts the surrounding transaction, so wrapping the
    session counts in a ``try`` would take down every panel that comes after
    them instead of only the sessions figure.
    """
    statements = []
    db = _db(statements, sessions_table_exists=True)

    with patch(
        "apowerb.admin.router.administered_user_ids",
        new=AsyncMock(return_value=None),
    ):
        await router_module.platform_metrics(days=30, db=db, _=_admin())

    order = [i for i, (sql, _) in enumerate(statements) if "to_regclass" in sql]
    session_reads = [i for i, (sql, _) in enumerate(statements) if ".sessions" in sql]
    assert len(order) == 1, "la presence doit etre demandee une seule fois"
    assert session_reads, "la table presente doit etre lue"
    assert order[0] < min(session_reads)


@pytest.mark.asyncio
async def test_an_existing_table_is_still_counted():
    """The guard must not become a silent zero once the table is there."""
    statements = []
    db = _db(statements, sessions_table_exists=True)

    async def execute(stmt, params=None):
        sql = str(stmt)
        statements.append((sql, params or {}))
        result = MagicMock()
        if "to_regclass" in sql:
            result.scalar.return_value = "sessions"
            return result
        if "count(*) FROM" in sql and ".sessions" in sql:
            result.scalar.return_value = 7
            return result
        result.first.return_value = (0, 0, 0, 0)
        result.scalar.return_value = 0
        result.all.return_value = []
        result.scalars.return_value.all.return_value = ["a@example.com"]
        return result

    db.execute = AsyncMock(side_effect=execute)

    with patch(
        "apowerb.admin.router.administered_user_ids",
        new=AsyncMock(return_value=None),
    ):
        out = await router_module.platform_metrics(days=30, db=db, _=_admin())

    assert out.totals.sessions == 7
