"""``workflow_done`` (T2) — kind 5/5 : un workflow en écoute la fin d'un autre.

``core.workflow_triggers.launch_triggered_run`` est remplacé par un double :
ce fichier vérifie le FILTRAGE (owner, ``on``, chaîne anti-boucle) et le
CÂBLAGE (lecture du run source, extraction de la chaîne), pas l'exécution
d'un graphe — déjà couverte par ``test_workflow_defs_api.py``.
"""

import json

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from apowerb.core import run_main
from apowerb.core import workflow_main as wm
from apowerb.core import workflow_triggers as wt

ALICE = "alice@acme.fr"
BOB = "bob@other.fr"


def _webhook_graph():
    return {
        "version": 1,
        "nodes": [{"id": "start", "type": "trigger", "config": {"kind": "webhook"}}],
        "edges": [],
    }


def _workflow_done_graph(source_id, on="success"):
    return {
        "version": 1,
        "nodes": [
            {
                "id": "start",
                "type": "trigger",
                "config": {
                    "kind": "workflow_done",
                    "workflow_id": source_id,
                    "on": on,
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
    monkeypatch.setattr(run_main.run_store, "engine", engine)
    run_main.run_store.metadata.create_all(engine)
    return engine


class _Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, *, workflow_id, owner_id, kind, detail, payload):
        self.calls.append(
            dict(
                workflow_id=workflow_id,
                owner_id=owner_id,
                kind=kind,
                detail=detail,
                payload=payload,
            )
        )
        return f"run-{workflow_id}", None


@pytest.fixture()
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(wt, "launch_triggered_run", rec)
    return rec


def _publish(workflow_id, owner_id, version):
    wm.update_workflow(
        workflow_id, owner_id=owner_id, expected_version=version, status="published"
    )


# --- next_trigger_chain (pure) ----------------------------------------------


def test_next_trigger_chain_extends_with_new_workflow():
    chain = wt.next_trigger_chain(["a"], "b")
    assert chain == ["a", "b"]


def test_next_trigger_chain_refuses_a_cycle():
    assert wt.next_trigger_chain(["a", "b"], "a") is None


def test_next_trigger_chain_refuses_beyond_max_depth():
    long_chain = [f"w{i}" for i in range(wt.MAX_TRIGGER_CHAIN)]
    assert wt.next_trigger_chain(long_chain, "new") is None


def test_next_trigger_chain_empty_prior_starts_fresh():
    assert wt.next_trigger_chain([], "a") == ["a"]


# --- fire_workflow_done_triggers --------------------------------------------


async def test_fires_listener_on_matching_status(recorder):
    src = wm.create_workflow(owner_id=ALICE, name="Source", graph=_webhook_graph())
    _publish(src["workflow_id"], ALICE, src["version"])
    dst = wm.create_workflow(
        owner_id=ALICE,
        name="Listener",
        graph=_workflow_done_graph(src["workflow_id"], on="success"),
    )
    _publish(dst["workflow_id"], ALICE, dst["version"])

    started = await wt.fire_workflow_done_triggers(
        source_workflow_id=src["workflow_id"],
        source_owner_id=ALICE,
        source_run_id="run-src-1",
        status="success",
        output={"x": 1},
        error=None,
        prior_chain=[],
    )

    assert started == [f"run-{dst['workflow_id']}"]
    call = recorder.calls[0]
    assert call["workflow_id"] == dst["workflow_id"]
    assert call["kind"] == "workflow_done"
    assert call["detail"] == {
        "workflow_id": src["workflow_id"],
        "on": "success",
        "chain": [src["workflow_id"]],
    }
    assert call["payload"] == {
        "workflow_id": src["workflow_id"],
        "run_id": "run-src-1",
        "status": "success",
        "output": {"x": 1},
        "error": None,
    }


async def test_does_not_fire_on_status_mismatch(recorder):
    src = wm.create_workflow(owner_id=ALICE, name="Source", graph=_webhook_graph())
    _publish(src["workflow_id"], ALICE, src["version"])
    dst = wm.create_workflow(
        owner_id=ALICE,
        name="Listener",
        graph=_workflow_done_graph(src["workflow_id"], on="error"),
    )
    _publish(dst["workflow_id"], ALICE, dst["version"])

    started = await wt.fire_workflow_done_triggers(
        source_workflow_id=src["workflow_id"],
        source_owner_id=ALICE,
        source_run_id="run-src-1",
        status="success",
        output=None,
        error=None,
        prior_chain=[],
    )

    assert started == []
    assert recorder.calls == []


async def test_on_any_fires_regardless_of_status(recorder):
    src = wm.create_workflow(owner_id=ALICE, name="Source", graph=_webhook_graph())
    _publish(src["workflow_id"], ALICE, src["version"])
    dst = wm.create_workflow(
        owner_id=ALICE,
        name="Listener",
        graph=_workflow_done_graph(src["workflow_id"], on="any"),
    )
    _publish(dst["workflow_id"], ALICE, dst["version"])

    started = await wt.fire_workflow_done_triggers(
        source_workflow_id=src["workflow_id"],
        source_owner_id=ALICE,
        source_run_id="run-src-1",
        status="error",
        output=None,
        error={"code": "run_failed", "detail": "boom"},
        prior_chain=[],
    )

    assert started == [f"run-{dst['workflow_id']}"]


async def test_cycle_is_blocked_and_logged_not_fired(recorder):
    src = wm.create_workflow(owner_id=ALICE, name="Source", graph=_webhook_graph())
    _publish(src["workflow_id"], ALICE, src["version"])
    dst = wm.create_workflow(
        owner_id=ALICE,
        name="Listener",
        graph=_workflow_done_graph(src["workflow_id"], on="any"),
    )
    _publish(dst["workflow_id"], ALICE, dst["version"])

    # La chaîne porte déjà le workflow source : A a déjà été traversé plus
    # haut dans le déclenchement en cours -> cycle, rien ne part.
    started = await wt.fire_workflow_done_triggers(
        source_workflow_id=src["workflow_id"],
        source_owner_id=ALICE,
        source_run_id="run-src-1",
        status="success",
        output=None,
        error=None,
        prior_chain=["z", src["workflow_id"]],
    )

    assert started == []
    assert recorder.calls == []


async def test_unpublished_listener_is_silently_skipped(recorder, monkeypatch):
    src = wm.create_workflow(owner_id=ALICE, name="Source", graph=_webhook_graph())
    _publish(src["workflow_id"], ALICE, src["version"])
    dst = wm.create_workflow(
        owner_id=ALICE,
        name="Listener",
        graph=_workflow_done_graph(src["workflow_id"], on="any"),
    )
    _publish(dst["workflow_id"], ALICE, dst["version"])

    async def _raises(**kwargs):
        raise wt.TriggerNotActive(kwargs["workflow_id"])

    monkeypatch.setattr(wt, "launch_triggered_run", _raises)

    started = await wt.fire_workflow_done_triggers(
        source_workflow_id=src["workflow_id"],
        source_owner_id=ALICE,
        source_run_id="run-src-1",
        status="success",
        output=None,
        error=None,
        prior_chain=[],
    )

    assert started == []


# --- notify_run_finished : lit le run source et extrait sa chaîne ----------


async def test_notify_run_finished_reads_workflow_id_from_run_config(recorder):
    src = wm.create_workflow(owner_id=ALICE, name="Source", graph=_webhook_graph())
    _publish(src["workflow_id"], ALICE, src["version"])
    dst = wm.create_workflow(
        owner_id=ALICE,
        name="Listener",
        graph=_workflow_done_graph(src["workflow_id"], on="success"),
    )
    _publish(dst["workflow_id"], ALICE, dst["version"])

    run_id = run_main.start_run(
        trigger="workflow",
        owner_id=ALICE,
        config={"workflow_id": src["workflow_id"], "version": 1, "payload": {}},
    )

    await wt.notify_run_finished(
        run_id=run_id,
        owner_id=ALICE,
        status="success",
        output={"ok": True},
        error_message=None,
    )

    assert recorder.calls[0]["workflow_id"] == dst["workflow_id"]
    assert recorder.calls[0]["payload"]["run_id"] == run_id


async def test_notify_run_finished_extracts_prior_chain_from_own_trigger(recorder):
    src = wm.create_workflow(owner_id=ALICE, name="Source", graph=_webhook_graph())
    _publish(src["workflow_id"], ALICE, src["version"])
    dst = wm.create_workflow(
        owner_id=ALICE,
        name="Listener",
        graph=_workflow_done_graph(src["workflow_id"], on="success"),
    )
    _publish(dst["workflow_id"], ALICE, dst["version"])

    run_id = run_main.start_run(
        trigger=json.dumps(
            {"kind": "workflow_done", "detail": {"chain": ["ancestor"]}}
        ),
        owner_id=ALICE,
        config={"workflow_id": src["workflow_id"], "version": 1, "payload": {}},
    )

    await wt.notify_run_finished(
        run_id=run_id,
        owner_id=ALICE,
        status="success",
        output=None,
        error_message=None,
    )

    assert recorder.calls[0]["detail"]["chain"] == ["ancestor", src["workflow_id"]]


async def test_notify_run_finished_ignores_runs_without_workflow_id(recorder):
    # Run canvas legacy (config sans "workflow_id") : rien à écouter.
    run_id = run_main.start_run(
        trigger="workflow", owner_id=ALICE, config={"agents": ["agent1"]}
    )

    await wt.notify_run_finished(
        run_id=run_id, owner_id=ALICE, status="success", output=None, error_message=None
    )

    assert recorder.calls == []


async def test_notify_run_finished_unknown_run_is_a_noop(recorder):
    await wt.notify_run_finished(
        run_id="does-not-exist",
        owner_id=ALICE,
        status="success",
        output=None,
        error_message=None,
    )
    assert recorder.calls == []


def test_error_detail_wraps_message_only_on_error_status():
    assert wt._error_detail("success", "boom") is None
    assert wt._error_detail("error", None) is None
    assert wt._error_detail("error", "boom") == {"code": "run_failed", "detail": "boom"}


# --- validation (owner_id) : au niveau workflow_graph -----------------------


def test_validate_refuses_workflow_done_on_a_workflow_of_another_owner():
    src = wm.create_workflow(owner_id=BOB, name="Bob's", graph=_webhook_graph())

    report = wm.check_workflow(
        _workflow_done_graph(src["workflow_id"], on="success"), owner_id=ALICE
    )

    assert report["valid"] is False
    assert "introuvable" in report["errors"][0]


def test_validate_accepts_workflow_done_on_own_workflow():
    src = wm.create_workflow(owner_id=ALICE, name="Mine", graph=_webhook_graph())

    report = wm.check_workflow(
        _workflow_done_graph(src["workflow_id"], on="success"), owner_id=ALICE
    )

    assert report["valid"] is True


def test_publish_refuses_workflow_done_cross_owner_source(recorder):
    src = wm.create_workflow(owner_id=BOB, name="Bob's", graph=_webhook_graph())
    dst = wm.create_workflow(
        owner_id=ALICE,
        name="Listener",
        graph=_workflow_done_graph(src["workflow_id"], on="success"),
    )

    with pytest.raises(wm.InvalidWorkflow):
        wm.update_workflow(
            dst["workflow_id"],
            owner_id=ALICE,
            expected_version=dst["version"],
            status="published",
        )
