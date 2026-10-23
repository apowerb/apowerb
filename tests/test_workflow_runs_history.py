"""L'historique des runs d'un workflow, tel que le Studio le montre.

Un utilisateur métier ne voyait les échecs de ses workflows planifiés que par
l'API, et seulement sous forme d'un message brut : la liste ne savait ni se
limiter à un workflow, ni à un statut, ni se paginer, et un run passé ne
gardait rien de son déroulé nœud par nœud.

Ce que ces tests figent :

- ``list_runs`` filtre par workflow, par statut et se pagine, toujours dans
  le périmètre du propriétaire ;
- un run de graphe garde son échec sous forme structurée (code, paramètres,
  ``ref``, nœud fautif) en plus du message ;
- sa trace d'événements est conservée, plafonnée, et relue par son seul
  propriétaire ;
- le déclencheur est normalisé (``trigger_kind``) pour que l'interface n'ait
  rien à analyser ;
- ``failure_listeners`` dit quels workflows publiés écoutent l'échec.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, text
from sqlalchemy.pool import StaticPool

from apowerb.core import run_main

OWNER = "u@example.com"
OTHER = "someone-else@example.com"


def _sqlite_engine():
    """SQLite qui répond au schéma du store (``public`` attaché, vide ou non)."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _attach_public_schema(dbapi_connection, _record):  # pragma: no cover
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS public")

    return engine


@pytest.fixture
def store(monkeypatch, tmp_path):
    engine = _sqlite_engine()
    run_store = run_main.run_store
    monkeypatch.setattr(run_store, "engine", engine)
    run_store.metadata.create_all(engine)
    monkeypatch.setattr(run_main, "_run_input_dir", lambda run_id: tmp_path / run_id)
    return run_store


def _graph_run(workflow_id="wf-1", owner=OWNER, trigger="workflow", **extra):
    return run_main.start_run(
        trigger=trigger,
        owner_id=owner,
        config={"workflow_id": workflow_id, "version": 1, "payload": {}},
        **extra,
    )


# --- filtres et pagination ---------------------------------------------------


def test_runs_can_be_listed_for_one_workflow(store):
    mine = _graph_run("wf-1")
    _graph_run("wf-2")

    listed = run_main.list_runs(OWNER, workflow_id="wf-1")

    assert [r["run_id"] for r in listed] == [mine]
    assert listed[0]["workflow_id"] == "wf-1"


def test_a_workflow_filter_never_reaches_another_owner(store):
    _graph_run("wf-1", owner=OTHER)

    assert run_main.list_runs(OWNER, workflow_id="wf-1") == []


def test_runs_can_be_filtered_by_status(store):
    ok = _graph_run()
    run_main.finish_run(ok, status="success")
    failed = _graph_run()
    run_main.finish_run(failed, status="error", error_message="boom")
    waiting = _graph_run()
    with store.engine.begin() as conn:
        conn.execute(
            store.run_table.update()
            .where(store.run_table.c.run_id == waiting)
            .values(status="awaiting")
        )

    assert [r["run_id"] for r in run_main.list_runs(OWNER, statuses=["error"])] == [failed]
    assert [r["run_id"] for r in run_main.list_runs(OWNER, statuses=["awaiting"])] == [waiting]
    both = {r["run_id"] for r in run_main.list_runs(OWNER, statuses=["error", "success"])}
    assert both == {ok, failed}


def test_an_unknown_status_is_refused_rather_than_ignored(store):
    with pytest.raises(ValueError):
        run_main.list_runs(OWNER, statuses=["failed"])


def test_runs_are_paginated_newest_first(store):
    ids = [_graph_run() for _ in range(5)]

    first = run_main.list_runs(OWNER, workflow_id="wf-1", limit=2)
    second = run_main.list_runs(OWNER, workflow_id="wf-1", limit=2, offset=2)
    last = run_main.list_runs(OWNER, workflow_id="wf-1", limit=2, offset=4)

    assert [r["run_id"] for r in first] == ids[::-1][:2]
    assert [r["run_id"] for r in second] == ids[::-1][2:4]
    assert [r["run_id"] for r in last] == ids[::-1][4:]


def test_a_replay_inherits_the_workflow_it_replays(store):
    original = _graph_run("wf-9")
    run_main.finish_run(original, status="error", error_message="boom")

    replay = run_main.prepare_replay(original, owner_id=OWNER)

    listed = run_main.list_runs(OWNER, workflow_id="wf-9")
    assert {r["run_id"] for r in listed} == {original, replay["run_id"]}


# --- déclencheur normalisé et durée -----------------------------------------


def test_the_trigger_is_normalised_for_the_interface(store):
    manual = _graph_run(trigger="workflow")
    simulated = _graph_run(trigger="workflow_simulated_email")
    scheduled = _graph_run(
        trigger=json.dumps({"kind": "schedule", "detail": {"cron": "0 9 * * *"}})
    )
    chained = _graph_run(
        trigger=json.dumps({"kind": "workflow_done", "detail": {"workflow_id": "wf-0", "on": "error"}})
    )

    by_id = {r["run_id"]: r for r in run_main.list_runs(OWNER)}
    assert by_id[manual]["trigger_kind"] == "manual"
    assert by_id[simulated]["trigger_kind"] == "simulated_email"
    assert by_id[scheduled]["trigger_kind"] == "schedule"
    assert by_id[scheduled]["trigger_detail"] == {"cron": "0 9 * * *"}
    assert by_id[chained]["trigger_kind"] == "workflow_done"
    assert by_id[chained]["trigger_detail"]["on"] == "error"


def test_a_finished_run_carries_its_duration(store):
    run_id = _graph_run()
    with store.engine.begin() as conn:
        conn.execute(
            store.run_table.update()
            .where(store.run_table.c.run_id == run_id)
            .values(
                created_at="2026-10-22 10:00:00.000000",
                finished_at="2026-10-22 10:00:02.500000",
                status="success",
            )
        )

    run = run_main.get_run(run_id, owner_id=OWNER)
    assert run["duration_ms"] == 2500


def test_a_run_without_an_end_has_no_duration(store):
    run_id = _graph_run()
    assert run_main.get_run(run_id, owner_id=OWNER)["duration_ms"] is None


# --- échec structuré ---------------------------------------------------------


def test_a_failure_keeps_its_code_params_and_node(store):
    run_id = _graph_run()

    run_main.finish_run(run_id, status="error", error_message="agent2 : réponse non JSON")
    run_main.record_failure(
        run_id,
        {"code": "output_type_mismatch", "params": {"node": "agent2"}, "failed_node": "agent2"},
    )

    run = run_main.get_run(run_id, owner_id=OWNER)
    assert run["error_message"] == "agent2 : réponse non JSON"
    assert run["error"] == {
        "code": "output_type_mismatch",
        "params": {"node": "agent2"},
        "failed_node": "agent2",
    }
    # Sans instantané de reprise, ``failure_node`` reste vide : il dit
    # « reprise enregistrée », pas seulement « où ».
    assert run["failure_node"] is None


def _text_columns(run_id) -> str:
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        row = conn.execute(t.select().where(t.c.run_id == run_id)).fetchone()._asdict()
    return " ".join(str(v) for v in row.values() if isinstance(v, str))


def test_a_failure_keeps_only_the_structural_params_in_clear(store):
    """La question d'un agent est une sortie du run : elle ne reste pas en clair en base."""
    run_id = _graph_run()
    run_main.finish_run(run_id, status="error", error_message="agent2 : question")
    run_main.record_failure(
        run_id,
        {
            "code": "agent_asked_question",
            "params": {"node": "agent2", "question": "MARQUEUR-SORTIE ?", "max": 3},
            "failed_node": "agent2",
        },
    )

    run = run_main.get_run(run_id, owner_id=OWNER)
    # La clé reste, pour que le message se traduise ; sa valeur est masquée.
    assert run["error"]["params"] == {"node": "agent2", "question": "…", "max": 3}
    assert "MARQUEUR-SORTIE" not in _text_columns(run_id)


def test_the_node_saved_with_the_retry_snapshot_is_not_overwritten(store):
    """``failure_node`` n'a qu'un auteur, le moteur : ``save_failure`` l'écrit le premier."""
    run_id = _graph_run()
    run_main.save_failure(run_id, {"failed_node": "writer", "graph": {}, "outputs": {}})
    run_main.finish_run(run_id, status="error", error_message="quota")
    run_main.record_failure(run_id, {"code": "workflow_error", "failed_node": "autre"})

    assert run_main.get_run(run_id, owner_id=OWNER)["failure_node"] == "writer"


def test_a_success_has_no_structured_error(store):
    run_id = _graph_run()
    run_main.finish_run(run_id, status="success")
    run_main.record_failure(run_id, {"code": "internal", "failed_node": "n1"})
    assert run_main.get_run(run_id, owner_id=OWNER)["error"] is None


def test_a_replayed_run_does_not_inherit_a_stale_failure(store):
    """``finish_run`` remet l'échec structuré à zéro : il n'appartient qu'à l'issue écrite."""
    run_id = _graph_run()
    run_main.finish_run(run_id, status="error", error_message="a")
    run_main.record_failure(run_id, {"code": "internal"})
    with run_main.run_store.engine.begin() as conn:
        t = run_main.run_store.run_table
        conn.execute(t.update().where(t.c.run_id == run_id).values(status="running"))
    run_main.finish_run(run_id, status="error", error_message="b")
    assert run_main.get_run(run_id, owner_id=OWNER)["error"] is None


# --- la trace : nœud fautif ------------------------------------------------


def _sse(event):
    return f"data: {json.dumps(event)}\n\n"


def _trace(*events):
    trace = run_main.RunTrace()
    for e in events:
        trace.add(_sse(e))
    return trace


def test_the_faulty_node_is_the_one_the_engine_names():
    """Contre-exemple : le dernier ``node_error`` (rattrapé par un ``try``) n'est
    pas le fautif ; seul le ``failed_node`` du moteur compte."""
    trace = _trace(
        {"event": "node_error", "node_id": "agent3", "code": "output_type_mismatch", "detail": "b"},
        {"event": "node_error", "node_id": "try2.agent4", "attempt": 0, "code": "internal", "detail": "c"},
        {"event": "error", "code": "output_type_mismatch", "detail": "b", "params": {"node": "agent3"}, "failed_node": "agent3", "retryable": True},
    )
    assert trace.failure() == {
        "code": "output_type_mismatch",
        "params": {"node": "agent3"},
        "failed_node": "agent3",
    }


def test_a_run_level_error_names_no_node():
    trace = _trace(
        {"event": "node_error", "node_id": "agent9", "code": "invalid_graph", "detail": "bad"},
        {"event": "error", "code": "invalid_graph", "detail": "bad"},
    )
    assert trace.failure() == {"code": "invalid_graph"}


def test_a_run_that_did_not_fail_has_no_failure():
    trace = _trace({"event": "node_complete", "node_id": "a", "output": 1}, {"event": "done", "output": 1})
    assert trace.failure() is None


# --- la trace : plafonds -----------------------------------------------------


def test_a_huge_output_is_cut_in_the_trace_only():
    big = "x" * (run_main.TRACE_FIELD_MAX_CHARS + 10)
    trace = _trace({"event": "node_complete", "node_id": "a", "output": big})

    kept = trace.events[0]
    assert kept["node_id"] == "a"
    assert kept["output"] != big
    assert kept["output_truncated"] is True
    assert trace.truncated is True


def test_the_trace_stops_growing_past_its_budget_but_keeps_the_ending(monkeypatch):
    monkeypatch.setattr(run_main, "TRACE_MAX_BYTES", 2_000)
    events = [{"event": "node_complete", "node_id": f"n{i}", "output": "y" * 300} for i in range(30)]
    events.append({"event": "error", "code": "internal", "detail": "fin", "ref": "q"})
    trace = _trace(*events)

    assert trace.truncated is True
    assert trace.events[-1]["event"] == "error"
    assert len(json.dumps(trace.events)) < 2_000 + 1_000


def test_non_json_chunks_are_ignored():
    trace = run_main.RunTrace()
    trace.add(": keep-alive\n\n")
    trace.add(b"data: not json\n\n")
    assert trace.events == []


# --- la trace : enregistrement et lecture ---------------------------------


def test_a_trace_is_saved_and_read_back_by_its_owner_only(store):
    run_id = _graph_run()
    trace = _trace({"event": "node_start", "node_id": "a"}, {"event": "done", "output": None})

    run_main.save_trace(run_id, trace)

    assert run_main.get_trace(run_id, owner_id=OWNER) == {
        "events": trace.events,
        "truncated": False,
        "available": True,
    }
    assert run_main.get_trace(run_id, owner_id=OTHER) is None


def test_a_trace_saved_again_replaces_the_previous_one(store):
    """Une reprise après décision réémet tout le déroulé : elle remplace la trace de la pause."""
    run_id = _graph_run()
    run_main.save_trace(run_id, _trace({"event": "awaiting_input", "node_id": "d"}))
    run_main.save_trace(run_id, _trace({"event": "done", "output": 1}))

    assert run_main.get_trace(run_id, owner_id=OWNER)["events"] == [{"event": "done", "output": 1}]


def test_an_old_run_without_trace_says_so(store):
    run_id = _graph_run()
    assert run_main.get_trace(run_id, owner_id=OWNER) == {
        "events": [],
        "truncated": False,
        "available": False,
    }


def test_the_list_does_not_carry_the_trace(store):
    run_id = _graph_run()
    run_main.save_trace(run_id, _trace({"event": "done", "output": "z" * 1000}))
    listed = run_main.list_runs(OWNER)
    assert "events" not in listed[0]


# --- la trace au repos : chiffrée, puis oubliée --------------------------------


def _stored_trace(store, run_id):
    t = store.trace_table
    with store.engine.begin() as conn:
        row = conn.execute(t.select().where(t.c.run_id == run_id)).fetchone()
    return row._asdict()["events"] if row else None


def test_a_trace_is_encrypted_at_rest(store):
    run_id = _graph_run()
    run_main.save_trace(
        run_id,
        _trace(
            {
                "event": "node_complete",
                "node_id": "agent1",
                "input": "iban FR76-secret",
                "output": "réponse-confidentielle",
                "tool_calls": [{"name": "crm", "args": {"q": "client-caché"}}],
            }
        ),
    )

    raw = _stored_trace(store, run_id)
    assert raw and not raw.lstrip().startswith("[")
    for clear in ("FR76-secret", "confidentielle", "client-cach", "agent1", "node_complete"):
        assert clear not in raw
    assert run_main.get_trace(run_id, owner_id=OWNER)["events"][0]["output"] == "réponse-confidentielle"


def test_without_a_key_no_trace_is_written(store, monkeypatch):
    from apowerb.helpers import encryptor

    monkeypatch.setattr(encryptor, "fernet", None)
    run_id = _graph_run()

    with pytest.raises(RuntimeError):
        run_main.save_trace(run_id, _trace({"event": "done", "output": "secret"}))
    assert _stored_trace(store, run_id) is None


def test_a_trace_that_can_no_longer_be_read_says_so(store):
    """Clé changée depuis l'écriture : la trace est indisponible, pas une erreur 500."""
    run_id = _graph_run()
    with store.engine.begin() as conn:
        conn.execute(
            store.trace_table.insert().values(
                run_id=run_id, events="gAAAAA-plus-lisible", truncated=0, updated_at=run_main._now()
            )
        )

    assert run_main.get_trace(run_id, owner_id=OWNER) == {
        "events": [],
        "truncated": False,
        "available": False,
    }


def test_a_trace_stored_in_clear_is_not_served(store):
    """Une trace se lit déchiffrée ou pas du tout : un JSON en clair n'est pas relu."""
    run_id = _graph_run()
    with store.engine.begin() as conn:
        conn.execute(
            store.trace_table.insert().values(
                run_id=run_id,
                events=json.dumps([{"event": "done", "output": "en clair"}]),
                truncated=0,
                updated_at=run_main._now(),
            )
        )

    assert run_main.get_trace(run_id, owner_id=OWNER)["available"] is False


def test_forgetting_a_workflow_drops_its_traces_only(store):
    mine = _graph_run("wf-1")
    sibling = _graph_run("wf-2")
    theirs = _graph_run("wf-1", owner=OTHER)
    for run_id in (mine, sibling, theirs):
        run_main.save_trace(run_id, _trace({"event": "done", "output": 1}))

    assert run_main.forget_workflow_traces("wf-1", owner_id=OWNER) == 1

    assert _stored_trace(store, mine) is None
    assert _stored_trace(store, sibling) is not None
    assert _stored_trace(store, theirs) is not None


def test_deleting_a_workflow_drops_the_traces_of_its_runs(store, trigger_store):
    wm, _ = trigger_store
    wf = wm.create_workflow(owner_id=OWNER, name="Jetable", graph={"version": 1, "nodes": [], "edges": []})
    run_id = _graph_run(wf["workflow_id"])
    run_main.save_trace(run_id, _trace({"event": "done", "output": "sortie"}))

    wm.delete_workflow(wf["workflow_id"], owner_id=OWNER)

    assert _stored_trace(store, run_id) is None


def _age_trace(store, run_id, stamp="2020-01-01 00:00:00.000000"):
    with store.engine.begin() as conn:
        conn.execute(
            store.trace_table.update().where(store.trace_table.c.run_id == run_id).values(updated_at=stamp)
        )


def test_old_traces_are_purged_but_a_waiting_run_keeps_its_own(store):
    old = _graph_run()
    recent = _graph_run()
    waiting = _graph_run()
    for run_id in (old, recent, waiting):
        run_main.save_trace(run_id, _trace({"event": "done", "output": 1}))
    _age_trace(store, old)
    _age_trace(store, waiting)
    with store.engine.begin() as conn:
        conn.execute(
            store.run_table.update().where(store.run_table.c.run_id == waiting).values(status="awaiting")
        )

    assert run_main.purge_old_traces(days=30) == 1

    assert _stored_trace(store, old) is None
    assert _stored_trace(store, recent) is not None
    assert _stored_trace(store, waiting) is not None


@pytest.mark.parametrize(
    ("raw", "days"),
    [(None, 30), ("7", 7), ("0", 30), ("abc", 30)],
)
def test_the_trace_retention_is_read_from_the_environment(monkeypatch, raw, days):
    if raw is None:
        monkeypatch.delenv("RUN_TRACE_RETENTION_DAYS", raising=False)
    else:
        monkeypatch.setenv("RUN_TRACE_RETENTION_DAYS", raw)
    assert run_main.trace_retention_days() == days


# --- mutations survivantes de la première revue ---------------------------------


def test_runs_started_in_the_same_instant_keep_a_stable_order(store, monkeypatch):
    monkeypatch.setattr(run_main, "_now", lambda: "2026-10-22 10:00:00.000000")
    ids = [_graph_run() for _ in range(6)]

    first = run_main.list_runs(OWNER, limit=3)
    second = run_main.list_runs(OWNER, limit=3, offset=3)

    assert [r["run_id"] for r in first + second] == sorted(ids, reverse=True)


# --- heures : ISO UTC pour le navigateur ----------------------------------------


@pytest.fixture
def paris_time(monkeypatch):
    """Un serveur hors UTC : sur une machine en UTC, le test ne prouverait rien."""
    monkeypatch.setenv("TZ", "Europe/Paris")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_run_times_are_sent_as_utc_iso(store, paris_time):
    from datetime import datetime, timezone

    run_id = _graph_run()
    run_main.finish_run(run_id, status="success")
    stored = run_main.run_store.run_table
    with store.engine.begin() as conn:
        row = conn.execute(stored.select().where(stored.c.run_id == run_id)).fetchone()._asdict()

    run = run_main.get_run(run_id, owner_id=OWNER)

    for field in ("created_at", "finished_at"):
        sent = datetime.fromisoformat(run[field])
        assert sent.utcoffset().total_seconds() == 0
        # Le même instant que l'heure locale stockée, à la milliseconde près.
        local = datetime.strptime(row[field], "%Y-%m-%d %H:%M:%S.%f").astimezone()
        assert abs((sent - local.astimezone(timezone.utc)).total_seconds()) < 0.001
    assert run["duration_ms"] is not None


# --- le flux réel : _streaming_run consigne l'échec structuré et la trace --


def _user(email=OWNER):
    return SimpleNamespace(email=email, user_id=1, role="USER")


async def _drain(response):
    chunks = [c async for c in response.body_iterator]
    return b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks)


@pytest.fixture
def workflows(monkeypatch, store):
    from apowerb.routers import workflows as module

    module._runs.clear()

    async def _quiet(**_kwargs):
        return None

    from apowerb.core import workflow_triggers

    monkeypatch.setattr(workflow_triggers, "notify_run_finished", _quiet)
    return module


@pytest.mark.asyncio
async def test_a_failed_graph_run_is_recorded_with_its_faulty_node_and_trace(workflows):
    run_id = _graph_run("wf-7")

    async def _runner(_cancel):
        yield _sse({"event": "node_start", "node_id": "agent1", "type": "agent"})
        yield _sse({"event": "node_error", "node_id": "agent1", "code": "output_type_mismatch", "detail": "pas du JSON", "params": {"node": "agent1"}})
        yield _sse({"event": "error", "code": "output_type_mismatch", "detail": "pas du JSON", "params": {"node": "agent1"}, "failed_node": "agent1", "retryable": False})

    response = workflows._streaming_run(
        run_id=run_id, agent_ids=[], file_bytes=None, owner=OWNER, runner=_runner
    )
    await _drain(response)

    run = run_main.get_run(run_id, owner_id=OWNER)
    assert run["status"] == "error"
    assert run["error_message"] == "pas du JSON"
    assert run["error"] == {
        "code": "output_type_mismatch",
        "params": {"node": "agent1"},
        "failed_node": "agent1",
    }
    events = run_main.get_trace(run_id, owner_id=OWNER)["events"]
    assert [e["event"] for e in events] == ["run_started", "node_start", "node_error", "error"]


@pytest.mark.asyncio
async def test_the_failure_alert_names_the_node_the_history_shows(workflows, monkeypatch):
    """Un seul nœud fautif : celui du moteur (``failed_node``), pas ``params.node``."""
    from apowerb.core import workflow_triggers

    sent = []

    async def _notify(**kwargs):
        sent.append(kwargs)

    monkeypatch.setattr(workflow_triggers, "notify_run_finished", _notify)
    run_id = _graph_run("wf-7")

    async def _runner(_cancel):
        # Échec dans un sous-workflow : ``params.node`` nomme le nœud interne.
        yield _sse({"event": "error", "code": "output_type_mismatch", "detail": "x", "params": {"node": "interne"}, "failed_node": "sous1"})

    await _drain(workflows._streaming_run(
        run_id=run_id, agent_ids=[], file_bytes=None, owner=OWNER, runner=_runner
    ))

    assert run_main.get_run(run_id, owner_id=OWNER)["error"]["failed_node"] == "sous1"
    assert sent[-1]["error_node"] == "sous1"


@pytest.mark.asyncio
async def test_a_paused_run_keeps_its_trace_so_far(workflows):
    run_id = _graph_run("wf-7")

    async def _runner(_cancel):
        yield _sse({"event": "node_complete", "node_id": "agent1", "output": "ok"})
        yield _sse({"event": "awaiting_input", "node_id": "decision1", "question": "?"})

    response = workflows._streaming_run(
        run_id=run_id, agent_ids=[], file_bytes=None, owner=OWNER, runner=_runner
    )
    await _drain(response)

    events = run_main.get_trace(run_id, owner_id=OWNER)["events"]
    assert events[-1]["event"] == "awaiting_input"


@pytest.mark.asyncio
async def test_without_a_key_the_run_is_recorded_without_its_trace(workflows, monkeypatch):
    from apowerb.helpers import encryptor

    monkeypatch.setattr(encryptor, "fernet", None)
    run_id = _graph_run("wf-7")

    async def _runner(_cancel):
        yield _sse({"event": "node_complete", "node_id": "agent1", "output": "secret"})
        yield _sse({"event": "done", "output": "secret"})

    response = workflows._streaming_run(
        run_id=run_id, agent_ids=[], file_bytes=None, owner=OWNER, runner=_runner
    )
    await _drain(response)

    assert run_main.get_run(run_id, owner_id=OWNER)["status"] == "success"
    assert _stored_trace(run_main.run_store, run_id) is None


# --- les routes ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_runs_route_passes_its_filters(workflows):
    failed = _graph_run("wf-1")
    run_main.finish_run(failed, status="error", error_message="boom")
    _graph_run("wf-1")
    _graph_run("wf-2")

    listed = await workflows.list_runs(
        limit=10, offset=0, workflow_id="wf-1", status="error", current_user=_user()
    )

    assert [r["run_id"] for r in listed] == [failed]


@pytest.mark.asyncio
async def test_the_runs_route_refuses_an_unknown_status(workflows):
    with pytest.raises(HTTPException) as excinfo:
        await workflows.list_runs(
            limit=10, offset=0, workflow_id=None, status="nope", current_user=_user()
        )
    assert excinfo.value.status_code == 422


@pytest.mark.asyncio
async def test_the_runs_route_bounds_its_page(workflows):
    with pytest.raises(HTTPException) as excinfo:
        await workflows.list_runs(
            limit=10_000, offset=0, workflow_id=None, status=None, current_user=_user()
        )
    assert excinfo.value.status_code == 422
    with pytest.raises(HTTPException):
        await workflows.list_runs(
            limit=10, offset=-1, workflow_id=None, status=None, current_user=_user()
        )


@pytest.mark.asyncio
async def test_the_trace_route_hides_a_foreign_run(workflows):
    run_id = _graph_run(owner=OTHER)
    run_main.save_trace(run_id, _trace({"event": "done", "output": "secret"}))

    with pytest.raises(HTTPException) as excinfo:
        await workflows.get_run_trace(run_id, current_user=_user())
    assert excinfo.value.status_code == 404

    mine = await workflows.get_run_trace(run_id, current_user=_user(OTHER))
    assert mine["events"][0]["output"] == "secret"


# --- base déjà en service ----------------------------------------------------


def _legacy_runs_table(monkeypatch, rows):
    """Une table ``agent_runs`` d'avant les colonnes ajoutées, avec ces lignes."""
    engine = _sqlite_engine()
    run_store = run_main.run_store
    monkeypatch.setattr(run_store, "engine", engine)
    schema = run_store.run_table.schema
    qualified = f"{schema}.agent_runs" if schema else "agent_runs"
    with engine.begin() as conn:
        conn.execute(
            text(
                # Les colonnes d'origine de la table, avant toutes les ajoutées.
                f"CREATE TABLE {qualified} (run_id VARCHAR PRIMARY KEY, trigger VARCHAR, "
                "owner_id VARCHAR, organization_id VARCHAR, agent_ids VARCHAR, config VARCHAR, "
                "input_file_name VARCHAR, input_file_path VARCHAR, status VARCHAR, "
                "attempts INTEGER, error_message VARCHAR, created_at VARCHAR, "
                "finished_at VARCHAR, replay_of VARCHAR)"
            )
        )
        for i, (run_id, trigger, config) in enumerate(rows):
            conn.execute(
                text(
                    f"INSERT INTO {qualified} (run_id, trigger, owner_id, status, config, created_at) "
                    "VALUES (:id, :trigger, :o, 'error', :c, :at)"
                ),
                {"id": run_id, "trigger": trigger, "o": OWNER, "c": config, "at": f"2026-01-01 00:00:0{i}.000000"},
            )
    return engine, schema


def test_an_existing_runs_table_gets_the_new_columns_and_its_workflows_back(monkeypatch):
    from sqlalchemy import inspect as sa_inspect

    engine, schema = _legacy_runs_table(
        monkeypatch,
        [
            ("old1", "workflow", json.dumps({"workflow_id": "wf-old", "version": 2, "payload": {}})),
            ("old2", "chat", json.dumps({"agent_name": "a"})),
            # Une config illisible qui cite la clé ne bloque pas les autres.
            ("old3", "workflow", '{"workflow_id": pas du json'),
        ],
    )

    run_main.run_store.create_table()

    columns = {c["name"] for c in sa_inspect(engine).get_columns("agent_runs", schema=schema)}
    assert {"workflow_id", "error_info"} <= columns
    assert [r["run_id"] for r in run_main.list_runs(OWNER, workflow_id="wf-old")] == ["old1"]


def test_a_failed_backfill_does_not_undo_the_new_columns(monkeypatch):
    """Le rattrapage tourne après le COMMIT des ALTER : son échec ne les annule pas."""
    from sqlalchemy import inspect as sa_inspect

    engine, schema = _legacy_runs_table(
        monkeypatch, [("old1", "workflow", json.dumps({"workflow_id": "wf-old"}))]
    )

    def _broken(self):
        raise RuntimeError("panne pendant le rattrapage")

    monkeypatch.setattr(type(run_main.run_store), "_backfill_workflow_id", _broken)

    run_main.run_store.create_table()

    columns = {c["name"] for c in sa_inspect(engine).get_columns("agent_runs", schema=schema)}
    assert {"workflow_id", "error_info"} <= columns


# --- qui écoute l'échec -----------------------------------------------------


@pytest.fixture
def trigger_store(monkeypatch):
    from apowerb.core import workflow_main as wm
    from apowerb.core import workflow_triggers as wt

    engine = _sqlite_engine()
    monkeypatch.setattr(wm.workflow_store, "engine", engine)
    wm.workflow_store.metadata.create_all(engine)
    monkeypatch.setattr(wt.workflow_trigger_store, "engine", engine)
    wt.workflow_trigger_store.metadata.create_all(engine)
    return wm, wt


def _listener_graph(source_id, on):
    return {
        "version": 1,
        "nodes": [
            {"id": "start", "type": "trigger", "config": {"kind": "workflow_done", "workflow_id": source_id, "on": on}}
        ],
        "edges": [],
    }


def _publish(wm, wf, owner):
    wm.update_workflow(wf["workflow_id"], owner_id=owner, expected_version=wf["version"], status="published")


def test_failure_listeners_are_the_published_workflows_that_hear_an_error(trigger_store):
    wm, wt = trigger_store
    src = wm.create_workflow(owner_id=OWNER, name="Source", graph={"version": 1, "nodes": [], "edges": []})
    on_error = wm.create_workflow(owner_id=OWNER, name="Alerte", graph=_listener_graph(src["workflow_id"], "error"))
    _publish(wm, on_error, OWNER)
    on_any = wm.create_workflow(owner_id=OWNER, name="Journal", graph=_listener_graph(src["workflow_id"], "any"))
    _publish(wm, on_any, OWNER)
    on_success = wm.create_workflow(owner_id=OWNER, name="Suite", graph=_listener_graph(src["workflow_id"], "success"))
    _publish(wm, on_success, OWNER)
    # Un brouillon n'écoute rien tant qu'il n'est pas publié.
    wm.create_workflow(owner_id=OWNER, name="Brouillon", graph=_listener_graph(src["workflow_id"], "error"))

    listeners = wt.failure_listeners(src["workflow_id"], OWNER)

    assert sorted(item["name"] for item in listeners) == ["Alerte", "Journal"]


def test_failure_listeners_never_cross_owners(trigger_store):
    wm, wt = trigger_store
    src = wm.create_workflow(owner_id=OWNER, name="Source", graph={"version": 1, "nodes": [], "edges": []})
    theirs = wm.create_workflow(owner_id=OTHER, name="Espion", graph=_listener_graph(src["workflow_id"], "error"))
    try:
        _publish(wm, theirs, OTHER)
    except Exception:  # noqa: BLE001 - la validation peut déjà refuser une source étrangère
        pass

    assert wt.failure_listeners(src["workflow_id"], OWNER) == []
