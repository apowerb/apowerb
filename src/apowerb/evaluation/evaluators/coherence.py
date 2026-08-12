"""LLM-judge evaluator: coherence.

Maps to Azure AI Foundry's "Quality" family. Reads the same `events`
transcript as `task_completion_judge.py` and asks an independent judge
whether the agent's successive responses stay consistent with each other
-- no self-contradiction across turns -- as opposed to whether the task
got done (that is `task_completion_judge`) or whether the request was
fully covered (that is `completeness.py`).

Same hard constraint as `task_completion_judge.py`: the judge must never
be the model being judged. `SameJudgeError` is imported from there rather
than redefined, so there is exactly one definition of that guard.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.configs.settings import get_settings
from apowerb.evaluation.evaluators._shared_judge import (
    attach_billing,
    extract_usage,
    resolve_judge,
    fetch_transcript,
    parse_judge_json,
    same_model,
    same_provider,
    transcript_text,
    truncate_transcript,
)
from apowerb.evaluation.evaluators.base import EvaluationOutcome, rationale_language
from apowerb.evaluation.evaluators.task_completion_judge import SameJudgeError

logger = logging.getLogger(__name__)

def _judge_system_prompt(locale: str | None) -> str:
    return (
        "You are an impartial evaluator of AI agent conversations. You took no "
        "part in the conversation below and have no stake in its outcome. Read "
        "the transcript and judge only whether the agent's successive "
        "responses are consistent with each other -- not whether the task was "
        "completed.\n\n"
        "Score `coherence` from 0.0 (the agent contradicts itself or its "
        "earlier statements/actions) to 1.0 (every response is consistent with "
        "what came before).\n\n"
        "Reply with ONLY a JSON object, no markdown fence: "
        f'{{"coherence": <float>, "rationale": "<one sentence, in {rationale_language(locale)}>"}}'
    )


async def evaluate_coherence(
    db: AsyncSession,
    *,
    app_name: str,
    user_id: str,
    session_id: str,
    judged_model: str,
    judge_model: str | None = None,
    judge_api_key: str | None = None,
    locale: str | None = None,
    transcript: list[dict] | None = None,
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

    # Every judge of a run reads the same transcript. When the caller has
    # already fetched it, reuse it: that saves a redundant query, and it
    # leaves this coroutine with no database work at all — which is what
    # lets the judges of one run be awaited concurrently on a session that
    # forbids concurrent operations.
    if transcript is None:
        transcript = await fetch_transcript(
            db, app_name=app_name, user_id=user_id, session_id=session_id
        )
    if not transcript:
        return EvaluationOutcome.not_applicable(
            evaluator="coherence",
            kind="llm_judge",
            reason="the session has no transcript to judge",
            session_id=session_id,
        )

    import litellm

    transcript_body, truncated = truncate_transcript(transcript_text(transcript))

    response = await litellm.acompletion(
        model=judge_model,
        api_key=judge_key,
        messages=[
            {"role": "system", "content": _judge_system_prompt(locale)},
            {"role": "user", "content": transcript_body},
        ],
        temperature=0.0,
        # See task_completion_judge.py: a reasoning model spends this
        # budget thinking before it writes anything. Do not go below 2000.
        max_tokens=2000,
        # Bounds the model's own thinking budget independently of
        # transcript length -- see task_completion_judge.py.
        reasoning_effort="low",
        timeout=60,
        num_retries=1,
    )
    message = response.choices[0].message
    content = getattr(message, "content", None)
    finish_reason = getattr(response.choices[0], "finish_reason", None)
    if not content:
        usage = getattr(response, "usage", None)
        usage_details = getattr(usage, "completion_tokens_details", None)
        reasoning = getattr(usage_details, "reasoning_tokens", None)
        raise attach_billing(
            RuntimeError(
                "the judge returned no content "
                f"(finish_reason={finish_reason}, reasoning_tokens={reasoning}). "
                "A reasoning model may have spent the whole completion budget "
                "before answering."
            ),
            response=response, judge_model=judge_model, judge_is_byom=is_byom,
        )
    try:
        parsed = parse_judge_json(content)
    except ValueError as exc:
        reason = str(exc)
        if finish_reason == "length":
            reason = (
                "the judge's response was truncated before completing its "
                f"JSON verdict (finish_reason=length): {content!r}"
            )
        raise attach_billing(
            ValueError(reason),
            response=response, judge_model=judge_model, judge_is_byom=is_byom,
        ) from exc

    if "coherence" not in parsed:
        raise attach_billing(
            ValueError("judge response missing required key(s): ['coherence']"),
            response=response, judge_model=judge_model, judge_is_byom=is_byom,
        )
    coherence = float(parsed["coherence"])
    score = round(coherence, 4)

    return EvaluationOutcome(
        evaluator="coherence",
        kind="llm_judge",
        score=score,
        passed=score >= get_settings().evaluation_pass_threshold_coherence,
        details={
            "session_id": session_id,
            "judged_model": judged_model,
            "judge_model": judge_model,
            "coherence": coherence,
            # `run_service._record_judge_usage` reads these two: without
            # them this judge burns tokens nobody is billed for.
            "judge_is_byom": is_byom,
            "judge_usage": extract_usage(response),
            "rationale": parsed.get("rationale"),
            "turns": len(transcript),
            "judge_shares_provider_with_judged": same_provider(
                judge_model, judged_model
            ),
            "truncated": truncated,
            "criteria": [
                {"name": "coherence", "value": coherence, "kind": "score"},
                {"name": "turns", "value": len(transcript), "kind": "count"},
            ],
        },
    )
