"""LLM-judge evaluator: task completion / intent resolution.

Maps to Azure AI Foundry's "System" family. Reconstructs the conversation
transcript from ADK's own `events` table -- the ground truth of what was
actually said -- not from `llm_usage`, which only carries token counts.

Hard constraint: the judge model must never be the model being judged
(self-preference bias: a model scores its own outputs higher than an
independent judge would). This module takes the judged model as an
explicit argument and refuses to run if it resolves to the same model as
the configured judge.

Judge model selection: server default, or bring-your-own-model (BYOM).
Callers may pass `judge_model` / `judge_api_key` to run this evaluation
against their own judge instead of the server's shared one -- `run_service`
enforces at the HTTP boundary that a `judge_model` never arrives without
its key, but this function repeats the check (never fall back to the
server's key just because a caller-supplied key was empty). The BYOM key
is used for exactly one `litellm.acompletion` call and is never written to
`details`, never logged, never persisted.
"""

from __future__ import annotations

import json
import logging
import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import quoted_name

from apowerb.configs.settings import get_settings
from apowerb.evaluation.evaluators.base import EvaluationOutcome, rationale_language

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

def _judge_system_prompt(locale: str | None) -> str:
    # `rationale` addresses the person reading the screen, not the judged
    # conversation -- it follows the interface's locale. See
    # evaluators/base.rationale_language.
    return (
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
        f'"rationale": "<one sentence, in {rationale_language(locale)}>"}}'
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


def _truncate_transcript(text: str, *, limit: int = 20_000) -> tuple[str, bool]:
    """Keep the END of a transcript when it overflows, not the start --
    `_events_sql` reads `ORDER BY timestamp ASC`, so the tail is where the
    resolution lives. Duplicated in `_shared_judge.truncate_transcript` for
    coherence/completeness/hallucination rather than imported, to avoid a
    circular import (`_shared_judge` already imports `_extract_usage` from
    this module)."""
    if len(text) <= limit:
        return text, False
    return text[-limit:], True


def _attach_billing(
    exc: Exception, *, response, judge_model: str, judge_is_byom: bool
) -> Exception:
    """Tag a post-call failure with what the litellm call already cost, so
    `run_service._record_judge_usage` can still bill it. See
    `_shared_judge.attach_billing` (duplicated here for the same
    circular-import reason as `_truncate_transcript`)."""
    exc.judge_usage = _extract_usage(response)  # type: ignore[attr-defined]
    exc.judge_model = judge_model  # type: ignore[attr-defined]
    exc.judge_is_byom = judge_is_byom  # type: ignore[attr-defined]
    return exc


def _same_model(judge_model: str, judged_model: str) -> bool:
    """Exact match once the litellm provider prefix is stripped. No fuzzy
    "close enough" heuristic -- that judgment call belongs to whoever
    configures the judge, not to this guard.
    """

    def _norm(model: str) -> str:
        return model.split("/", 1)[-1].strip().lower()

    return _norm(judge_model) == _norm(judged_model)


def _same_provider(judge_model: str, judged_model: str) -> bool:
    """Same litellm provider prefix, e.g. both ``gemini/``.

    Self-preference is documented at the family and provider level, not only
    for the exact checkpoint, so a flash model judging a pro model of the
    same family is still exposed to it. Not an error — refusing it would
    leave installs with a single provider unable to evaluate at all — but it
    belongs in the record, next to the score it may have tilted.
    """

    def _provider(model: str) -> str:
        return model.split("/", 1)[0].strip().lower() if "/" in model else ""

    judge_provider = _provider(judge_model)
    return bool(judge_provider) and judge_provider == _provider(judged_model)


def _extract_usage(response) -> dict[str, int]:
    """Pull token counts off a litellm response for `llm_usage` accounting.

    Field names mirror `LlmUsage` columns directly (`thoughts_tokens` for
    reasoning, not litellm's `reasoning_tokens`) so `run_service` can pass
    this dict straight into the ORM row. A reasoning judge (gemini-2.5-pro
    et al.) spends part of `max_tokens` thinking before it answers -- that
    spend is real cost and must be counted, not dropped because it never
    became visible text.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "thoughts_tokens": 0,
            "cached_tokens": 0,
            "total_tokens": 0,
        }
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    total_tokens = int(getattr(usage, "total_tokens", 0) or (input_tokens + output_tokens))
    completion_details = getattr(usage, "completion_tokens_details", None)
    thoughts_tokens = int(getattr(completion_details, "reasoning_tokens", 0) or 0)
    prompt_details = getattr(usage, "prompt_tokens_details", None)
    cached_tokens = int(getattr(prompt_details, "cached_tokens", 0) or 0)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "thoughts_tokens": thoughts_tokens,
        "cached_tokens": cached_tokens,
        "total_tokens": total_tokens,
    }


async def evaluate_task_completion(
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
    settings = get_settings()
    is_byom = bool(judge_model)
    if is_byom:
        resolved_judge_model = judge_model.strip()
        resolved_judge_key = (judge_api_key or "").strip()
        if not resolved_judge_key:
            # The router is the primary 400 gate for this; repeated here so
            # a caller of this function directly can never make the shared
            # server key run someone else's model by leaving the key empty.
            raise RuntimeError(
                "judge_api_key is required when judge_model is provided "
                "(bring-your-own-model)."
            )
    else:
        resolved_judge_model = (settings.evaluation_judge_model or "").strip()
        resolved_judge_key = (settings.evaluation_judge_api_key or "").strip()

    if not resolved_judge_model or not resolved_judge_key:
        raise RuntimeError(
            "EVALUATION_JUDGE_MODEL / EVALUATION_JUDGE_API_KEY are not configured."
        )
    if _same_model(resolved_judge_model, judged_model):
        raise SameJudgeError(
            f"refusing to judge {judged_model!r} with {resolved_judge_model!r}: "
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
        # Nothing was said, so nothing can be judged. Scoring this 0.0 would
        # rank an empty session next to an agent that answered wrongly.
        return EvaluationOutcome.not_applicable(
            evaluator="task_completion_judge",
            kind="llm_judge",
            reason="the session has no transcript to judge",
            session_id=session_id,
        )

    raw_transcript_text = "\n".join(f"{turn['role']}: {turn['text']}" for turn in transcript)
    transcript_body, truncated = _truncate_transcript(raw_transcript_text)

    import litellm

    response = await litellm.acompletion(
        model=resolved_judge_model,
        api_key=resolved_judge_key,
        messages=[
            {"role": "system", "content": _judge_system_prompt(locale)},
            {"role": "user", "content": transcript_body},
        ],
        temperature=0.0,
        # A reasoning model spends this budget on its own thinking before it
        # writes anything: measured on gemini-2.5-pro, a short prompt burns
        # ~110 reasoning tokens for 15 of answer. On a 20k-character
        # transcript the old 400 was consumed entirely by the reasoning and
        # the call came back with content=None -- read as "the judge returned
        # no JSON", which sounds like a broken prompt rather than a budget.
        # The verdict itself is ~100 tokens; the rest is headroom.
        max_tokens=2000,
        # Bounds the model's OWN thinking budget (litellm maps this to
        # ~1024 reasoning tokens for Gemini) independently of transcript
        # length, instead of letting it compete with the verdict for the
        # same max_tokens -- that competition is what let reasoning models
        # consume the whole budget and return content=None (see
        # test_judge_budget.py).
        reasoning_effort="low",
        timeout=60,
        num_retries=1,
    )
    message = response.choices[0].message
    content = getattr(message, "content", None)
    finish_reason = getattr(response.choices[0], "finish_reason", None)
    if not content:
        # Say which of the two it was. "No JSON" sends the reader to the
        # prompt; an exhausted budget is a different fix entirely.
        usage = getattr(response, "usage", None)
        usage_details = getattr(usage, "completion_tokens_details", None)
        reasoning = getattr(usage_details, "reasoning_tokens", None)
        raise _attach_billing(
            RuntimeError(
                "the judge returned no content "
                f"(finish_reason={finish_reason}, reasoning_tokens={reasoning}). "
                "A reasoning model may have spent the whole completion budget "
                "before answering."
            ),
            response=response,
            judge_model=resolved_judge_model,
            judge_is_byom=is_byom,
        )
    try:
        parsed = _parse_judge_json(content)
    except ValueError as exc:
        reason = str(exc)
        if finish_reason == "length":
            # A truncated reply is a distinct failure mode from garbage
            # output: the regex `\{.*\}` needs a closing brace, so a
            # verdict cut short by the token budget looks identical to a
            # judge that never produced JSON at all unless this is said
            # explicitly.
            reason = (
                "the judge's response was truncated before completing its "
                f"JSON verdict (finish_reason=length): {content!r}"
            )
        raise _attach_billing(
            ValueError(reason),
            response=response,
            judge_model=resolved_judge_model,
            judge_is_byom=is_byom,
        ) from exc

    missing = [k for k in ("task_completion", "intent_resolution") if k not in parsed]
    if missing:
        # A key the judge did not return is an absence of data, not a
        # failing score -- defaulting it to 0.0 would rank an unparseable
        # reply next to an agent that failed outright.
        raise _attach_billing(
            ValueError(f"judge response missing required key(s): {missing}"),
            response=response,
            judge_model=resolved_judge_model,
            judge_is_byom=is_byom,
        )

    task_completion = float(parsed["task_completion"])
    intent_resolution = float(parsed["intent_resolution"])
    score = round((task_completion + intent_resolution) / 2, 4)

    return EvaluationOutcome(
        evaluator="task_completion_judge",
        kind="llm_judge",
        score=score,
        # Follows the composite score the screen shows, not task_completion
        # alone -- task_completion=1.0/intent_resolution=0.0 used to render
        # a 50% score next to a green badge.
        passed=score >= settings.evaluation_pass_threshold_task_completion,
        details={
            "session_id": session_id,
            "judged_model": judged_model,
            "judge_model": resolved_judge_model,
            # Distinguishes a client-paid evaluation from a platform-paid
            # one -- `run_service._record_judge_usage` reads this to set
            # `llm_usage.billed_to_thaink2`.
            "judge_is_byom": is_byom,
            "task_completion": task_completion,
            "intent_resolution": intent_resolution,
            "rationale": parsed.get("rationale"),
            "turns": len(transcript),
            # Recorded next to the score it may have tilted, not raised: an
            # install with a single provider must still be able to evaluate.
            "judge_shares_provider_with_judged": _same_provider(
                resolved_judge_model, judged_model
            ),
            "judge_usage": _extract_usage(response),
            # True when the transcript sent to the judge was cut down to
            # the last 20k characters -- a score computed on a partial
            # conversation must say so, never silently.
            "truncated": truncated,
            "criteria": [
                {"name": "task_completion", "value": task_completion, "kind": "score"},
                {"name": "intent_resolution", "value": intent_resolution, "kind": "score"},
                {"name": "turns", "value": len(transcript), "kind": "count"},
            ],
        },
    )
