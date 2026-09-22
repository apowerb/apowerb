"""Workflows persistés : CRUD, historique restaurable, isolation par propriétaire.

Banc SQLite réel (même montage que ``test_agent_runs``) : ce qui est vérifié,
c'est ce qui est réellement écrit en base, pas des appels à un faux.
"""

import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from apowerb.core import workflow_main as wm
from apowerb.core import workflow_triggers as wt

ALICE, BOB = "alice@acme.fr", "bob@other.fr"

GRAPH = {
    "version": 1,
    "nodes": [
        {"id": "start", "type": "trigger"},
        {"id": "a", "type": "agent", "config": {"agent_id": "agent1"}},
    ],
    "edges": [{"source": "start", "target": "a"}],
}
# Forme canonique stockée : les champs par défaut (config vide) sont explicités.
CANON = {
    "version": 1,
    "nodes": [
        {"id": "start", "type": "trigger", "config": {}},
        {"id": "a", "type": "agent", "config": {"agent_id": "agent1"}},
    ],
    "edges": [{"source": "start", "target": "a"}],
}


def _sqlite_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection, _record):  # pragma: no cover
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS public")

    return engine


@pytest.fixture(autouse=True)
def store(monkeypatch):
    engine = _sqlite_engine()
    monkeypatch.setattr(wm.workflow_store, "engine", engine)
    wm.workflow_store.metadata.create_all(engine)
    # workflow_main synchronise le trigger (workflow_triggers) à chaque
    # écriture réussie (création, édition, publication...) : même engine
    # partagé, sinon la synchronisation tenterait de joindre le Postgres
    # réel des réglages par défaut.
    monkeypatch.setattr(wt.workflow_trigger_store, "engine", engine)
    wt.workflow_trigger_store.metadata.create_all(engine)
    return wm.workflow_store


def _create(owner=ALICE, **kw):
    return wm.create_workflow(
        owner_id=owner, name=kw.pop("name", "Tri"), graph=kw.pop("graph", GRAPH), **kw
    )


def test_create_then_get_round_trips_the_graph():
    wf = _create(description="d")
    got = wm.get_workflow(wf["workflow_id"], owner_id=ALICE)
    assert got["graph"] == CANON and got["version"] == 1 and got["status"] == "draft"
    assert got["organization_id"] == "acme.fr"


def test_other_owner_cannot_read_update_or_delete():
    wf = _create()
    wid = wf["workflow_id"]
    assert wm.get_workflow(wid, owner_id=BOB) is None
    with pytest.raises(wm.WorkflowNotFound):
        wm.update_workflow(wid, owner_id=BOB, expected_version=1, name="x")
    with pytest.raises(wm.WorkflowNotFound):
        wm.delete_workflow(wid, owner_id=BOB)
    assert [w["workflow_id"] for w in wm.list_workflows(owner_id=BOB)] == []
    assert wm.get_workflow(wid, owner_id=ALICE)["name"] == "Tri"


def test_update_bumps_version_and_archives_the_previous_state():
    wid = _create()["workflow_id"]
    g2 = json.loads(json.dumps(CANON))
    g2["nodes"][1]["config"]["agent_id"] = "agent2"
    wm.update_workflow(wid, owner_id=ALICE, expected_version=1, graph=g2, name="Tri v2")
    got = wm.get_workflow(wid, owner_id=ALICE)
    assert (got["version"], got["name"], got["graph"]) == (2, "Tri v2", g2)
    revs = wm.list_revisions(wid, owner_id=ALICE)
    assert [(r["version"], r["name"]) for r in revs] == [(1, "Tri")]
    assert "graph" not in revs[0]


def test_stale_version_is_a_conflict_not_a_silent_overwrite():
    wid = _create()["workflow_id"]
    wm.update_workflow(wid, owner_id=ALICE, expected_version=1, name="onglet A")
    with pytest.raises(wm.VersionConflict) as err:
        wm.update_workflow(wid, owner_id=ALICE, expected_version=1, name="onglet B")
    assert err.value.current_version == 2
    assert wm.get_workflow(wid, owner_id=ALICE)["name"] == "onglet A"


def test_restore_revision_archives_current_then_restores():
    wid = _create()["workflow_id"]
    wm.update_workflow(
        wid,
        owner_id=ALICE,
        expected_version=1,
        name="B",
        graph={"version": 1, "nodes": [], "edges": []},
    )
    rev = wm.list_revisions(wid, owner_id=ALICE)[0]
    wm.restore_revision(wid, rev["revision_id"], owner_id=ALICE)
    got = wm.get_workflow(wid, owner_id=ALICE)
    assert (got["name"], got["graph"], got["version"]) == ("Tri", CANON, 3)
    assert [r["name"] for r in wm.list_revisions(wid, owner_id=ALICE)] == ["B", "Tri"]


def test_invalid_structure_is_refused_but_semantic_errors_are_saved_as_draft():
    with pytest.raises(wm.InvalidWorkflow):
        _create(
            graph={
                "version": 1,
                "nodes": [{"id": "1bad", "type": "agent"}],
                "edges": [],
            }
        )
    wf = _create(
        graph={"version": 1, "nodes": [{"id": "a", "type": "agent"}], "edges": []}
    )
    report = wm.check_workflow(
        wm.get_workflow(wf["workflow_id"], owner_id=ALICE)["graph"]
    )
    assert report["valid"] is False and "agent_id" in report["errors"][0]


def test_publish_requires_a_valid_graph():
    wf = _create(
        graph={"version": 1, "nodes": [{"id": "a", "type": "agent"}], "edges": []}
    )
    with pytest.raises(wm.InvalidWorkflow):
        wm.update_workflow(
            wf["workflow_id"], owner_id=ALICE, expected_version=1, status="published"
        )
    ok = _create()
    wm.update_workflow(
        ok["workflow_id"], owner_id=ALICE, expected_version=1, status="published"
    )
    assert wm.get_workflow(ok["workflow_id"], owner_id=ALICE)["status"] == "published"


def test_duplicate_copies_graph_under_a_new_id():
    wid = _create()["workflow_id"]
    copy = wm.duplicate_workflow(wid, owner_id=ALICE)
    assert (
        copy["workflow_id"] != wid
        and copy["name"] == "Tri (copie)"
        and copy["version"] == 1
    )
    assert wm.get_workflow(copy["workflow_id"], owner_id=ALICE)["graph"] == CANON


def test_delete_removes_workflow_and_its_revisions():
    wid = _create()["workflow_id"]
    wm.update_workflow(wid, owner_id=ALICE, expected_version=1, name="B")
    wm.delete_workflow(wid, owner_id=ALICE)
    assert wm.get_workflow(wid, owner_id=ALICE) is None
    with wm.workflow_store.engine.begin() as conn:
        left = conn.execute(wm.workflow_store.revision_table.select()).fetchall()
    assert left == []


def test_list_is_newest_first_and_without_graph():
    a = _create(name="A")["workflow_id"]
    b = _create(name="B")["workflow_id"]
    wm.update_workflow(a, owner_id=ALICE, expected_version=1, name="A2")
    rows = wm.list_workflows(owner_id=ALICE)
    assert [r["workflow_id"] for r in rows] == [a, b]
    assert all("graph" not in r and "node_count" in r for r in rows)


def test_concurrent_writer_between_read_and_update_is_a_conflict(monkeypatch):
    # Revue n°2 : le contrôle de version se faisait sur une lecture ; l'UPDATE
    # ne filtrait pas la version. Un écrivain concurrent qui passe entre les
    # deux était écrasé en silence.
    wid = _create()["workflow_id"]
    original = wm._fetch
    raced = []

    def _fetch_then_race(conn, workflow_id, owner_id):
        row = original(conn, workflow_id, owner_id)
        if not raced:
            raced.append(True)
            t = wm.workflow_store.workflow_table
            conn.execute(
                t.update()
                .where(t.c.workflow_id == workflow_id)
                .values(name="autre onglet", version=2)
            )
        return row

    monkeypatch.setattr(wm, "_fetch", _fetch_then_race)
    with pytest.raises(wm.VersionConflict) as err:
        wm.update_workflow(wid, owner_id=ALICE, expected_version=1, name="moi")
    assert err.value.current_version == 2
    monkeypatch.setattr(wm, "_fetch", original)
    # Notre écriture n a pas eu lieu. (L écrivain simulé partage notre
    # transaction, annulée avec le conflit ; un vrai onglet aurait la sienne.)
    assert wm.get_workflow(wid, owner_id=ALICE)["name"] != "moi"


def test_each_revision_says_which_change_replaced_it():
    """The history labels come from the server: an edit, a publication, a
    return to draft, a restore -- never a vague "update" the interface cannot
    name."""
    wid = _create()["workflow_id"]
    wm.update_workflow(wid, owner_id=ALICE, expected_version=1, name="edited")
    wm.update_workflow(wid, owner_id=ALICE, expected_version=2, status="published")
    wm.update_workflow(wid, owner_id=ALICE, expected_version=3, status="draft")
    first = wm.list_revisions(wid, owner_id=ALICE)[-1]
    wm.restore_revision(wid, first["revision_id"], owner_id=ALICE)
    reasons = [r["reason"] for r in wm.list_revisions(wid, owner_id=ALICE)]
    assert reasons == ["restore", "unpublish", "publish", "edit"]


def test_revisions_archived_before_the_labels_read_as_edits(store):
    """Rows written by 0.2.29 carry ``update``: they were edits."""
    wid = _create()["workflow_id"]
    wm.update_workflow(wid, owner_id=ALICE, expected_version=1, name="edited")
    with store.engine.begin() as conn:
        conn.execute(store.revision_table.update().values(reason="update"))
    assert [r["reason"] for r in wm.list_revisions(wid, owner_id=ALICE)] == ["edit"]


# --- Cas de test enregistrés dans le graphe -----------------------------------

CASE = {
    "id": "t_1",
    "name": "haute",
    "payload": {"priority": "high"},
    "expect": {"status": "done", "routes": {"a": "x"}},
}


def _with_tests(graph, tests):
    g = json.loads(json.dumps(graph))
    g["tests"] = tests
    return g


def test_graph_without_tests_is_stored_unchanged():
    wf = _create()
    assert "tests" not in wm.get_workflow(wf["workflow_id"], owner_id=ALICE)["graph"]


def test_create_keeps_test_cases():
    wf = _create(graph=_with_tests(GRAPH, [CASE]))
    got = wm.get_workflow(wf["workflow_id"], owner_id=ALICE)
    assert got["graph"]["tests"] == [CASE]


def test_update_keeps_test_cases():
    wid = _create()["workflow_id"]
    wm.update_workflow(
        wid, owner_id=ALICE, expected_version=1, graph=_with_tests(CANON, [CASE])
    )
    assert wm.get_workflow(wid, owner_id=ALICE)["graph"]["tests"] == [CASE]


def test_duplicate_test_ids_are_refused_on_save():
    wid = _create()["workflow_id"]
    with pytest.raises(wm.InvalidWorkflow):
        wm.update_workflow(
            wid,
            owner_id=ALICE,
            expected_version=1,
            graph=_with_tests(CANON, [CASE, CASE]),
        )


def test_restore_brings_back_the_tests_of_the_revision():
    wid = _create(graph=_with_tests(GRAPH, [CASE]))["workflow_id"]
    wm.update_workflow(wid, owner_id=ALICE, expected_version=1, graph=CANON)
    assert "tests" not in wm.get_workflow(wid, owner_id=ALICE)["graph"]
    rev = wm.list_revisions(wid, owner_id=ALICE)[0]
    wm.restore_revision(wid, rev["revision_id"], owner_id=ALICE)
    assert wm.get_workflow(wid, owner_id=ALICE)["graph"]["tests"] == [CASE]


def test_duplicate_copies_the_tests():
    wid = _create(graph=_with_tests(GRAPH, [CASE]))["workflow_id"]
    copy = wm.duplicate_workflow(wid, owner_id=ALICE)
    got = wm.get_workflow(copy["workflow_id"], owner_id=ALICE)
    assert got["graph"]["tests"] == [CASE]


def test_adding_a_test_to_a_published_workflow_keeps_it_published():
    wid = _create()["workflow_id"]
    wm.update_workflow(wid, owner_id=ALICE, expected_version=1, status="published")
    stale = dict(CASE, expect={"status": "done", "routes": {"router9": "x"}})
    wm.update_workflow(
        wid, owner_id=ALICE, expected_version=2, graph=_with_tests(CANON, [stale])
    )
    got = wm.get_workflow(wid, owner_id=ALICE)
    assert got["status"] == "published" and got["graph"]["tests"] == [stale]
