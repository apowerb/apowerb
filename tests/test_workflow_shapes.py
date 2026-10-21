"""Forme des sorties connue sans exécuter tout le workflow (Studio proactif, étape 2).

Une forme décrit des clés et des types, jamais des valeurs : c'est ce que le
Studio propose pour composer l'entrée du nœud suivant.
"""

import json
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from apowerb.core import workflow_graph as wg
from apowerb.core import workflow_main as wm
from apowerb.core import workflow_shapes as ws

ALICE, BOB = "alice@acme.fr", "bob@other.fr"
S, N, B = {"type": "string"}, {"type": "number"}, {"type": "boolean"}


def _graph(nodes, edges=()):
    return wg.WorkflowGraph.model_validate(
        {"version": 1, "nodes": nodes, "edges": list(edges)}
    )


@pytest.fixture()
def store(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection, _record):  # pragma: no cover
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS public")

    monkeypatch.setattr(wm.workflow_store, "engine", engine)
    wm.workflow_store.metadata.create_all(engine)
    return engine


# --- shape_of : des types, jamais des valeurs ----------------------------------


def test_shape_of_keeps_keys_and_types_but_no_value():
    shape = ws.shape_of(
        {"name": "Ada", "age": 36, "vip": True, "tags": ["a"], "boss": None, "addr": {"city": "Metz"}}
    )
    assert shape == {
        "type": "object",
        "fields": {
            "name": S,
            "age": N,
            "vip": B,
            "tags": {"type": "array", "items": S},
            "boss": {"type": "null"},
            "addr": {"type": "object", "fields": {"city": S}},
        },
    }
    assert "Ada" not in json.dumps(shape) and "Metz" not in json.dumps(shape)


def test_shape_of_is_capped_in_depth_keys_and_bytes():
    deep = {"a": {"b": {"c": {"d": {"e": {"f": {"g": {"h": 1}}}}}}}}
    text = json.dumps(ws.capped_shape(deep))
    assert '"h"' not in text  # coupé avant la profondeur 8
    # Des clés fixes de types variés : un objet, pas une table indexée.
    wide = {f"field_{i}": (i if i % 2 else str(i)) for i in range(500)}
    shape = ws.capped_shape(wide)
    assert len(shape["fields"]) == ws.MAX_KEYS and shape["truncated"] is True
    huge = {f"key_{i:03d}_" + "x" * 150: {f"sub_{j}_" + "y" * 150: 1 for j in range(40)} for i in range(40)}
    assert len(json.dumps(ws.capped_shape(huge))) <= ws.MAX_SHAPE_BYTES


# --- tables indexées par des données : jamais les clés en base -------------------


@pytest.mark.parametrize(
    "keys",
    [
        ["ada@acme.fr", "bob@other.fr"],
        ["2026-10-01", "2026-10-02"],
        ["10423", "10424"],
        ["C-0042", "C-0043"],
        ["0f8fad5b-d9cb-469f-a165-70867728950e", "7c9e6679-7425-40de-944b-e07fc1f90ae7"],
    ],
)
def test_a_dict_keyed_by_data_is_a_map_without_its_keys(keys):
    value = {k: {"total": 12.5, "paid": True} for k in keys}
    shape = ws.shape_of(value)
    assert shape == {"type": "map", "values": {"type": "object", "fields": {"total": N, "paid": B}}}
    text = json.dumps(shape)
    assert not any(k in text for k in keys)


def test_one_data_key_among_fixed_ones_is_enough_to_hide_them():
    shape = ws.shape_of({"total": 3, "ada@acme.fr": 1})
    assert shape["type"] == "map" and "ada@acme.fr" not in json.dumps(shape)


def test_many_keys_with_values_of_the_same_shape_are_a_map():
    value = {f"Produit {i}": {"qty": i, "sku": f"S{i}"} for i in range(30)}
    shape = ws.shape_of(value)
    assert shape == {"type": "map", "values": {"type": "object", "fields": {"qty": N, "sku": S}}}
    assert "Produit" not in json.dumps(shape)


def test_an_object_with_fixed_keys_is_described_as_before():
    assert ws.shape_of({"total": 1200, "mois": "octobre"}) == {
        "type": "object",
        "fields": {"total": N, "mois": S},
    }


def test_a_template_into_a_map_reaches_its_values():
    graph = _graph(
        [
            {"id": "t", "type": "trigger", "config": {"sample_payload": {"ada@acme.fr": {"n": 1}}}},
            {"id": "s", "type": "set", "config": {"fields": [{"key": "one", "value": "{{t.anything.n}}"}]}},
        ],
        [{"source": "t", "target": "s"}],
    )
    shapes = ws.node_shapes(graph)
    assert shapes["s"]["shape"] == {"type": "object", "fields": {"one": N}}


def test_a_tool_answer_keyed_by_emails_leaves_no_address_in_base_or_route(client, store):
    answer = {"ada@acme.fr": {"orders": 3}, "bob@other.fr": {"orders": 1}}
    ws.record_tool_shape(ALICE, "crm.lookup", answer)
    with store.begin() as conn:
        rows = [dict(r._mapping) for r in conn.execute(wm.workflow_store.tool_shape_table.select())]
    assert len(rows) == 1 and "@" not in rows[0]["shape"]  # owner_id est un e-mail, lui
    body = client.post("/api/workflows/defs/node-shapes", json={"graph": SHAPE_GRAPH}).json()
    assert "@" not in json.dumps(body)
    assert body["shapes"]["tool1"]["shape"]["type"] == "map"


# --- purge des formes avec le workflow -------------------------------------------


def _tool_graph(*tools, loop_tool=None):
    nodes = [{"id": "t", "type": "trigger", "config": {"kind": "manual"}}]
    edges = []
    for i, ref in enumerate(tools):
        nodes.append({"id": f"tool{i}", "type": "tool", "config": {"tool": ref}})
        edges.append({"source": "t", "target": f"tool{i}"})
    if loop_tool:
        body = {
            "version": 1,
            "nodes": [
                {"id": "item", "type": "trigger", "config": {"kind": "manual"}},
                {"id": "inner", "type": "tool", "config": {"tool": loop_tool}},
            ],
            "edges": [{"source": "item", "target": "inner"}],
        }
        nodes.append({"id": "each", "type": "loop", "config": {"items": "{{t.rows}}", "body": body}})
        edges.append({"source": "t", "target": "each"})
    return {"version": 1, "nodes": nodes, "edges": edges}


def _learned(owner):
    return set(ws.read_tool_shapes(owner, ["crm.lookup", "erp.lines", "mail.read"]))


def test_deleting_a_workflow_forgets_the_shapes_no_other_workflow_uses(client):
    w1 = wm.create_workflow(owner_id=ALICE, name="A", graph=_tool_graph("crm.lookup", loop_tool="mail.read"))
    w2 = wm.create_workflow(owner_id=ALICE, name="B", graph=_tool_graph("crm.lookup", "erp.lines"))
    for ref in ("crm.lookup", "erp.lines", "mail.read"):
        ws.record_tool_shape(ALICE, ref, {"a": 1})
    ws.record_tool_shape(BOB, "erp.lines", {"a": 1})

    wm.delete_workflow(w2["workflow_id"], owner_id=ALICE)
    # erp.lines n'est plus utilisé par Alice ; crm.lookup l'est encore par A.
    assert _learned(ALICE) == {"crm.lookup", "mail.read"}
    assert _learned(BOB) == {"erp.lines"}  # rien chez les autres

    wm.delete_workflow(w1["workflow_id"], owner_id=ALICE)
    assert _learned(ALICE) == set()  # outil de boucle compris
    assert _learned(BOB) == {"erp.lines"}


# --- node_shapes : forme statique des nœuds --------------------------------------


def test_static_shapes_of_trigger_set_extract_http_merge_convert():
    graph = _graph(
        [
            {
                "id": "trigger1",
                "type": "trigger",
                "config": {"kind": "webhook", "sample_payload": {"email": "a@b.fr", "amount": 12}},
            },
            {
                "id": "s",
                "type": "set",
                "config": {
                    "fields": [
                        {"key": "who", "value": "{{trigger1.email}}"},
                        {"key": "greeting", "value": "Bonjour {{trigger1.email}}"},
                        {"key": "fixed", "value": 3},
                    ]
                },
            },
            {
                "id": "x",
                "type": "extract",
                "config": {
                    "agent_id": "ag",
                    "fields": [
                        {"name": "total", "type": "number"},
                        {"name": "lines", "type": "list"},
                    ],
                },
            },
            {"id": "h", "type": "http", "config": {"method": "GET", "url": "https://example.com"}},
            {"id": "m", "type": "merge"},
            {"id": "c", "type": "convert", "config": {"to": "number"}},
            {"id": "a", "type": "agent", "config": {"agent_id": "ag"}},
        ],
        [
            {"source": "trigger1", "target": "s"},
            {"source": "trigger1", "target": "x"},
            {"source": "trigger1", "target": "h"},
            {"source": "s", "target": "m"},
            {"source": "h", "target": "m"},
            {"source": "m", "target": "c"},
            {"source": "c", "target": "a"},
        ],
    )
    shapes = ws.node_shapes(graph)

    assert shapes["trigger1"] == {
        "source": "static",
        "shape": {"type": "object", "fields": {"email": S, "amount": N}},
    }
    assert shapes["s"]["shape"] == {
        "type": "object",
        "fields": {"who": S, "greeting": S, "fixed": N},
    }
    assert shapes["x"]["shape"] == {
        "type": "object",
        "fields": {"total": N, "lines": {"type": "array"}},
    }
    http = {"type": "object", "fields": {"status": N, "headers": {"type": "object"}, "body": {"type": "any"}}}
    assert shapes["h"]["shape"] == http
    assert shapes["m"]["shape"] == {
        "type": "object",
        "fields": {"s": shapes["s"]["shape"], "h": http},
    }
    assert shapes["c"]["shape"] == N
    assert "a" not in shapes  # un agent sans output_type : forme inconnue
    assert "a@b.fr" not in json.dumps(shapes)


def test_a_router_passes_its_input_shape_through():
    graph = _graph(
        [
            {"id": "t", "type": "trigger", "config": {"sample_payload": {"kind": "vip"}}},
            {
                "id": "r",
                "type": "router",
                "config": {"rules": [{"route": "vip", "field": "{{t.kind}}", "value": "vip"}], "default_route": "o"},
            },
        ],
        [{"source": "t", "target": "r"}],
    )
    assert ws.node_shapes(graph)["r"]["shape"] == {"type": "object", "fields": {"kind": S}}


def test_a_text_agent_is_a_string_and_a_tool_uses_its_learned_shape():
    graph = _graph(
        [
            {"id": "t", "type": "trigger"},
            {"id": "a", "type": "agent", "config": {"agent_id": "ag", "output_type": "text"}},
            {"id": "tool1", "type": "tool", "config": {"tool": "crm.lookup"}},
            {"id": "tool2", "type": "tool", "config": {"tool": "never.ran"}},
        ],
        [{"source": "t", "target": "a"}, {"source": "a", "target": "tool1"}, {"source": "a", "target": "tool2"}],
    )
    learned = {"crm.lookup": {"type": "object", "fields": {"id": S}}}
    shapes = ws.node_shapes(graph, tool_shapes=learned)

    assert shapes["a"] == {"source": "static", "shape": S}
    assert shapes["tool1"] == {"source": "learned", "shape": learned["crm.lookup"]}
    assert "tool2" not in shapes
    assert "t" not in shapes  # pas de sample_payload : rien à proposer


# --- formes apprises des outils ---------------------------------------------------


def test_a_tool_shape_is_learned_per_user_without_any_value(store):
    ws.record_tool_shape(ALICE, "crm.lookup", {"customer": "SECRET-ACME", "score": 0.9})

    assert ws.read_tool_shapes(ALICE, ["crm.lookup"]) == {
        "crm.lookup": {"type": "object", "fields": {"customer": S, "score": N}}
    }
    assert ws.read_tool_shapes(BOB, ["crm.lookup"]) == {}
    with store.begin() as conn:
        rows = [dict(r._mapping) for r in conn.execute(wm.workflow_store.tool_shape_table.select())]
    assert len(rows) == 1 and "SECRET-ACME" not in json.dumps(rows)


def test_a_new_run_replaces_the_learned_shape(store):
    ws.record_tool_shape(ALICE, "crm.lookup", {"a": 1})
    ws.record_tool_shape(ALICE, "crm.lookup", {"b": "x"})
    assert ws.read_tool_shapes(ALICE, ["crm.lookup"])["crm.lookup"]["fields"] == {"b": S}


def test_a_tool_error_is_never_learned_as_its_shape(store):
    ws.record_tool_shape(ALICE, "crm.lookup", {"a": 1})
    ws.record_tool_shape(ALICE, "crm.lookup", {"status": "error", "error_message": "boom"})
    assert ws.read_tool_shapes(ALICE, ["crm.lookup"])["crm.lookup"]["fields"] == {"a": N}


def test_recording_a_shape_never_breaks_the_run(monkeypatch):
    broken = MagicMock()
    broken.begin.side_effect = RuntimeError("db down")
    monkeypatch.setattr(wm.workflow_store, "engine", broken)
    ws.record_tool_shape(ALICE, "crm.lookup", {"a": 1})  # ne lève pas


async def test_learning_wrapper_records_each_tool_once_per_run(store):
    calls = []

    async def run_tool(ref, args):
        calls.append(ref)
        return {"n": len(calls)}

    wrapped = ws.learning_run_tool(ALICE, run_tool)
    assert await wrapped("t.a", {}) == {"n": 1}
    await wrapped("t.a", {})
    assert calls == ["t.a", "t.a"]
    assert ws.read_tool_shapes(ALICE, ["t.a"]) == {"t.a": {"type": "object", "fields": {"n": N}}}


# --- route ----------------------------------------------------------------------------


def _user(email):
    u = MagicMock()
    u.email, u.user_id, u.role = email, 1, "USER"
    return u


@pytest.fixture()
def client(store, monkeypatch):
    from apowerb.auth.dependencies import get_current_user
    from apowerb.core import workflow_triggers as wt
    from apowerb.routers import workflow_defs

    monkeypatch.setattr(wt.workflow_trigger_store, "engine", store)
    wt.workflow_trigger_store.metadata.create_all(store)
    who = {"email": ALICE}
    app = FastAPI()
    app.include_router(workflow_defs.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: _user(who["email"])
    c = TestClient(app)
    c.who = who
    return c


SHAPE_GRAPH = {
    "version": 1,
    "nodes": [
        {"id": "t", "type": "trigger", "config": {"sample_payload": {"id": "B7"}}},
        {"id": "tool1", "type": "tool", "config": {"tool": "crm.lookup"}},
    ],
    "edges": [{"source": "t", "target": "tool1"}],
}


def test_node_shapes_route_reads_the_draft_and_only_the_callers_tools(client):
    ws.record_tool_shape(BOB, "crm.lookup", {"bob_only": 1})
    r = client.post("/api/workflows/defs/node-shapes", json={"graph": SHAPE_GRAPH})
    assert r.status_code == 200
    assert r.json() == {
        "shapes": {"t": {"source": "static", "shape": {"type": "object", "fields": {"id": S}}}}
    }
    ws.record_tool_shape(ALICE, "crm.lookup", {"row": "x"})
    shapes = client.post("/api/workflows/defs/node-shapes", json={"graph": SHAPE_GRAPH}).json()["shapes"]
    assert shapes["tool1"] == {"source": "learned", "shape": {"type": "object", "fields": {"row": S}}}


def test_node_shapes_route_refuses_a_malformed_graph(client):
    r = client.post("/api/workflows/defs/node-shapes", json={"graph": {"nodes": [{"id": "1bad"}]}})
    assert r.status_code == 422
