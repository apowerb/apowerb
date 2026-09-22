"""Un run déclenché reçoit les mêmes branchements qu'un run manuel.

``launch_triggered_run`` est remplacé par un double dans les tests webhook et
schedule : ici on l'exécute pour de bon, avec le vrai ``bindings_for``, afin
qu'un changement de sa forme de retour (rag, notification, sous-workflow)
ne casse pas silencieusement tous les triggers.
"""

import asyncio

from apowerb.core import run_gate, run_main, workflow_graph, workflow_main
from apowerb.core import workflow_triggers as wt
from apowerb.routers import workflows as workflows_router

GRAPH = {
    "version": 1,
    "nodes": [
        {"id": "t", "type": "trigger", "config": {"kind": "webhook"}},
        {"id": "o", "type": "output", "config": {"value": "{{t.x}}"}},
    ],
    "edges": [{"source": "t", "target": "o"}],
}


def test_a_triggered_run_gets_rag_notify_and_subworkflow_bindings(monkeypatch):
    seen = {}

    monkeypatch.setattr(
        workflow_main,
        "get_workflow",
        lambda wid, owner_id: {
            "status": "published",
            "graph": GRAPH,
            "version": 3,
        },
    )

    async def plan(owner):
        return None

    monkeypatch.setattr(run_gate, "resolve_owner_plan", plan)
    monkeypatch.setattr(run_main, "start_run", lambda **kw: "run-1")

    def fake_run_graph(graph, **kwargs):
        seen.update(kwargs)
        return iter(())

    monkeypatch.setattr(workflow_graph, "run_graph", fake_run_graph)

    class _Resp:
        async def _empty(self):
            return
            yield

        def __init__(self):
            self.body_iterator = self._empty()

    def fake_streaming_run(*, runner, **kw):
        runner(asyncio.Event())
        return _Resp()

    monkeypatch.setattr(workflows_router, "_streaming_run", fake_streaming_run)

    async def go():
        run_id, task = await wt.launch_triggered_run(
            workflow_id="wf1",
            owner_id="alice@example.com",
            kind="webhook",
            detail={},
            payload={"x": 1},
        )
        await task
        return run_id

    assert asyncio.run(go()) == "run-1"
    assert callable(seen["run_agent"]) and callable(seen["run_tool"])
    assert callable(seen["run_rag"])
    assert callable(seen["run_notify"])
    assert callable(seen["run_subworkflow"])
    assert seen["workflow_id"] == "wf1"
    assert seen["payload"] == {"x": 1}
