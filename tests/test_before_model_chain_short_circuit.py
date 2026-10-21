"""Un garde-fou qui bloque doit bloquer à travers toute la chaîne de ``to_agent``.

``to_agent`` enveloppe le before_model_callback des garde-fous dans la
troncature de l'historique puis le retrait des gros payloads. Si un wrapper
jette la valeur rendue par le callback qu'il enveloppe, le ``LlmResponse`` du
garde-fou est perdu et le modèle est appelé quand même.

Vraie ``to_agent`` (ligne d'agent fixe, sans base), vrai ``Runner`` ADK,
modèle factice qui compte ses appels.
"""
import asyncio
import json

from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

OWNER = "guard@example.com"


def _build_agent(monkeypatch):
    from apowerb.core.agent_helpers import agent_utils, extras_loader

    row = {
        "agent_id": 4242, "agent_name": "Guarded", "agent_model": "gemini/gemini-2.5-flash",
        "agent_model_params": "{}", "agent_description": "", "agent_instruction": "Réponds.",
        "agent_tools": "[]", "agent_type": "base", "sub_agents": "[]", "owner_id": OWNER,
        "organization_id": "test", "project_id": "test", "memory_enabled": "false",
        "artifacts_enabled": "false", "mcp_servers": None,
        "guardrails_config": json.dumps({"blocked_terms": ["interdit"]}),
    }
    monkeypatch.setattr(extras_loader, "_owner_category_params", lambda *a, **k: {})
    monkeypatch.setattr(agent_utils, "get_agent_details", lambda agent_id, **_: dict(row))
    return agent_utils.to_agent("agent4242")


class _CountingLlm(BaseLlm):
    calls: int = 0

    async def generate_content_async(self, llm_request, stream=False):
        self.calls += 1
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text="réponse du modèle")]))


def _run(agent, text):
    async def go():
        svc = InMemorySessionService()
        await svc.create_session(app_name="t", user_id=OWNER, session_id="s")
        runner = Runner(app_name="t", agent=agent, session_service=svc)
        msg = types.Content(role="user", parts=[types.Part(text=text)])
        out = []
        async for ev in runner.run_async(user_id=OWNER, session_id="s", new_message=msg):
            if ev.content and ev.content.parts:
                out += [p.text for p in ev.content.parts if p.text]
        return " ".join(out)

    return asyncio.run(go())


def test_blocked_term_short_circuits_the_model_through_the_whole_chain(monkeypatch):
    agent = _build_agent(monkeypatch)
    agent.model = llm = _CountingLlm(model="fake")

    text = _run(agent, "un mot interdit ici")

    assert llm.calls == 0
    assert "not allowed by the guardrail policy" in text


def test_allowed_message_still_reaches_the_model(monkeypatch):
    agent = _build_agent(monkeypatch)
    agent.model = llm = _CountingLlm(model="fake")

    text = _run(agent, "bonjour")

    assert llm.calls == 1
    assert "réponse du modèle" in text
