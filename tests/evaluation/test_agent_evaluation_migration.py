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

from apowerb.helpers.agent_evaluation_migration import ensure_agent_evaluation_table


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
