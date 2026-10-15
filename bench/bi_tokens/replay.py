"""Rejoue la conversation BI de la démo (DEV 14/10) hors ligne et mesure la taille
de chaque requête envoyée au modèle.

Le modèle est remplacé par un script qui reproduit exactement les appels d'outils
enregistrés ; l'agent, ses outils, sa consigne et la conversion LiteLLM sont ceux
du code. Les outils marqués LIVE tournent pour de vrai (sur les données
enregistrées) ; les autres renvoient leur réponse enregistrée.

    python bench/bi_tokens/replay.py            # tableau par appel + totaux
    python bench/bi_tokens/replay.py --json     # sortie machine
    python bench/bi_tokens/replay.py --targeted # + requêtes ciblées (estimation)

Scénario (scenario.json) : la conversation filmée pour la démo BI, sur l'agent
« BI Assistant » (gabarit forecasting_agent) en DEV le 14/10/2026 — création
d'un tableau depuis un CSV joint, puis « Which channel brings the best return
on ad spend? » depuis le tableau. Données fictives. measured reprend
llm_usage.input_tokens de chaque appel réel.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).parent
SCENARIO = json.loads((HERE / "scenario.json").read_text())
OWNER = "bench@example.com"
MODEL = "gemini/gemini-2.5-flash"


def _agent_row() -> dict:
    from apowerb.core.superagents import get_superagent_template

    tpl = get_superagent_template("forecasting_agent")
    return {
        "agent_id": 1421, "agent_name": "BI Assistant", "agent_model": MODEL,
        "agent_model_params": "{}", "agent_description": tpl["description"],
        "agent_instruction": tpl["agent_instruction"], "agent_tools": "[]",
        "agent_type": "base", "sub_agents": "[]", "owner_id": OWNER,
        "organization_id": "bench", "project_id": "bench", "memory_enabled": "false",
        "artifacts_enabled": "true", "superagent_template_id": "forecasting_agent",
        "agent_skills": json.dumps(tpl.get("skills") or ["forecasting", "data-visualization"]),
        "mcp_servers": None,
    }


def build_agent():
    from apowerb.core.agent_helpers import agent_utils, extras_loader

    extras_loader._inject_tool_config_env = lambda *a, **k: None  # base tool_config absente
    row = _agent_row()
    agent_utils.get_agent_details = lambda agent_id, items_to_select="*": dict(row)
    return agent_utils.to_agent("agent1421")


# --- outils LIVE : le vrai code sur les données enregistrées ---------------
def _query_fixture():
    """Réponses enregistrées de tool_bi_query_dashboard, indexées par filtre canal."""
    by_filter = {}
    for step in SCENARIO["question"]["steps"]:
        for call, resp in zip(step["calls"], step["responses"]):
            if call["name"] == "tool_bi_query_dashboard":
                f = json.loads(call["args"].get("filters_json") or "{}")
                by_filter[f.get("canal")] = resp["response"]
    return by_filter


def install_live_tools():
    import apowerb.tools_store.portfolio.bi_dashboard_tools as t
    from apowerb.bi.spec.models import ComponentResult, DashboardDataResponse, KpiValue, ResolvedFilter

    recorded = _query_fixture()
    first = recorded[None]
    comps = [SimpleNamespace(id=c["id"], visual=SimpleNamespace(title=c.get("title"), kpi=None, type=c["type"]))
             for c in first["components"]]
    dashboard = SimpleNamespace(id=first["dashboard_id"], title=first["title"], components=comps, sources={"s": 1})

    class Store:
        async def get(self, dashboard_id):
            return dashboard if dashboard_id == dashboard.id else None

    import contextlib

    @contextlib.asynccontextmanager
    async def store(owner):
        yield Store()

    async def compute(dash, filters, owner, components=None):
        rec = recorded[(filters or {}).get("canal")]
        results = {}
        for c in rec["components"]:
            if components and c["id"] not in components:
                continue
            if c["type"] == "kpi":
                kv = KpiValue(value=c.get("raw"), formatted=c["value"], status=c.get("status"), secondary=c.get("secondary"))
                results[c["id"]] = ComponentResult(type="kpi", value=kv)
            elif c["type"] in ("text", "note"):
                results[c["id"]] = ComponentResult(type=c["type"], text=c.get("text"))
            else:
                results[c["id"]] = ComponentResult(type=c["type"], rows=c.get("rows"))
        filt = {k: ResolvedFilter(value=v["value"], label=v["label"], date_from=v.get("from"), date_to=v.get("to"))
                for k, v in rec["filters"].items()}
        return DashboardDataResponse(dashboard_id=dash.id, filters=filt, filter_options=rec["filter_options"],
                                     sources={}, components=results)

    t._dashboard_store = store
    t._compute = compute
    t._agent_owner = lambda: OWNER

    def run_tool(coro):  # l'outil est appelé depuis la boucle de l'agent
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(1) as pool:
            return pool.submit(asyncio.run, coro).result()

    t._run_tool = run_tool
    return {"tool_bi_query_dashboard", "tool_bi_list_dashboard_templates"}


# --- comptage ---------------------------------------------------------------
_TOKENIZER = None


def count_text(text: str) -> int | None:
    """Tokens selon le tokenizer local Gemini (paquet sentencepiece requis),
    None s'il est absent : le banc donne alors les caractères seuls.

    Compté sur les messages et outils tels que LiteLLM les envoie, ce chiffre
    dépasse llm_usage.input_tokens mesuré en DEV de 9 à 16 % (15/10) :
    il sert à comparer avant/après, pas à facturer.
    """
    global _TOKENIZER
    try:
        if _TOKENIZER is None:
            import warnings
            from google.genai.local_tokenizer import LocalTokenizer

            warnings.simplefilter("ignore")
            _TOKENIZER = LocalTokenizer(model_name="gemini-2.5-flash")
    except ImportError:
        return None
    return _TOKENIZER.count_tokens(text).total_tokens


# --- modèle scripté ---------------------------------------------------------
def scripted_llm(steps, log):
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.lite_llm import _get_completion_inputs
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types

    queue = list(steps)

    class Scripted(BaseLlm):
        async def generate_content_async(self, llm_request, stream=False):
            if not queue:  # en réel l'action-card met le tour en pause ici
                yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=" ")]))
                return
            messages, tools, *_ = await _get_completion_inputs(llm_request, MODEL)
            msgs = [m if isinstance(m, dict) else m.model_dump(exclude_none=True) for m in messages]
            system = sum(len(json.dumps(m, ensure_ascii=False)) for m in msgs if m.get("role") in ("system", "developer"))
            history = sum(len(json.dumps(m, ensure_ascii=False)) for m in msgs if m.get("role") not in ("system", "developer"))
            tools_chars = len(json.dumps(tools or [], ensure_ascii=False))
            step = queue.pop(0)
            log.append({"system": system, "tools": tools_chars, "history": history,
                        "tokens": count_text(json.dumps(msgs, ensure_ascii=False) + json.dumps(tools or [], ensure_ascii=False)),
                        "n_tools": len(tools or []), "measured": step["measured_input_tokens"]})
            parts = [types.Part(text=x, thought=True) for x in step["thoughts"]]
            if step["text"]:
                parts.append(types.Part(text=step["text"]))
            parts += [types.Part(function_call=types.FunctionCall(name=c["name"], args=c["args"])) for c in step["calls"]]
            yield LlmResponse(content=types.Content(role="model", parts=parts))

    return Scripted(model="scripted")


def targeted(turn: dict) -> dict:
    """La question, si le modèle suit la consigne de tool_bi_query_dashboard :
    les requêtes par canal ne demandent que le KPI ROAS (ESTIMATION : le vrai
    modèle peut s'y prendre autrement)."""
    turn = json.loads(json.dumps(turn))
    for step in turn["steps"][1:]:
        for call in step["calls"]:
            if call["name"] == "tool_bi_query_dashboard":
                call["args"]["component_ids"] = "ROAS"
    return turn


async def run_turn(name: str, live: set[str]) -> list[dict]:
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    turn = targeted(SCENARIO["question"]) if name == "question_targeted" else SCENARIO[name]
    agent = build_agent()
    log: list[dict] = []
    agent.model = scripted_llm(turn["steps"], log)
    recorded = [r for s in turn["steps"] for r in s["responses"]]

    def replay_tool(tool, args, tool_context):
        if tool.name in live:
            return None
        for i, r in enumerate(recorded):
            if r["name"] == tool.name:
                return recorded.pop(i)["response"]
        return {"error": "not recorded"}

    cbs = agent.before_tool_callback
    agent.before_tool_callback = [replay_tool] + (cbs if isinstance(cbs, list) else [cbs] if cbs else [])
    svc = InMemorySessionService()
    await svc.create_session(app_name="bench", user_id=OWNER, session_id=name)
    runner = Runner(app_name="bench", agent=agent, session_service=svc)
    msg = types.Content(role="user", parts=[types.Part(text=turn["user"])])
    async for _ in runner.run_async(user_id=OWNER, session_id=name, new_message=msg):
        pass
    return log


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--targeted", action="store_true", help="ajoute la variante question_targeted (estimation)")
    args = ap.parse_args()
    os.environ.setdefault("STORAGE_MODE", "local")
    live = install_live_tools()
    names = ["creation", "question"] + (["question_targeted"] if args.targeted else [])
    report = {name: asyncio.run(run_turn(name, live)) for name in names}
    if args.json:
        print(json.dumps(report))
        return
    for name, log in report.items():
        print(f"== {name}")
        print(f"{'call':>4} {'system':>7} {'tools':>7} {'history':>8} {'chars':>8} {'tokens':>7} {'measured':>8}")
        # system/tools/history/chars : caractères du message LiteLLM ; tokens : tokenizer Gemini local
        for i, r in enumerate(log, 1):
            total = r["system"] + r["tools"] + r["history"]
            print(f"{i:>4} {r['system']:>7} {r['tools']:>7} {r['history']:>8} {total:>8} {r['tokens'] or '':>7} {r['measured'] or '':>8}")
        print(f"     total chars {sum(r['system'] + r['tools'] + r['history'] for r in log)}"
              f"  tokens {sum(r['tokens'] or 0 for r in log)}  measured on DEV {sum(r['measured'] or 0 for r in log)}")


if __name__ == "__main__":
    sys.exit(main())
