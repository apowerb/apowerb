"""Banc roadmap#86 : les règles seules contre les règles + le modèle (#85).

Question posée : le modèle apporte-t-il assez pour être montré dans l'éditeur ?
Go si le bon type de nœud entre dans les trois premières pastilles au moins
15 points plus souvent avec le modèle, ET si la latence p95 reste sous 3 s.

Les cas ne sont pas écrits à la main un par un : chaque workflow COMPLET de
``workflows.json`` est rejoué dans l'ordre où on le construirait (largeur
d'abord, routes dans l'ordre déclaré). À chaque étape, le graphe partiel, le
nœud dont une place est libre et le nœud qui l'occupe dans le workflow complet
forment un cas. La réponse attendue est donc fixée avant tout appel au modèle.

Les règles sont celles de l'éditeur (``apowerb-ui`` ``src/lib/nextNodeSuggestions.js``),
recopiées ici ; ``rules_parity.mjs`` vérifie qu'elles donnent les mêmes
pastilles que le code JS sur tous les cas.

Le modèle passe par la vraie route ``POST /api/workflows/defs/suggest-next``,
dans ce processus : seuls le portier, la liste d'agents et l'écriture
``llm_usage`` sont remplacés, faute de base. Voir README.md pour le lancer.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import math
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
DATASET = HERE / "workflows.json"
OWNER = "bench@example.com"
TOP_K = 3
GO_POINTS = 15.0
GO_P95_S = 3.0

# --- Règles de l'éditeur (copie de nextNodeSuggestions.js) -------------------

AFTER = {
    "trigger": ["agent", "classifier", "convert"],
    "agent": ["output", "condition", "notification"],
    "rag": ["agent", "output"],
    "extract": ["condition", "output", "notification"],
    "tool": ["convert", "output", "notification"],
    "http": ["convert", "output", "notification"],
    "convert": ["agent", "condition", "output"],
    "set": ["http", "output", "notification"],
    "loop": ["output", "notification"],
    "merge": ["agent", "output"],
    "subworkflow": ["output", "notification"],
    "approval": ["notification", "output"],
    "notification": ["output"],
    "router": ["agent", "output"],
    "classifier": ["agent", "output"],
    "condition": ["agent", "output"],
    "try": ["agent", "notification"],
    "output": [],
}
RULES_MAX = 3


def declared_routes(node: dict) -> Optional[list[str]]:
    cfg = node.get("config") or {}
    if node["type"] == "condition":
        routes = ["true", "false"]
    elif node["type"] == "try":
        routes = ["ok", "error"]
    elif node["type"] == "router":
        routes = [r.get("route") for r in cfg.get("rules") or []] + [cfg.get("default_route")]
    elif node["type"] == "classifier":
        routes = [r.get("route") for r in cfg.get("routes") or []]
    else:
        return None
    return list(dict.fromkeys(r for r in routes if r))


def rule_suggestions(node: dict, nodes: list[dict], edges: list[dict]) -> list[dict]:
    """``suggestNextNodes`` : types et branche, sans la pré-configuration."""
    routes = declared_routes(node)
    outgoing = [e for e in edges if e["source"] == node["id"]]
    if routes is None:
        if outgoing:
            return []
        route = None
    else:
        wired = {e.get("route") for e in outgoing}
        route = next((r for r in routes if r not in wired), None)
        if route is None:
            return []
    types = AFTER.get(node["type"], [])
    if "output" in types and not any(n["type"] == "output" for n in nodes):
        types = ["output"] + [t for t in types if t != "output"]
    return [{"type": t, "route": route} for t in types[:RULES_MAX]]


def merge_suggestions(rules: list[dict], ai: list[dict]) -> list[dict]:
    """``mergeAiSuggestions`` : le modèle d'abord, les règles d'un autre type ensuite."""
    ai_types = {s["type"] for s in ai}
    return [*ai, *(s for s in rules if s["type"] not in ai_types)]


# --- Cas ---------------------------------------------------------------------


def load_dataset(path: Path = DATASET) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _children(graph: dict, node: dict) -> list[dict]:
    out = [e for e in graph["edges"] if e["source"] == node["id"]]
    routes = declared_routes(node)
    if routes is None:
        return out
    return sorted(out, key=lambda e: routes.index(e["route"]) if e.get("route") in routes else len(routes))


def build_cases(data: dict) -> list[dict]:
    """Un cas par nœud ajouté en rejouant chaque workflow complet."""
    cases = []
    for wf in data["workflows"]:
        graph = wf["graph"]
        by_id = {n["id"]: n for n in graph["nodes"]}
        trigger = next(n for n in graph["nodes"] if n["type"] == "trigger")
        included, queue = [trigger["id"]], [trigger["id"]]
        while queue:
            parent = by_id[queue.pop(0)]
            for edge in _children(graph, parent):
                child = by_id[edge["target"]]
                if child["id"] in included:
                    continue
                kept = set(included)
                cases.append(
                    {
                        "id": f"{wf['key']}/{parent['id']}->{child['id']}",
                        "workflow": wf["key"],
                        "name": wf["name"],
                        "description": wf.get("description") or "",
                        "node_id": parent["id"],
                        "graph": {
                            "version": graph.get("version", 1),
                            "nodes": [by_id[i] for i in included],
                            "edges": [
                                e for e in graph["edges"] if e["source"] in kept and e["target"] in kept
                            ],
                        },
                        "expected": {
                            "type": child["type"],
                            "route": edge.get("route"),
                            "config": child.get("config") or {},
                        },
                    }
                )
                included.append(child["id"])
                queue.append(child["id"])
    return cases


def parity_diff(cases: list[dict], js: dict) -> list[str]:
    """Écarts entre cette copie des règles et la sortie de ``rules_parity.mjs``."""
    diffs = []
    for case in cases:
        node = next(n for n in case["graph"]["nodes"] if n["id"] == case["node_id"])
        rules = rule_suggestions(node, case["graph"]["nodes"], case["graph"]["edges"])
        ai = [{"type": case["expected"]["type"]}, {"type": "output"}]
        mine = {"rules": rules, "merged": [s["type"] for s in merge_suggestions(rules, ai)]}
        if js.get(case["id"]) != mine:
            diffs.append(f"{case['id']}: JS {js.get(case['id'])} ≠ Python {mine}")
    return diffs


# --- Score -------------------------------------------------------------------


def _refs(node_type: str, config: dict) -> set[str]:
    from apowerb.core.workflow_graph import Node, _outer_refs

    return _outer_refs(Node(id="candidate", type=node_type, config=config))


def score_case(case: dict, rules: list[dict], ai: list[dict], route: Optional[str]) -> dict:
    expected = case["expected"]
    merged = merge_suggestions(rules, ai)
    rule_types = [s["type"] for s in rules[:TOP_K]]
    merged_types = [s["type"] for s in merged[:TOP_K]]
    ai_match = next((s for s in ai if s["type"] == expected["type"]), None)
    config = None
    if ai_match is not None:
        cfg = ai_match.get("config") or {}
        want_agent = expected["config"].get("agent_id")
        config = {
            "agent_ok": None if not want_agent else str(cfg.get("agent_id")) == str(want_agent),
            "reads_source": case["node_id"] in _refs(ai_match["type"], cfg),
        }
    return {
        "rules_top3": expected["type"] in rule_types,
        "merged_top3": expected["type"] in merged_types,
        "rules_top1": rule_types[:1] == [expected["type"]],
        "merged_top1": merged_types[:1] == [expected["type"]],
        "ai_hit": ai_match is not None,
        "route_ok": route == expected["route"],
        "config": config,
        "rule_types": rule_types,
        "merged_types": merged_types,
        "ai_types": [s["type"] for s in ai],
    }


def percentile(values: list[float], pct: float) -> Optional[float]:
    """Rang le plus proche : la valeur sous laquelle tombent ``pct`` % des mesures."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(pct / 100 * len(ordered)) - 1)]


def _rate(flags: list[bool]) -> Optional[float]:
    return round(100 * sum(flags) / len(flags), 1) if flags else None


def summarize(records: list[dict], *, usd_per_eur: Optional[float]) -> dict:
    """Agrège les appels : chaque (cas, répétition) compte pour un."""
    ok = [r for r in records if r["status"] == 200]
    latencies = [r["latency_s"] for r in records]
    by_repeat: dict[int, list[dict]] = {}
    for r in records:
        by_repeat.setdefault(r["repeat"], []).append(r)
    merged_by_repeat = [_rate([x["score"]["merged_top3"] for x in rs]) for _, rs in sorted(by_repeat.items())]
    configs = [r["score"]["config"] for r in records if r["score"]["config"]]
    usd = [r["cost_usd"] for r in records if r["cost_usd"] is not None]
    kept = sum(len(r["score"]["ai_types"]) for r in ok)
    rejected = sum(len(r.get("rejected", [])) for r in ok)
    rules_top3 = _rate([r["score"]["rules_top3"] for r in records])
    merged_top3 = _rate([r["score"]["merged_top3"] for r in records])
    p95 = percentile(latencies, 95)
    gain = None if rules_top3 is None or merged_top3 is None else round(merged_top3 - rules_top3, 1)
    return {
        "calls": len(records),
        "cases": len({r["case"] for r in records}),
        "rules_top3_pct": rules_top3,
        "merged_top3_pct": merged_top3,
        "merged_top3_by_repeat_pct": merged_by_repeat,
        "gain_points": gain,
        "rules_top1_pct": _rate([r["score"]["rules_top1"] for r in records]),
        "merged_top1_pct": _rate([r["score"]["merged_top1"] for r in records]),
        "ai_hit_pct": _rate([r["score"]["ai_hit"] for r in records]),
        "answered_pct": _rate([r["status"] == 200 for r in records]),
        "failures": _count(r.get("reason") or str(r["status"]) for r in records if r["status"] != 200),
        "rejected_pct": round(100 * rejected / (kept + rejected), 1) if kept + rejected else None,
        "rejected_reasons": _count(
            reason.split(" : ")[0] + " : " + reason.split(" : ")[-1][:60]
            for r in ok
            for reason in r.get("rejected", [])
        ),
        "ai_suggestions_avg": round(statistics.mean(len(r["score"]["ai_types"]) for r in ok), 2) if ok else None,
        "route_ok_pct": _rate([r["score"]["route_ok"] for r in ok]),
        "config_agent_ok_pct": _rate([c["agent_ok"] for c in configs if c["agent_ok"] is not None]),
        "config_reads_source_pct": _rate([c["reads_source"] for c in configs]),
        "latency_p50_s": _round(percentile(latencies, 50)),
        "latency_p95_s": _round(p95),
        "latency_max_s": _round(max(latencies) if latencies else None),
        "tokens_in_avg": round(statistics.mean(r["tokens_in"] for r in ok), 1) if ok else None,
        "tokens_out_avg": round(statistics.mean(r["tokens_out"] for r in ok), 1) if ok else None,
        "cost_usd_per_call": _round(statistics.mean(usd), 7) if usd else None,
        "cost_eur_per_call": _round(statistics.mean(usd) / usd_per_eur, 7) if usd and usd_per_eur else None,
        "go": bool(gain is not None and p95 is not None and gain >= GO_POINTS and p95 < GO_P95_S),
    }


def _count(items) -> dict:
    out: dict[str, int] = {}
    for i in items:
        out[i] = out.get(i, 0) + 1
    return out


def _top(counts: dict, n: int = 5) -> str:
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])[:n]
    return "; ".join(f"{k} ×{v}" for k, v in ranked) or "aucune"


def _round(value: Optional[float], digits: int = 3) -> Optional[float]:
    return None if value is None else round(value, digits)


# --- Appel de la route -------------------------------------------------------


class Route:
    """La route #85 servie dans ce processus, sans base ni authentification."""

    def __init__(self, agents: list[dict]):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from apowerb.auth.dependencies import get_current_user
        from apowerb.configs.settings import get_settings
        from apowerb.core import run_gate, workflow_suggest
        from apowerb.core.agent_helpers import usage_recorder
        from apowerb.routers import workflow_defs

        settings = get_settings()
        settings.workflow_suggest_enabled = True
        self.model = (settings.workflow_suggest_model or settings.default_llm_model).strip()
        self.timeout_s = settings.workflow_suggest_timeout_s
        if not workflow_suggest.suggest_enabled():
            raise SystemExit("DEFAULT_LLM_MODEL et DEFAULT_LLM_API_KEY sont requis (voir README.md).")

        self.usage: list[dict] = []
        # Les propositions écartées par la validation ne sortent pas de la
        # route : on les lit dans son journal, un appel à la fois.
        self.rejected: list[str] = []
        route = self

        class Rejections(logging.Handler):
            def emit(self, record):
                message = record.getMessage()
                if "écartée" in message:
                    route.rejected.append(message.split("proposition ", 1)[-1][:160])

        self.log_handler = Rejections(level=logging.INFO)
        suggest_log = logging.getLogger(workflow_suggest.__name__)
        suggest_log.addHandler(self.log_handler)
        if suggest_log.getEffectiveLevel() > logging.INFO:
            suggest_log.setLevel(logging.INFO)

        async def no_guard(**_):
            return None

        async def no_plan(_owner):
            return None

        async def keep_usage(**fields):
            self.usage.append(fields)

        run_gate.apply_run_guards = no_guard
        run_gate.resolve_owner_plan = no_plan
        usage_recorder._persist_usage_row = keep_usage
        workflow_suggest._owner_agents = lambda _owner: agents

        class User:
            email, user_id, role = OWNER, 0, "USER"

        app = FastAPI()
        app.include_router(workflow_defs.router, prefix="/api")
        app.dependency_overrides[get_current_user] = lambda: User()
        self.client = TestClient(app)

    def ask(self, case: dict) -> dict:
        self.usage.clear()
        self.rejected.clear()
        started = time.perf_counter()
        response = self.client.post(
            "/api/workflows/defs/suggest-next",
            json={
                "graph": case["graph"],
                "node_id": case["node_id"],
                "name": case["name"],
                "description": case["description"],
            },
        )
        latency = time.perf_counter() - started
        body = response.json()
        usage = self.usage[0] if self.usage else {}
        return {
            "status": response.status_code,
            "latency_s": round(latency, 3),
            "route": body.get("route") if response.status_code == 200 else None,
            "suggestions": body.get("suggestions", []) if response.status_code == 200 else [],
            "reason": (body.get("detail") or {}).get("reason") if response.status_code != 200 else None,
            "tokens_in": int(usage.get("input_tokens") or 0),
            "tokens_out": int(usage.get("output_tokens") or 0),
            "rejected": list(self.rejected),
        }


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> Optional[float]:
    import litellm

    try:
        prompt, completion = litellm.cost_per_token(
            model=model, prompt_tokens=tokens_in, completion_tokens=tokens_out
        )
    except Exception:  # noqa: BLE001 - modèle absent de la grille de litellm
        return None
    return prompt + completion


def run(cases: list[dict], route: Route, repeats: int) -> list[dict]:
    records = []
    for repeat in range(1, repeats + 1):
        for case in cases:
            node = next(n for n in case["graph"]["nodes"] if n["id"] == case["node_id"])
            rules = rule_suggestions(node, case["graph"]["nodes"], case["graph"]["edges"])
            answer = route.ask(case)
            ai = [{"type": s["type"], "config": s.get("config") or {}} for s in answer["suggestions"]]
            records.append(
                {
                    "case": case["id"],
                    "repeat": repeat,
                    "expected": {k: case["expected"][k] for k in ("type", "route")},
                    **answer,
                    "cost_usd": cost_usd(route.model, answer["tokens_in"], answer["tokens_out"])
                    if answer["tokens_in"]
                    else None,
                    "score": score_case(case, rules, ai, answer["route"]),
                }
            )
            print(
                f"[{repeat}/{repeats}] {case['id']}: {answer['status']} {answer['latency_s']}s "
                f"ai={[s['type'] for s in ai]} expected={case['expected']['type']}",
                file=sys.stderr,
            )
    return records


# --- Rapport -----------------------------------------------------------------


def _pct(value: Optional[float]) -> str:
    return "—" if value is None else f"{value:.1f} %"


def report_markdown(meta: dict, summary: dict, records: list[dict]) -> str:
    s = summary
    verdict = "**GO**" if s["go"] else "**NO-GO**"
    eur = "—" if s["cost_eur_per_call"] is None else f"{s['cost_eur_per_call'] * 1000:.4f} € les 1 000 → {s['cost_eur_per_call']:.6f} € par appel"
    lines = [
        f"# Banc suggestions du nœud suivant — {meta['date']}",
        "",
        f"Modèle `{meta['model']}`, délai {meta['timeout_s']} s, {s['cases']} cas × {meta['repeats']} passages = {s['calls']} appels.",
        f"Règles : copie vérifiée contre `{meta['rules_source']}`.",
        "",
        f"## Verdict : {verdict}",
        "",
        f"- Top 3 : règles seules {_pct(s['rules_top3_pct'])}, règles + modèle {_pct(s['merged_top3_pct'])} "
        f"(par passage : {', '.join(_pct(v) for v in s['merged_top3_by_repeat_pct'])}) → **{s['gain_points']:+.1f} points** (seuil +{GO_POINTS:.0f}).",
        f"- Latence : p50 {s['latency_p50_s']} s, **p95 {s['latency_p95_s']} s** (seuil < {GO_P95_S:.0f} s), max {s['latency_max_s']} s.",
        "",
        "## Détail",
        "",
        f"- Top 1 : règles {_pct(s['rules_top1_pct'])}, règles + modèle {_pct(s['merged_top1_pct'])}.",
        f"- Le modèle propose le bon type dans {_pct(s['ai_hit_pct'])} des appels ; "
        f"{s['ai_suggestions_avg']} proposition(s) retenue(s) par réponse en moyenne.",
        f"- Réponses utilisables : {_pct(s['answered_pct'])} ; échecs : {s['failures'] or 'aucun'}.",
        f"- Propositions écartées par la validation : {_pct(s['rejected_pct'])} "
        f"({sum(s['rejected_reasons'].values())}) — {_top(s['rejected_reasons'])}.",
        f"- Branche à câbler juste : {_pct(s['route_ok_pct'])} (calculée par le serveur, pas par le modèle).",
        f"- Configuration, quand le modèle a le bon type : bon agent {_pct(s['config_agent_ok_pct'])}, "
        f"lit le nœud source {_pct(s['config_reads_source_pct'])}.",
        f"- Jetons par appel : {s['tokens_in_avg']} en entrée, {s['tokens_out_avg']} en sortie.",
        f"- Coût : {eur} (grille litellm ; {meta['fx']}).",
        "",
        "## Cas",
        "",
        "| Cas | Attendu | Règles (top 3) | Modèle (par passage) | Top 3 règles / + modèle |",
        "|---|---|---|---|---|",
    ]
    by_case: dict[str, list[dict]] = {}
    for r in records:
        by_case.setdefault(r["case"], []).append(r)
    for case_id, rs in by_case.items():
        first = rs[0]
        expected = first["expected"]["type"] + (f" ({first['expected']['route']})" if first["expected"]["route"] else "")
        ai = " · ".join(",".join(r["score"]["ai_types"]) or (r.get("reason") or "∅") for r in rs)
        hits = f"{'✓' if first['score']['rules_top3'] else '✗'} / {sum(r['score']['merged_top3'] for r in rs)}/{len(rs)}"
        lines.append(f"| `{case_id}` | {expected} | {', '.join(first['score']['rule_types'])} | {ai} | {hits} |")
    lines.append("")
    return "\n".join(lines)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--usd-per-eur", type=float, help="taux BCE : combien de dollars pour 1 €")
    parser.add_argument("--fx-date", default="", help="date du taux BCE cité")
    parser.add_argument("--rules-source", default="apowerb-ui src/lib/nextNodeSuggestions.js")
    parser.add_argument("--out", type=Path, default=HERE / "results")
    parser.add_argument("--label", default="", help="suffixe du rapport, pour garder les runs précédents")
    parser.add_argument("--dump-cases", type=Path, help="écrit les cas (pour rules_parity.mjs) et s'arrête")
    parser.add_argument("--check-parity", type=Path, help="compare la sortie de rules_parity.mjs et s'arrête")
    args = parser.parse_args(argv)

    data = load_dataset()
    cases = build_cases(data)
    if args.dump_cases:
        args.dump_cases.write_text(json.dumps(cases, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{len(cases)} cas écrits dans {args.dump_cases}")
        return 0
    if args.check_parity:
        diffs = parity_diff(cases, json.loads(args.check_parity.read_text(encoding="utf-8")))
        print("\n".join(diffs) or f"parité : {len(cases)}/{len(cases)} cas identiques")
        return 1 if diffs else 0

    route = Route(data["agents"])
    records = run(cases, route, args.repeats)
    summary = summarize(records, usd_per_eur=args.usd_per_eur)
    if not summary["answered_pct"]:
        # Sans une seule réponse, « règles + modèle » vaut « règles seules » :
        # un NO-GO à +0 point mesurerait la clé ou le réseau, pas le modèle.
        print(f"aucune réponse du modèle ({summary['failures']}) : pas de verdict", file=sys.stderr)
        return 1
    meta = {
        "date": dt.date.today().isoformat(),
        "model": route.model,
        "timeout_s": route.timeout_s,
        "repeats": args.repeats,
        "rules_source": args.rules_source,
        "fx": f"1 € = {args.usd_per_eur} $, BCE {args.fx_date}" if args.usd_per_eur else "pas de taux BCE fourni",
    }
    args.out.mkdir(parents=True, exist_ok=True)
    stem = f"{meta['date']}-{route.model.replace('/', '_')}" + (f"-{args.label}" if args.label else "")
    (args.out / f"{stem}.json").write_text(
        json.dumps({"meta": meta, "summary": summary, "records": records}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    (args.out / f"{stem}.md").write_text(report_markdown(meta, summary, records), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print(f"rapport : {args.out / (stem + '.md')}")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(HERE.parents[1] / "src"))
    os.environ.setdefault("LITELLM_LOG", "ERROR")
    raise SystemExit(main())
