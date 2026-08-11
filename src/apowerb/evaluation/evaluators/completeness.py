"""LLM-judge evaluator: completeness.

Maps to Azure AI Foundry's "Quality" family (answer completeness). Reads
the same `events` transcript as `task_completion_judge.py` and asks an
independent judge whether the final response covers everything the user
asked for -- as opposed to whether the task got done at all
(`task_completion_judge`) or whether the responses stay consistent with
each other (`coherence.py`). An agent can complete a task coherently and
still leave half the request unanswered; this evaluator is what catches
that.

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
from apowerb.evaluation.evaluators.base import EvaluationOutcome, rationale_language
from apowerb.evaluation.evaluators.task_completion_judge import SameJudgeError

logger = logging.getLogger(__name__)

def _judge_system_prompt(locale: str | None) -> str:
    return (
        "You are an impartial evaluator of AI agent conversations. You took no "
        "part in the conversation below and have no stake in its outcome. Read "
        "the transcript and judge only whether the agent's response covers "
        "everything the user's request asked for -- not whether it was "
        "resolved correctly, and not whether earlier turns were consistent.\n\n"
        "Score `completeness` from 0.0 (most of the request was left "
        "unanswered) to 1.0 (every part of the request was addressed).\n\n"
        "Reply with ONLY a JSON object, no markdown fence: "
        f'{{"completeness": <float>, "rationale": "<one sentence, in {rationale_language(locale)}>"}}'
    )


async def evaluate_completeness(
    db: AsyncSession,
    *,
    app_name: str,
    user_id: str,
    session_id: str,
    judged_model: str,
    judge_model: str | None = None,
    judge_api_key: str | None = None,
    locale: str | None = None,
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
            evaluator="completeness",
            kind="llm_judge",
            reason="the session has no transcript to judge",
            session_id=session_id,
        )

    import litellm

    response = await litellm.acompletion(
        model=judge_model,
        api_key=judge_key,
        messages=[
            {"role": "system", "content": _judge_system_prompt(locale)},
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
    completeness = float(parsed.get("completeness", 0.0))

    return EvaluationOutcome(
        evaluator="completeness",
        kind="llm_judge",
        score=round(completeness, 4),
        passed=completeness >= 0.7,
        details={
            "session_id": session_id,
            "judged_model": judged_model,
            "judge_model": judge_model,
            "completeness": completeness,
            # `run_service._record_judge_usage` reads these two: without
            # them this judge burns tokens nobody is billed for.
            "judge_is_byom": is_byom,
            "judge_usage": extract_usage(response),
            "rationale": parsed.get("rationale"),
            "turns": len(transcript),
            "judge_shares_provider_with_judged": same_provider(
                judge_model, judged_model
            ),
            "criteria": [
                {"name": "completeness", "value": completeness, "kind": "score"},
                {"name": "turns", "value": len(transcript), "kind": "count"},
            ],
        },
    )
