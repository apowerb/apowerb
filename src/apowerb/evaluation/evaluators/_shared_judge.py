"""Shared plumbing for the LLM-judge evaluators.

Not itself an evaluator (not registered in `run_service.KNOWN_EVALUATORS`).
Factors out what `coherence.py`, `completeness.py` and `hallucination.py`
would otherwise each duplicate from `task_completion_judge.py`: reading
ADK's `events` table into a compact transcript, and parsing the judge's
JSON reply. `task_completion_judge.py` itself is left untouched -- its
`SameJudgeError` is imported from there, not redefined, so there is exactly
one definition of "the judge cannot be the model it judges".
"""

from __future__ import annotations

import json
import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import quoted_name

from apowerb.configs.settings import get_settings


def events_sql():
    # Same schema-qualification and column caveat as
    # task_completion_judge.py: `author` lives inside `event_data`, not as
    # a standalone column.
    schema = quoted_name(get_settings().db_schema, quote=True)
    return text(
        f"SELECT event_data FROM {schema}.events "
        "WHERE app_name = :app_name AND user_id = :user_id AND session_id = :session_id "
        "ORDER BY timestamp ASC"
    )


def extract_transcript(rows) -> list[dict]:
    """Turn ADK `events` rows into a compact (role, text) transcript.

    Function calls and their responses are rendered as short markers
    rather than dropped -- a judge blind to tool use would misjudge
    coherence/completeness/hallucination from half the conversation.
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


def transcript_text(transcript: list[dict]) -> str:
    return "\n".join(f"{turn['role']}: {turn['text']}" for turn in transcript)


def parse_judge_json(raw: str) -> dict:
    match = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not match:
        raise ValueError(f"judge did not return a JSON object: {raw!r}")
    return json.loads(match.group(0))


def same_model(judge_model: str, judged_model: str) -> bool:
    def _norm(model: str) -> str:
        return model.split("/", 1)[-1].strip().lower()

    return _norm(judge_model) == _norm(judged_model)


def same_provider(judge_model: str, judged_model: str) -> bool:
    def _provider(model: str) -> str:
        return model.split("/", 1)[0].strip().lower() if "/" in model else ""

    judge_provider = _provider(judge_model)
    return bool(judge_provider) and judge_provider == _provider(judged_model)


async def fetch_transcript(
    db: AsyncSession, *, app_name: str, user_id: str, session_id: str
) -> list[dict]:
    rows = (
        await db.execute(
            events_sql(),
            {"app_name": app_name, "user_id": user_id, "session_id": session_id},
        )
    ).fetchall()
    return extract_transcript(rows)
