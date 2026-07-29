"""Pure prompt-building and response-parsing for the Business Analyst loop.

Kept separate from analyst.py (the control flow) and from text_to_sql.py (the
live litellm/DB wiring) so the brittle parts -- assembling the planner /
interpreter prompts and surviving messy LLM JSON -- are unit-testable with no
model and no database.

The planner and interpreter are asked to emit JSON. Local models (Mistral) wrap
it in prose or markdown fences, so the parsers are deliberately tolerant and
fail SAFE: an unparseable planner reply stops the investigation
({"done": True}) rather than looping, and an unparseable interpreter reply
degrades to a plain-text narrative rather than crashing.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List

_ROW_PREVIEW = 5          # rows of evidence shown back to the planner/interpreter
_CELL_MAX = 60            # truncate long cell values

_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)


def _truncate(value: Any) -> str:
    s = str(value)
    return s if len(s) <= _CELL_MAX else s[:_CELL_MAX] + "..."


def steps_digest(steps: List[Any]) -> str:
    """Render prior investigation steps compactly for a follow-up prompt.

    Shows each sub-question, its SQL, and either an error or a small preview of
    the returned rows -- enough for the model to decide the next query or to
    interpret the evidence, without dumping whole result sets into the prompt.
    """
    if not steps:
        return "(no queries run yet)"
    blocks: List[str] = []
    for i, s in enumerate(steps, 1):
        head = f"[{i}] {s.sub_question}\n    SQL: {s.sql}"
        if s.error:
            blocks.append(head + f"\n    ERROR: {s.error}")
            continue
        rows = s.rows[:_ROW_PREVIEW]
        if not rows:
            blocks.append(head + "\n    RESULT: 0 rows")
            continue
        lines = [
            "    " + ", ".join(f"{k}={_truncate(v)}" for k, v in dict(r).items())
            for r in rows
        ]
        more = "" if len(s.rows) <= _ROW_PREVIEW else \
            f"\n    ... (+{len(s.rows) - _ROW_PREVIEW} more rows)"
        blocks.append(head + f"\n    RESULT ({s.row_count} rows):\n"
                      + "\n".join(lines) + more)
    return "\n".join(blocks)


def build_planner_messages(
    question: str, schema_prompt: str, steps: List[Any], max_steps: int,
) -> List[Dict[str, str]]:
    """Messages asking the model for the NEXT investigation query (or to stop)."""
    system = (
        "You are a senior data analyst planning a database investigation. "
        "A business question is answered by a SEQUENCE of SELECT queries: start "
        "broad, then drill into anomalies, comparisons or trends the previous "
        "results reveal. You decide ONE next query at a time.\n\n"
        "Reply with ONLY a JSON object, no prose, no markdown:\n"
        '  {"done": false, "sub_question": "<what this query answers>", '
        '"sql": "<one SELECT statement>"}\n'
        "or, when the evidence already answers the question:\n"
        '  {"done": true}\n\n'
        "Rules: SELECT only. Build on previous results -- do not repeat an "
        "identical query. Answer from the data that EXISTS: do NOT fabricate "
        "forecasts or projections for values absent from the data (e.g. a future "
        "year with no rows) unless the user EXPLICITLY asked to forecast. Stop "
        "(done:true) as soon as you have enough to give a useful analysis, and "
        f"never plan more than {max_steps} queries total."
    )
    user = (
        f"{schema_prompt}\n\n"
        f"Business question: {question}\n\n"
        f"Investigation so far:\n{steps_digest(steps)}\n\n"
        "Return the JSON for the next step."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def build_interpreter_messages(
    question: str, steps: List[Any],
) -> List[Dict[str, str]]:
    """Messages asking the model to turn the evidence into an analysis."""
    system = (
        "You are a senior data analyst writing up an investigation for a "
        "business stakeholder. Ground every statement in the data provided -- "
        "never invent numbers. Identify trends, anomalies and comparisons. "
        "Never present an extrapolation beyond the data's actual range as a "
        "fact or finding. If the question targets a value the data does not "
        "contain (e.g. a future year with no rows), say so plainly; give a "
        "projection ONLY if the user explicitly asked for one, and clearly mark "
        "it as a rough, caveated estimate -- not a finding.\n\n"
        "Reply with ONLY a JSON object, no prose, no markdown:\n"
        "{\n"
        '  "narrative": "<2-4 sentence plain-language analysis>",\n'
        '  "findings": ["<key fact>", ...],\n'
        '  "recommendations": ["<actionable next step>", ...],\n'
        '  "chart": {"chart_type": "bar|line|pie|scatter", "x": "<col>", '
        '"y": "<col>", "title": "<title>"} or null\n'
        "}\n"
        "Suggest a chart only when a result set has a sensible x/y to plot; "
        "otherwise use null. Respond in the language of the business question."
    )
    user = (
        f"Business question: {question}\n\n"
        f"Evidence gathered:\n{steps_digest(steps)}\n\n"
        "Return the analysis JSON."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _loads_lenient(raw: str) -> Dict[str, Any]:
    """Best-effort JSON object extraction from a messy LLM reply."""
    if not raw:
        return {}
    t = raw.strip()
    # strip markdown fences if present
    if "```" in t:
        t = re.sub(r"```(?:json)?", "", t).strip()
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        pass
    m = _JSON_OBJ_RE.search(t)
    if m:
        try:
            obj = json.loads(m.group(0))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


def parse_planner_response(raw: str) -> Dict[str, Any]:
    """Parse a planner reply. Fail SAFE: unparseable -> stop the investigation."""
    obj = _loads_lenient(raw)
    if not obj:
        return {"done": True}
    if obj.get("done"):
        return {"done": True}
    sql = obj.get("sql")
    if not sql or not str(sql).strip():
        return {"done": True}
    return {
        "done": False,
        "sub_question": str(obj.get("sub_question") or "").strip(),
        "sql": str(sql),
    }


def parse_interpreter_response(raw: str) -> Dict[str, Any]:
    """Parse an interpreter reply. Degrade to plain text rather than crash."""
    obj = _loads_lenient(raw)
    if not obj:
        return {
            "narrative": (raw or "").strip(),
            "findings": [], "recommendations": [], "chart": None,
        }
    chart = obj.get("chart")
    if not isinstance(chart, dict):
        chart = None
    return {
        "narrative": str(obj.get("narrative") or "").strip(),
        "findings": [str(x) for x in (obj.get("findings") or [])],
        "recommendations": [str(x) for x in (obj.get("recommendations") or [])],
        "chart": chart,
    }
