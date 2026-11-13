"""Passation entre deux nœuds agent : ce que le run montre et ce que l'agent reçoit.

Cas réel (21/10/26) : un agent business_analyst relié à un agent forecasting ;
le second répondait « je n'ai pas pu récupérer les valeurs de l'agent
précédent ». La sortie passait bien, mais le premier finissait par une question adressée à
un humain absent, que le second prenait pour une demande.
"""

import asyncio
import json

from apowerb.core import workflow_graph as wg

PAYLOAD = {"message": "Analyse les ventes mensuelles"}


def _graph(nodes, edges):
    return wg.WorkflowGraph.model_validate(
        {"version": 1, "nodes": nodes, "edges": edges}
    )


def _chain(agent2_cfg=None):
    return _graph(
        [
            {"id": "trigger1", "type": "trigger", "config": {"kind": "manual"}},
            {"id": "agent1", "type": "agent", "config": {"agent_id": "a1"}},
            {
                "id": "agent2",
                "type": "agent",
                "config": {"agent_id": "a2", **(agent2_cfg or {})},
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
        return replies.get(agent_id, f"out:{agent_id}")

    async def run_tool(tool, args):
        return {}

    async def _go():
        out = []
        async for chunk in wg.run_graph(
            graph,
            payload=PAYLOAD,
            run_agent=run_agent,
            run_tool=run_tool,
            cancel_event=asyncio.Event(),
        ):
            out.append(json.loads(chunk[len("data: ") :]))
        return out

    return asyncio.run(_go()), calls


def _events(events, kind, node_id):
    return [e for e in events if e["event"] == kind and e.get("node_id") == node_id]










def test_text_output_reaches_next_agent_unquoted():
    """Une réponse texte passe telle quelle, pas en chaîne JSON échappée."""
    _, calls = _run(_chain(), {"a1": "Résumé : retard.\nPriorité : haute"})
    msg = calls[1][1]
    assert "Résumé : retard.\nPriorité : haute" in msg
    assert '"Résumé' not in msg


def _bound_run_agent(monkeypatch):
    from apowerb.core import workflow_runtime as rt

    sent = []

    async def fake_run_agent_message(folder, message, **kw):
        sent.append(message)
        return "ok"

    monkeypatch.setattr(rt, "access_token_factory", lambda owner: lambda: "t")
    monkeypatch.setattr(rt, "check_agent_owner", lambda agent_id, owner: "folder1")
    monkeypatch.setattr(rt, "run_agent_message", fake_run_agent_message)
    run_agent, *_ = rt.bindings_for("alice@acme.fr", None)
    return rt, run_agent, sent


def test_only_agent_nodes_get_the_unattended_note(monkeypatch):
    """Un classifieur ou une extraction a son propre format de réponse :
    la consigne ne doit pas l'inciter à rendre plus qu'un libellé ou un JSON."""
    rt, run_agent, sent = _bound_run_agent(monkeypatch)
    asyncio.run(run_agent("agent1", wg.AgentNodeMessage("Prévois")))
    asyncio.run(run_agent("agent1", "Classify the input below…"))
    assert sent[0].startswith(rt.WORKFLOW_AGENT_NOTE)
    assert sent[1] == "Classify the input below…"


def test_graph_marks_agent_node_messages_only():
    graph = _graph(
        [
            {"id": "trigger1", "type": "trigger", "config": {"kind": "manual"}},
            {
                "id": "c1",
                "type": "classifier",
                "config": {"agent_id": "cls", "routes": [{"route": "a"}, {"route": "b"}]},
            },
            {"id": "agent1", "type": "agent", "config": {"agent_id": "a1"}},
        ],
        [
            {"source": "trigger1", "target": "c1"},
            {"source": "c1", "target": "agent1", "route": "a"},
        ],
    )
    _, calls = _run(graph, {"cls": "a"})
    kinds = {agent_id: type(msg) for agent_id, msg in calls}
    assert kinds["cls"] is str
    assert kinds["a1"] is wg.AgentNodeMessage


def test_blank_input_field_means_upstream_output():
    """Un champ Entrée effacé (``""`` ou espaces) se comporte comme un champ vide."""
    for blank in ("", "   \n"):
        _, calls = _run(_chain({"input": blank}), {"a1": "série mensuelle"})
        assert calls[1][1] == "série mensuelle"


def test_agent_is_told_it_runs_unattended(monkeypatch):
    from apowerb.core import workflow_runtime as rt

    sent = []

    async def fake_run_agent_message(folder, message, **kw):
        sent.append((folder, message))
        return "ok"

    monkeypatch.setattr(rt, "access_token_factory", lambda owner: lambda: "t")
    monkeypatch.setattr(rt, "check_agent_owner", lambda agent_id, owner: "folder1")
    monkeypatch.setattr(rt, "run_agent_message", fake_run_agent_message)
    run_agent, *_ = rt.bindings_for("alice@acme.fr", None)
    asyncio.run(run_agent("agent1", wg.AgentNodeMessage("Prévois les ventes")))
    ((folder, message),) = sent
    assert folder == "folder1"
    assert message.startswith(rt.WORKFLOW_AGENT_NOTE)
    assert message.endswith("\n\nPrévois les ventes")




def test_agent_ending_with_a_question_is_flagged():
    events, _ = _run(
        _chain(),
        {"a1": "La table est introuvable.\nQue souhaitez-vous faire ?"},
    )
    (done1,) = _events(events, "node_complete", "agent1")
    (done2,) = _events(events, "node_complete", "agent2")
    assert done1["asks_question"] is True
    assert "asks_question" not in done2


def test_question_mark_inside_the_answer_is_not_a_question():
    events, _ = _run(_chain(), {"a1": "Pourquoi ? Saisonnalité.\nPrévision : 120"})
    (done1,) = _events(events, "node_complete", "agent1")
    assert "asks_question" not in done1


def test_json_output_is_never_flagged():
    events, _ = _run(_chain(), {"a1": {"question": "ok?"}})
    (done1,) = _events(events, "node_complete", "agent1")
    assert "asks_question" not in done1


def test_question_wrapped_in_quotes_or_brackets_is_flagged():
    for reply in (
        "Il me manque la table.\n« Laquelle dois-je utiliser ? »",
        "Il me manque la table.\n(Laquelle dois-je utiliser ?)",
        'Il me manque la table.\n"Which table should I use?"',
    ):
        events, _ = _run(_chain(), {"a1": reply})
        (done1,) = _events(events, "node_complete", "agent1")
        assert done1.get("asks_question") is True, reply
