"""Data model for stored evaluation results.

Migrated on boot by `helpers/agent_evaluation_migration.py`
(`ensure_agent_evaluation_table` for the table itself,
`ensure_agent_evaluation_run_id_column` for the `run_id` column added
after the fact), the same idempotent, purely-additive pattern as every
other `ensure_*` table in this project. `poc_runner.py`
still prints its own report to stdout and never writes here -- it is a
standalone script, unrelated to the HTTP path that now persists through
this model. Kept in the OSS core (not `apowerb-commercial`) because
evaluation, like tracing and usage, is a cross-cutting concern the
commercial extensions need too (see the OSS/commercial split documented in
`apowerb-commercial/CLAUDE.md`).
"""

from __future__ import annotations

from sqlalchemy import JSON, Boolean, Column, DateTime, Float, Index, Integer, String, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB

from apowerb.helpers.database import Base

# Real jsonb on Postgres (production, and every environment this table is
# actually created in) -- but SQLite, used only as a stand-in engine by
# tests/test_core_tables.py, cannot compile `dialects.postgresql.JSONB`.
# `Base.metadata` is shared process-wide: any test that imports this module
# registers `EvaluationResult` on it, so `ensure_core_tables()`'s SQLite
# fixture would otherwise fail to create the *unrelated* `user` table too,
# the moment both are collected in the same pytest run. `with_variant` keeps
# jsonb where it has always run, and only swaps the type where SQLAlchemy's
# generic JSON already stood in for it.
_DETAILS_TYPE = JSONB().with_variant(JSON(), "sqlite")


class EvaluationResult(Base):
    """One evaluator's verdict on one session.

    One row per (evaluator, session) pair -- a session scored by both the
    deterministic tool evaluator and an LLM judge produces two rows, not
    one row with two score columns. Adding a new evaluator is then a
    row-level change, never a migration.
    """

    __tablename__ = "agent_evaluation_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    agent_id = Column(Integer, nullable=False)
    # Shared by every result of one POST /run call -- see run_service.py.
    # Existing rows predating this column are backfilled by
    # helpers/agent_evaluation_migration.py's ensure_agent_evaluation_run_id_column,
    # grouped by (session_id, created_at): the six results of one run come
    # from a single transaction and so already share that pair to the
    # microsecond. SQLAlchemy's generic Uuid compiles to a native UUID on
    # Postgres and a CHAR(32) hex string on SQLite -- no with_variant needed,
    # unlike details below (JSONB has no such generic counterpart).
    run_id = Column(Uuid(as_uuid=True), nullable=False)
    session_id = Column(String(255), nullable=False)
    invocation_id = Column(String(255), nullable=True)
    evaluator_name = Column(String(100), nullable=False)
    # "deterministic" | "llm_judge"
    evaluator_kind = Column(String(20), nullable=False)
    # Null for deterministic evaluators.
    judge_model = Column(String(255), nullable=True)
    # Nullable: an evaluator that had nothing to judge stores no score. See
    # EvaluationOutcome.not_applicable -- flattening that to 0.0 would make a
    # non-instrumented session indistinguishable from a failing agent.
    score = Column(Float, nullable=True)
    passed = Column(Boolean, nullable=True)
    details = Column(_DETAILS_TYPE, nullable=False, server_default="{}")

    __table_args__ = (
        Index("ix_agent_eval_agent_created", "agent_id", "created_at"),
        Index("ix_agent_eval_session", "session_id"),
        Index("ix_agent_eval_evaluator", "evaluator_name", "created_at"),
        Index("ix_agent_eval_run_id", "run_id"),
    )
