"""Relevance ordering of tables for a question, with FK-neighbour boosting.

Pure lexical scoring: tokens shared between the question and a table's
name+columns. Tables referenced by a relevant table (FK targets) are boosted so
joins stay coherent. Deterministic (stable sort, no randomness).
"""

import re
from typing import Any, Dict, List, Optional

_TOKEN_RE = re.compile(r"[a-zA-Z_]{3,}")


def _tokens(text: str) -> set:
    return {t.lower().strip("_") for t in _TOKEN_RE.findall(text or "") if t}


def _table_terms(name: str, info: Dict[str, Any]) -> set:
    terms = set(_tokens(name))
    for col in info.get("columns") or []:
        terms |= _tokens(col.get("column_name", ""))
    return terms


def score_and_order_tables(
    schema_info: Dict[str, Any], question: Optional[str] = None
) -> List[str]:
    """Order table names by relevance to ``question`` (most relevant first).

    With no question, returns names sorted alphabetically (deterministic).
    """
    tables = schema_info.get("tables", {})
    names = list(tables.keys())
    if not question or not question.strip():
        return sorted(names)

    q = _tokens(question)
    scores: Dict[str, float] = {}
    for name, info in tables.items():
        scores[name] = float(len(q & _table_terms(name, info)))

    # FK-neighbour boost: a relevant table pulls in the tables it references,
    # just below its own score, so joins are not orphaned.
    boosted = dict(scores)
    for name, info in tables.items():
        if scores[name] <= 0:
            continue
        for fk in info.get("foreign_keys") or []:
            tgt = fk.get("foreign_table_name")
            if tgt in boosted:
                boosted[tgt] = max(boosted[tgt], scores[name] - 0.5)

    return sorted(names, key=lambda n: (-boosted[n], n))
