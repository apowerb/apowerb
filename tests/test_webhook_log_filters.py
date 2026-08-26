"""Server-side filters on /api/webhooks/logs.

Filtering in the browser would only ever cover the page the client holds,
turning "show me the failures" into "show me the failures among the last 40" --
the same answer on a healthy day and a catastrophic one. These tests pin the
parts that decide what the query selects.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from apowerb.routers.webhooks import (
    _LOG_STATUSES,
    _STATUS_GROUPS,
    _parse_log_moment,
)


class TestParseMoment:
    def test_a_bare_date_starts_the_day(self):
        assert _parse_log_moment("2026-08-21", "since") == datetime(
            2026, 8, 21, 0, 0, 0, tzinfo=timezone.utc
        )

    def test_a_bare_until_date_covers_the_whole_day(self):
        """`until=2026-08-21` must include 21 August.

        Comparing a bare date against a timestamp otherwise cuts at midnight
        and silently drops the very day the operator asked for -- a filter
        that returns fewer rows than it should is indistinguishable from a
        quiet system.
        """
        got = _parse_log_moment("2026-08-21", "until")
        assert got.hour == 23 and got.minute == 59 and got.second == 59

    def test_a_full_datetime_is_left_alone(self):
        got = _parse_log_moment("2026-08-21T14:43:00", "until")
        assert (got.hour, got.minute) == (14, 43)

    def test_a_naive_moment_becomes_utc(self):
        assert _parse_log_moment("2026-08-21T06:00:00", "since").tzinfo is not None

    def test_an_explicit_offset_is_preserved(self):
        got = _parse_log_moment("2026-08-21T14:43:00+02:00", "since")
        assert got.utcoffset().total_seconds() == 7200

    def test_garbage_is_refused_not_ignored(self):
        with pytest.raises(HTTPException) as exc:
            _parse_log_moment("hier", "since")
        assert exc.value.status_code == 422
        assert "since" in str(exc.value.detail)


class TestStatusVocabulary:
    def test_failed_covers_both_stored_states(self):
        """An operator hunting failures means error AND retrying: a row that
        exhausted its attempts and one still bouncing are both 'not through'."""
        assert set(_STATUS_GROUPS["failed"]) == {"error", "retrying"}

    def test_every_stored_status_is_selectable(self):
        assert _LOG_STATUSES == {
            "pending",
            "in_progress",
            "success",
            "error",
            "retrying",
        }

    def test_groups_do_not_collide_with_real_statuses(self):
        """A group name that shadowed a status would make one unreachable."""
        assert not (set(_STATUS_GROUPS) & _LOG_STATUSES)
