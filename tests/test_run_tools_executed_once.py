"""``agent_runs.tools_executed`` is writable once, and the database enforces it.

``NULL`` means "unknown" and blocks an automatic replay. That rule used to hold
only by convention in the application: any later ``UPDATE`` could rewrite the
column. A trigger created with the table now lets the column go from ``NULL``
to a value once and rejects every later write to it.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.pool import StaticPool

from apowerb.core import run_main

OWNER = "u@example.com"


@pytest.fixture
def store(monkeypatch, tmp_path):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _attach_public_schema(dbapi_connection, _record):  # pragma: no cover
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS public")

    run_store = run_main.run_store
    monkeypatch.setattr(run_store, "engine", engine)
    # The same entry point the application uses at startup.
    run_store.create_table()
    monkeypatch.setattr(run_main, "_run_input_dir", lambda run_id: tmp_path / run_id)
    return run_store


def _start() -> str:
    return run_main.start_run(
        trigger="workflow", owner_id=OWNER, agent_ids=["3"], config={"rows": 2}
    )


def _tools(store, run_id):
    with store.engine.begin() as conn:
        value = conn.execute(
            select(store.run_table.c.tools_executed).where(
                store.run_table.c.run_id == run_id
            )
        ).scalar_one()
    return json.loads(value) if value is not None else None


def test_tools_executed_can_be_written_once_from_null(store):
    run_id = _start()
    assert _tools(store, run_id) is None

    run_main.record_tools_executed(run_id, ["send_mail"])

    assert _tools(store, run_id) == ["send_mail"]


def test_a_second_write_fails_at_the_database_level(store):
    run_id = _start()
    run_main.record_tools_executed(run_id, ["send_mail"])

    with pytest.raises(DatabaseError, match="write-once"):
        run_main.record_tools_executed(run_id, [])

    assert _tools(store, run_id) == ["send_mail"]


def test_the_column_cannot_go_back_to_null_or_be_rewritten_with_raw_sql(store):
    run_id = _start()
    run_main.record_tools_executed(run_id, ["send_mail"])

    for statement in (
        "UPDATE {t} SET tools_executed = NULL WHERE run_id = :r",
        "UPDATE {t} SET tools_executed = tools_executed WHERE run_id = :r",
    ):
        with pytest.raises(DatabaseError, match="write-once"):
            with store.engine.begin() as conn:
                conn.execute(
                    text(statement.format(t=_qualified(store))), {"r": run_id}
                )

    assert _tools(store, run_id) == ["send_mail"]


def test_other_columns_stay_writable_after_tools_are_recorded(store):
    run_id = _start()
    run_main.record_tools_executed(run_id, ["send_mail"])

    run_main.finish_run(run_id, status=run_main.STATUS_ERROR, error_message="boom")

    with store.engine.begin() as conn:
        row = conn.execute(
            select(store.run_table.c.status, store.run_table.c.error_message).where(
                store.run_table.c.run_id == run_id
            )
        ).one()
    assert (row.status, row.error_message) == (run_main.STATUS_ERROR, "boom")
    assert _tools(store, run_id) == ["send_mail"]


def test_a_run_with_unknown_tools_stays_unknown(store):
    run_id = _start()

    run_main.settle_run(run_id, run_main.STATUS_ERROR, "boom", None)

    assert _tools(store, run_id) is None


def test_ensure_columns_is_idempotent(store):
    store.ensure_columns()
    store.ensure_columns()

    run_id = _start()
    run_main.record_tools_executed(run_id, ["a"])
    with pytest.raises(DatabaseError, match="write-once"):
        run_main.record_tools_executed(run_id, ["b"])


def _qualified(store) -> str:
    schema = f'"{store.db_schema}".' if store.db_schema else ""
    return f'{schema}"{store.table_name}"'
