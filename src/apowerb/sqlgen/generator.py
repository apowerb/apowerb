"""SQL extraction from raw LLM output.

A small, model-tolerant layer: local LLMs (e.g. Mistral on OVH) wrap SQL in
markdown fences or add a preamble ("Here is the query:"). extract_sql pulls out
the first SELECT/WITH statement so downstream safety validation and execution
get clean SQL. This is the seam the Business Analyst layer will build on:
generate -> extract -> validate -> execute -> interpret.
"""

import re

_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_START_RE = re.compile(r"\b(SELECT|WITH)\b", re.IGNORECASE)


def extract_sql(text: str) -> str:
    """Best-effort extraction of a single SQL statement from LLM output."""
    if not text:
        return ""
    t = text.strip()
    fence = _FENCE_RE.search(t)
    if fence:
        t = fence.group(1).strip()
    else:
        t = t.replace("```sql", "").replace("```", "").strip()
    m = _START_RE.search(t)
    if m:
        t = t[m.start():]
    return t.strip().rstrip(";").strip()
