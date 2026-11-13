"""Banc tokens + qualité des agents BI : rejoue des conversations hors ligne et
mesure chaque requête envoyée au modèle.

Le modèle est remplacé par un script qui refait les appels d'outils enregistrés ;
l'agent (`to_agent`), ses outils, sa consigne et la conversion LiteLLM d'ADK sont
ceux du code. Les outils LIVE tournent pour de vrai sur des données fixes ; les
autres renvoient leur réponse enregistrée.

    python bench/bi_tokens/replay.py                    # tableau + contrôles qualité
    python bench/bi_tokens/replay.py --estimates        # + variantes estimées
    python bench/bi_tokens/replay.py --save main        # écrit results/main.json
    python bench/bi_tokens/replay.py --compare results/main.json   # avant/après en Markdown

Scénarios : `scenarios/*.json` (voir README). `creation` et `question` sont la
conversation filmée pour la démo BI (agent « BI Assistant », gabarit
forecasting_agent, DEV, 14/10/2026), enregistrée dans `scenario.json` avec
llm_usage.input_tokens de chaque appel réel. Les autres sont construits à partir
d'elle (multi-tours) ou synthétiques (prévision). Données fictives.

Qualité, vérifiée à chaque appel du modèle (sortie non nulle si un contrôle échoue) :
- declared : chaque outil que le modèle appelle est déclaré dans la requête ;
- tool_ok : aucun outil LIVE ne renvoie d'erreur ;
- grounded : chaque nombre et lien /bi/ de la réponse finale d'un tour figure
  dans ce que le modèle voit au moment de l'écrire ;
- empty_reply : le tour se termine sans aucun texte (budget épuisé en raisonnement, par
  exemple) ; en échec et compté à part, « grounded » ne jugeant pas du vide ;
- facts : expressions propres au scénario, cherchées au même endroit.
Le modèle étant scripté, ces contrôles disent si l'information nécessaire reste
disponible, pas si un vrai modèle ferait les mêmes choix : seul le mode réel
(`dev_usage.py`, llm_usage en DEV) le mesure.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).parent
SCENARIO = json.loads((HERE / "scenario.json").read_text())
OWNER = "bench@example.com"
MODEL = "gemini/gemini-2.5-flash"
AGENT = "agent1421"


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

    extras_loader._owner_category_params = lambda *a, **k: {}  # base tool_config absente
    row = _agent_row()
    agent_utils.get_agent_details = lambda agent_id, items_to_select="*": dict(row)
    return agent_utils.to_agent(AGENT)


# --- outils LIVE : le vrai code sur des données fixes -----------------------
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
    # Composants spec v2 : visual renseigné, ni chart ni key_value (anciens tableaux)
    comps = [SimpleNamespace(id=c["id"], visual=SimpleNamespace(title=c.get("title"), kpi=None, type=c["type"]),
                             chart=None, key_value=None)
             for c in first["components"]]
    dashboard = SimpleNamespace(id=first["dashboard_id"], title=first["title"], components=comps, sources={"s": 1}, filters=[])

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

    # tool_get_dashboard_data lit le même tableau par le service des tableaux de bord
    import apowerb.bi.db_stores as db_stores
    import apowerb.tools_store.portfolio.business_intelligence as bi

    @contextlib.asynccontextmanager
    async def no_db():
        yield None

    bi._get_session = no_db
    bi._agent_owner = lambda: OWNER
    db_stores.DatabaseDashboardStore = lambda db, owner=None: Store()
    return {"tool_bi_query_dashboard", "tool_bi_list_dashboard_templates", "read_uploaded_file",
            "tool_get_dashboard_data"}


def write_orders_csv(path: Path, rows: int = 13554) -> None:
    """Le CSV de la démo (mêmes colonnes, même nombre de lignes), valeurs
    tirées d'une graine fixe : read_uploaded_file le lit pour de vrai."""
    import random
    from datetime import date, timedelta

    rnd = random.Random(1421)
    channels = [("Email", 0.4), ("Google Ads", 9.0), ("Instagram", 11.0), ("Organic", 0.0)]
    start = date(2025, 10, 1)
    lines = ["order_date,order_id,customer_id,order_amount,product_cost,channel,ad_spend,new_customer,status"]
    for i in range(rows):
        day = start + timedelta(days=int(365 * i / rows))
        amount = round(rnd.uniform(15, 180), 2)
        channel, cpa = channels[rnd.randrange(4)]
        lines.append(
            f"{day.isoformat()},ORD-{i + 1:06d},C{rnd.randrange(1, 4200):05d},{amount},"
            f"{round(amount * rnd.uniform(0.5, 0.75), 2)},{channel},{round(cpa * rnd.uniform(0.5, 1.5), 2)},"
            f"{int(rnd.random() < 0.3)},{rnd.choice(['completed'] * 18 + ['refunded', 'cancelled'])}"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")


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


def visible_text(msgs: list[dict]) -> str:
    """Ce que le modèle lit, sans guillemets ni échappements : les contrôles
    de faits ne dépendent ni de l'ordre des clés ni du JSON imbriqué."""
    raw = json.dumps(msgs, ensure_ascii=False)
    for _ in range(3):
        raw = raw.replace('\\"', '"').replace("\\\\", "\\")
    return re.sub(r"\s+", " ", re.sub(r'["\\]', "", raw))


# Noms d'outils cités par la consigne : un nom cité mais non déclaré pousse le
# modèle vers un appel qui échouera (suivi dans « undeclared », comparé avant/après).
_TOOL_NAME = re.compile(
    r"\b(?:tool_[a-z0-9_]*[a-z0-9]|request_[a-z_]+|confirm_destructive|embed_chart|notify_user|find_tools"
    r"|propose_[a-z_]+|create_tool|create_downloadable_file|read_uploaded_file|schedule_followup)\b")
_NUMBER = re.compile(r"(?<![\w.])\d+(?:[.,]\d+)*(?![\w])")
_BI_LINK = re.compile(r"/bi/[0-9a-f-]{36}")


def grounding_misses(answer: str, seen: str) -> list[str]:
    """Nombres et liens de la réponse finale absents de ce que le modèle a vu.
    Une réponse vide n'en contient aucun et passerait : la traiter avant (empty_reply)."""
    wanted = set(_BI_LINK.findall(answer)) | {n for n in _NUMBER.findall(answer) if len(n) > 1}
    def found(w: str) -> bool:  # nombre entier : « 32.8 » ne se trouve pas dans « 132.85 »
        return any(re.search(rf"(?<![\d.]){re.escape(v)}(?![\d])", seen) for v in (w, w.replace(",", "")))

    return sorted(w for w in wanted if not found(w))


# --- modèle scripté ---------------------------------------------------------
class Script:
    """File d'étapes du tour en cours ; le banc la remplit tour par tour."""

    def __init__(self):
        self.steps: list[dict] = []
        self.turn = 0
        self.log: list[dict] = []
        self.checks: list[dict] = []
        self.facts: list[str] = []
        self.undeclared: set[str] = set()


def scripted_llm(script: Script):
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.lite_llm import _get_completion_inputs
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types

    class Scripted(BaseLlm):
        async def generate_content_async(self, llm_request, stream=False):
            if not script.steps:  # en réel l'action-card met le tour en pause ici
                yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=" ")]))
                return
            messages, tools, *_ = await _get_completion_inputs(llm_request, MODEL)
            msgs = [m if isinstance(m, dict) else m.model_dump(exclude_none=True) for m in messages]
            system = sum(len(json.dumps(m, ensure_ascii=False)) for m in msgs if m.get("role") in ("system", "developer"))
            history = sum(len(json.dumps(m, ensure_ascii=False)) for m in msgs if m.get("role") not in ("system", "developer"))
            tools_chars = len(json.dumps(tools or [], ensure_ascii=False))
            declared = {t["function"]["name"] for t in tools or []}
            step = script.steps.pop(0)
            n = len(script.log) + 1
            script.log.append({"turn": script.turn, "system": system, "tools": tools_chars, "history": history,
                               "tokens": count_text(json.dumps(msgs, ensure_ascii=False) + json.dumps(tools or [], ensure_ascii=False)),
                               "n_tools": len(tools or []), "measured": step.get("measured_input_tokens")})
            missing = [c["name"] for c in step["calls"] if c["name"] not in declared]
            script.checks.append({"check": "declared", "call": n, "ok": not missing, "detail": missing})
            system_text = " ".join(str(m.get("content")) for m in msgs if m.get("role") in ("system", "developer"))
            script.undeclared |= {w for w in _TOOL_NAME.findall(system_text) if w not in declared}
            seen = visible_text(msgs)
            final = not script.steps or (step["text"].strip() and not step["calls"])
            facts = step.get("facts", []) + (script.facts if final else [])
            if facts:
                absent = [f for f in facts if not re.search(f, seen)]
                script.checks.append({"check": "facts", "call": n, "ok": not absent, "detail": absent})
            if not step["text"].strip() and not step["calls"]:  # le modèle clôt le tour sans texte
                script.checks.append({"check": "empty_reply", "call": n, "ok": False,
                                      "detail": ["réponse finale sans texte"]})
            elif final:
                misses = grounding_misses(step["text"], seen)
                script.checks.append({"check": "grounded", "call": n, "ok": not misses, "detail": misses})
            parts = [types.Part(text=x, thought=True) for x in step["thoughts"]]
            if step["text"]:
                parts.append(types.Part(text=step["text"]))
            parts += [types.Part(function_call=types.FunctionCall(name=c["name"], args=c["args"])) for c in step["calls"]]
            yield LlmResponse(content=types.Content(role="model", parts=parts))

    return Scripted(model="scripted")


# --- scénarios --------------------------------------------------------------
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


def grouped(turn: dict) -> dict:
    """La question, si le modèle compare les canaux en un appel (group_by)
    au lieu d'un appel par canal (ESTIMATION, comme targeted)."""
    turn = json.loads(json.dumps(turn))
    by_canal = turn["steps"][1]
    dash_id = by_canal["calls"][0]["args"]["dashboard_id"]
    by_canal["calls"] = [{"name": "tool_bi_query_dashboard",
                          "args": {"dashboard_id": dash_id, "group_by": "canal", "component_ids": "ROAS"}}]
    by_canal["responses"] = []
    return turn


TRANSFORMS = {"targeted": targeted, "grouped": grouped}


def load_scenarios(estimates: bool = False) -> dict[str, dict]:
    out = {}
    for path in sorted((HERE / "scenarios").glob("*.json")):
        sc = json.loads(path.read_text())
        if sc.get("estimate") and not estimates:
            continue
        turns = []
        for t in sc["turns"]:
            turn = json.loads(json.dumps(SCENARIO[t["recorded"]])) if "recorded" in t else dict(t)
            if t.get("transform"):  # appels changés : la mesure DEV ne vaut plus
                turn = TRANSFORMS[t["transform"]](turn)
                for step in turn["steps"]:
                    step["measured_input_tokens"] = None
            turn["facts"] = t.get("facts", [])
            turns.append(turn)
        out[path.stem] = {**sc, "turns": turns}
    return out


async def run_scenario(sc: dict, live: set[str]) -> Script:
    """Rejoue ``sc`` ; ``env`` du scénario est posé le temps du rejeu."""
    saved_env = {k: os.environ.get(k) for k in sc.get("env", {})}
    os.environ.update(sc.get("env", {}))
    try:
        return await _run_scenario(sc, live)
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


async def _run_scenario(sc: dict, live: set[str]) -> Script:
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    agent = build_agent()
    script = Script()
    agent.model = scripted_llm(script)
    recorded: list[dict] = []

    def replay_tool(tool, args, tool_context):
        if tool.name in live:
            return None
        for i, r in enumerate(recorded):
            if r["name"] == tool.name:
                return recorded.pop(i)["response"]
        script.checks.append({"check": "tool_ok", "call": len(script.log), "ok": False,
                              "detail": [f"{tool.name}: no recorded response"]})
        return {"error": "not recorded"}

    def check_live(tool, args, tool_context, tool_response):
        if tool.name in live and isinstance(tool_response, dict):
            failed = tool_response.get("success") is False or tool_response.get("status") == "error"
            script.checks.append({"check": "tool_ok", "call": len(script.log), "ok": not failed,
                                  "detail": [tool.name] if failed else []})
        return None

    cbs = agent.before_tool_callback
    agent.before_tool_callback = [replay_tool] + (cbs if isinstance(cbs, list) else [cbs] if cbs else [])
    after = agent.after_tool_callback
    agent.after_tool_callback = (after if isinstance(after, list) else [after] if after else []) + [check_live]
    svc = InMemorySessionService()
    await svc.create_session(app_name="bench", user_id=OWNER, session_id="bench")
    runner = Runner(app_name="bench", agent=agent, session_service=svc)
    for i, turn in enumerate(sc["turns"], 1):
        script.turn, script.steps, script.facts = i, list(turn["steps"]), turn["facts"]
        recorded[:] = [r for s in turn["steps"] for r in s["responses"]]
        msg = types.Content(role="user", parts=[types.Part(text=turn["user"])])
        async for _ in runner.run_async(user_id=OWNER, session_id="bench", new_message=msg):
            pass
        if script.steps:
            script.checks.append({"check": "declared", "call": len(script.log), "ok": False,
                                  "detail": [f"turn {i}: {len(script.steps)} scripted call(s) never reached"]})
    return script


async def run_turn(name: str, live: set[str]) -> list[dict]:
    """Un tour enregistré seul (creation, question, question_targeted)."""
    turn = targeted(SCENARIO["question"]) if name == "question_targeted" else SCENARIO[name]
    script = await run_scenario({"turns": [{**turn, "facts": []}]}, live)
    return script.log


def summarize(sc: dict, script: Script) -> dict:
    log = script.log
    return {
        "description": sc.get("description", ""), "source": sc.get("source", ""),
        "estimate": bool(sc.get("estimate")), "known_failure": sc.get("known_failure"),
        "turns": len(sc["turns"]), "calls": log,
        "tokens": sum(r["tokens"] or 0 for r in log) if all(r["tokens"] for r in log) else None,
        "chars": sum(r["system"] + r["tools"] + r["history"] for r in log),
        "first_call_tokens": log[0]["tokens"] if log else None,
        "n_tools": log[0]["n_tools"] if log else 0,
        "measured": sum(r["measured"] for r in log) if log and all(r["measured"] for r in log) else None,
        "quality": {"ok": all(c["ok"] for c in script.checks), "checks": script.checks,
                    "empty_replies": sum(c["check"] == "empty_reply" for c in script.checks),
                    "undeclared_in_instruction": sorted(script.undeclared)},
    }


def run_all(estimates: bool = False) -> dict:
    import logging
    import shutil

    from apowerb.configs.settings import get_settings

    logging.disable(logging.CRITICAL)  # les journaux de to_agent noieraient le tableau
    # Réglages changés sur le singleton puis rendus : vider le cache de
    # get_settings laisserait aux autres tests un objet différent de celui
    # que leurs modules ont importé.
    settings = get_settings()
    saved = {k: getattr(settings, k) for k in ("storage_mode", "uploads_dir")}
    uploads = Path(tempfile.mkdtemp(prefix="bi-bench-uploads-"))
    settings.storage_mode, settings.uploads_dir = "local", str(uploads)
    write_orders_csv(uploads / AGENT / "online_store_orders.csv")
    try:
        live = install_live_tools()
        scenarios = load_scenarios(estimates)
        return {name: summarize(sc, asyncio.run(run_scenario(sc, live))) for name, sc in scenarios.items()}
    finally:
        shutil.rmtree(uploads, ignore_errors=True)
        for k, v in saved.items():
            setattr(settings, k, v)
        logging.disable(logging.NOTSET)


# --- sorties ----------------------------------------------------------------
def _git_sha() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=HERE, capture_output=True,
                              text=True, check=True).stdout.strip()
    except Exception:
        return "unknown"


def print_report(report: dict) -> None:
    for name, s in report.items():
        tag = (" (ESTIMATION)" if s["estimate"] else "") + (
            f" (ÉCHEC CONNU : {s['known_failure']})" if s.get("known_failure") else "")
        print(f"== {name}{tag} — {s['turns']} tour(s), {len(s['calls'])} appels, {s['n_tools']} outils")
        print(f"{'call':>4} {'turn':>4} {'system':>7} {'tools':>7} {'history':>8} {'chars':>8} {'tokens':>7} {'measured':>8}")
        for i, r in enumerate(s["calls"], 1):
            total = r["system"] + r["tools"] + r["history"]
            print(f"{i:>4} {r['turn']:>4} {r['system']:>7} {r['tools']:>7} {r['history']:>8} {total:>8} "
                  f"{r['tokens'] or '':>7} {r['measured'] or '':>8}")
        bad = [c for c in s["quality"]["checks"] if not c["ok"]]
        print(f"     total chars {s['chars']}  tokens {s['tokens']}  measured on DEV {s['measured'] or '-'}"
              f"  quality {'OK' if not bad else 'FAIL'} ({len(s['quality']['checks'])} checks)")
        if s["quality"].get("empty_replies"):
            print(f"     réponses vides : {s['quality']['empty_replies']}")
        if s["quality"]["undeclared_in_instruction"]:
            print(f"     consigne : outils cités non déclarés {s['quality']['undeclared_in_instruction']}")
        for c in bad:
            print(f"     ✗ {c['check']} @call {c['call']}: {c['detail']}")


def _pct(before, after) -> str:
    return f"{(after - before) / before * 100:+.1f} %" if before and after is not None else "–"


def _n(v) -> str:
    return "–" if v is None else f"{v:,}"


def compare(before: dict, after: dict) -> tuple[str, bool]:
    """Tableau Markdown avant/après (tokens = tokenizer Gemini local)."""
    b_meta, a_meta = before.get("meta", {}), after.get("meta", {})
    lines = [f"Avant `{b_meta.get('label', '?')}` ({b_meta.get('git_sha', '?')}) → après "
             f"`{a_meta.get('label', '?')}` ({a_meta.get('git_sha', '?')})", "",
             "| Scénario | Appels | Outils | 1er appel (tok) | Total avant | Total après | Δ | Qualité |",
             "|---|---|---|---|---|---|---|---|"]
    ok = True
    for name, a in after["scenarios"].items():
        b = before["scenarios"].get(name)
        est = " *(estim.)*" if a["estimate"] else ""
        new_mentions = b and set(a["quality"]["undeclared_in_instruction"]) - set(
            b["quality"]["undeclared_in_instruction"])
        if new_mentions:  # la consigne cite un outil que l'agent n'a plus
            a = {**a, "quality": {**a["quality"], "ok": False}}
        # Un échec connu s'affiche mais ne fait pas échouer la comparaison.
        mark = "✅" if a["quality"]["ok"] else "❌ connu" if a.get("known_failure") else "❌"
        ok &= a["quality"]["ok"] or bool(a.get("known_failure"))
        if not b:
            lines.append(f"| {name}{est} | {len(a['calls'])} | {a['n_tools']} | {_n(a['first_call_tokens'])} | – | "
                         f"{_n(a['tokens'])} | nouveau | {mark} |")
            continue
        lines.append(
            f"| {name}{est} | {len(b['calls'])}→{len(a['calls'])} | {b['n_tools']}→{a['n_tools']} | "
            f"{_n(b['first_call_tokens'])}→{_n(a['first_call_tokens'])} | {_n(b['tokens'])} | {_n(a['tokens'])} | "
            f"{_pct(b['tokens'], a['tokens'])} | {mark} |")
    return "\n".join(lines).replace(",", " "), ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="sortie machine sur stdout")
    ap.add_argument("--estimates", action="store_true", help="ajoute les variantes estimées")
    ap.add_argument("--targeted", action="store_true", help=argparse.SUPPRESS)  # ancien nom de --estimates
    ap.add_argument("--save", metavar="LABEL", help="écrit results/LABEL.json")
    ap.add_argument("--compare", metavar="BEFORE_JSON", help="tableau Markdown avant (fichier) / après (ce code)")
    ap.add_argument("--after", metavar="AFTER_JSON", help="avec --compare : après = ce fichier au lieu de rejouer")
    args = ap.parse_args()
    if args.compare and args.after:
        table, ok = compare(json.loads(Path(args.compare).read_text()), json.loads(Path(args.after).read_text()))
        print(table)
        return 0 if ok else 1
    scenarios = run_all(args.estimates or args.targeted)
    result = {"meta": {"label": args.save or "current", "git_sha": _git_sha(),
                       "date": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"), "model": MODEL},
              "scenarios": scenarios}
    if args.save:
        out = HERE / "results" / f"{args.save}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(json.dumps(result, indent=1, ensure_ascii=False) + "\n")
    if args.json:
        print(json.dumps(result))
    elif args.compare:
        table, ok = compare(json.loads(Path(args.compare).read_text()), result)
        print(table)
        return 0 if ok else 1
    else:
        print_report(scenarios)
    return 0 if all(s["quality"]["ok"] or s.get("known_failure") for s in scenarios.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
