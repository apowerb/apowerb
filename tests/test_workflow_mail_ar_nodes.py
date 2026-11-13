"""Nœuds ``document``, ``classifier`` moteur Jev et ``db_write`` (moteur seul).

Les services (OCR, Jev, écriture en base) sont simulés via ``NodeServices`` ;
leur branchement réel (contrôle du propriétaire, requête paramétrée) est
couvert par ``test_workflow_mail_ar_services.py``.
"""

import asyncio
import json

import pytest

from apowerb.core import workflow_graph as wg

T = {"id": "mail", "type": "trigger"}


class FakeServices:
    def __init__(self, docs=None, jev=None):
        self.docs = docs or {}
        self.jev = jev or {}
        self.writes = []
        self.jev_calls = []

    async def document(self, ref):
        if ref not in self.docs:
            raise wg.GraphError(f"pièce jointe introuvable : {ref}", code="attachment_not_found")
        return {"name": f"{ref}.pdf", "text": self.docs[ref], "page_count": 1}

    async def jev_classify(self, labels, text, instructions, min_confidence):
        self.jev_calls.append({"labels": labels, "text": text, "min": min_confidence})
        return self.jev[text]

    async def db_write(self, connection, table, row):
        self.writes.append((connection, table, row))
        return {"inserted": 1}

    def bundle(self):
        return wg.NodeServices(
            document=self.document, jev_classify=self.jev_classify, db_write=self.db_write
        )


def _graph(nodes, edges):
    return wg.WorkflowGraph.model_validate({"version": 1, "nodes": nodes, "edges": edges})


def _run(nodes, edges, payload, services):
    async def run_tool(tool, args):
        pytest.fail("pas d'outil")

    async def run_agent(agent_id, message):
        pytest.fail("pas d'agent")

    async def go():
        out = []
        async for chunk in wg.run_graph(
            _graph(nodes, edges),
            payload=payload,
            run_agent=run_agent,
            run_tool=run_tool,
            cancel_event=asyncio.Event(),
            services=services.bundle() if services else None,
        ):
            out.append(json.loads(chunk[len("data: ") :]))
        return out

    return asyncio.run(go())


def _complete(events, node_id):
    return next(e for e in events if e["event"] == "node_complete" and e["node_id"] == node_id)


def _error(events, node_id):
    return next(e for e in events if e["event"] == "node_error" and e["node_id"] == node_id)


MAIL = {
    "subject": "AR commande",
    "attachments": [
        {"name": "a.pdf", "ref": "att_a", "size": 1, "content_type": "application/pdf"},
        {"name": "b.pdf", "ref": "att_b", "size": 1, "content_type": "application/pdf"},
    ],
}
DOC = {"id": "doc", "type": "document", "config": {"files": "{{mail.attachments}}"}}
OUT = {"id": "out", "type": "output", "config": {"value": "{{doc.text}}"}}
DOC_EDGES = [{"source": "mail", "target": "doc"}, {"source": "doc", "target": "out"}]


# --- document -----------------------------------------------------------------


def test_document_reads_every_attachment_in_order():
    svc = FakeServices(docs={"att_a": "texte A", "att_b": "texte B"})
    events = _run([T, DOC, OUT], DOC_EDGES, MAIL, svc)
    out = _complete(events, "doc")["output"]
    assert out["count"] == 2
    assert [d["ref"] for d in out["documents"]] == ["att_a", "att_b"]
    assert out["text"] == "texte A\n\ntexte B"
    assert events[-1] == {"event": "done", "output": "texte A\n\ntexte B"}


@pytest.mark.parametrize(
    "files, payload",
    [
        ("{{mail.attachments.0.ref}}", MAIL),  # une seule ref
        ("{{mail.attachments.0}}", MAIL),  # un seul objet
    ],
)
def test_document_accepts_a_single_ref_or_object(files, payload):
    svc = FakeServices(docs={"att_a": "texte A"})
    doc = {**DOC, "config": {"files": files}}
    events = _run([T, doc, OUT], DOC_EDGES, payload, svc)
    assert _complete(events, "doc")["output"]["count"] == 1


def test_document_without_attachment_fails_loudly():
    events = _run([T, DOC, OUT], DOC_EDGES, {"attachments": []}, FakeServices())
    assert _error(events, "doc")["code"] == "document_no_file"
    assert events[-1]["event"] == "error"


def test_document_refuses_entries_without_ref():
    payload = {"attachments": [{"name": "a.pdf", "size": 1}]}
    events = _run([T, DOC, OUT], DOC_EDGES, payload, FakeServices())
    assert _error(events, "doc")["code"] == "document_bad_ref"


def test_document_unknown_ref_fails_the_run():
    payload = {"attachments": [{"name": "x.pdf", "ref": "att_x"}]}
    events = _run([T, DOC, OUT], DOC_EDGES, payload, FakeServices())
    assert _error(events, "doc")["code"] == "attachment_not_found"


def test_document_needs_the_service():
    events = _run([T, DOC, OUT], DOC_EDGES, MAIL, None)
    assert _error(events, "doc")["code"] == "service_unavailable"


def test_document_validation_requires_files():
    with pytest.raises(wg.GraphError, match="files"):
        wg.validate_graph(_graph([T, {"id": "doc", "type": "document", "config": {}}], [{"source": "mail", "target": "doc"}]))


def test_document_output_keys_are_known_to_templates():
    bad = {"id": "out", "type": "output", "config": {"value": "{{doc.nope}}"}}
    with pytest.raises(wg.GraphError):
        wg.validate_graph(_graph([T, DOC, bad], DOC_EDGES))


# --- classifier moteur Jev ----------------------------------------------------

ROUTES = [
    {"route": "ar", "description": "Accusé de réception de commande"},
    {"route": "autre", "description": "Tout autre document"},
]


def _clf(**extra):
    return {
        "id": "clf",
        "type": "classifier",
        "config": {"engine": "jev", "routes": ROUTES, "text": "{{doc.text}}", **extra},
    }


AR_OUT = {"id": "o_ar", "type": "output", "config": {"value": "AR"}}
OTHER_OUT = {"id": "o_autre", "type": "output", "config": {"value": "autre"}}
UNC_OUT = {"id": "o_unc", "type": "output", "config": {"value": "à revoir"}}
CLF_EDGES = [
    {"source": "mail", "target": "doc"},
    {"source": "doc", "target": "clf"},
    {"source": "clf", "target": "o_ar", "route": "ar"},
    {"source": "clf", "target": "o_autre", "route": "autre"},
]


def _jev(label, conf=0.95, uncertain=False):
    return {"label": None if uncertain else label, "confidence": conf, "uncertain": uncertain, "best_guess": label}


ONE = {"attachments": [{"name": "a.pdf", "ref": "att_a"}]}


@pytest.mark.parametrize("label, final", [("ar", "AR"), ("autre", "autre")])
def test_jev_classifier_routes_on_the_label(label, final):
    svc = FakeServices(docs={"att_a": "doc"}, jev={"doc": _jev(label)})
    events = _run([T, DOC, _clf(), AR_OUT, OTHER_OUT], CLF_EDGES, ONE, svc)
    assert {"event": "route", "node_id": "clf", "route": label} in events
    assert events[-1] == {"event": "done", "output": final}
    call = svc.jev_calls[0]
    assert call["labels"] == {r["route"]: r["description"] for r in ROUTES}
    assert call["min"] == 0.7


def test_jev_classifier_uncertain_goes_to_the_uncertain_route():
    svc = FakeServices(docs={"att_a": "doc"}, jev={"doc": _jev("ar", 0.5, uncertain=True)})
    edges = CLF_EDGES + [{"source": "clf", "target": "o_unc", "route": "uncertain"}]
    events = _run([T, DOC, _clf(), AR_OUT, OTHER_OUT, UNC_OUT], edges, ONE, svc)
    assert events[-1] == {"event": "done", "output": "à revoir"}


def test_jev_classifier_uncertain_without_route_fails_loudly():
    svc = FakeServices(docs={"att_a": "doc"}, jev={"doc": _jev("ar", 0.5, uncertain=True)})
    events = _run([T, DOC, _clf(), AR_OUT, OTHER_OUT], CLF_EDGES, ONE, svc)
    assert _error(events, "clf")["code"] == "classifier_uncertain"
    assert events[-1]["event"] == "error"


USAGE = {"prompt_tokens": 484, "completion_tokens": 32, "cost": 0.00002}
UNC_EDGES = CLF_EDGES + [{"source": "clf", "target": "o_unc", "route": "uncertain"}]


def _clf_run(answer):
    svc = FakeServices(docs={"att_a": "doc"}, jev={"doc": answer})
    return _run([T, DOC, _clf(), AR_OUT, OTHER_OUT, UNC_OUT], UNC_EDGES, ONE, svc)


def test_jev_trace_tells_a_decided_autre_from_an_uncertain_run():
    decided = _complete(_clf_run(_jev("autre", 0.91)), "clf")
    hesitant = _complete(_clf_run(_jev("autre", 0.62, uncertain=True)), "clf")
    assert decided["classification"] == {
        "route": "autre",
        "label": "autre",
        "confidence": 0.91,
        "uncertain": False,
    }
    assert hesitant["classification"] == {
        "route": "uncertain",
        "label": None,
        "confidence": 0.62,
        "uncertain": True,
        "best_guess": "autre",
    }
    # Les nœuds suivants reçoivent toujours l'entrée du classifieur, inchangée.
    assert decided["output"] == hesitant["output"]


def test_jev_trace_carries_the_token_cost():
    events = _clf_run({**_jev("ar"), "usage": USAGE})
    assert _complete(events, "clf")["classification"]["usage"] == USAGE


def test_jev_uncertain_failure_keeps_the_classification_in_the_trace():
    answer = {**_jev("ar", 0.5, uncertain=True), "usage": USAGE}
    svc = FakeServices(docs={"att_a": "doc"}, jev={"doc": answer})
    err = _error(_run([T, DOC, _clf(), AR_OUT, OTHER_OUT], CLF_EDGES, ONE, svc), "clf")
    assert err["code"] == "classifier_uncertain"
    assert err["classification"] == {
        "label": None,
        "confidence": 0.5,
        "uncertain": True,
        "best_guess": "ar",
        "usage": USAGE,
    }


def test_jev_classifier_empty_text_fails_before_calling_jev():
    svc = FakeServices(docs={"att_a": "   "})
    events = _run([T, DOC, _clf(), AR_OUT, OTHER_OUT], CLF_EDGES, ONE, svc)
    assert _error(events, "clf")["code"] == "classifier_empty_input"
    assert svc.jev_calls == []


def test_jev_classifier_needs_no_agent_but_descriptions():
    graph = _graph([T, DOC, _clf(min_confidence=0.8), AR_OUT, OTHER_OUT], CLF_EDGES)
    wg.validate_graph(graph)
    bad = _clf()
    bad["config"]["routes"] = [{"route": "ar"}, {"route": "autre"}]
    with pytest.raises(wg.GraphError, match="description"):
        wg.validate_graph(_graph([T, DOC, bad, AR_OUT, OTHER_OUT], CLF_EDGES))


@pytest.mark.parametrize("value", [0, 1.5, "0.7", True])
def test_jev_classifier_min_confidence_bounds(value):
    with pytest.raises(wg.GraphError, match="min_confidence"):
        wg.validate_graph(_graph([T, DOC, _clf(min_confidence=value), AR_OUT, OTHER_OUT], CLF_EDGES))


def test_unknown_engine_is_refused():
    with pytest.raises(wg.GraphError, match="engine"):
        wg.validate_graph(_graph([T, DOC, _clf(engine="gpt"), AR_OUT, OTHER_OUT], CLF_EDGES))


def test_uncertain_cannot_be_a_declared_route_name():
    bad = _clf()
    bad["config"]["routes"] = ROUTES + [{"route": "uncertain", "description": "x"}]
    with pytest.raises(wg.GraphError, match="uncertain"):
        wg.validate_graph(_graph([T, DOC, bad, AR_OUT, OTHER_OUT], CLF_EDGES))


# --- db_write -----------------------------------------------------------------


def _dbw(**cfg):
    base = {
        "connection": "tool_config12",
        "table": "documents_recus",
        "values": {"sujet": "{{mail.subject}}", "classe": "AR", "pages": "{{doc.count}}"},
    }
    return {"id": "save", "type": "db_write", "config": {**base, **cfg}}


DBW_EDGES = [{"source": "mail", "target": "doc"}, {"source": "doc", "target": "save"}]


def test_db_write_renders_values_and_calls_the_service():
    svc = FakeServices(docs={"att_a": "texte"})
    payload = {"subject": "x'); DROP TABLE t; --", "attachments": [{"name": "a.pdf", "ref": "att_a"}]}
    events = _run([T, DOC, _dbw()], DBW_EDGES, payload, svc)
    assert svc.writes == [
        ("tool_config12", "documents_recus", {"sujet": "x'); DROP TABLE t; --", "classe": "AR", "pages": 1})
    ]
    assert _complete(events, "save")["output"] == {"table": "documents_recus", "inserted": 1}


def test_db_write_serialises_objects_as_json():
    svc = FakeServices(docs={"att_a": "t"})
    node = _dbw(values={"meta": "{{doc.documents.0}}"})
    _run([T, DOC, node], DBW_EDGES, ONE, svc)
    row = svc.writes[0][2]
    assert json.loads(row["meta"])["ref"] == "att_a"


@pytest.mark.parametrize(
    "cfg, match",
    [
        ({"connection": "postgres://u:p@h/db"}, "connection"),
        ({"connection": ""}, "connection"),
        ({"table": "t; drop"}, "table"),
        ({"table": "a.b.c"}, "table"),
        ({"values": {}}, "values"),
        ({"values": {"bad col": "x"}}, "colonne"),
        ({"values": {f"c{i}": "x" for i in range(51)}}, "values"),
    ],
)
def test_db_write_validation(cfg, match):
    with pytest.raises(wg.GraphError, match=match):
        wg.validate_graph(_graph([T, DOC, _dbw(**cfg)], DBW_EDGES))


def test_db_write_accepts_schema_qualified_table():
    wg.validate_graph(_graph([T, DOC, _dbw(table="demo.documents_recus")], DBW_EDGES))
