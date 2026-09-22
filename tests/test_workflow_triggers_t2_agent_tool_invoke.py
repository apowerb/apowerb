"""``core.workflow_triggers.call_agent_tool`` — invocation SYNCHRONE bornée.

Couvre la garde anti-récursion (``next_trigger_chain``), le timeout et le
mappage des 3 issues du run déclenché (``done``/``cancelled``/``error``) vers
le dict de retour. ``launch_triggered_run`` est remplacé par un double : la
véritable exécution d'un graphe est hors périmètre de ce fichier.
"""

import asyncio

from apowerb.core import workflow_triggers as wt

ALICE = "alice@acme.fr"


class _ImmediateRecorder:
    """``launch_triggered_run`` dont la tâche renvoyée est déjà terminée."""

    def __init__(self, terminal: dict, run_id: str = "run-1"):
        self.terminal = terminal
        self.run_id = run_id
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

        async def _done():
            return self.terminal

        return self.run_id, asyncio.create_task(_done())


async def test_returns_output_on_done(monkeypatch):
    rec = _ImmediateRecorder({"event": "done", "output": {"sum": 3}})
    monkeypatch.setattr(wt, "launch_triggered_run", rec)

    result = await wt.call_agent_tool(
        workflow_id="wf-1",
        owner_id=ALICE,
        tool_name="lookup_order",
        arguments={"order_id": "42"},
        prior_chain=[],
    )

    assert result == {"run_id": "run-1", "output": {"sum": 3}}
    assert rec.calls[0]["kind"] == "agent_tool"
    assert rec.calls[0]["detail"] == {"tool_name": "lookup_order", "chain": ["wf-1"]}
    assert rec.calls[0]["payload"] == {"order_id": "42"}


async def test_returns_error_key_on_run_error(monkeypatch):
    rec = _ImmediateRecorder({"event": "error", "detail": "boom"})
    monkeypatch.setattr(wt, "launch_triggered_run", rec)

    result = await wt.call_agent_tool(
        workflow_id="wf-1",
        owner_id=ALICE,
        tool_name="t",
        arguments={},
        prior_chain=[],
    )

    assert result == {"error": "run_failed", "run_id": "run-1", "detail": "boom"}


async def test_returns_error_key_on_cancelled(monkeypatch):
    rec = _ImmediateRecorder({"event": "cancelled"})
    monkeypatch.setattr(wt, "launch_triggered_run", rec)

    result = await wt.call_agent_tool(
        workflow_id="wf-1", owner_id=ALICE, tool_name="t", arguments={}, prior_chain=[]
    )

    assert result == {"error": "cancelled", "run_id": "run-1"}


async def test_recursion_beyond_max_depth_never_launches_a_run(monkeypatch):
    rec = _ImmediateRecorder({"event": "done", "output": None})
    monkeypatch.setattr(wt, "launch_triggered_run", rec)
    prior_chain = [f"w{i}" for i in range(wt.MAX_TRIGGER_CHAIN)]

    result = await wt.call_agent_tool(
        workflow_id="wf-new",
        owner_id=ALICE,
        tool_name="t",
        arguments={},
        prior_chain=prior_chain,
    )

    assert result["error"] == "recursion_blocked"
    assert rec.calls == []


async def test_self_call_cycle_never_launches_a_run(monkeypatch):
    rec = _ImmediateRecorder({"event": "done", "output": None})
    monkeypatch.setattr(wt, "launch_triggered_run", rec)

    # wf-1 apparaît déjà dans sa propre chaîne : le workflow s'appelle
    # lui-même via son outil -> refusé, pas seulement plafonné.
    result = await wt.call_agent_tool(
        workflow_id="wf-1",
        owner_id=ALICE,
        tool_name="t",
        arguments={},
        prior_chain=["wf-1"],
    )

    assert result["error"] == "recursion_blocked"
    assert rec.calls == []


async def test_unpublished_workflow_returns_not_active(monkeypatch):
    async def _raises(**kwargs):
        raise wt.TriggerNotActive(kwargs["workflow_id"])

    monkeypatch.setattr(wt, "launch_triggered_run", _raises)

    result = await wt.call_agent_tool(
        workflow_id="wf-1", owner_id=ALICE, tool_name="t", arguments={}, prior_chain=[]
    )

    assert result == {"error": "not_active", "detail": "ce workflow n'est plus publié"}


async def test_timeout_returns_timeout_key_without_hanging(monkeypatch):
    async def _slow(**kwargs):
        async def _never():
            await asyncio.sleep(3600)

        return "run-slow", asyncio.create_task(_never())

    monkeypatch.setattr(wt, "launch_triggered_run", _slow)
    monkeypatch.setattr(wt, "AGENT_TOOL_TIMEOUT_SECONDS", 0.05)

    result = await wt.call_agent_tool(
        workflow_id="wf-1", owner_id=ALICE, tool_name="t", arguments={}, prior_chain=[]
    )

    assert result["error"] == "timeout"
    assert result["run_id"] == "run-slow"
