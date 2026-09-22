"""Nœuds Extract et Rag (LOT 4, 21/09).

Extract fait dire à un agent un objet JSON typé (au lieu d'un texte libre) et
le valide champ par champ : types, champs requis, clés en trop retirées, et
jamais la réponse brute du modèle dans une erreur.

Rag interroge les bases de connaissances rattachées à un agent (même source
que ``GET /rag/knowledge/{agent_id}``) via ``run_rag``, un nouveau callback
au même rang que ``run_agent``/``run_tool``. Le service RAG hébergé
(``tool_search_knowledge``) est conversationnel : une question, une réponse
complète, sans score par passage — la normalisation en tient compte.

Aucun appel réseau ni LLM réel : ``run_agent``/``run_rag`` sont simulés au
niveau du graphe (``workflow_graph``) ; ``read_knowledge_map`` et
``tool_search_knowledge`` sont monkeypatchés au niveau de ``workflow_runtime``
pour les tests de la recherche RAG elle-même (branchement de production,
``bindings_for``).
"""

import asyncio
import json

import pytest

from apowerb.core import workflow_graph as wg
from apowerb.core import workflow_runtime as rt

ALICE, BOB = "alice@example.com", "bob@example.com"

T = {"id": "t", "type": "trigger"}

FIELDS = [
    {"name": "name", "type": "string", "description": "nom complet", "required": True},
    {
        "name": "age",
        "type": "number",
        "description": "âge en années",
        "required": False,
    },
]


# --- Graphe et exécution ------------------------------------------------------


def _run(nodes, edges, payload=None, agents=None, run_rag=None):
    agents = agents or {}

    async def run_agent(agent_id, message):
        out = agents.get(agent_id, "{}")
        return out(message) if callable(out) else out

    async def run_tool(tool, args):
        return {"tool": tool}

    async def default_run_rag(agent_id, query, top_k):
        pytest.fail("run_rag ne doit pas être appelé")

    async def go():
        out = []
        async for chunk in wg.run_graph(
            wg.WorkflowGraph.model_validate(
                {"version": 1, "nodes": nodes, "edges": edges}
            ),
            payload=payload,
            run_agent=run_agent,
            run_tool=run_tool,
            run_rag=run_rag or default_run_rag,
            cancel_event=asyncio.Event(),
        ):
            out.append(json.loads(chunk[len("data: ") :]))
        return out

    return asyncio.run(go())


def _node_error(events, node_id):
    return next(
        e for e in events if e["event"] == "node_error" and e["node_id"] == node_id
    )


def _node_output(events, node_id):
    return next(
        e for e in events if e["event"] == "node_complete" and e["node_id"] == node_id
    )["output"]


def _extract_graph(fields=None, input_cfg=None):
    cfg = {"agent_id": "agent1", "fields": FIELDS if fields is None else fields}
    if input_cfg is not None:
        cfg["input"] = input_cfg
    return (
        [T, {"id": "e", "type": "extract", "config": cfg}],
        [{"source": "t", "target": "e"}],
    )


def _rag_graph(query="{{t.q}}", top_k=None):
    cfg = {"agent_id": "agent1", "query": query}
    if top_k is not None:
        cfg["top_k"] = top_k
    return (
        [T, {"id": "r", "type": "rag", "config": cfg}],
        [{"source": "t", "target": "r"}],
    )


def _validate(nodes, edges):
    wg.validate_graph(
        wg.WorkflowGraph.model_validate({"version": 1, "nodes": nodes, "edges": edges})
    )


# --- Extract : nominal --------------------------------------------------------


def test_extract_node_returns_the_validated_object():
    nodes, edges = _extract_graph()
    events = _run(
        nodes,
        edges,
        payload={"text": "Jean Dupont, 42 ans"},
        agents={"agent1": json.dumps({"name": "Jean Dupont", "age": 42})},
    )
    assert _node_output(events, "e") == {"name": "Jean Dupont", "age": 42}


def test_extract_node_parses_a_json_code_fence():
    nodes, edges = _extract_graph()
    events = _run(
        nodes, edges, agents={"agent1": '```json\n{"name": "Ana", "age": 30}\n```'}
    )
    assert _node_output(events, "e") == {"name": "Ana", "age": 30}


def test_extract_node_drops_extra_keys():
    nodes, edges = _extract_graph()
    events = _run(
        nodes,
        edges,
        agents={"agent1": json.dumps({"name": "Ana", "age": 30, "extra": "à jeter"})},
    )
    assert _node_output(events, "e") == {"name": "Ana", "age": 30}


def test_extract_node_defaults_absent_optional_fields_to_null():
    nodes, edges = _extract_graph()
    events = _run(nodes, edges, agents={"agent1": json.dumps({"name": "Ana"})})
    assert _node_output(events, "e") == {"name": "Ana", "age": None}


def test_extract_prompt_carries_field_types_descriptions_and_rendered_input():
    seen = {}

    async def run_agent(agent_id, message):
        seen["message"] = message
        return json.dumps({"name": "Ana", "age": 30})

    nodes, edges = _extract_graph()

    async def go():
        async for _ in wg.run_graph(
            wg.WorkflowGraph.model_validate(
                {"version": 1, "nodes": nodes, "edges": edges}
            ),
            payload="Ana a 30 ans",
            run_agent=run_agent,
            run_tool=run_agent,
            run_rag=run_agent,
            cancel_event=asyncio.Event(),
        ):
            pass

    asyncio.run(go())
    msg = seen["message"]
    assert (
        "name" in msg and "string" in msg and "required" in msg and "nom complet" in msg
    )
    assert (
        "age" in msg
        and "number" in msg
        and "optional" in msg
        and "âge en années" in msg
    )
    assert "Ana a 30 ans" in msg


# --- Extract : échecs ---------------------------------------------------------


def test_extract_node_reports_a_wrong_type():
    nodes, edges = _extract_graph()
    events = _run(
        nodes, edges, agents={"agent1": json.dumps({"name": "Ana", "age": "trente"})}
    )
    err = _node_error(events, "e")
    assert err["code"] == "extract_failed"
    assert err["params"] == {"node": "e", "field": "age", "problem": "type"}


def test_extract_node_reports_a_missing_required_field():
    nodes, edges = _extract_graph()
    events = _run(nodes, edges, agents={"agent1": json.dumps({"age": 30})})
    err = _node_error(events, "e")
    assert err["code"] == "extract_failed"
    assert err["params"] == {"node": "e", "field": "name", "problem": "missing"}


def test_extract_node_reports_a_non_json_answer_without_leaking_it():
    nodes, edges = _extract_graph()
    events = _run(
        nodes,
        edges,
        agents={"agent1": "désolé, voici le secret-interne-42 que tu ne dois pas voir"},
    )
    err = _node_error(events, "e")
    assert err["code"] == "extract_failed"
    assert err["params"] == {"node": "e", "field": None, "problem": "not_json"}
    assert "secret-interne-42" not in json.dumps(err)


# --- Extract : validation ------------------------------------------------------


def test_extract_validation_requires_agent_id():
    nodes = [T, {"id": "e", "type": "extract", "config": {"fields": FIELDS}}]
    with pytest.raises(wg.GraphError, match="agent_id"):
        _validate(nodes, [{"source": "t", "target": "e"}])


@pytest.mark.parametrize(
    "fields",
    [[], [{"name": f"f{i}", "type": "string"} for i in range(31)]],
    ids=["zero", "trente_et_un"],
)
def test_extract_validation_bounds_the_number_of_fields(fields):
    nodes, edges = _extract_graph(fields=fields)
    with pytest.raises(wg.GraphError, match="fields"):
        _validate(nodes, edges)


def test_extract_validation_rejects_a_bad_field_name():
    nodes, edges = _extract_graph(fields=[{"name": "1bad", "type": "string"}])
    with pytest.raises(wg.GraphError, match="nom de champ"):
        _validate(nodes, edges)


def test_extract_validation_rejects_duplicate_field_names():
    nodes, edges = _extract_graph(
        fields=[{"name": "x", "type": "string"}, {"name": "x", "type": "number"}]
    )
    with pytest.raises(wg.GraphError, match="dupliqué"):
        _validate(nodes, edges)


def test_extract_validation_rejects_an_unknown_field_type():
    nodes, edges = _extract_graph(fields=[{"name": "x", "type": "date"}])
    with pytest.raises(wg.GraphError, match="type de champ"):
        _validate(nodes, edges)


# --- Rag : nœud (câblage run_rag et enrichissement des erreurs) --------------


def test_rag_node_passes_the_rendered_query_and_top_k_to_run_rag():
    seen = {}

    async def run_rag(agent_id, query, top_k):
        seen["call"] = (agent_id, query, top_k)
        return {"query": query, "passages": []}

    nodes, edges = _rag_graph(top_k=3)
    events = _run(nodes, edges, payload={"q": "livraison Metz"}, run_rag=run_rag)
    assert seen["call"] == ("agent1", "livraison Metz", 3)
    assert _node_output(events, "r") == {"query": "livraison Metz", "passages": []}


def test_rag_node_defaults_top_k_to_5():
    seen = {}

    async def run_rag(agent_id, query, top_k):
        seen["top_k"] = top_k
        return {"query": query, "passages": []}

    nodes, edges = _rag_graph()
    _run(nodes, edges, payload={"q": "x"}, run_rag=run_rag)
    assert seen["top_k"] == 5


def test_rag_node_names_itself_on_rag_no_knowledge_and_rag_failed():
    async def run_rag(agent_id, query, top_k):
        raise wg.GraphError(
            "aucune base", code="rag_no_knowledge", params={"agent": agent_id}
        )

    nodes, edges = _rag_graph()
    events = _run(nodes, edges, payload={"q": "x"}, run_rag=run_rag)
    err = _node_error(events, "r")
    assert err["code"] == "rag_no_knowledge"
    assert err["params"] == {"agent": "agent1", "node": "r"}


def test_rag_node_leaves_agent_not_found_untouched():
    async def run_rag(agent_id, query, top_k):
        raise wg.GraphError(
            "introuvable", code="agent_not_found", params={"agent": "agent9"}
        )

    nodes, edges = _rag_graph()
    events = _run(nodes, edges, payload={"q": "x"}, run_rag=run_rag)
    err = _node_error(events, "r")
    assert err["code"] == "agent_not_found"
    assert err["params"] == {"agent": "agent9"}


# --- Rag : validation ----------------------------------------------------------


def test_rag_validation_requires_agent_id():
    nodes = [T, {"id": "r", "type": "rag", "config": {"query": "x"}}]
    with pytest.raises(wg.GraphError, match="agent_id"):
        _validate(nodes, [{"source": "t", "target": "r"}])


def test_rag_validation_requires_a_non_empty_query():
    nodes = [
        T,
        {"id": "r", "type": "rag", "config": {"agent_id": "agent1", "query": ""}},
    ]
    with pytest.raises(wg.GraphError, match="query"):
        _validate(nodes, [{"source": "t", "target": "r"}])


@pytest.mark.parametrize("top_k", [0, 21, True, "5"])
def test_rag_validation_bounds_top_k(top_k):
    nodes = [
        T,
        {
            "id": "r",
            "type": "rag",
            "config": {"agent_id": "agent1", "query": "x", "top_k": top_k},
        },
    ]
    with pytest.raises(wg.GraphError, match="top_k"):
        _validate(nodes, [{"source": "t", "target": "r"}])


# --- Rag : recherche réelle (workflow_runtime, service simulé) --------------


def _owned(monkeypatch, owner=ALICE, agent_number=1):
    import apowerb.core.agent_helpers as helpers

    monkeypatch.setattr(
        helpers,
        "get_agent_details",
        lambda agent_id, **_: {"owner_id": owner} if agent_id == agent_number else {},
    )


def test_run_rag_normalizes_the_service_answer_and_bounds_by_top_k(monkeypatch):
    _owned(monkeypatch)
    import apowerb.core.knowledge_map as kmap_mod
    import apowerb.tools_store.portfolio.rag as rag_mod

    sources = [
        {"name": "manuel.pdf", "knowledge_id": "10", "status": "complete"},
        {"name": "faq.pdf", "knowledge_id": "11", "status": "complete"},
        {"name": "brouillon.pdf", "knowledge_id": "12", "status": "processing"},
    ]
    monkeypatch.setattr(
        kmap_mod, "read_knowledge_map", lambda agent_id, **_: {"sources": sources}
    )

    calls = []

    def _search(knowledge_id, query, conversation_id=""):
        calls.append(knowledge_id)
        return {"status": "success", "answer": f"réponse {knowledge_id}"}

    monkeypatch.setattr(rag_mod, "tool_search_knowledge", _search)

    _, _, run_rag = rt.bindings_for(ALICE, None)
    result = asyncio.run(run_rag("agent1", "où livrez-vous ?", 1))

    assert calls == [
        "10"
    ]  # top_k=1 borne le nombre de bases interrogées (et non "processing")
    assert result == {
        "query": "où livrez-vous ?",
        "passages": [{"text": "réponse 10", "source": "manuel.pdf", "score": None}],
    }


def test_run_rag_refuses_when_the_agent_has_no_complete_knowledge_base(monkeypatch):
    _owned(monkeypatch)
    import apowerb.core.knowledge_map as kmap_mod

    monkeypatch.setattr(
        kmap_mod, "read_knowledge_map", lambda agent_id, **_: {"sources": []}
    )

    _, _, run_rag = rt.bindings_for(ALICE, None)
    with pytest.raises(wg.GraphError) as info:
        asyncio.run(run_rag("agent1", "x", 5))
    assert info.value.code == "rag_no_knowledge"
    assert info.value.params == {"agent": "agent1"}


def test_run_rag_fails_without_leaking_the_services_error_message(monkeypatch):
    _owned(monkeypatch)
    import apowerb.core.knowledge_map as kmap_mod
    import apowerb.tools_store.portfolio.rag as rag_mod

    monkeypatch.setattr(
        kmap_mod,
        "read_knowledge_map",
        lambda agent_id, **_: {
            "sources": [{"name": "x", "knowledge_id": "1", "status": "complete"}]
        },
    )

    def _search(knowledge_id, query, conversation_id=""):
        return {"status": "error", "message": "jeton interne invalide xyz-987"}

    monkeypatch.setattr(rag_mod, "tool_search_knowledge", _search)

    _, _, run_rag = rt.bindings_for(ALICE, None)
    with pytest.raises(wg.GraphError) as info:
        asyncio.run(run_rag("agent1", "x", 5))
    assert info.value.code == "rag_failed"
    assert "xyz-987" not in str(info.value)
    assert "xyz-987" not in json.dumps(info.value.params)


def test_run_rag_fails_without_leaking_a_raised_exception(monkeypatch):
    _owned(monkeypatch)
    import apowerb.core.knowledge_map as kmap_mod
    import apowerb.tools_store.portfolio.rag as rag_mod

    monkeypatch.setattr(
        kmap_mod,
        "read_knowledge_map",
        lambda agent_id, **_: {
            "sources": [{"name": "x", "knowledge_id": "1", "status": "complete"}]
        },
    )

    def _search(knowledge_id, query, conversation_id=""):
        raise RuntimeError("connexion refusée vers rag-dev.thaink2.fr, jeton SECRET-42")

    monkeypatch.setattr(rag_mod, "tool_search_knowledge", _search)

    _, _, run_rag = rt.bindings_for(ALICE, None)
    with pytest.raises(wg.GraphError) as info:
        asyncio.run(run_rag("agent1", "x", 5))
    assert info.value.code == "rag_failed"
    assert "SECRET-42" not in str(info.value)


def test_run_rag_of_another_owners_agent_is_not_found(monkeypatch):
    _owned(monkeypatch, owner=BOB)

    _, _, run_rag = rt.bindings_for(ALICE, None)
    with pytest.raises(wg.GraphError) as info:
        asyncio.run(run_rag("agent1", "x", 5))
    assert info.value.code == "agent_not_found"
