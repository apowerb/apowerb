"""The BI tools' sync-to-async bridge keeps the invocation's identity.

From a running event loop, ``_run_async`` runs the coroutine in a new thread.
A new thread starts with an empty context, so the agent identity bound per
invocation (ContextVar, #398) was lost there: ``tool_create_chart`` found no
root agent, so no database connection, and saved a SQL chart without one,
which then failed to render with ``HTTP 422 ... has no valid data source``.
"""

from __future__ import annotations

import asyncio

import pytest

from apowerb.core import invocation_context as ic
from apowerb.tools_store.portfolio import bi_datasets, business_intelligence


async def _identity():
    return ic.get_root_agent_id(), ic.get_agent_owner()


@pytest.mark.parametrize("module", [business_intelligence, bi_datasets], ids=["bi", "bi_datasets"])
def test_run_async_from_a_running_loop_sees_the_bound_identity(module, monkeypatch):
    monkeypatch.delenv("ROOT_AGENT_ID", raising=False)

    async def tool_call():
        ic.bind_agent_identity(owner="owner@example.com", organization_id=None,
                               project_id=None, agent_id="42", invocation_id="inv-1")
        # A sync ADK tool called while the agent's loop is running.
        return module._run_async(_identity())

    assert asyncio.run(tool_call()) == ("42", "owner@example.com")


@pytest.mark.parametrize("module", [business_intelligence, bi_datasets], ids=["bi", "bi_datasets"])
def test_run_async_does_not_leak_the_identity_back(module, monkeypatch):
    monkeypatch.delenv("ROOT_AGENT_ID", raising=False)

    async def set_inside():
        ic.bind_agent_identity(owner=None, organization_id=None, project_id=None,
                               agent_id="99", invocation_id="inv-2")

    async def tool_call():
        module._run_async(set_inside())
        return ic.get_root_agent_id()

    assert asyncio.run(tool_call()) == ""
