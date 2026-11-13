"""Budget de taille des requêtes au modèle sur les conversations BI du banc.

Rejoue bench/bi_tokens (modèle scripté, vrai agent, vrais outils BI) : les
contrôles qualité du banc doivent rester verts, et l'historique envoyé reste
borné : une réponse d'outil BI qui grossit fait échouer ce test au lieu de
renchérir chaque conversation en silence. Les déclarations d'outils, communes
à tous les agents de chat, ne sont pas bornées ici.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "bench" / "bi_tokens"


@pytest.fixture(scope="module")
def replay():
    import apowerb.bi.db_stores as db_stores
    import apowerb.tools_store.portfolio.bi_dashboard_tools as t
    import apowerb.tools_store.portfolio.business_intelligence as bi
    from apowerb.core.agent_helpers import agent_utils, extras_loader

    saved = [(mod, name, getattr(mod, name)) for mod, name in (
        (t, "_dashboard_store"), (t, "_compute"), (t, "_agent_owner"), (t, "_run_tool"),
        (bi, "_get_session"), (bi, "_agent_owner"), (db_stores, "DatabaseDashboardStore"),
        (agent_utils, "get_agent_details"), (extras_loader, "_owner_category_params"),
    )]
    sys.path.insert(0, str(BENCH))
    try:
        import replay as module
    finally:
        sys.path.remove(str(BENCH))
    yield module, module.install_live_tools()
    for mod, name, value in saved:
        setattr(mod, name, value)


def _history(log):
    return sum(r["history"] for r in log)


def test_demo_conversation_history_stays_under_its_budget(replay):
    module, live = replay
    creation = asyncio.run(module.run_turn("creation", live))
    question = asyncio.run(module.run_turn("question", live))
    # 15/10, caractères d'historique sur les appels du tour :
    # 98 466 et 59 135 avant, 70 041 et 43 379 après.
    assert _history(creation) <= 77_000
    assert _history(question) <= 48_000


def test_targeted_queries_still_carry_the_roas_of_every_channel(replay):
    """Ce que le modèle a répondu en démo (Email 32,8, Google Ads 3,6,
    Instagram 3,0) doit rester lisible quand il ne demande que le KPI ROAS."""
    module, _ = replay
    import apowerb.tools_store.portfolio.bi_dashboard_tools as t

    dash_id = module._query_fixture()[None]["dashboard_id"]
    seen = {}
    for canal in ("Email", "Google Ads", "Instagram"):
        res = t.tool_bi_query_dashboard(dash_id, json.dumps({"canal": canal}), component_ids="ROAS")
        assert [c["title"] for c in res["components"]] == ["ROAS"]
        seen[canal] = res["components"][0]["value"]
    assert seen == {"Email": "32.8", "Google Ads": "3.6", "Instagram": "3.0"}


def test_every_bench_scenario_keeps_its_quality_checks_green(replay):
    """Outils appelés toujours déclarés, outils LIVE sans erreur, chiffres et
    liens de chaque réponse finale encore visibles par le modèle."""
    module, _ = replay
    report = module.run_all(estimates=True)
    assert {"creation", "question", "dashboard_chat", "multi_turn", "forecast", "main_chat_spec"} <= set(report)
    failed = {name: [c for c in s["quality"]["checks"] if not c["ok"]]
              for name, s in report.items() if not s["quality"]["ok"] and not s["known_failure"]}
    assert failed == {}
    # Un échec connu doit rester un échec : quand le correctif arrive, ce test
    # le signale et le champ known_failure du scénario est à retirer.
    still_failing = {name: s["quality"]["ok"] for name, s in report.items() if s["known_failure"]}
    assert still_failing == {}


def test_group_by_gives_the_same_roas_as_one_query_per_channel(replay):
    """group_by="canal" en un appel = les quatre requêtes filtrées de la démo."""
    module, _ = replay
    import apowerb.tools_store.portfolio.bi_dashboard_tools as t

    dash_id = module._query_fixture()[None]["dashboard_id"]
    res = t.tool_bi_query_dashboard(dash_id, group_by="canal", component_ids="ROAS")
    assert res["success"] and res["group_by"] == "canal" and "canal" not in res["filters"]
    grouped = {g["value"]: [c["value"] for c in g["components"]] for g in res["groups"]}
    one_by_one = {
        canal: [c["value"] for c in t.tool_bi_query_dashboard(
            dash_id, json.dumps({"canal": canal}), component_ids="ROAS")["components"]]
        for canal in ("Email", "Google Ads", "Instagram", "Organic")
    }
    assert grouped == one_by_one
    assert grouped["Email"] == ["32.8"]


@pytest.mark.parametrize("kwargs,error", [
    ({"group_by": "canal"}, "needs component_ids"),
    ({"group_by": "region", "component_ids": "ROAS"}, "Filter 'region' has no values"),
])
def test_group_by_misuse_is_explained(replay, kwargs, error):
    module, _ = replay
    import apowerb.tools_store.portfolio.bi_dashboard_tools as t

    res = t.tool_bi_query_dashboard(module._query_fixture()[None]["dashboard_id"], **kwargs)
    assert res["success"] is False and error in res["error"]

def test_compare_does_not_fail_on_a_known_failure(replay):
    module, _ = replay

    def scenario(ok, known=None):
        return {"estimate": False, "known_failure": known, "calls": [{}], "n_tools": 1,
                "first_call_tokens": 1, "tokens": 10,
                "quality": {"ok": ok, "undeclared_in_instruction": []}}

    before = {"meta": {}, "scenarios": {"a": scenario(True)}}
    known = {"meta": {}, "scenarios": {"a": scenario(True), "b": scenario(False, "défaut connu")}}
    broken = {"meta": {}, "scenarios": {"a": scenario(False)}}
    table, ok = module.compare(before, known)
    assert ok and "❌ connu" in table
    assert module.compare(before, broken)[1] is False


def _summary(module, live, sc):
    return module.summarize(sc, asyncio.run(module.run_scenario(sc, live)))


def _dashboard_chat_ending_with(module, text):
    """Le scénario dashboard_chat dont la réponse finale est remplacée par ``text``."""
    sc = json.loads(json.dumps(module.load_scenarios()["dashboard_chat"]))
    sc["turns"][-1]["steps"][-1]["text"] = text
    return sc


@pytest.mark.parametrize("text", ["", "  \n"])
def test_a_final_reply_without_text_is_its_own_failed_outcome(replay, text):
    """Un modèle qui épuise son budget en raisonnement ne rend aucun texte : sans
    nombre ni lien à vérifier, « grounded » le laissait passer."""
    module, live = replay
    quality = _summary(module, live, _dashboard_chat_ending_with(module, text))["quality"]
    empty = [c for c in quality["checks"] if c["check"] == "empty_reply"]
    grounded = [c for c in quality["checks"] if c["check"] == "grounded"]
    assert quality["ok"] is False
    assert quality["empty_replies"] == 1 and len(empty) == 1 and empty[0]["ok"] is False
    assert all(c["call"] != empty[0]["call"] for c in grounded)


def test_a_final_reply_with_text_is_still_checked_for_grounding(replay):
    module, live = replay
    quality = _summary(module, live, module.load_scenarios()["dashboard_chat"])["quality"]
    assert quality["ok"] and quality["empty_replies"] == 0
    assert "grounded" in [c["check"] for c in quality["checks"]]


def test_a_turn_paused_on_a_tool_call_is_not_an_empty_reply(replay):
    """L'action-card met le tour en pause sur un appel d'outil sans texte : le
    modèle n'a pas rendu une réponse vide."""
    module, live = replay
    turn = json.loads(json.dumps(module.SCENARIO["creation"]))
    assert turn["steps"][-1]["calls"]
    turn["steps"][-1]["text"] = ""
    quality = _summary(module, live, {"turns": [{**turn, "facts": []}]})["quality"]
    assert quality["ok"] and quality["empty_replies"] == 0


def test_the_report_names_an_empty_reply(replay, capsys):
    module, live = replay
    summary = _summary(module, live, _dashboard_chat_ending_with(module, ""))
    module.print_report({"dashboard_chat": summary})
    out = capsys.readouterr().out
    assert "quality FAIL" in out and "✗ empty_reply" in out
    assert "réponses vides : 1" in out
