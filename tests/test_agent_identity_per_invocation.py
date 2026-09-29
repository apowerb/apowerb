"""Owner-scoped identity must be per-invocation, not a process-global env var.

Before the fix, ``to_agent`` wrote AGENT_OWNER / ORG / PROJECT and the Gemini
key into ``os.environ`` at BUILD time; owner-scoped tools read them at CALL
time. Two customers' agents cached in one worker then crossed identities. These
tests pin the fix: readers prefer the per-task ContextVar, and the Gemini key
travels as an ``api_key`` param, never through ``os.environ``.
"""

from __future__ import annotations

import asyncio
import contextvars
import os

import pytest

from apowerb.core import invocation_context as ic
from apowerb.tools_store.portfolio import bi_datasets
from apowerb.tools_store.portfolio import business_intelligence as bi


@pytest.fixture(autouse=True)
def _reset_identity():
    for var in (
        ic._agent_owner_var,
        ic._agent_org_var,
        ic._agent_project_var,
        ic._root_agent_id_var,
        ic._root_invocation_var,
    ):
        var.set(None)
    yield


def _bind(owner, org, project, agent_id, invocation_id):
    fn = ic.make_identity_before_agent_callback(
        owner=owner, organization_id=org, project_id=project, agent_id=agent_id
    )

    class _Ctx:
        pass

    ctx = _Ctx()
    ctx.invocation_id = invocation_id
    ctx.agent_name = f"agent{agent_id}"
    fn(ctx)


def test_contextvar_wins_over_stale_env(monkeypatch):
    # Another customer's agent was built LAST in this worker (stale env).
    monkeypatch.setenv("AGENT_OWNER", "bob@b.test")
    monkeypatch.setenv("AGENT_ORGANIZATION_ID", "org-b")
    monkeypatch.setenv("AGENT_PROJECT_ID", "pb")
    monkeypatch.setenv("ROOT_AGENT_ID", "2")
    # This invocation runs Alice's agent -> callback binds Alice.
    _bind("alice@a.test", "org-a", "pa", 1, "inv-alice")
    assert bi._agent_owner() == "alice@a.test"
    assert bi._agent_org() == "org-a"
    assert bi._agent_project() == "pa"
    assert bi_datasets._agent_owner() == "alice@a.test"
    assert ic.get_root_agent_id() == "1"


def test_env_fallback_when_no_invocation(monkeypatch):
    # Background/scheduler path: no callback fired -> env still honoured.
    monkeypatch.setenv("AGENT_OWNER", "carol@c.test")
    assert bi._agent_owner() == "carol@c.test"


def test_root_agent_id_kept_from_first_agent_of_invocation():
    _bind("alice@a.test", "org-a", "pa", 1, "inv-1")  # root
    _bind("alice@a.test", "org-a", "pa", 4, "inv-1")  # sub-agent, same run
    assert ic.get_root_agent_id() == "1"
    # A brand new invocation resets the root.
    _bind("alice@a.test", "org-a", "pa", 7, "inv-2")
    assert ic.get_root_agent_id() == "7"


async def test_two_invocations_do_not_cross(monkeypatch):
    monkeypatch.delenv("AGENT_OWNER", raising=False)

    async def run_as(owner, org, agent_id, inv):
        _bind(owner, org, "p", agent_id, inv)
        await asyncio.sleep(0.01)  # let the other task interleave
        return bi._agent_owner(), bi._agent_org()

    (a_owner, a_org), (b_owner, b_org) = await asyncio.gather(
        run_as("alice@a.test", "org-a", 1, "inv-a"),
        run_as("bob@b.test", "org-b", 2, "inv-b"),
    )
    assert (a_owner, a_org) == ("alice@a.test", "org-a")
    assert (b_owner, b_org) == ("bob@b.test", "org-b")


async def test_gemini_key_travels_as_param_not_env(monkeypatch):
    import litellm
    from cryptography.fernet import Fernet
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    from apowerb.core.agent_helpers.llm_model_builder import build_litellm_model
    from apowerb.helpers import encryptor
    from apowerb.helpers.encryptor import encrypt_value

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(encryptor, "fernet", Fernet(Fernet.generate_key()))

    def details(owner, key):
        return {
            "agent_model": "gemini/gemini-2.5-flash",
            "owner_id": owner,
            "agent_model_params": {"model_api_key": encrypt_value(key)},
        }

    alice_model = build_litellm_model(details("alice@a.test", "KEY-ALICE"), temperature=None)
    build_litellm_model(details("bob@b.test", "KEY-BOB"), temperature=None)

    # The leak: os.environ must NOT carry any agent's Gemini key.
    assert os.environ.get("GEMINI_API_KEY") is None

    seen: dict = {}

    async def fake_acompletion(*args, **kwargs):
        seen["api_key"] = kwargs.get("api_key")
        seen["env"] = os.environ.get("GEMINI_API_KEY")
        raise RuntimeError("stop before network")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    request = LlmRequest(
        model="gemini/gemini-2.5-flash",
        contents=[types.Content(role="user", parts=[types.Part(text="hi")])],
    )
    try:
        async for _ in alice_model.generate_content_async(request):
            pass
    except Exception:
        pass

    assert seen.get("api_key") == "KEY-ALICE"
    assert seen.get("env") is None
