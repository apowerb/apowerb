"""Deterministic evaluator: tool execution outcome.

Maps to Azure AI Foundry's "Process" family (tool call accuracy). Reads the
tool's OWN response payload to decide success or failure -- never the OTel
span status. Verified against real DEV data on 2026-08-10
(session_1786030573591, agent basic_agent/1234, `tool_pdf_to_images`): ADK
leaves `status_code` UNSET on a business failure of a tool (here, "file not
found") and only sets ERROR on a technical/transport failure. An evaluator
that trusted `status_code` alone would have scored that run 100% successful.

Correlation path (verified against `th2agent_dev`, no dedicated FK exists):

    session_id (ADK `sessions` / `llm_usage`)
      -> pulse_conversation_map.conversation_id  (exact string match)
      -> pulse_conversation_map.trace_id
      -> pulse_spans.trace_id, filtered to name LIKE 'execute_tool %'

`invocation_id` is not usable for this join: it is shared by the whole
agent tree of one turn, and pulse_spans attributes do not carry it at all --
only `pulse_conversation_map` bridges the ADK session and the OTel trace.
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import quoted_name

from apowerb.configs.settings import get_settings
from apowerb.evaluation.evaluators.base import EvaluationOutcome

logger = logging.getLogger(__name__)


def _schema() -> str:
    """th2pulse writes to the same DB but is not modelled by the core ORM
    (separate repo -- see the OSS/commercial split), so these queries are
    raw SQL and must be schema-qualified by hand: unlike ORM-declared
    tables, `text()` does not pick up `Settings.db_schema` on its own.
    """
    return str(quoted_name(get_settings().db_schema, quote=True))


def _trace_ids_sql():
    return text(
        f"SELECT trace_id FROM {_schema()}.pulse_conversation_map "
        "WHERE conversation_id = :session_id"
    )


def _tool_spans_sql():
    return text(
        "SELECT span_id, name, status_code, business_error, attributes "
        f"FROM {_schema()}.pulse_spans "
        "WHERE trace_id = ANY(:trace_ids) AND name LIKE 'execute_tool %' "
        "ORDER BY ts"
    )


def _tool_response_failed(attributes: dict) -> bool | None:
    """True/False read from the tool's own JSON response; None if the
    payload does not carry a `status` field we recognize."""
    raw = (attributes or {}).get("gcp.vertex.agent.tool_response")
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if isinstance(payload, dict):
        status = str(payload.get("status", "")).lower()
        if status:
            return status == "error"
    return None


async def evaluate_tool_execution_outcome(
    db: AsyncSession, session_id: str
) -> EvaluationOutcome:
    """Score every `execute_tool` span of a session by its real business
    outcome, and report what a naive, status_code-driven evaluator would
    have concluded -- so the gap is visible in `details` instead of being
    silently absorbed into a single, misleadingly clean number.
    """
    trace_rows = (
        await db.execute(_trace_ids_sql(), {"session_id": session_id})
    ).fetchall()
    trace_ids = [row[0] for row in trace_rows]
    if not trace_ids:
        return EvaluationOutcome(
            evaluator="tool_execution_outcome",
            kind="deterministic",
            score=0.0,
            passed=False,
            details={
                "error": "no trace mapped for this session_id in pulse_conversation_map",
                "session_id": session_id,
            },
        )

    rows = (await db.execute(_tool_spans_sql(), {"trace_ids": trace_ids})).fetchall()

    total = 0
    real_failures = 0
    naive_failures = 0
    per_tool: list[dict] = []
    for span_id, name, status_code, business_error, attributes in rows:
        total += 1
        tool_name = name.removeprefix("execute_tool ").strip()
        response_failed = _tool_response_failed(attributes)
        # Ground truth: the response payload when parseable, else the
        # `business_error` column the collector already derived from it.
        real_failed = response_failed if response_failed is not None else bool(business_error)
        naive_failed = (status_code or "").upper() == "ERROR"
        real_failures += int(real_failed)
        naive_failures += int(naive_failed)
        per_tool.append(
            {
                "span_id": span_id,
                "tool": tool_name,
                "status_code": status_code or "UNSET",
                "business_error_column": bool(business_error),
                "real_outcome": "error" if real_failed else "ok",
                "naive_outcome_from_status_code": "error" if naive_failed else "ok",
            }
        )

    success_rate = 1.0 - (real_failures / total) if total else 0.0
    naive_success_rate = 1.0 - (naive_failures / total) if total else 0.0

    return EvaluationOutcome(
        evaluator="tool_execution_outcome",
        kind="deterministic",
        score=round(success_rate, 4),
        passed=real_failures == 0,
        details={
            "session_id": session_id,
            "trace_ids": trace_ids,
            "tool_calls": total,
            "real_failures": real_failures,
            "naive_failures_from_status_code": naive_failures,
            "naive_success_rate": round(naive_success_rate, 4),
            "status_code_is_reliable": naive_failures == real_failures,
            "per_tool": per_tool,
        },
    )
