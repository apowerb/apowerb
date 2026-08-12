"""A result with no run_id must not take an agent's whole history down.

`run_id` was made nullable to unbreak a database where it had been added
NOT NULL ahead of the code that fills it. Rows written in that window carry
none — 29 of 107 on dev — and the history route grouped them into a NULL
bucket that `IN (:run_ids)` can never match back, since NULL = NULL is never
true in SQL. It then indexed [0] of an empty list and returned 500 for the
whole agent.
"""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apowerb.routers.evaluations import list_evaluation_runs


REAL_RUN = uuid.uuid4()


def _user(email="user@example.com"):
    return MagicMock(email=email, role="USER")


def _result(run_id, evaluator="tool_execution_outcome"):
    row = MagicMock()
    row.run_id = run_id
    row.session_id = "session_1"
    row.evaluator_name = evaluator
    row.evaluator_kind = "deterministic"
    row.score = 1.0
    row.passed = True
    row.details = {}
    return row


def _run_page(*run_ids):
    rows = []
    for run_id in run_ids:
        row = MagicMock()
        row.run_id = run_id
        row.created_at = datetime.now(timezone.utc)
        rows.append(row)
    return rows


@pytest.mark.asyncio
async def test_a_run_with_no_rows_is_skipped_not_fatal():
    """The NULL bucket: grouped in, never matched back."""
    db = AsyncMock()
    calls = {"n": 0}

    async def execute(*args, **kwargs):
        calls["n"] += 1
        result = MagicMock()
        if calls["n"] == 1:  # total
            result.scalar.return_value = 3
        elif calls["n"] == 2:  # the page of runs: one real, one NULL
            result.all.return_value = _run_page(REAL_RUN, None)
        else:  # the results, which only cover the real one
            result.scalars.return_value.all.return_value = [_result(REAL_RUN)]
        return result

    db.execute = AsyncMock(side_effect=execute)

    with patch(
        "apowerb.routers.evaluations.owned_agent_ids", new=AsyncMock(return_value={1234})
    ):
        response = await list_evaluation_runs(
            agent_id=1234, limit=20, offset=0, db=db, current_user=_user()
        )

    # The real run survives; the unshowable one is dropped rather than
    # taking the response with it.
    assert len(response.items) == 1
    assert response.items[0].run_id == REAL_RUN
