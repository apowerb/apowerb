"""Un nœud outil agit au nom du PROPRIÉTAIRE du workflow, sans agent.

Les outils mail (Outlook, Gmail), MCP, BI, S3, Odoo… lisent l'identité dans
``invocation_context`` (invoker, propriétaire de l'agent, agent racine). Hors
agent, rien ne la liait : l'outil retombait sur les variables d'environnement
du processus (``AGENT_OWNER``, ``ROOT_AGENT_ID``) — l'identité d'un autre
compte, ou aucune. ``bindings_for(...).run_tool`` la lie désormais pour
chaque appel, y compris pour un run déclenché en arrière-plan.
"""

import asyncio

from apowerb.core import invocation_context as ic
from apowerb.core import workflow_runtime as rt

ALICE = "alice@exemple.test"


def _who():
    return {
        "invoker": ic.resolve_integration_user(prefer_invoker=True),
        "owner": ic.get_agent_owner(),
        "root": ic.get_root_agent_id(),
    }


def test_tool_node_sees_the_workflow_owner_not_the_process_env(monkeypatch):
    monkeypatch.setenv("AGENT_OWNER", "autre-client@exemple.test")
    monkeypatch.setenv("ROOT_AGENT_ID", "999")
    monkeypatch.setattr(rt, "resolve_tool", lambda ref, owner: _who)
    _agent, run_tool, _rag, _notify = rt.bindings_for(ALICE, None)

    seen = asyncio.run(run_tool("x.who", {}))

    assert seen == {"invoker": ALICE, "owner": ALICE, "root": ""}


def test_binding_does_not_leak_out_of_the_tool_call(monkeypatch):
    monkeypatch.setenv("AGENT_OWNER", "env@exemple.test")
    monkeypatch.setattr(rt, "resolve_tool", lambda ref, owner: _who)
    _agent, run_tool, _rag, _notify = rt.bindings_for(ALICE, None)

    async def go():
        await run_tool("x.who", {})
        return ic.get_agent_owner()

    assert asyncio.run(go()) == "env@exemple.test"
