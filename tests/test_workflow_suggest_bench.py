"""Banc roadmap#86 (bench/workflow_suggest) : ses cas et son score sont-ils justes ?

Le banc ne décide rien s'il se trompe sur la réponse attendue. On vérifie ici,
sans modèle : que chaque workflow du jeu est valide, que chaque cas a bien une
place libre là où le workflow complet a son nœud suivant, que ce nœud était
proposable par le modèle, et que le score compte ce qu'il annonce.
"""

import importlib.util
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import litellm
import pytest

from apowerb.configs.settings import get_settings
from apowerb.core import run_gate, workflow_suggest
from apowerb.core import workflow_graph as wg
from apowerb.core.agent_helpers import usage_recorder

_PATH = Path(__file__).resolve().parents[1] / "bench" / "workflow_suggest" / "bench.py"
_spec = importlib.util.spec_from_file_location("workflow_suggest_bench", _PATH)
bench = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bench)

DATA = bench.load_dataset()
CASES = bench.build_cases(DATA)
OWNED = {a["agent_id"] for a in DATA["agents"]}


@pytest.mark.parametrize("wf", DATA["workflows"], ids=lambda w: w["key"])
def test_every_complete_workflow_is_valid(wf):
    wg.validate_graph(wg.WorkflowGraph.model_validate(wf["graph"]))
    used = {n["config"]["agent_id"] for n in wf["graph"]["nodes"] if n["config"].get("agent_id")}
    assert used <= OWNED


def test_one_case_per_node_after_the_trigger():
    assert len(CASES) == sum(len(w["graph"]["nodes"]) - 1 for w in DATA["workflows"]) == 44
    assert len({c["id"] for c in CASES}) == len(CASES)


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_the_server_and_the_rules_see_the_slot_the_answer_fills(case):
    graph = wg.WorkflowGraph.model_validate(case["graph"])
    source = next(n for n in graph.nodes if n.id == case["node_id"])
    assert workflow_suggest.target_route(graph, source) == (True, case["expected"]["route"])
    node = next(n for n in case["graph"]["nodes"] if n["id"] == case["node_id"])
    rules = bench.rule_suggestions(node, case["graph"]["nodes"], case["graph"]["edges"])
    assert rules and {r["route"] for r in rules} == {case["expected"]["route"]}


@pytest.mark.parametrize(
    "case",
    [c for c in CASES if c["expected"]["type"] in workflow_suggest.SUGGESTIBLE_TYPES],
    ids=lambda c: c["id"],
)
def test_the_expected_node_is_one_the_model_may_propose(case):
    """Sinon le banc reprocherait au modèle une réponse que la route écarterait."""
    graph = wg.WorkflowGraph.model_validate(case["graph"])
    source = next(n for n in graph.nodes if n.id == case["node_id"])
    raw = {"type": case["expected"]["type"], "config": case["expected"]["config"]}
    kept = workflow_suggest.check_proposal(
        raw, graph=graph, source=source, owned_agents=OWNED, taken_ids={n.id for n in graph.nodes}
    )
    assert kept is not None and kept["config"] == case["expected"]["config"]


def test_only_http_is_out_of_the_model_reach():
    unreachable = {c["expected"]["type"] for c in CASES} - set(workflow_suggest.SUGGESTIBLE_TYPES)
    assert unreachable == {"http"}


def _case(expected_type, node_id="n", route=None, agent=None):
    cfg = {"agent_id": agent} if agent else {}
    return {"node_id": node_id, "expected": {"type": expected_type, "route": route, "config": cfg}}


def test_the_model_goes_first_and_pushes_rule_chips_down():
    rules = [{"type": t, "route": None} for t in ("output", "condition", "notification")]
    ai = [{"type": "agent", "config": {"agent_id": "agent2", "input": "{{n}}"}}, {"type": "set", "config": {}}]
    score = bench.score_case(_case("notification", agent=None), rules, ai, None)
    assert score["rules_top3"] and not score["merged_top3"]
    assert score["merged_types"] == ["agent", "set", "output"]

    score = bench.score_case(_case("agent", agent="agent2"), rules, ai, None)
    assert not score["rules_top3"] and score["merged_top3"] and score["merged_top1"]
    assert score["config"] == {"agent_ok": True, "reads_source": True}


def test_a_duplicate_type_from_the_model_replaces_the_rule_chip():
    rules = [{"type": t, "route": "b"} for t in ("output", "agent")]
    ai = [{"type": "output", "config": {"value": "{{elsewhere}}"}}]
    score = bench.score_case(_case("agent", route="b"), rules, ai, "b")
    assert score["merged_types"] == ["output", "agent"] and score["merged_top3"] and score["route_ok"]
    assert score["config"] is None


def _record(case, repeat, rules_hit, merged_hit, latency, status=200):
    return {
        "case": case,
        "repeat": repeat,
        "status": status,
        "reason": None if status == 200 else "timeout",
        "latency_s": latency,
        "tokens_in": 100,
        "tokens_out": 20,
        "cost_usd": 0.0001,
        "score": {
            "rules_top3": rules_hit,
            "merged_top3": merged_hit,
            "rules_top1": False,
            "merged_top1": False,
            "ai_hit": merged_hit,
            "route_ok": True,
            "config": None,
            "ai_types": ["agent"],
        },
    }


def test_go_needs_both_the_gain_and_the_latency():
    fast_gain = [_record(f"c{i}", 1, i < 5, i < 7, 1.0) for i in range(10)]
    s = bench.summarize(fast_gain, usd_per_eur=1.1)
    assert (s["rules_top3_pct"], s["merged_top3_pct"], s["gain_points"], s["go"]) == (50.0, 70.0, 20.0, True)
    assert s["cost_eur_per_call"] == round(0.0001 / 1.1, 7)

    slow = fast_gain[:-1] + [_record("c9", 1, False, False, 3.2)]
    assert bench.summarize(slow, usd_per_eur=None)["latency_p95_s"] == 3.2
    assert not bench.summarize(slow, usd_per_eur=None)["go"]

    small_gain = [_record(f"c{i}", 1, i < 5, i < 6, 1.0) for i in range(10)]
    assert not bench.summarize(small_gain, usd_per_eur=None)["go"]


def test_percentile_is_nearest_rank():
    values = [float(i) for i in range(1, 21)]
    assert bench.percentile(values, 95) == 19.0 and bench.percentile(values, 50) == 10.0


def test_parity_flags_any_difference_with_the_editor():
    js = {}
    for c in CASES:
        node = next(n for n in c["graph"]["nodes"] if n["id"] == c["node_id"])
        rules = bench.rule_suggestions(node, c["graph"]["nodes"], c["graph"]["edges"])
        ai = [{"type": c["expected"]["type"]}, {"type": "output"}]
        js[c["id"]] = {"rules": rules, "merged": [s["type"] for s in bench.merge_suggestions(rules, ai)]}
    assert bench.parity_diff(CASES, js) == []
    js[CASES[0]["id"]]["rules"] = js[CASES[0]["id"]]["rules"][::-1]
    assert [d.split(":")[0] for d in bench.parity_diff(CASES, js)] == [CASES[0]["id"]]


def test_a_run_goes_through_the_real_route(monkeypatch):
    settings = get_settings()
    for name, value in (
        ("workflow_suggest_enabled", False),
        ("workflow_suggest_model", ""),
        ("default_llm_model", "gemini/gemini-test"),
        ("default_llm_api_key", "k-test"),
        ("default_llm_api_base", ""),
    ):
        monkeypatch.setattr(settings, name, value)
    # Route() rewires these module attributes; monkeypatch puts them back.
    for module, name in (
        (run_gate, "apply_run_guards"),
        (run_gate, "resolve_owner_plan"),
        (usage_recorder, "_persist_usage_row"),
        (workflow_suggest, "_owner_agents"),
    ):
        monkeypatch.setattr(module, name, getattr(module, name))
    sent = []

    async def fake_completion(**kwargs):
        sent.append(json.loads(kwargs["messages"][1]["content"]))
        answer = {"suggestions": [{"type": "rag", "config": {"agent_id": "agent5"}}, {"type": "classifier", "config": {"agent_id": "agent1", "input": "{{trigger1.body}}", "routes": [{"route": "a"}, {"route": "b"}]}}]}
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(answer)))],
            usage=SimpleNamespace(prompt_tokens=300, completion_tokens=40, total_tokens=340),
        )

    monkeypatch.setattr(litellm, "acompletion", fake_completion)
    route = bench.Route(DATA["agents"])
    records = bench.run(CASES[:1], route, repeats=2)
    logging.getLogger(workflow_suggest.__name__).removeHandler(route.log_handler)

    assert [r["status"] for r in records] == [200, 200]
    assert records[0]["score"]["ai_types"] == ["classifier"] and records[0]["score"]["merged_top1"]
    assert (records[0]["tokens_in"], records[0]["tokens_out"]) == (300, 40)
    assert sent[0]["agents"] == DATA["agents"] and sent[0]["selected_node"] == "trigger1"
    assert [len(r["rejected"]) for r in records] == [1, 1] and "query" in records[0]["rejected"][0]
    summary = bench.summarize(records, usd_per_eur=None)
    assert summary["rejected_pct"] == 50.0 and sum(summary["rejected_reasons"].values()) == 2


def test_no_verdict_when_the_model_never_answered(monkeypatch, tmp_path):
    class Down:
        model, timeout_s = "gemini/gemini-test", 4.0

        def __init__(self, agents):
            pass

        def ask(self, case):
            return {"status": 503, "latency_s": 0.05, "route": None, "suggestions": [],
                    "reason": "error", "tokens_in": 0, "tokens_out": 0}

    monkeypatch.setattr(bench, "Route", Down)
    assert bench.main(["--repeats", "1", "--out", str(tmp_path)]) == 1
    assert list(tmp_path.iterdir()) == []


def test_a_label_keeps_earlier_reports(monkeypatch, tmp_path):
    class Up:
        model, timeout_s = "gemini/gemini-test", 4.0

        def __init__(self, agents):
            pass

        def ask(self, case):
            return {"status": 200, "latency_s": 1.0, "route": case["expected"]["route"],
                    "suggestions": [], "reason": None, "tokens_in": 0, "tokens_out": 0, "rejected": []}

    monkeypatch.setattr(bench, "Route", Up)
    assert bench.main(["--repeats", "1", "--out", str(tmp_path)]) == 0
    assert bench.main(["--repeats", "1", "--out", str(tmp_path), "--label", "v2"]) == 0
    names = sorted(p.name for p in tmp_path.iterdir())
    assert len(names) == 4 and sum(n.endswith("-v2.md") for n in names) == 1
