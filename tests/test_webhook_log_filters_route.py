"""The /logs filters, exercised through the route rather than around it.

A first version of this bench only imported the constants and the date parser.
An external review pointed out the obvious: none of it called the endpoint, so
nothing proved that the filters reached the query at all, that they combined
with AND, that an unknown status was refused, or that the COUNT and the SELECT
saw the same predicates. These tests go through the app.

They inspect the compiled SQL rather than a database: what matters here is
which predicates the endpoint builds, and that the count and the page build the
same ones. Whether PostgreSQL then executes them correctly is its own concern,
and is not something a unit test can honestly claim.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.auth.dependencies import get_current_user
from apowerb.helpers.database import get_db
from apowerb.routers import webhooks as webhooks_router


class _Recorder:
    """Async session stub that records every statement it is handed."""

    def __init__(self):
        self.statements = []
        self.count_statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        res = MagicMock()
        res.scalars = MagicMock(
            return_value=MagicMock(all=MagicMock(return_value=[]))
        )
        return res

    async def scalar(self, stmt):
        self.count_statements.append(stmt)
        return 7


def _client(session):
    app = FastAPI()
    app.include_router(webhooks_router.router, prefix="/api")

    user = MagicMock()
    user.user_id = 42
    user.email = "operator@example.com"

    async def _db_override():
        yield session

    app.dependency_overrides[get_db] = _db_override
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app)


def _sql(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


@pytest.fixture
def session():
    return _Recorder()


class TestFiltersReachTheQuery:
    def test_status_failed_expands_to_both_stored_states(self, session):
        _client(session).get("/api/webhooks/logs?status=failed")
        sql = _sql(session.statements[0])
        assert "'error'" in sql and "'retrying'" in sql
        assert "'success'" not in sql

    def test_dates_bound_the_query_on_both_sides(self, session):
        _client(session).get(
            "/api/webhooks/logs?since=2026-08-21&until=2026-08-22"
        )
        sql = _sql(session.statements[0])
        assert "created_at >=" in sql and "created_at <=" in sql

    def test_search_covers_subject_and_sender(self, session):
        _client(session).get("/api/webhooks/logs?q=socomec")
        sql = _sql(session.statements[0]).lower()
        assert "email_subject" in sql and "email_sender" in sql

    def test_search_escapes_like_wildcards(self, session):
        """A subject containing '%' must match itself, not everything."""
        _client(session).get("/api/webhooks/logs?q=100%25")
        sql = _sql(session.statements[0])
        assert r"\%" in sql

    def test_uncategorized_selects_rows_without_the_key(self, session):
        _client(session).get("/api/webhooks/logs?classification=uncategorized")
        sql = _sql(session.statements[0])
        assert "IS NULL" in sql
        assert "NOT" in sql.upper()
        # A VALUE, not the mere presence of the word: 21 stored runs contain
        # "email_classification" only inside a validation error naming the
        # field the agent failed to produce, and those are uncategorised.
        assert '[^"]+' in sql

    def test_a_named_category_cannot_match_its_own_negation(self, session):
        """'ar' must not also select 'not_ar': the quoted value is matched."""
        _client(session).get("/api/webhooks/logs?classification=ar")
        sql = _sql(session.statements[0])
        # Tolerant to spacing, and anchored on the quotes so 'ar' cannot also
        # select 'not_ar'.
        assert '"email_classification"[[:space:]]*:[[:space:]]*"ar"' in sql

    def test_filters_combine_with_and(self, session):
        _client(session).get(
            "/api/webhooks/logs?status=success&q=abc&since=2026-08-01"
        )
        sql = _sql(session.statements[0])
        # Owner + status + search + since = four predicates, so at least three
        # ANDs join them.
        assert sql.upper().count(" AND ") >= 3
        for fragment in ("'success'", "%abc%", "2026-08-01"):
            assert fragment in sql, fragment
        # The only OR belongs to the search, and it is parenthesised so it
        # cannot widen the conjunction around it.
        assert " OR " not in sql.upper().split("(")[0]


class TestCountAndPageAgree:
    def test_the_count_uses_the_same_predicates_as_the_page(self, session):
        """A total computed on different predicates is a lie with a number on it."""
        _client(session).get("/api/webhooks/logs?status=failed&q=abc")
        page_sql = _sql(session.statements[0])
        count_sql = _sql(session.count_statements[0])

        for fragment in ("'error'", "'retrying'", "%abc%"):
            assert fragment in page_sql, fragment
            assert fragment in count_sql, fragment

    def test_the_total_is_returned(self, session):
        body = _client(session).get("/api/webhooks/logs").json()
        assert body["total"] == 7


class TestBadInputIsRefused:
    def test_an_unknown_status_is_a_422(self, session):
        resp = _client(session).get("/api/webhooks/logs?status=whatever")
        assert resp.status_code == 422
        assert "whatever" in resp.text

    def test_an_empty_status_is_a_422(self, session):
        """ is a caller asking to narrow the list with an empty hand.
        A falsy string used to fall through the guard and return everything."""
        assert _client(session).get("/api/webhooks/logs?status=").status_code == 422

    def test_the_order_is_total_so_paging_cannot_skip_a_row(self, session):
        """created_at alone is not a total order: two rows stored in the same
        instant can swap between requests, and with offset pagination a swap at
        a page boundary serves one row twice and another never."""
        _client(session).get("/api/webhooks/logs")
        sql = _sql(session.statements[0]).lower()
        assert "order by" in sql
        tail = sql.split("order by")[1]
        assert "created_at desc" in tail and "id desc" in tail

    def test_a_status_naming_nothing_is_a_422(self, session):
        """',,,' asked to narrow the list. Returning everything under that
        label hands back a result the caller will read as filtered."""
        assert _client(session).get("/api/webhooks/logs?status=,,,").status_code == 422

    def test_an_unparseable_date_is_a_422(self, session):
        assert _client(session).get("/api/webhooks/logs?since=hier").status_code == 422

    def test_no_filter_still_scopes_to_the_caller(self, session):
        """The owner filter is not one of the optional ones."""
        _client(session).get("/api/webhooks/logs")
        assert "user_id" in _sql(session.statements[0])
        assert "42" in _sql(session.statements[0])


class TestUntilCoversTheWholeDay:
    def test_a_bare_until_date_reaches_the_end_of_that_day(self, session):
        _client(session).get("/api/webhooks/logs?until=2026-08-21")
        sql = _sql(session.statements[0])
        assert "23:59:59" in sql

    def test_a_bare_since_date_starts_at_midnight(self, session):
        _client(session).get("/api/webhooks/logs?since=2026-08-21")
        assert "2026-08-21 00:00:00" in _sql(session.statements[0])


def test_the_parser_is_timezone_aware():
    got = webhooks_router._parse_log_moment("2026-08-21T06:00:00", "since")
    assert got.tzinfo is not None
    assert got == datetime(2026, 8, 21, 6, 0, tzinfo=timezone.utc)
