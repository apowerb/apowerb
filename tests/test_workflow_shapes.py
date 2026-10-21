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
    names = [f"f_{chr(97 + i // 676)}{chr(97 + i // 26 % 26)}{chr(97 + i % 26)}" for i in range(500)]
    wide = {name: (i if i % 2 else str(i)) for i, name in enumerate(names)}
    shape = ws.capped_shape(wide)
    assert len(shape["fields"]) == ws.MAX_KEYS and shape["truncated"] is True
    huge = {f"key_{i:03d}_" + "x" * 150: {f"sub_{j}_" + "y" * 150: 1 for j in range(40)} for i in range(40)}
    assert len(json.dumps(ws.capped_shape(huge))) <= ws.MAX_SHAPE_BYTES


# --- seules des clés « de programmeur » deviennent des champs ---------------------

# Des clés qui sont des données : aucune ne doit atteindre la base ni la route.
DATA_KEYS = [
    "ada@acme.fr",
    "2026-10-01",
    "10423",
    "C-0042",
    "0f8fad5b-d9cb-469f-a165-70867728950e",
    "+33612345678",
    "client_CF101197",
    "Dupont Jean",
    "06 12 34 56 78",
    "06-12-34-56-78",
    "CLIENT101197",
    "ABCDE12345",
    "101197CF",
    "12345A",
    "CF-2026-0042",
    "C42",
    "user:42",
    "192.168.1.20",
    "1 oct. 2026",
    "FR76 3000 6000 0112 3456 7890 189",
]
FIELD_KEYS = ["total", "mois", "items", "item2", "q1_total", "createdAt", "order_amount"]


@pytest.mark.parametrize("key", DATA_KEYS)
def test_a_data_key_is_never_a_field_name(key):
    alone = ws.shape_of({key: {"n": 1}})
    assert alone == {"type": "map", "values": {"type": "object", "fields": {"n": N}}}
    mixed = ws.shape_of({"total": 3, key: {"n": 1}})
    assert mixed == {
        "type": "object",
        "fields": {"total": N},
        "others": {"type": "object", "fields": {"n": N}},
    }
    assert key not in json.dumps(alone) + json.dumps(mixed)


@pytest.mark.parametrize("key", FIELD_KEYS)
def test_a_programmer_identifier_stays_a_field(key):
    assert ws.shape_of({key: 1}) == {"type": "object", "fields": {key: N}}


def test_a_dict_keyed_by_data_only_is_a_map():
    value = {f"Produit {i}": {"qty": i, "sku": f"S{i}"} for i in range(30)}
    shape = ws.shape_of(value)
    assert shape == {"type": "map", "values": {"type": "object", "fields": {"qty": N, "sku": S}}}
    assert "Produit" not in json.dumps(shape)


def test_a_wide_row_of_named_columns_stays_an_object():
    row = {f"col_{chr(97 + i // 26)}{chr(97 + i % 26)}": "x" for i in range(25)}
    shape = ws.shape_of(row)
    assert shape["type"] == "object" and set(shape["fields"]) == set(row)


def test_a_template_reaches_the_values_of_a_map_or_of_the_other_keys():
    graph = _graph(
        [
            {
                "id": "t",
                "type": "trigger",
                "config": {"sample_payload": {"total": 1, "ada@acme.fr": {"n": 1}}},
            },
            {
                "id": "s",
                "type": "set",
                "config": {
                    "fields": [
                        {"key": "one", "value": "{{t.anything.n}}"},
                        {"key": "sum", "value": "{{t.total}}"},
                    ]
                },
            },
        ],
        [{"source": "t", "target": "s"}],
    )
    shapes = ws.node_shapes(graph)
    assert shapes["s"]["shape"] == {"type": "object", "fields": {"one": N, "sum": N}}


def test_a_tool_answer_leaves_no_data_key_in_base_or_route(client, store):
    answer = {"total": 2, **{k: {"orders": 3} for k in DATA_KEYS}}
    ws.record_tool_shape(ALICE, "crm.lookup", answer)
    with store.begin() as conn:
        rows = [dict(r._mapping) for r in conn.execute(wm.workflow_store.tool_shape_table.select())]
    body = client.post("/api/workflows/defs/node-shapes", json={"graph": SHAPE_GRAPH}).json()
    stored, served = rows[0]["shape"], json.dumps(body, ensure_ascii=False)
    for key in DATA_KEYS:
        assert key not in stored and key not in served
    assert body["shapes"]["tool1"]["shape"]["fields"] == {"total": N}



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
