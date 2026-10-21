"""Ce qu'un nœud Agent de workflow rend au nœud suivant.

Reproduction du 21/10/2026 (Workflow Studio, analyste -> forecasting) : un
graphique créé par un outil disparaît de la sortie du nœud, qui ne garde que
« J'ai généré le graphique… » ; un ``output_schema`` sans clé
« instruction » est ignoré, donc l'agent ne rend jamais de JSON lisible par
``{{agent1.champ}}``.
"""

import asyncio
import json


from apowerb.core import workflow_engine as we
from apowerb.core import workflow_graph as wg
from apowerb.core.agent_helpers.llm_model_builder import apply_output_schema


# --- Exécution d'un agent ADK simulée --------------------------------------


def _patch_adk(monkeypatch, events):
    import apowerb.core.adk_runner as adk
    import apowerb.core.run_gate as gate

    sent = []

    async def _guards(**kw):
        return None

    async def _session(**kw):
        return None

    async def _run(**kw):
        sent.append(kw["new_message"])
        return events

    monkeypatch.setattr(gate, "apply_run_guards", _guards)
    monkeypatch.setattr(adk, "create_adk_agent_session", _session)
    monkeypatch.setattr(adk, "run_adk_agent", _run)
    return sent


def _ask(message="Analyse les ventes"):
    return asyncio.run(
        we.run_agent_message(
            "agent3",
            message,
            owner_email="a@x.fr",
            plan=None,
            token_factory=lambda: "T",
        )
    )


def test_the_legacy_canvas_runner_still_returns_the_bare_output(monkeypatch):
    sent = _patch_adk(monkeypatch, [{"content": {"parts": [{"text": '{"ok": 1}'}]}}])
    leaf = we.make_http_leaf_runner(
        owner_email="a@x.fr", plan=None, token_factory=lambda: "T"
    )
    out = asyncio.run(leaf(we.AgentSpec(agent_id="agent3"), {"q": 1}))
    assert out == {"ok": 1}
    assert sent[0]["parts"] == [{"text": '{"q": 1}'}]


# --- Identifiants produits par les outils --------------------------------


def _tool_event(name, response, camel=True):
    key = "functionResponse" if camel else "function_response"
    return {"content": {"parts": [{key: {"name": name, "response": response}}]}}


def test_tool_artifacts_keeps_the_ids_returned_by_tools():
    events = [
        _tool_event(
            "tool_create_chart", {"success": True, "chart_id": "c1", "title": "Ventes"}
        ),
        _tool_event(
            "tool_create_forecast_dashboard",
            {"dashboard_id": "d1", "forecast_chart_id": "f1"},
            camel=False,
        ),
        {"content": {"parts": [{"text": "J'ai généré le graphique."}]}},
    ]
    assert we.tool_artifacts(events) == [
        {"tool": "tool_create_chart", "chart_id": "c1"},
        {
            "tool": "tool_create_forecast_dashboard",
            "dashboard_id": "d1",
            "forecast_chart_id": "f1",
        },
    ]


def test_tool_artifacts_ignores_failures_and_responses_without_ids():
    events = [
        _tool_event(
            "tool_create_chart", {"success": False, "chart_id": "c1", "error": "x"}
        ),
        _tool_event("tool_list_datasets", {"datasets": []}),
        "not an event",
        {"content": None},
    ]
    assert we.tool_artifacts(events) == []
    assert we.tool_artifacts("plain text") == []


def test_tool_artifacts_dedupes_the_same_id_read_twice():
    ev = _tool_event("tool_describe_dataset", {"dataset_id": "ds1"})
    assert we.tool_artifacts([ev, ev]) == [
        {"tool": "tool_describe_dataset", "dataset_id": "ds1"}
    ]


def test_run_agent_message_returns_the_text_and_the_tool_ids(monkeypatch):
    _patch_adk(
        monkeypatch,
        [
            _tool_event("tool_create_chart", {"success": True, "chart_id": "c1"}),
            {"content": {"parts": [{"text": "J'ai généré le graphique."}]}},
        ],
    )
    reply = _ask()
    assert isinstance(reply, we.AgentReply)
    assert reply.output == "J'ai généré le graphique."
    assert reply.artifacts == [{"tool": "tool_create_chart", "chart_id": "c1"}]


# --- Sortie d'un nœud agent ------------------------------------------------


def _graph(nodes, edges):
    return wg.WorkflowGraph.model_validate(
        {"version": 1, "nodes": nodes, "edges": edges}
    )


AGENT_CHAIN = _graph(
    [
        {"id": "trigger1", "type": "trigger", "config": {"kind": "manual"}},
        {"id": "agent1", "type": "agent", "config": {"agent_id": "agent1"}},
        {
            "id": "agent2",
            "type": "agent",
            "config": {"agent_id": "agent2", "input": "{{agent1.total}}"},
        },
    ],
    [
        {"source": "trigger1", "target": "agent1"},
        {"source": "agent1", "target": "agent2"},
    ],
)


def _run(graph, replies):
    calls = []

    async def run_agent(agent_id, message):
        calls.append((agent_id, message))
        return replies[agent_id]

    async def run_tool(tool, args):  # pragma: no cover - aucun nœud outil ici
        raise AssertionError(tool)

    async def _go():
        return [
            json.loads(c[len("data: ") :])
            async for c in wg.run_graph(
                graph,
                payload={"message": "go"},
                run_agent=run_agent,
                run_tool=run_tool,
                cancel_event=asyncio.Event(),
            )
        ]

    return asyncio.run(_go()), calls


def _completes(events):
    return {e["node_id"]: e for e in events if e["event"] == "node_complete"}


def test_agent_reply_artifacts_reach_node_complete_and_json_stays_addressable():
    events, calls = _run(
        AGENT_CHAIN,
        {
            "agent1": we.AgentReply(
                output={"total": 42, "unit": "EUR"},
                artifacts=[{"tool": "tool_create_chart", "chart_id": "c1"}],
            ),
            "agent2": we.AgentReply(output="ok", artifacts=[]),
        },
    )
    assert events[-1] == {"event": "done", "output": "ok"}
    done = _completes(events)
    assert done["agent1"]["output"] == {"total": 42, "unit": "EUR"}
    assert done["agent1"]["artifacts"] == [
        {"tool": "tool_create_chart", "chart_id": "c1"}
    ]
    assert "artifacts" not in done["agent2"]
    # 3. Une sortie JSON se lit par {{agent1.champ}} dans le nœud suivant.
    assert calls[1] == ("agent2", "42")


def test_classifier_and_extract_unwrap_agent_replies():
    graph = _graph(
        [
            {"id": "trigger1", "type": "trigger", "config": {"kind": "manual"}},
            {
                "id": "ext",
                "type": "extract",
                "config": {
                    "agent_id": "agent1",
                    "fields": [{"name": "total", "type": "number"}],
                },
            },
        ],
        [{"source": "trigger1", "target": "ext"}],
    )
    events, _ = _run(
        graph, {"agent1": we.AgentReply(output={"total": 3}, artifacts=[])}
    )
    assert events[-1]["event"] == "done", events
    assert events[-1]["output"]["total"] == 3


# --- output_schema sans clé « instruction » -----------------------------


def test_output_schema_with_instruction_is_unchanged():
    out = apply_output_schema("Base", {"instruction": "Return JSON {total}"}, "a")
    assert out == "Base\n\n## Output Format\nReturn JSON {total}"


def test_a_bare_json_schema_becomes_an_output_instruction():
    schema = {
        "type": "object",
        "properties": {
            "total": {"type": "number", "description": "Chiffre d'affaires"},
            "months": {"type": "array"},
        },
        "required": ["total"],
    }
    out = apply_output_schema("Base", schema, "a")
    assert out.startswith("Base\n\n## Output Format\n")
    assert "ONLY JSON" in out
    assert json.dumps(schema, ensure_ascii=False) in out


def test_an_empty_or_useless_schema_adds_nothing():
    assert apply_output_schema("Base", None, "a") == "Base"
    assert apply_output_schema("Base", {}, "a") == "Base"
    assert apply_output_schema("Base", {"foo": "bar"}, "a") == "Base"


def test_an_oversized_bare_schema_is_skipped():
    schema = {
        "type": "object",
        "properties": {f"f{i}": {"type": "string"} for i in range(400)},
    }
    assert apply_output_schema("Base", schema, "a") == "Base"


# --- Gabarit FORECASTING AGENT ------------------------------------------


def test_forecasting_template_says_a_summary_is_not_enough():
    from apowerb.core.superagents.templates.data import DATA_TEMPLATES

    tpl = next(t for t in DATA_TEMPLATES if t["template_id"] == "forecasting_agent")
    desc = tpl["description"].lower()
    for must in ("file", "dataset", "sql", "summary"):
        assert must in desc, must


def test_an_array_schema_is_also_turned_into_an_instruction():
    out = apply_output_schema(
        "Base", {"type": "array", "items": {"type": "number"}}, "a"
    )
    assert "ONLY JSON" in out


def test_tool_artifacts_skips_booleans_and_is_capped():
    assert we.tool_artifacts([_tool_event("t", {"dataset_id": True})]) == []
    many = [_tool_event("t", {"dataset_id": f"d{i}"}) for i in range(50)]
    assert len(we.tool_artifacts(many)) == we._MAX_ARTIFACTS
