"""Mode réel du banc : ce que des conversations ont vraiment coûté, lu dans
llm_usage (lecture seule).

À lancer là où la base est joignable (VM DEV), après avoir rejoué à la main
les messages d'un scénario (`scenarios/*.json`, champ `user` de chaque tour)
sur l'agent BI :

    DATABASE_URL=postgresql://… python bench/bi_tokens/dev_usage.py \\
        --schema th2agent_dev --session dashboard-chat-<uuid> [--session …] [--json]
    … --agent-id 1421 --since 2026-10-16T09:00   # toutes les sessions de l'agent

Sans DATABASE_URL, l'URL est composée depuis DB_USER, DB_PASSWORD, DB_HOST,
DB_PORT, DB_NAME. Rien n'est écrit : la transaction est en READ ONLY.

total_tokens est ce que somme la jauge client (cache compris) ; input_tokens
se compare à la colonne `tokens` de replay.py, qui le dépasse de 9 à 16 %.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from urllib.parse import quote


def _url() -> str:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    env = {k: os.environ.get(k, "") for k in ("DB_USER", "DB_PASSWORD", "DB_HOST", "DB_PORT", "DB_NAME")}
    return (f"postgresql://{quote(env['DB_USER'])}:{quote(env['DB_PASSWORD'])}@{env['DB_HOST']}:"
            f"{env['DB_PORT'] or 5432}/{env['DB_NAME']}")


def fetch(sessions: list[str], agent_id: int | None, since: str | None, schema: str) -> list[dict]:
    from sqlalchemy import create_engine, text

    where, params = [], {}
    if sessions:
        where.append("session_id = ANY(:sessions)")
        params["sessions"] = sessions
    if agent_id is not None:
        where.append("agent_id = :agent_id")
        params["agent_id"] = agent_id
    if since:
        where.append("created_at >= :since")
        params["since"] = since
    if not where:
        raise SystemExit("--session ou --agent-id requis")
    if schema and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", schema):
        raise SystemExit(f"--schema invalide : {schema!r}")
    table = f"{schema}.llm_usage" if schema else "llm_usage"
    sql = (f"SELECT session_id, created_at, model, input_tokens, cached_tokens, output_tokens, "
           f"thoughts_tokens, total_tokens, tool_names FROM {table} WHERE {' AND '.join(where)} "
           f"ORDER BY session_id, created_at")
    engine = create_engine(_url())
    with engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        rows = [dict(r._mapping) for r in conn.execute(text(sql), params)]
    for r in rows:
        r["created_at"] = r["created_at"].isoformat()
    return rows


def by_session(rows: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for r in rows:
        s = out.setdefault(r["session_id"], {"calls": [], "input_tokens": 0, "total_tokens": 0, "cached_tokens": 0})
        s["calls"].append(r)
        for k in ("input_tokens", "total_tokens", "cached_tokens"):
            s[k] += r[k] or 0
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", action="append", default=[])
    ap.add_argument("--agent-id", type=int)
    ap.add_argument("--since")
    ap.add_argument("--schema", default=os.environ.get("DB_SCHEMA", ""))
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    report = by_session(fetch(args.session, args.agent_id, args.since, args.schema))
    if args.json:
        print(json.dumps(report, ensure_ascii=False))
        return 0
    for sid, s in report.items():
        print(f"== {sid} — {len(s['calls'])} appels")
        print(f"{'call':>4} {'input':>7} {'cached':>7} {'output':>7} {'total':>7}  tools")
        for i, r in enumerate(s["calls"], 1):
            print(f"{i:>4} {r['input_tokens']:>7} {r['cached_tokens']:>7} {r['output_tokens']:>7} "
                  f"{r['total_tokens']:>7}  {r['tool_names'] or ''}")
        print(f"     input {s['input_tokens']}  cached {s['cached_tokens']}  total (jauge) {s['total_tokens']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
