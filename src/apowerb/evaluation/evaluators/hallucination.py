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
        f'{{"internal_plausibility": <float>, "rationale": "<one sentence, in {rationale_language(locale)}>"}}'
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
            evaluator="hallucination",
            kind="llm_judge",
            reason="the session has no transcript to judge",
            session_id=session_id,
            grounding="unavailable",
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

    if "internal_plausibility" not in parsed:
        raise attach_billing(
            ValueError(
                "judge response missing required key(s): ['internal_plausibility']"
            ),
            response=response, judge_model=judge_model, judge_is_byom=is_byom,
        )
    internal_plausibility = float(parsed["internal_plausibility"])
    score = round(internal_plausibility, 4)

    return EvaluationOutcome(
        evaluator="hallucination",
        kind="llm_judge",
        score=score,
        passed=score >= get_settings().evaluation_pass_threshold_hallucination,
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
            "truncated": truncated,
            # Never a groundedness score: no source chunks are logged for
            # this evaluator to check claims against. See module docstring.
            "grounding": "unavailable",
            "criteria": [
                {"name": "internal_plausibility", "value": internal_plausibility, "kind": "score"},
                {"name": "turns", "value": len(transcript), "kind": "count"},
                # False: this install cannot check claims against source
                # chunks -- see the module docstring and details["grounding"].
                {"name": "grounding", "value": False, "kind": "flag"},
            ],
        },
    )
