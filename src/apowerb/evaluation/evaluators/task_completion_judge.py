"""LLM-judge evaluator: task completion / intent resolution.

Maps to Azure AI Foundry's "System" family. Reconstructs the conversation
transcript from ADK's own `events` table -- the ground truth of what was
actually said -- not from `llm_usage`, which only carries token counts.

Hard constraint: the judge model must never be the model being judged
(self-preference bias: a model scores its own outputs higher than an
independent judge would). This module takes the judged model as an
explicit argument and refuses to run if it resolves to the same model as
the configured judge.
"""

from __future__ import annotations

import json
import logging
import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import quoted_name

from apowerb.configs.settings import get_settings
from apowerb.evaluation.evaluators.base import EvaluationOutcome

logger = logging.getLogger(__name__)


def _events_sql():
    # `events` IS modelled by the ADK/core ORM elsewhere, but this module
    # reads it with raw SQL for the same reason as the tool evaluator: no
    # dependency on ADK's internal session-service models. Schema must be
    # qualified by hand for the same reason -- see tool_execution_outcome.py.
    # NOTE: `author` is not a DB column -- it lives inside `event_data`
    # (ADK's Event.author), verified against real DEV rows on 2026-08-10.
    schema = quoted_name(get_settings().db_schema, quote=True)
    return text(
        f"SELECT event_data FROM {schema}.events "
        "WHERE app_name = :app_name AND user_id = :user_id AND session_id = :session_id "
        "ORDER BY timestamp ASC"
    )

_JUDGE_SYSTEM_PROMPT = (
    "You are an impartial evaluator of AI agent conversations. You took no "
    "part in the conversation below and have no stake in its outcome. Read "
    "the transcript and decide whether the agent resolved the user's "
    "request.\n\n"
    "Score `task_completion` from 0.0 (not resolved at all) to 1.0 (fully "
    "resolved). Score `intent_resolution` from 0.0 (misunderstood the "
    "request) to 1.0 (correctly understood it), independently of whether "
    "it was completed.\n\n"
    "Reply with ONLY a JSON object, no markdown fence: "
    '{"task_completion": <float>, "intent_resolution": <float>, '
    '"rationale": "<one sentence, in English>"}'
)


class SameJudgeError(ValueError):
    """Raised when the configured judge model equals the judged model."""


def _extract_transcript(rows) -> list[dict]:
    """Turn ADK `events` rows into a compact (role, text) transcript.

    Function calls and their responses are rendered as short markers
    rather than dropped: a judge blind to tool use over-credits an agent
    that "just talked a lot" as having completed the task.
    """
    transcript: list[dict] = []
    for (event_data,) in rows:
        event_data = event_data or {}
        content = event_data.get("content") or {}
        role = content.get("role") or event_data.get("author")
        for part in content.get("parts") or []:
            if part.get("text"):
                transcript.append({"role": role, "text": part["text"]})
            elif "function_call" in part:
                fc = part["function_call"]
                transcript.append(
                    {
                        "role": role,
                        "text": f"[called tool {fc.get('name')} with {fc.get('args')}]",
                    }
                )
            elif "function_response" in part:
                fr = part["function_response"]
                transcript.append(
                    {
                        "role": role,
                        "text": f"[tool {fr.get('name')} returned {fr.get('response')}]",
                    }
                )
    return transcript


def _parse_judge_json(raw: str) -> dict:
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        raise ValueError(f"judge did not return a JSON object: {raw!r}")
    return json.loads(match.group(0))


def _same_model(judge_model: str, judged_model: str) -> bool:
    """Exact match once the litellm provider prefix is stripped. No fuzzy
    "close enough" heuristic -- that judgment call belongs to whoever
    configures the judge, not to this guard.
    """

    def _norm(model: str) -> str:
        return model.split("/", 1)[-1].strip().lower()

    return _norm(judge_model) == _norm(judged_model)


async def evaluate_task_completion(
    db: AsyncSession,
    *,
    app_name: str,
    user_id: str,
    session_id: str,
    judged_model: str,
) -> EvaluationOutcome:
    settings = get_settings()
    judge_model = (settings.evaluation_judge_model or "").strip()
    judge_key = (settings.evaluation_judge_api_key or "").strip()
    if not judge_model or not judge_key:
        raise RuntimeError(
            "EVALUATION_JUDGE_MODEL / EVALUATION_JUDGE_API_KEY are not configured."
        )
    if _same_model(judge_model, judged_model):
        raise SameJudgeError(
            f"refusing to judge {judged_model!r} with {judge_model!r}: "
            "configure a judge from a different model/provider."
        )

    rows = (
        await db.execute(
            _events_sql(),
            {"app_name": app_name, "user_id": user_id, "session_id": session_id},
        )
    ).fetchall()
    transcript = _extract_transcript(rows)
    if not transcript:
        return EvaluationOutcome(
            evaluator="task_completion_judge",
            kind="llm_judge",
            score=0.0,
            passed=False,
            details={"error": "empty transcript", "session_id": session_id},
        )

    transcript_text = "\n".join(f"{turn['role']}: {turn['text']}" for turn in transcript)

    import litellm

    response = await litellm.acompletion(
        model=judge_model,
        api_key=judge_key,
        messages=[
            {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": transcript_text[:20_000]},
        ],
        temperature=0.0,
        max_tokens=400,
        timeout=30,
        num_retries=1,
    )
    parsed = _parse_judge_json(response.choices[0].message.content)
    task_completion = float(parsed.get("task_completion", 0.0))
    intent_resolution = float(parsed.get("intent_resolution", 0.0))

    return EvaluationOutcome(
        evaluator="task_completion_judge",
        kind="llm_judge",
        score=round((task_completion + intent_resolution) / 2, 4),
        passed=task_completion >= 0.7,
        details={
            "session_id": session_id,
            "judged_model": judged_model,
            "judge_model": judge_model,
            "task_completion": task_completion,
            "intent_resolution": intent_resolution,
            "rationale": parsed.get("rationale"),
            "turns": len(transcript),
        },
    )
