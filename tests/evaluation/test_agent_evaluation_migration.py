"""Unit tests for the `agent_evaluation_results` boot migration.

Uses mocks rather than test_core_tables.py's in-memory SQLite engine:
`EvaluationResult.details` is `dialects.postgresql.JSONB`, deliberately
Postgres-only (see evaluation/models.py), and the SQLite dialect refuses to
compile it (`CompileError: can't render element of type JSONB`) -- unlike
the core models, which never use a Postgres-specific column type. The real
DDL was exercised once against a live Postgres schema (th2agent_dev on
OVH_DEV_VM) as a manual boot-equivalent check; see the dev report for that
evidence.
"""

from unittest.mock import MagicMock, patch

from apowerb.helpers.agent_evaluation_migration import (
    ensure_agent_evaluation_run_id_column,
    ensure_agent_evaluation_table,
)


@patch("apowerb.helpers.agent_evaluation_migration.inspect")
def test_creates_table_when_missing(mock_inspect):
    mock_inspect.return_value.get_table_names.return_value = []
    engine = MagicMock()

    with patch("apowerb.evaluation.models.EvaluationResult.__table__") as mock_table:
        ensure_agent_evaluation_table(engine=engine)

    mock_table.create.assert_called_once_with(bind=engine, checkfirst=True)


@patch("apowerb.helpers.agent_evaluation_migration.inspect")
def test_skips_creation_when_table_already_exists(mock_inspect):
    mock_inspect.return_value.get_table_names.return_value = ["agent_evaluation_results"]
    engine = MagicMock()

    with patch("apowerb.evaluation.models.EvaluationResult.__table__") as mock_table:
        ensure_agent_evaluation_table(engine=engine)

    mock_table.create.assert_not_called()


@patch("apowerb.helpers.agent_evaluation_migration.inspect")
def test_never_raises_when_the_migration_fails(mock_inspect):
    """A boot migration failure must be logged, never crash the process --
    same contract as every other `ensure_*` helper in this project."""
    mock_inspect.side_effect = RuntimeError("db unreachable")
    engine = MagicMock()

    ensure_agent_evaluation_table(engine=engine)  # must not raise


@patch("apowerb.helpers.agent_evaluation_migration.inspect")
def test_never_touches_the_engine_it_did_not_create(mock_inspect):
    """Passing an explicit engine (as tests do) must not dispose of it --
    only an engine this function built itself is its own to close."""
    mock_inspect.return_value.get_table_names.return_value = ["agent_evaluation_results"]
    engine = MagicMock()

    ensure_agent_evaluation_table(engine=engine)

    engine.dispose.assert_not_called()


# ---------------------------------------------------------------------------
# run_id column (additive, backfilled from (session_id, created_at))
# ---------------------------------------------------------------------------


def _connect_mock(engine):
    """The mocked `conn` object yielded by `with engine.connect() as conn:`."""
    return engine.connect.return_value.__enter__.return_value


@patch("apowerb.helpers.agent_evaluation_migration.inspect")
def test_adds_and_backfills_run_id_column_when_missing(mock_inspect):
    mock_inspect.return_value.get_columns.return_value = [{"name": "id"}, {"name": "session_id"}]
    engine = MagicMock()

    ensure_agent_evaluation_run_id_column(engine=engine)

    conn = _connect_mock(engine)
    executed = " ".join(str(call.args[0]) for call in conn.execute.call_args_list)
    assert "ADD COLUMN run_id" in executed
    assert "gen_random_uuid()" in executed
    assert "SET NOT NULL" in executed
    assert "CREATE INDEX" in executed
    assert "run_id" in executed
    conn.commit.assert_called_once()


@patch("apowerb.helpers.agent_evaluation_migration.inspect")
def test_backfills_orphans_when_the_column_is_already_there(mock_inspect):
    """Rows written while run_id was nullable carry none, and a result
    without one is invisible to every screen that groups by it. Stopping at
    "the column exists" left that hole open with nothing able to close it."""
    mock_inspect.return_value.get_columns.return_value = [{"name": "run_id"}]
    engine = MagicMock()
    conn = engine.connect.return_value.__enter__.return_value
    conn.execute.return_value.scalar.return_value = 29

    ensure_agent_evaluation_run_id_column(engine=engine)

    statements = " ".join(str(call.args[0]) for call in conn.execute.call_args_list)
    assert "run_id IS NULL" in statements
    assert "gen_random_uuid()" in statements
    # The constraint stays off: it was dropped because it had been applied
    # ahead of the code that fills the column, and every insert failed.
    assert "SET NOT NULL" not in statements
    conn.commit.assert_called_once()


@patch("apowerb.helpers.agent_evaluation_migration.inspect")
def test_writes_nothing_when_the_column_is_there_and_full(mock_inspect):
    mock_inspect.return_value.get_columns.return_value = [{"name": "run_id"}]
    engine = MagicMock()
    conn = engine.connect.return_value.__enter__.return_value
    conn.execute.return_value.scalar.return_value = 0

    ensure_agent_evaluation_run_id_column(engine=engine)

    # One COUNT, and nothing else: a boot must not rewrite rows for nothing.
    assert conn.execute.call_count == 1
    conn.commit.assert_not_called()


@patch("apowerb.helpers.agent_evaluation_migration.inspect")
def test_never_raises_when_the_run_id_migration_fails(mock_inspect):
    mock_inspect.side_effect = RuntimeError("db unreachable")
    engine = MagicMock()

    ensure_agent_evaluation_run_id_column(engine=engine)  # must not raise


@patch("apowerb.helpers.agent_evaluation_migration.inspect")
def test_run_id_migration_never_disposes_an_engine_it_did_not_create(mock_inspect):
    mock_inspect.return_value.get_columns.return_value = [{"name": "run_id"}]
    engine = MagicMock()

    ensure_agent_evaluation_run_id_column(engine=engine)

    engine.dispose.assert_not_called()
