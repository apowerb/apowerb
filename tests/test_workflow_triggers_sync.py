"""Synchronisation du trigger à la publication / dépublication / suppression.

Banc SQLite réel partagé entre ``workflow_store`` et
``workflow_trigger_store`` (même montage que ``test_workflow_defs.py``) :
``workflow_main`` appelle ``workflow_triggers.sync_trigger_for_workflow``
après chaque écriture réussie, et c'est CE comportement qui est vérifié ici,
pas un faux.
"""

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from apowerb.core import workflow_main as wm
from apowerb.core import workflow_triggers as wt

ALICE = "alice@acme.fr"

MANUAL_GRAPH = {
    "version": 1,
    "nodes": [{"id": "start", "type": "trigger"}],
    "edges": [],
}


def _webhook_graph(hmac=False):
    return {
        "version": 1,
        "nodes": [
            {
                "id": "start",
                "type": "trigger",
                "config": {"kind": "webhook", "hmac": hmac},
            }
        ],
        "edges": [],
    }


def _schedule_graph(cron="*/5 * * * *"):
    return {
        "version": 1,
        "nodes": [
            {
                "id": "start",
                "type": "trigger",
                "config": {
                    "kind": "schedule",
                    "cron": cron,
                    "timezone": "Europe/Paris",
                },
            }
        ],
        "edges": [],
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
    monkeypatch.setattr(wt.workflow_trigger_store, "engine", engine)
    wt.workflow_trigger_store.metadata.create_all(engine)
    return engine


def _row(workflow_id):
    t = wt.workflow_trigger_store.trigger_table
    with wt.workflow_trigger_store.engine.begin() as conn:
        row = conn.execute(t.select().where(t.c.workflow_id == workflow_id)).fetchone()
    return dict(row._mapping) if row is not None else None


def test_creating_a_draft_provisions_an_inactive_trigger_row():
    wf = wm.create_workflow(owner_id=ALICE, name="W", graph=_webhook_graph())
    row = _row(wf["workflow_id"])
    assert row is not None
    assert row["kind"] == "webhook"
    assert row["active"] is False  # brouillon : pas encore publié
    assert row["token_hash"]  # jeton déjà provisionné (URL copiable avant publication)


def test_publishing_activates_and_unpublishing_deactivates():
    wf = wm.create_workflow(owner_id=ALICE, name="W", graph=_webhook_graph())
    wid = wf["workflow_id"]

    published = wm.update_workflow(
        wid, owner_id=ALICE, expected_version=wf["version"], status="published"
    )
    assert _row(wid)["active"] is True

    unpublished = wm.update_workflow(
        wid, owner_id=ALICE, expected_version=published["version"], status="draft"
    )
    assert _row(wid)["active"] is False
    assert unpublished["status"] == "draft"


def test_webhook_token_is_stable_across_publish_unpublish_cycles():
    wf = wm.create_workflow(owner_id=ALICE, name="W", graph=_webhook_graph())
    wid = wf["workflow_id"]
    token_hash_before = _row(wid)["token_hash"]

    wf2 = wm.update_workflow(
        wid, owner_id=ALICE, expected_version=wf["version"], status="published"
    )
    wf3 = wm.update_workflow(
        wid, owner_id=ALICE, expected_version=wf2["version"], status="draft"
    )
    wm.update_workflow(
        wid, owner_id=ALICE, expected_version=wf3["version"], status="published"
    )

    assert _row(wid)["token_hash"] == token_hash_before


def test_editing_unrelated_fields_keeps_the_same_token():
    wf = wm.create_workflow(owner_id=ALICE, name="W", graph=_webhook_graph())
    wid = wf["workflow_id"]
    token_hash_before = _row(wid)["token_hash"]

    wm.update_workflow(
        wid, owner_id=ALICE, expected_version=wf["version"], name="Renamed"
    )

    assert _row(wid)["token_hash"] == token_hash_before


def test_changing_trigger_kind_replaces_the_token():
    wf = wm.create_workflow(owner_id=ALICE, name="W", graph=_webhook_graph())
    wid = wf["workflow_id"]
    token_hash_before = _row(wid)["token_hash"]

    wm.update_workflow(
        wid, owner_id=ALICE, expected_version=wf["version"], graph=_schedule_graph()
    )

    row = _row(wid)
    assert row["kind"] == "schedule"
    assert row["token_hash"] is None
    assert token_hash_before is not None


def test_deleting_a_workflow_removes_its_trigger_row():
    wf = wm.create_workflow(owner_id=ALICE, name="W", graph=_webhook_graph())
    wid = wf["workflow_id"]
    assert _row(wid) is not None

    wm.delete_workflow(wid, owner_id=ALICE)

    assert _row(wid) is None


def test_manual_trigger_never_activates_even_published():
    wf = wm.create_workflow(owner_id=ALICE, name="W", graph=MANUAL_GRAPH)
    wid = wf["workflow_id"]
    wm.update_workflow(
        wid, owner_id=ALICE, expected_version=wf["version"], status="published"
    )

    row = _row(wid)
    assert row["kind"] == "manual"
    assert row["active"] is False


def test_publishing_a_schedule_trigger_computes_next_run_at():
    wf = wm.create_workflow(owner_id=ALICE, name="W", graph=_schedule_graph())
    wid = wf["workflow_id"]

    wm.update_workflow(
        wid, owner_id=ALICE, expected_version=wf["version"], status="published"
    )

    row = _row(wid)
    assert row["active"] is True
    assert row["next_run_at"] is not None


def test_unpublishing_a_schedule_trigger_clears_next_run_at():
    wf = wm.create_workflow(owner_id=ALICE, name="W", graph=_schedule_graph())
    wid = wf["workflow_id"]
    wf2 = wm.update_workflow(
        wid, owner_id=ALICE, expected_version=wf["version"], status="published"
    )
    assert _row(wid)["next_run_at"] is not None

    wm.update_workflow(
        wid, owner_id=ALICE, expected_version=wf2["version"], status="draft"
    )

    assert _row(wid)["next_run_at"] is None


def test_restoring_a_revision_deactivates_the_trigger_like_unpublishing():
    wf = wm.create_workflow(owner_id=ALICE, name="W", graph=_webhook_graph())
    wid = wf["workflow_id"]
    wm.update_workflow(
        wid, owner_id=ALICE, expected_version=wf["version"], status="published"
    )
    revisions = wm.list_revisions(wid, owner_id=ALICE)
    assert revisions  # au moins l'état "draft" archivé par la publication

    wm.restore_revision(wid, revisions[-1]["revision_id"], owner_id=ALICE)

    assert _row(wid)["active"] is False


def test_hmac_enabled_provisions_an_encrypted_secret_never_returned_by_sync():
    wf = wm.create_workflow(owner_id=ALICE, name="W", graph=_webhook_graph(hmac=True))
    wid = wf["workflow_id"]

    row = _row(wid)
    assert row["hmac_enabled"] is True
    assert row["hmac_secret_encrypted"]  # provisionné...
    # ... mais create_workflow() ne le rend jamais en clair à l'appelant.
    assert "hmac_secret" not in wf


# --- Revue 22/09 : un PUT graphe seul sur un workflow publié est validé -------


def test_graph_only_edit_of_a_published_workflow_is_validated():
    """Publier un schedule valide puis PUT du graphe seul avec un cron sous le
    plancher de 5 min : refusé, et le trigger garde l'ancienne config."""
    wf = wm.create_workflow(
        owner_id=ALICE, name="W", graph=_schedule_graph("0 9 * * *")
    )
    wid = wf["workflow_id"]
    published = wm.update_workflow(
        wid, owner_id=ALICE, expected_version=wf["version"], status="published"
    )
    before = _row(wid)

    with pytest.raises(wm.InvalidWorkflow):
        wm.update_workflow(
            wid,
            owner_id=ALICE,
            expected_version=published["version"],
            graph=_schedule_graph("* * * * *"),
        )

    after = _row(wid)
    assert after["active"] is True
    assert after["config"] == before["config"]
    assert after["next_run_at"] == before["next_run_at"]
    assert wm.get_workflow(wid, owner_id=ALICE)["version"] == published["version"]


def test_graph_only_edit_of_a_draft_is_not_blocked_by_publish_rules():
    wf = wm.create_workflow(
        owner_id=ALICE, name="W", graph=_schedule_graph("0 9 * * *")
    )
    updated = wm.update_workflow(
        wf["workflow_id"],
        owner_id=ALICE,
        expected_version=wf["version"],
        graph=_schedule_graph("* * * * *"),
    )
    assert updated["status"] == "draft"
    assert _row(wf["workflow_id"])["active"] is False


def test_valid_graph_only_edit_of_a_published_workflow_rearms_the_trigger():
    wf = wm.create_workflow(
        owner_id=ALICE, name="W", graph=_schedule_graph("0 9 * * *")
    )
    wid = wf["workflow_id"]
    published = wm.update_workflow(
        wid, owner_id=ALICE, expected_version=wf["version"], status="published"
    )
    wm.update_workflow(
        wid,
        owner_id=ALICE,
        expected_version=published["version"],
        graph=_schedule_graph("*/10 * * * *"),
    )
    row = _row(wid)
    assert row["active"] is True
    assert "*/10 * * * *" in row["config"]
