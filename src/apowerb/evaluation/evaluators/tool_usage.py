"""Deterministic evaluator: tool usage efficiency.

Maps to Azure AI Foundry's "Process" family (task navigation efficiency).
Reads how an agent used its tools during a session -- call count, exact
repeats, turns spent, tokens spent -- from `pulse_spans`, the same
correlation path as `tool_execution_outcome.py`:

    session_id (ADK `sessions` / `llm_usage`)
      -> pulse_conversation_map.conversation_id  (exact string match)
      -> pulse_conversation_map.trace_id
      -> pulse_spans.trace_id

No LLM call: the only one of the four new evaluators that costs nothing to
run.

Token attributes live on `call_llm` spans only. ADK also emits a child
`generate_content` span per call; summing both would double the token
count for the same turn. Filtering strictly on `name = 'call_llm'` avoids
that -- verified against real DEV data (2026-08-07,
run_now_1201_1782986278_1786060801): `generate_content` spans there carry
no `gen_ai.usage.*` attributes at all, `call_llm` spans do.
"""

from __future__ import annotations

import logging

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import quoted_name

from apowerb.configs.settings import get_settings
from apowerb.evaluation.evaluators.base import EvaluationOutcome

logger = logging.getLogger(__name__)


def _schema() -> str:
    return str(quoted_name(get_settings().db_schema, quote=True))


def _trace_ids_sql():
    return text(
        f"SELECT trace_id FROM {_schema()}.pulse_conversation_map "
        "WHERE conversation_id = :session_id"
    )


def _spans_sql():
    return text(
        "SELECT span_id, name, attributes "
        f"FROM {_schema()}.pulse_spans "
        "WHERE trace_id = ANY(:trace_ids) "
        "AND (name = 'call_llm' OR name LIKE 'execute_tool %') "
        "ORDER BY ts"
    )


def _tool_call_signature(attributes: dict) -> tuple[str, str]:
    args = (attributes or {}).get("gcp.vertex.agent.tool_call_args") or "{}"
    return args


async def evaluate_tool_usage(db: AsyncSession, session_id: str) -> EvaluationOutcome:
    """Score how efficiently a session used its tools: how many turns it
    took, whether any tool call was repeated with identical arguments
    (a loop, not progress), and how many tokens the turns leading to those
    calls consumed.
    """
    try:
        trace_rows = (
            await db.execute(_trace_ids_sql(), {"session_id": session_id})
        ).fetchall()
    except SQLAlchemyError:
        logger.info(
            "[EVAL] Telemetry tables are unavailable; tool usage cannot be "
            "scored for session %s",
            session_id,
        )
        await db.rollback()
        return EvaluationOutcome.not_applicable(
            evaluator="tool_usage",
            kind="deterministic",
            reason=(
                "telemetry tables are unavailable — this evaluator needs "
                "th2pulse (pulse_conversation_map, pulse_spans)"
            ),
            session_id=session_id,
        )

    trace_ids = [row[0] for row in trace_rows]
    if not trace_ids:
        return EvaluationOutcome.not_applicable(
            evaluator="tool_usage",
            kind="deterministic",
            reason="no trace mapped for this session_id in pulse_conversation_map",
            session_id=session_id,
        )

    rows = (await db.execute(_spans_sql(), {"trace_ids": trace_ids})).fetchall()

    turns = 0
    input_tokens = 0
    output_tokens = 0
    cached_input_tokens = 0
    tool_calls: list[tuple[str, str]] = []
    for span_id, name, attributes in rows:
        attributes = attributes or {}
        if name == "call_llm":
            turns += 1
            input_tokens += int(attributes.get("gen_ai.usage.input_tokens") or 0)
            output_tokens += int(attributes.get("gen_ai.usage.output_tokens") or 0)
            cached_input_tokens += int(
                attributes.get("gen_ai.usage.cached_input_tokens") or 0
            )
        else:
            tool_name = name.removeprefix("execute_tool ").strip()
            args = _tool_call_signature(attributes)
            tool_calls.append((tool_name, args))

    if not tool_calls:
        return EvaluationOutcome.not_applicable(
            evaluator="tool_usage",
            kind="deterministic",
            reason="the session made no tool call",
            session_id=session_id,
            trace_ids=trace_ids,
        )

    counts: dict[tuple[str, str], int] = {}
    for signature in tool_calls:
        counts[signature] = counts.get(signature, 0) + 1

    duplicate_groups = [
        {"tool": tool, "args": args, "count": count}
        for (tool, args), count in counts.items()
        if count > 1
    ]
    duplicate_calls = sum(group["count"] - 1 for group in duplicate_groups)
    total = len(tool_calls)
    total_tokens = input_tokens + output_tokens

    duplicate_component = 1.0 - (duplicate_calls / total)

    # Turns and tokens were computed and stored but never entered the
    # score: 40 turns and 200k tokens for 3 tool calls scored 100%
    # efficient. A soft cap on tokens-per-call discounts that, but only
    # when an operator sets one -- 0 (the default) preserves the exact
    # prior score, since there is no ground truth here for what "normal"
    # resource use looks like across every possible agent. Same pattern as
    # the per-judge pass thresholds (point 9): configurable, not invented.
    soft_cap = get_settings().evaluation_tool_usage_tokens_per_call_soft_cap
    tokens_per_call = total_tokens / total if total else 0.0
    if soft_cap > 0 and tokens_per_call > soft_cap:
        resource_component = soft_cap / tokens_per_call
    else:
        resource_component = 1.0

    score = round(duplicate_component * resource_component, 4)

    return EvaluationOutcome(
        evaluator="tool_usage",
        kind="deterministic",
        score=score,
        passed=duplicate_calls == 0,
        details={
            "session_id": session_id,
            "trace_ids": trace_ids,
            "tool_calls": total,
            "distinct_tool_calls": len(counts),
            "duplicate_calls": duplicate_calls,
            "duplicate_groups": duplicate_groups,
            "turns": turns,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
            "cached_input_tokens": cached_input_tokens,
            "tokens_per_call": round(tokens_per_call, 4),
            "tokens_per_call_soft_cap": soft_cap,
            "criteria": [
                {"name": "tool_calls", "value": total, "kind": "count"},
                {"name": "distinct_tool_calls", "value": len(counts), "kind": "count"},
                {"name": "duplicate_calls", "value": duplicate_calls, "kind": "count"},
                {"name": "turns", "value": turns, "kind": "count"},
            ],
        },
    )
