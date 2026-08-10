"""Proposed v1 data model for stored evaluation results.

NOT migrated by this PR: no `create_all` call and no Alembic-equivalent
revision ships alongside it. `poc_runner.py` prints its report to stdout
and never writes here -- scoring a real DEV conversation must not change
the DEV schema before this model has been reviewed. Kept in the OSS core
(not `apowerb-commercial`) because evaluation, like tracing and usage, is a
cross-cutting concern the commercial extensions need too (see the OSS/
commercial split documented in `apowerb-commercial/CLAUDE.md`).
"""

from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, Float, Index, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB

from apowerb.helpers.database import Base


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
    session_id = Column(String(255), nullable=False)
    invocation_id = Column(String(255), nullable=True)
    evaluator_name = Column(String(100), nullable=False)
    # "deterministic" | "llm_judge"
    evaluator_kind = Column(String(20), nullable=False)
    # Null for deterministic evaluators.
    judge_model = Column(String(255), nullable=True)
    score = Column(Float, nullable=False)
    passed = Column(Boolean, nullable=False)
    details = Column(JSONB, nullable=False, server_default="{}")

    __table_args__ = (
        Index("ix_agent_eval_agent_created", "agent_id", "created_at"),
        Index("ix_agent_eval_session", "session_id"),
        Index("ix_agent_eval_evaluator", "evaluator_name", "created_at"),
    )
