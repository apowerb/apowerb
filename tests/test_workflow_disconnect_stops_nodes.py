"""A client disconnect must stop the workflow, not run the rest in the background.

``drive_workflow`` runs the whole graph in a detached ``asyncio.create_task`` and
relays its events through a queue. When the SSE response is cancelled (the client
disconnected), the generator is closed -- but the detached drive task was never
cancelled, so the remaining nodes kept executing on the server (measured:
``agent1`` 0->2s, ``agent2`` 2->4s, both after the cancel at t=1s). That wastes
compute and fires real side effects (agents, tools, notifications) for a run
nobody is listening to.
"""

import asyncio

from apowerb.core import workflow_graph as wg

_GRAPH = {
    "version": 1,
    "nodes": [
        {"id": "t", "type": "trigger"},
        {"id": "a", "type": "agent", "config": {"agent_id": "agent1"}},
        {"id": "b", "type": "agent", "config": {"agent_id": "agent2"}},
    ],
    "edges": [
        {"source": "t", "target": "a"},
        {"source": "a", "target": "b"},
    ],
}


async def test_client_disconnect_stops_the_remaining_nodes():
    graph = wg.WorkflowGraph.model_validate(_GRAPH)
    started: list[str] = []
    finished: list[str] = []

    async def run_agent(agent_id, message):
        started.append(agent_id)
        await asyncio.sleep(0.3)
        finished.append(agent_id)
        return {"text": "ok"}

    async def run_tool(tool, args):
        return {}

    cancel_event = asyncio.Event()

    async def consume():
        async for _chunk in wg.run_graph(
            graph,
            payload=None,
            run_agent=run_agent,
            run_tool=run_tool,
            cancel_event=cancel_event,
        ):
            pass

    task = asyncio.create_task(consume())

    # Wait until the first node is mid-flight, then simulate the disconnect:
    # Starlette cancels the streaming-response task.
    for _ in range(200):
        if started:
            break
        await asyncio.sleep(0.01)
    assert started == ["agent1"], f"first node did not start: {started}"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    # Give any orphaned background drive task time to run the rest of the graph.
    await asyncio.sleep(0.8)

    assert "agent2" not in started, (
        f"a node ran after the client disconnected: started={started}, finished={finished}"
    )
    assert finished == [], (
        f"the in-flight node completed after disconnect instead of being cancelled: {finished}"
    )
