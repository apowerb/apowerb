"""What a failed workflow run tells the user.

Every error event carries a stable ``code`` the interface translates, the
``detail`` as an English fallback, and -- when the cause stays in the logs --
one ``ref``. One failure is one reference: the node and the run must point to
the same log line, and it is logged once.
"""

import asyncio
import json
import logging

import aiohttp
import pytest
from aiohttp import web

from apowerb.core import workflow_graph as wg
from apowerb.core.adk_runner import AdkRunError, run_adk_agent
from apowerb.core.workflow_engine import WorkflowUserError


def _graph(agent_ok=True):
    return wg.WorkflowGraph.model_validate(
        {
            "version": 1,
            "nodes": [
                {"id": "t", "type": "trigger"},
                {"id": "a", "type": "agent", "config": {"agent_id": "agent1"}},
            ],
            "edges": [{"source": "t", "target": "a"}],
        }
    )


def _run(graph, run_agent):
    async def run_tool(tool, args):
        return {}

    async def run_rag(agent_id, query, top_k):
        return {"query": query, "passages": []}

    async def _go():
        out = []
        async for chunk in wg.run_graph(
            graph,
            payload=None,
            run_agent=run_agent,
            run_tool=run_tool,
            run_rag=run_rag,
            cancel_event=asyncio.Event(),
        ):
            out.append(json.loads(chunk[len("data: ") :]))
        return out

    return asyncio.run(_go())


def _failing(exc):
    async def run_agent(agent_id, message):
        raise exc

    return run_agent


def _node_error(events):
    return next(e for e in events if e["event"] == "node_error")


def test_one_failure_is_one_reference_logged_once(caplog):
    caplog.set_level(logging.ERROR)
    events = _run(_graph(), _failing(RuntimeError("socket closed by 10.0.0.5")))
    node, final = _node_error(events), events[-1]
    assert final["event"] == "error"
    assert node["code"] == final["code"] == "internal"
    assert node["ref"] and node["ref"] == final["ref"]
    logged = [r for r in caplog.records if node["ref"] in r.getMessage()]
    assert len(logged) == 1
    # The raw exception stays in the logs.
    assert "10.0.0.5" not in json.dumps(events)


def test_error_details_are_english_fallbacks_not_french_sentences():
    events = _run(_graph(), _failing(RuntimeError("x")))
    assert "Erreur" not in events[-1]["detail"]
    assert events[-1]["ref"] in events[-1]["detail"]


def test_a_provider_refusal_from_run_keeps_its_category_and_ref():
    exc = AdkRunError(
        502,
        code="model_provider_auth",
        detail="The model provider rejected the credentials of this agent's model.",
        ref="abcd1234",
    )
    events = _run(_graph(), _failing(exc))
    node, final = _node_error(events), events[-1]
    assert node["code"] == final["code"] == "model_provider_auth"
    assert node["ref"] == final["ref"] == "abcd1234"


def test_our_own_errors_are_shown_as_written():
    events = _run(_graph(), _failing(WorkflowUserError("agent inconnu : agent1")))
    assert events[-1]["code"] == "workflow_error"
    assert events[-1]["detail"] == "agent inconnu : agent1"
    assert "ref" not in events[-1]


def test_an_invalid_graph_has_its_own_code():
    graph = wg.WorkflowGraph.model_validate(
        {
            "version": 1,
            "nodes": [{"id": "a", "type": "agent", "config": {}}],
            "edges": [],
        }
    )
    events = _run(graph, _failing(RuntimeError("never")))
    assert events == [events[-1]] and events[-1]["code"] == "invalid_graph"


# --- run_adk_agent reads the error /run sends back -------------------------


async def _serve(status, body):
    async def handler(request):
        return web.json_response(body, status=status)

    app = web.Application()
    app.router.add_post("/run", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


def _call(status, body):
    async def _go():
        runner, base = await _serve(status, body)
        try:
            await run_adk_agent(
                "a", "u", "s", {"role": "user", "parts": []}, base_url=base
            )
        finally:
            await runner.cleanup()

    asyncio.run(_go())


def test_run_adk_agent_raises_the_category_sent_by_run():
    body = {
        "detail": "The model provider rejected...",
        "code": "model_provider_auth",
        "ref": "abcd1234",
    }
    with pytest.raises(AdkRunError) as info:
        _call(502, body)
    assert info.value.status == 502
    assert info.value.code == "model_provider_auth"
    assert info.value.ref == "abcd1234"
    # Existing callers catch aiohttp.ClientError: still true.
    assert isinstance(info.value, aiohttp.ClientError)


def test_run_adk_agent_without_a_category_stays_a_client_error():
    with pytest.raises(AdkRunError) as info:
        _call(500, {"detail": "Internal Server Error"})
    assert info.value.status == 500 and info.value.code is None
