"""LLM-judge evaluator: hallucination -- DEGRADED, ASSUMED.

Maps to Azure AI Foundry's "Quality" family (groundedness), but cannot
deliver what that family measures today. Groundedness needs the source
chunks an agent relied on (e.g. what the RAG tool actually retrieved), and
the RAG tool in this codebase does not log those chunks separately from
its own response (see `apowerb-eval-spec.md` §2/§5.5) -- there is no ground
truth to check a claim against.

What this evaluator actually delivers: a judge's read of the transcript's
*internal* plausibility -- does the agent assert specifics (numbers,
dates, names, facts) that are unsupported by, or contradict, anything
earlier in the same conversation. That is a materially weaker signal than
groundedness, and every result says so explicitly via
`details["grounding"] = "unavailable"` so nothing downstream can mistake
`score` for an anchored-in-sources hallucination score.

Same hard constraint as `task_completion_judge.py`: the judge must never
be the model being judged. `SameJudgeError` is imported from there rather
than redefined.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.evaluation.evaluators._shared_judge import (
    extract_usage,
    resolve_judge,
    fetch_transcript,
    parse_judge_json,
    same_model,
    same_provider,
    transcript_text,
)
from apowerb.evaluation.evaluators.base import EvaluationOutcome
from apowerb.evaluation.evaluators.task_completion_judge import SameJudgeError

logger = logging.getLogger(__name__)

_JUDGE_SYSTEM_PROMPT = (
    "You are an impartial evaluator of AI agent conversations. You took no "
    "part in the conversation below and have no stake in its outcome.\n\n"
    "IMPORTANT: you do not have access to the sources (documents, database "
    "rows, tool results) the agent may have used -- only the transcript "
    "below. You cannot verify any claim against ground truth. Judge only "
    "the transcript's INTERNAL plausibility: does the agent assert "
    "specific facts, numbers, dates or names that are unsupported by, or "
    "contradict, anything earlier in this same conversation.\n\n"
    "Score `internal_plausibility` from 0.0 (the agent invents specific, "
    "unsupported, or self-contradicting claims) to 1.0 (nothing asserted "
    "goes beyond what the conversation itself supports).\n\n"
    "Reply with ONLY a JSON object, no markdown fence: "
    '{"internal_plausibility": <float>, "rationale": "<one sentence, in English>"}'
)


async def evaluate_hallucination(
    db: AsyncSession,
    *,
    app_name: str,
    user_id: str,
    session_id: str,
    judged_model: str,
    judge_model: str | None = None,
    judge_api_key: str | None = None,
) -> EvaluationOutcome:
    judge_model, judge_key, is_byom = resolve_judge(judge_model, judge_api_key)
    if not judge_model or not judge_key:
        raise RuntimeError(
            "EVALUATION_JUDGE_MODEL / EVALUATION_JUDGE_API_KEY are not configured."
        )
    if same_model(judge_model, judged_model):
        raise SameJudgeError(
            f"refusing to judge {judged_model!r} with {judge_model!r}: "
            "configure a judge from a different model/provider."
        )

    transcript = await fetch_transcript(
        db, app_name=app_name, user_id=user_id, session_id=session_id
    )
    if not transcript:
        return EvaluationOutcome.not_applicable(
            evaluator="hallucination",
            kind="llm_judge",
            reason="the session has no transcript to judge",
            session_id=session_id,
            grounding="unavailable",
        )

    import litellm

    response = await litellm.acompletion(
        model=judge_model,
        api_key=judge_key,
        messages=[
            {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
            {"role": "user", "content": transcript_text(transcript)[:20_000]},
        ],
        temperature=0.0,
        # See task_completion_judge.py: a reasoning model spends this
        # budget thinking before it writes anything. Do not go below 2000.
        max_tokens=2000,
        timeout=60,
        num_retries=1,
    )
    message = response.choices[0].message
    content = getattr(message, "content", None)
    if not content:
        usage = getattr(response, "usage", None)
        details = getattr(usage, "completion_tokens_details", None)
        reasoning = getattr(details, "reasoning_tokens", None)
        raise RuntimeError(
            "the judge returned no content "
            f"(finish_reason={getattr(response.choices[0], 'finish_reason', None)}, "
            f"reasoning_tokens={reasoning}). A reasoning model may have spent "
            "the whole completion budget before answering."
        )
    parsed = parse_judge_json(content)
    internal_plausibility = float(parsed.get("internal_plausibility", 0.0))

    return EvaluationOutcome(
        evaluator="hallucination",
        kind="llm_judge",
        score=round(internal_plausibility, 4),
        passed=internal_plausibility >= 0.7,
        details={
            "session_id": session_id,
            "judged_model": judged_model,
            "judge_model": judge_model,
            "internal_plausibility": internal_plausibility,
            # `run_service._record_judge_usage` reads these two: without
            # them this judge burns tokens nobody is billed for.
            "judge_is_byom": is_byom,
            "judge_usage": extract_usage(response),
            "rationale": parsed.get("rationale"),
            "turns": len(transcript),
            "judge_shares_provider_with_judged": same_provider(
                judge_model, judged_model
            ),
            # Never a groundedness score: no source chunks are logged for
            # this evaluator to check claims against. See module docstring.
            "grounding": "unavailable",
        },
    )
