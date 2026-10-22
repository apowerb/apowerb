"""« Reprendre ici » : relancer un run en échec à partir du nœud fautif.

Banc SQLite réel (même montage que ``test_workflow_decision_routes``) et vrai
``run_graph`` : ce qui est vérifié, c'est l'instantané en base, le flux du
nouveau run (amont ``replayed``, jamais réexécuté) et les refus.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from apowerb.core import run_gate, run_main, workflow_main, workflow_runtime
from apowerb.core import workflow_triggers as wt

OWNER = "u@example.com"
STRANGER = "z@example.com"


def _graph(a2_prompt="b:{{a1}}"):
    return {
        "version": 1,
        "nodes": [
            {"id": "t", "type": "trigger", "config": {"kind": "manual"}},
            {"id": "a1", "type": "agent", "config": {"agent_id": "amont", "input": "a:{{t.x}}"}},
            {"id": "a2", "type": "agent", "config": {"agent_id": "fautif", "input": a2_prompt}},
            {"id": "o", "type": "output", "config": {"value": "fin:{{a2}}"}},
        ],
        "edges": [
            {"source": "t", "target": "a1"},
            {"source": "a1", "target": "a2"},
            {"source": "a2", "target": "o"},
        ],
    }


def _sqlite_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection, _record):  # pragma: no cover
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS public")

    return engine


@pytest.fixture
def api(monkeypatch, tmp_path):
    engine = _sqlite_engine()
    for store in (run_main.run_store, workflow_main.workflow_store, wt.workflow_trigger_store):
        monkeypatch.setattr(store, "engine", engine)
        store.metadata.create_all(engine)
    monkeypatch.setattr(run_main, "_run_input_dir", lambda run_id: tmp_path / run_id)

    async def _no_plan(owner):
        return None

    monkeypatch.setattr(run_gate, "resolve_owner_plan", _no_plan)

    async def _notify(**kwargs):
        pass

    monkeypatch.setattr(wt, "notify_run_finished", _notify)
    # Compteur d'appels par agent ; ``fail`` : les agents qui lèvent.
    calls: dict[str, list] = {}
    fail = {"fautif"}
    # ``block[agent_id] = (entré, porte)`` : l'agent signale son entrée puis
    # attend la porte (pour arrêter un run au milieu d'un nœud).
    block: dict[str, tuple] = {}

    async def _agent(agent_id, message):
        calls.setdefault(agent_id, []).append(message)
        if agent_id in block:
            entered, gate = block[agent_id]
            entered.set()
            await gate.wait()
        if agent_id in fail:
            raise RuntimeError(f"{agent_id} en panne")
        return f"{agent_id}-ok"

    async def _tool(tool, args):
        return {}

    monkeypatch.setattr(
        workflow_runtime, "bindings_for", lambda owner, plan: (_agent, _tool, None, None)
    )
    from apowerb.routers import workflow_defs, workflows

    workflows._runs.clear()
    return SimpleNamespace(
        defs=workflow_defs, runs=workflows, calls=calls, fail=fail, block=block
    )


def _user(email=OWNER):
    return SimpleNamespace(email=email, user_id=1, role="USER")


async def _drain(response):
    chunks = [chunk async for chunk in response.body_iterator]
    text = b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks).decode()
    return [json.loads(b[len("data: "):]) for b in text.split("\n\n") if b.startswith("data: ")]


async def _failed(api, graph=None, **body):
    wf = workflow_main.create_workflow(owner_id=OWNER, name="Devis", graph=graph or _graph())
    events = await _drain(
        await api.defs.run_workflow(
            wf["workflow_id"],
            api.defs.RunBody(payload={"x": "devis"}, **body),
            current_user=_user(),
        )
    )
    return wf, events


async def _retry(api, run_id, user=OWNER):
    return await api.runs.retry_from_failure(run_id, current_user=_user(user))


async def _refused(api, run_id, user=OWNER) -> HTTPException:
    with pytest.raises(HTTPException) as caught:
        await _retry(api, run_id, user)
    return caught.value


def _row(run_id):
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        return conn.execute(t.select().where(t.c.run_id == run_id)).fetchone()._asdict()


def _old(delta):
    return (datetime.now() - delta - timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S.%f")


async def _settled(api):
    """Attend les reprises détachées encore en cours."""
    await asyncio.gather(*list(api.runs._detached_runs), return_exceptions=True)


async def _blocked_retry(api, run_id):
    """Une reprise arrêtée dans le nœud en échec ; rend (flux, run de reprise, porte)."""
    entered, gate = asyncio.Event(), asyncio.Event()
    api.block["fautif"] = (entered, gate)
    api.fail.clear()
    stream = (await _retry(api, run_id)).body_iterator
    first = await stream.__anext__()
    first = first.decode() if isinstance(first, bytes) else first
    retry_id = json.loads(first[len("data: "):])["wid"]
    await asyncio.wait_for(entered.wait(), 5)
    return stream, retry_id, gate


async def _rest(stream):
    return [c async for c in stream]


def _set(run_id, **values):
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        conn.execute(t.update().where(t.c.run_id == run_id).values(**values))


# --- L'échec garde de quoi reprendre --------------------------------------


@pytest.mark.asyncio
async def test_a_failed_run_says_where_it_failed_and_that_it_can_resume_there(api):
    _, events = await _failed(api)
    last = events[-1]
    assert last["event"] == "error"
    assert last["failed_node"] == "a2" and last["retryable"] is True
    run = run_main.get_run(events[0]["wid"], owner_id=OWNER)
    assert run["status"] == "error"
    assert run["failure_node"] == "a2" and run["retry_available"] is True
    # L'instantané (sorties du run) ne sort jamais par l'API des runs.
    assert "failure_state" not in run
    assert "failure_state" not in run_main.list_runs(OWNER)[0]


@pytest.mark.asyncio
async def test_the_saved_state_is_sealed_at_rest(api):
    _, events = await _failed(api)
    raw = _row(events[0]["wid"])["failure_state"]
    assert raw and "amont-ok" not in raw and not raw.lstrip().startswith("{")


@pytest.mark.asyncio
async def test_a_successful_run_keeps_no_state(api):
    api.fail.clear()
    _, events = await _failed(api)
    assert events[-1]["event"] == "done"
    row = _row(events[0]["wid"])
    assert row["failure_state"] is None and row["failure_node"] is None


@pytest.mark.asyncio
async def test_a_cancelled_run_keeps_no_state(api):
    from apowerb.core import workflow_graph
    import asyncio

    cancel = asyncio.Event()
    cancel.set()
    saved = []
    out = [
        json.loads(c[len("data: "):])
        async for c in workflow_graph.run_graph(
            workflow_graph.WorkflowGraph.model_validate(_graph()),
            payload={"x": 1},
            run_agent=None,
            run_tool=None,
            cancel_event=cancel,
            on_fail=saved.append,
        )
    ]
    assert out[-1]["event"] == "cancelled" and saved == []


# --- La reprise : amont rejoué, jamais réexécuté ---------------------------


@pytest.mark.asyncio
async def test_resuming_replays_upstream_without_running_it_and_reruns_the_failed_node(api):
    _, events = await _failed(api)
    original = events[0]["wid"]
    assert len(api.calls["amont"]) == 1 and len(api.calls["fautif"]) == 1
    api.fail.clear()
    resumed = await _drain(await _retry(api, original))
    new_run = resumed[0]["wid"]
    assert new_run != original
    assert resumed[1] == {"event": "retry_started", "replay_of": original, "node_id": "a2"}
    assert resumed[-1] == {"event": "done", "output": "fin:fautif-ok"}
    # Contre-exemple discriminant : l'amont n'a pas été rappelé.
    assert len(api.calls["amont"]) == 1
    assert len(api.calls["fautif"]) == 2
    # Le nœud en échec reçoit la sortie rejouée de l'amont.
    assert api.calls["fautif"][-1] == "b:amont-ok"
    starts = {e["node_id"]: e for e in resumed if e["event"] == "node_start"}
    assert starts["t"].get("replayed") is True and starts["a1"].get("replayed") is True
    assert not starts["a2"].get("replayed") and not starts["o"].get("replayed")
    # Un nouveau run, lié ; l'original reste tel quel.
    run = run_main.get_run(new_run, owner_id=OWNER)
    assert run["replay_of"] == original and run["status"] == "success"
    assert run["config"]["retry_from"] == "a2"
    assert run_main.get_run(original, owner_id=OWNER)["status"] == "error"
    assert _row(new_run)["failure_state"] is None


@pytest.mark.asyncio
async def test_a_plain_replay_of_a_resumption_runs_everything_again(api):
    _, events = await _failed(api)
    api.fail.clear()
    resumed = await _drain(await _retry(api, events[0]["wid"]))
    replay = run_main.prepare_replay(resumed[0]["wid"], owner_id=OWNER, force=True)
    assert "retry_from" not in replay["config"]


@pytest.mark.asyncio
async def test_a_resumption_that_fails_again_can_be_resumed_in_turn(api):
    _, events = await _failed(api)
    again = await _drain(await _retry(api, events[0]["wid"]))
    assert again[-1]["event"] == "error" and again[-1]["retryable"] is True
    api.fail.clear()
    third = await _drain(await _retry(api, again[0]["wid"]))
    assert third[-1]["event"] == "done"
    assert len(api.calls["amont"]) == 1 and len(api.calls["fautif"]) == 3


@pytest.mark.asyncio
async def test_a_test_up_to_here_run_resumes_up_to_the_same_node(api):
    _, events = await _failed(api, until="a2")
    api.fail.clear()
    resumed = await _drain(await _retry(api, events[0]["wid"]))
    assert resumed[-1]["partial"] is True and resumed[-1]["until"] == "a2"
    assert not [e for e in resumed if e.get("node_id") == "o"]


@pytest.mark.asyncio
async def test_a_decision_answered_before_the_failure_is_replayed_not_asked_again(api):
    graph = _graph()
    graph["nodes"].insert(
        2,
        {
            "id": "d",
            "type": "approval",
            "config": {"question": "OK ?", "mode": "choices", "choices": [{"route": "oui", "label": "Oui"}]},
        },
    )
    graph["edges"] = [
        {"source": "t", "target": "a1"},
        {"source": "a1", "target": "d"},
        {"source": "d", "target": "a2", "route": "oui"},
        {"source": "a2", "target": "o"},
    ]
    _, events = await _failed(api, graph=graph)
    assert events[-1]["event"] == "awaiting_input"
    run_id = events[0]["wid"]
    after = await _drain(
        await api.runs.resume_run(run_id, api.runs.DecisionAnswer(choice="oui"), current_user=_user())
    )
    assert after[-1]["event"] == "error" and after[-1]["failed_node"] == "a2"
    api.fail.clear()
    resumed = await _drain(await _retry(api, run_id))
    assert resumed[-1]["event"] == "done"
    assert not [e for e in resumed if e["event"] == "awaiting_input"]
    d_start = next(e for e in resumed if e["event"] == "node_start" and e["node_id"] == "d")
    assert d_start["replayed"] is True
    assert len(api.calls["amont"]) == 1


# --- Version rejouée : la dernière sauvegarde, amont inchangé -------------


@pytest.mark.asyncio
async def test_the_failed_node_can_be_fixed_before_resuming(api):
    wf, events = await _failed(api)
    fixed = workflow_main.update_workflow(
        wf["workflow_id"], owner_id=OWNER, expected_version=wf["version"], graph=_graph("corrigé:{{a1}}")
    )
    api.fail.clear()
    resumed = await _drain(await _retry(api, events[0]["wid"]))
    assert resumed[-1]["event"] == "done"
    assert api.calls["fautif"][-1] == "corrigé:amont-ok"
    run = run_main.get_run(resumed[0]["wid"], owner_id=OWNER)
    assert run["config"]["version"] == fixed["version"]


@pytest.mark.asyncio
async def test_an_upstream_node_changed_since_the_failure_is_refused(api):
    wf, events = await _failed(api)
    graph = _graph()
    graph["nodes"][1]["config"]["input"] = "autre:{{t.x}}"
    workflow_main.update_workflow(
        wf["workflow_id"], owner_id=OWNER, expected_version=wf["version"], graph=graph
    )
    refused = await _refused(api, events[0]["wid"])
    assert refused.status_code == 409
    assert refused.detail["code"] == "retry_graph_changed"
    assert refused.detail["params"]["nodes"] == ["a1"]
    assert len(api.calls["amont"]) == 1
    # Refusé sans rien consommer : rétablir l'amont rend la reprise possible.
    assert run_main.get_run(events[0]["wid"], owner_id=OWNER)["retry_available"] is True


@pytest.mark.asyncio
async def test_a_new_input_wired_into_a_replayed_node_is_refused(api):
    wf, events = await _failed(api)
    graph = _graph()
    graph["nodes"].append({"id": "n", "type": "agent", "config": {"agent_id": "autre"}})
    graph["edges"].append({"source": "t", "target": "n"})
    graph["edges"].append({"source": "n", "target": "a1"})
    workflow_main.update_workflow(
        wf["workflow_id"], owner_id=OWNER, expected_version=wf["version"], graph=graph
    )
    refused = await _refused(api, events[0]["wid"])
    assert refused.detail["code"] == "retry_graph_changed"
    assert "a1" in refused.detail["params"]["nodes"]


@pytest.mark.asyncio
async def test_a_failed_node_deleted_since_the_failure_is_refused(api):
    wf, events = await _failed(api)
    graph = _graph()
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] != "a2"]
    graph["nodes"][-1]["config"]["value"] = "fin:{{a1}}"
    graph["edges"] = [{"source": "t", "target": "a1"}, {"source": "a1", "target": "o"}]
    workflow_main.update_workflow(
        wf["workflow_id"], owner_id=OWNER, expected_version=wf["version"], graph=graph
    )
    refused = await _refused(api, events[0]["wid"])
    assert refused.status_code == 409
    assert refused.detail["code"] == "retry_graph_changed"
    assert refused.detail["params"]["nodes"] == ["a2"]
    assert run_main.get_run(events[0]["wid"], owner_id=OWNER)["retry_available"] is True


@pytest.mark.asyncio
async def test_a_deleted_workflow_cannot_be_resumed(api):
    wf, events = await _failed(api)
    workflow_main.delete_workflow(wf["workflow_id"], owner_id=OWNER)
    refused = await _refused(api, events[0]["wid"])
    assert refused.status_code == 409 and refused.detail["code"] == "retry_workflow_missing"


# --- Droits et gardes --------------------------------------------------------


@pytest.mark.asyncio
async def test_another_user_gets_404(api):
    _, events = await _failed(api)
    refused = await _refused(api, events[0]["wid"], user=STRANGER)
    assert refused.status_code == 404
    assert run_main.get_run(events[0]["wid"], owner_id=OWNER)["retry_available"] is True


@pytest.mark.asyncio
async def test_an_unknown_run_gets_404(api):
    assert (await _refused(api, "nope")).status_code == 404


@pytest.mark.asyncio
async def test_a_failure_is_resumed_only_once(api):
    _, events = await _failed(api)
    first = await _retry(api, events[0]["wid"])
    refused = await _refused(api, events[0]["wid"])
    assert refused.status_code == 409
    assert refused.detail["code"] == "retry_already_started"
    started = await _drain(first)
    assert refused.detail["params"]["run_id"] == started[0]["wid"]


@pytest.mark.asyncio
async def test_a_run_that_did_not_fail_is_refused(api):
    api.fail.clear()
    _, events = await _failed(api)
    refused = await _refused(api, events[0]["wid"])
    assert refused.status_code == 409 and refused.detail["code"] == "retry_not_failed"


@pytest.mark.asyncio
async def test_an_expired_state_gives_a_clear_code(api):
    _, events = await _failed(api)
    run_id = events[0]["wid"]
    old = (datetime.now() - run_main.RETRY_STATE_TTL - timedelta(minutes=1)).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )
    _set(run_id, finished_at=old)
    assert run_main.get_run(run_id, owner_id=OWNER)["retry_available"] is False
    # Purgé à la lecture : l'instantané ne reste pas en base au-delà.
    assert _row(run_id)["failure_state"] is None
    refused = await _refused(api, run_id)
    assert refused.status_code == 409 and refused.detail["code"] == "retry_expired"
    assert len(api.calls["amont"]) == 1


@pytest.mark.asyncio
async def test_a_new_failure_purges_the_states_nobody_opened(api):
    _, events = await _failed(api)
    old_run = events[0]["wid"]
    old = (datetime.now() - run_main.RETRY_STATE_TTL - timedelta(minutes=1)).strftime(
        "%Y-%m-%d %H:%M:%S.%f"
    )
    _set(old_run, finished_at=old)
    _, again = await _failed(api)
    assert _row(old_run)["failure_state"] is None
    assert _row(again[0]["wid"])["failure_state"] is not None


@pytest.mark.asyncio
async def test_an_unreadable_state_gives_a_clear_code_and_is_dropped(api):
    _, events = await _failed(api)
    run_id = events[0]["wid"]
    _set(run_id, failure_state="gAAAAAinvalide")
    refused = await _refused(api, run_id)
    assert refused.status_code == 409 and refused.detail["code"] == "retry_state_unreadable"
    assert _row(run_id)["failure_state"] is None
    assert len(api.calls["amont"]) == 1


@pytest.mark.asyncio
async def test_a_run_without_state_is_refused(api):
    _, events = await _failed(api)
    run_id = events[0]["wid"]
    _set(run_id, failure_state=None, failure_node=None)
    refused = await _refused(api, run_id)
    assert refused.status_code == 409 and refused.detail["code"] == "retry_unavailable"


# --- Une reprise qui n'aboutit pas rend l'échec à l'original ------------------


@pytest.mark.asyncio
async def test_stopping_a_resumption_mid_node_gives_the_failure_back(api):
    _, events = await _failed(api)
    original = events[0]["wid"]
    stream, retry_id, gate = await _blocked_retry(api, original)
    await api.runs.cancel_workflow(retry_id, current_user=_user())
    await _rest(stream)
    await _settled(api)
    assert run_main.get_run(retry_id, owner_id=OWNER)["status"] == "cancelled"
    assert _row(retry_id)["failure_state"] is None
    assert run_main.get_run(original, owner_id=OWNER)["retry_available"] is True
    # La sonde de la revue : on peut reprendre encore, sans relancer l'amont.
    del api.block["fautif"]
    again = await _drain(await _retry(api, original))
    assert again[-1]["event"] == "done"
    assert len(api.calls["amont"]) == 1


@pytest.mark.asyncio
async def test_stopping_a_resumption_between_nodes_gives_the_failure_back(api):
    _, events = await _failed(api)
    original = events[0]["wid"]
    stream, retry_id, gate = await _blocked_retry(api, original)
    # Seul le signal : le nœud finit, l'arrêt est vu avant le suivant.
    api.runs._runs[retry_id]["cancel_event"].set()
    gate.set()
    rest = await _rest(stream)
    await _settled(api)
    assert b'"cancelled"' in b"".join(c if isinstance(c, bytes) else c.encode() for c in rest)
    assert run_main.get_run(retry_id, owner_id=OWNER)["status"] == "cancelled"
    assert run_main.get_run(original, owner_id=OWNER)["retry_available"] is True


@pytest.mark.asyncio
async def test_a_client_leaving_does_not_stop_a_resumption(api):
    _, events = await _failed(api)
    original = events[0]["wid"]
    stream, retry_id, gate = await _blocked_retry(api, original)
    await stream.aclose()
    gate.set()
    await _settled(api)
    assert run_main.get_run(retry_id, owner_id=OWNER)["status"] == "success"
    assert run_main.get_run(original, owner_id=OWNER)["retry_available"] is False


@pytest.mark.asyncio
async def test_a_successful_resumption_does_not_give_the_failure_back(api):
    _, events = await _failed(api)
    api.fail.clear()
    await _drain(await _retry(api, events[0]["wid"]))
    refused = await _refused(api, events[0]["wid"])
    assert refused.detail["code"] == "retry_already_started"


@pytest.mark.asyncio
async def test_a_resumption_cut_off_gives_the_failure_back_and_keeps_no_stale_state(api):
    _, events = await _failed(api)
    original = events[0]["wid"]
    retry = run_main.prepare_retry(original, owner_id=OWNER)
    child = retry["run_id"]
    # Tant qu'elle tourne, la reprise porte la copie mais ne se reprend pas.
    assert _row(child)["failure_state"] is not None
    assert run_main.get_run(child, owner_id=OWNER)["retry_available"] is False
    _set(child, created_at=_old(run_main.RUN_STALE_AFTER))
    assert run_main.get_run(child, owner_id=OWNER)["status"] == "error"
    assert _row(child)["failure_state"] is None
    assert run_main.get_run(original, owner_id=OWNER)["retry_available"] is True


@pytest.mark.asyncio
async def test_a_resumption_that_cannot_start_stays_resumable(api, monkeypatch):
    _, events = await _failed(api)

    def _down(owner, plan):
        raise RuntimeError("plan indisponible")

    monkeypatch.setattr(workflow_runtime, "bindings_for", _down)
    out = await _drain(await _retry(api, events[0]["wid"]))
    await _settled(api)
    assert out[-1]["event"] == "error"
    assert out[-1]["failed_node"] == "a2" and out[-1]["retryable"] is True
    assert run_main.get_run(out[0]["wid"], owner_id=OWNER)["retry_available"] is True


# --- Concurrence et refus ----------------------------------------------------


@pytest.mark.asyncio
async def test_the_loser_of_two_simultaneous_resumptions_hears_already_started(api, monkeypatch):
    _, events = await _failed(api)
    original = events[0]["wid"]
    real = workflow_main.check_workflow
    rival: dict = {}

    def _check(graph, **kwargs):
        # L'autre reprise passe entre la lecture et la consommation.
        if not rival:
            rival["retry"] = None
            rival["retry"] = run_main.prepare_retry(original, owner_id=OWNER)
        return real(graph, **kwargs)

    monkeypatch.setattr(workflow_main, "check_workflow", _check)
    with pytest.raises(HTTPException) as caught:
        run_main.prepare_retry(original, owner_id=OWNER)
    assert caught.value.detail["code"] == "retry_already_started"
    assert caught.value.detail["params"]["run_id"] == rival["retry"]["run_id"]
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        children = conn.execute(t.select().where(t.c.replay_of == original)).fetchall()
    assert len(children) == 1


@pytest.mark.asyncio
async def test_a_failure_taken_before_its_new_run_exists_says_already_started(api):
    _, events = await _failed(api)
    run_id = events[0]["wid"]
    _set(run_id, failure_state=None)
    refused = await _refused(api, run_id)
    assert refused.detail["code"] == "retry_already_started"


@pytest.mark.asyncio
async def test_a_test_up_to_here_bound_deleted_since_the_failure_is_refused(api):
    wf, events = await _failed(api, until="o")
    graph = _graph()
    graph["nodes"] = [n for n in graph["nodes"] if n["id"] != "o"]
    graph["edges"] = [e for e in graph["edges"] if e["target"] != "o"]
    workflow_main.update_workflow(
        wf["workflow_id"], owner_id=OWNER, expected_version=wf["version"], graph=graph
    )
    refused = await _refused(api, events[0]["wid"])
    assert refused.detail["code"] == "retry_graph_changed"
    assert refused.detail["params"]["nodes"] == ["o"]


# --- Runs déclenchés et rejeux ------------------------------------------------


async def _triggered(api):
    wf = workflow_main.create_workflow(owner_id=OWNER, name="Devis", graph=_graph())
    wf = workflow_main.update_workflow(
        wf["workflow_id"], owner_id=OWNER, expected_version=wf["version"], status="published"
    )
    run_id, task = await wt.launch_triggered_run(
        workflow_id=wf["workflow_id"], owner_id=OWNER, kind="webhook", detail={}, payload={"x": "devis"}
    )
    await task
    return wf, run_id


@pytest.mark.asyncio
async def test_a_failed_triggered_run_can_be_resumed(api):
    _, run_id = await _triggered(api)
    assert run_main.get_run(run_id, owner_id=OWNER)["retry_available"] is True


@pytest.mark.asyncio
async def test_a_triggered_run_is_resumed_on_its_published_version_not_a_draft(api):
    wf, run_id = await _triggered(api)
    workflow_main.update_workflow(
        wf["workflow_id"],
        owner_id=OWNER,
        expected_version=wf["version"],
        graph=_graph("brouillon:{{a1}}"),
        status="draft",
    )
    api.fail.clear()
    resumed = await _drain(await _retry(api, run_id))
    assert resumed[-1]["event"] == "done"
    assert api.calls["fautif"][-1] == "b:amont-ok"
    run = run_main.get_run(resumed[0]["wid"], owner_id=OWNER)
    assert run["config"]["version"] == wf["version"]


@pytest.mark.asyncio
async def test_a_triggered_run_is_resumed_on_a_fix_published_since(api):
    wf, run_id = await _triggered(api)
    fixed = workflow_main.update_workflow(
        wf["workflow_id"], owner_id=OWNER, expected_version=wf["version"], graph=_graph("corrigé:{{a1}}")
    )
    api.fail.clear()
    resumed = await _drain(await _retry(api, run_id))
    assert api.calls["fautif"][-1] == "corrigé:amont-ok"
    assert run_main.get_run(resumed[0]["wid"], owner_id=OWNER)["config"]["version"] == fixed["version"]


@pytest.mark.asyncio
async def test_a_failed_replay_can_be_resumed(api):
    _, events = await _failed(api)
    out = await _drain(await api.runs.replay_run(events[0]["wid"], current_user=_user()))
    assert out[-1]["event"] == "error" and out[-1]["retryable"] is True
    assert run_main.get_run(out[0]["wid"], owner_id=OWNER)["retry_available"] is True


# --- Moteur : la sauvegarde ne casse jamais l'erreur ------------------------


@pytest.mark.asyncio
async def test_a_failing_saver_still_ends_in_error_but_not_retryable(api):
    from apowerb.core import workflow_graph
    import asyncio

    async def _agent(agent_id, message):
        raise RuntimeError("panne")

    def _saver(snapshot):
        raise OSError("base indisponible")

    out = [
        json.loads(c[len("data: "):])
        async for c in workflow_graph.run_graph(
            workflow_graph.WorkflowGraph.model_validate(_graph()),
            payload={"x": 1},
            run_agent=_agent,
            run_tool=None,
            cancel_event=asyncio.Event(),
            on_fail=_saver,
        )
    ]
    assert out[-1]["event"] == "error"
    assert out[-1]["failed_node"] == "a1" and out[-1]["retryable"] is False


@pytest.mark.asyncio
async def test_an_oversized_state_is_not_saved(api, monkeypatch):
    from apowerb.core import workflow_graph

    monkeypatch.setattr(workflow_graph, "MAX_PAUSED_STATE_BYTES", 10)
    _, events = await _failed(api)
    assert events[-1]["event"] == "error" and events[-1]["retryable"] is False
    assert run_main.get_run(events[0]["wid"], owner_id=OWNER)["retry_available"] is False
