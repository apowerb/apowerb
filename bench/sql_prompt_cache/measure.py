"""How much of tool_text_to_sql's internal request a prompt cache can reuse.

Builds the real request (`_state_generate_sql`, model call stubbed) for a fixed
list of questions on fixed synthetic schemas, and measures, for each question,
the prefix it shares with the previous question's request (tiktoken o200k).
`azure_cacheable` applies the OpenAI/Azure rule: nothing under 1,024 shared
tokens, then whole 128-token blocks. It is an upper bound: the provider must
also route both requests to the same cache.

    uv pip install tiktoken                             # not a runtime dependency
    python bench/sql_prompt_cache/measure.py            # table
    python bench/sql_prompt_cache/measure.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import random
from types import SimpleNamespace

import tiktoken

ENC = tiktoken.get_encoding("o200k_base")
QUESTIONS = [
    "How many parcels were shipped per week in 2022?",
    "Total revenue per customer segment last year",
    "Quel est le nombre total de conversations ?",
    "Top 10 products by quantity sold",
    "Average delivery delay per carrier",
    "Number of open support tickets per agent",
    "Monthly invoice amount by region",
    "Which warehouses are below their stock threshold?",
    "Satisfaction score by channel",
    "Orders cancelled in the last 30 days",
]
_TYPES = ["integer", "numeric", "text", "varchar", "date", "timestamp", "boolean"]
_WORDS = ["customer", "order", "parcel", "carrier", "invoice", "product", "stock",
          "warehouse", "region", "agent", "ticket", "channel", "payment", "supplier",
          "shipment", "return", "campaign", "contract", "employee", "store", "route",
          "vehicle", "zone", "batch", "price", "discount", "review", "subscription"]


def synthetic_schema(n_tables: int, seed: int = 7) -> dict:
    rnd = random.Random(seed)
    names = [f"{_WORDS[i % len(_WORDS)]}s" + (f"_{i // len(_WORDS)}" if i >= len(_WORDS) else "")
             for i in range(n_tables)]
    tables = {}
    for i, name in enumerate(names):
        cols = [{"column_name": "id", "data_type": "integer"}]
        fks = []
        if i:
            target = names[rnd.randrange(i)]
            cols.append({"column_name": f"{target.rstrip('s')}_id", "data_type": "integer"})
            fks.append({"column_name": cols[-1]["column_name"], "foreign_table_name": target,
                        "foreign_column_name": "id"})
        for _ in range(rnd.randint(6, 12)):
            w = rnd.choice(_WORDS)
            cols.append({"column_name": f"{w}_{rnd.choice(['name', 'amount', 'date', 'status', 'count', 'code'])}",
                         "data_type": rnd.choice(_TYPES)})
        tables[name] = {"columns": cols, "primary_keys": ["id"], "foreign_keys": fks, "sample_data": []}
    return {"schema": "public", "db_type": "postgresql", "tables": tables}


def requests_for(schema: dict) -> list[list[dict]]:
    import apowerb.tools_store.portfolio.text_to_sql as t2s

    captured: list[list[dict]] = []

    def fake_completion(**kw):
        captured.append(kw["messages"])
        msg = SimpleNamespace(content="SELECT 1")
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    t2s.litellm.completion = fake_completion
    state = SimpleNamespace(agent_name="bench", schema_samples=False, model="azure/gpt-5.4",
                            api_base=None, api_key=None)
    for q in QUESTIONS:
        t2s._state_generate_sql(state, q, schema, record_usage=False)
    return captured


def _tokens(messages: list[dict]) -> list[int]:
    return ENC.encode("".join(f"<{m['role']}>{m['content']}" for m in messages))


def measure(n_tables: int) -> dict:
    schema = synthetic_schema(n_tables)
    rows, prev = [], None
    for q, msgs in zip(QUESTIONS, requests_for(schema)):
        toks = _tokens(msgs)
        shared = 0
        if prev is not None:
            while shared < min(len(prev), len(toks)) and prev[shared] == toks[shared]:
                shared += 1
        cacheable = 0 if shared < 1024 else shared // 128 * 128
        rows.append({"question": q, "input": len(toks), "shared_prefix": shared,
                     "azure_cacheable": cacheable})
        prev = toks
    later = rows[1:]
    return {"tables": n_tables, "schema_chars": len(msgs[1]["content"]),
            "input_total": sum(r["input"] for r in later),
            "cacheable_total": sum(r["azure_cacheable"] for r in later), "rows": rows}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    args = ap.parse_args()
    out = [measure(n) for n in (3, 25, 80)]
    for m in out:
        share = m["cacheable_total"] / m["input_total"] if m["input_total"] else 0
        print(f"{m['tables']:>3} tables, {m['schema_chars']:>6} chars: questions 2-10 send "
              f"{m['input_total']:>6} tokens, {m['cacheable_total']:>6} cacheable ({share:.0%})")
    if args.json:
        with open(args.json, "w") as fh:
            json.dump(out, fh, indent=1)


if __name__ == "__main__":
    main()
