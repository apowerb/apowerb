"""De bout en bout dans ADK : une conversation mémorise, une AUTRE s'en souvient.

Le vrai ``Runner`` d'ADK, le vrai outil ``load_memory``, le vrai callback posé
par ``to_agent`` et notre service sur SQLite. Seul le modèle est simulé : il
appelle ``load_memory`` quand on lui pose une question, et recopie ce que
l'outil lui a rendu. Ce que l'agent « sait » est donc exactement ce que la
mémoire lui a fourni, pour cet utilisateur et cet agent-là.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncGenerator

import pytest
from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.lite_llm import _safe_json_serialize
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import load_memory
from google.genai import types
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apowerb.memory.service import PersistentMemoryService, with_memory_callback


class _ScriptedLlm(BaseLlm):
    """Question (« ? ») -> appelle load_memory ; réponse d'outil -> la recopie.

    La réponse d'outil est sérialisée par la fonction même que LiteLlm utilise
    pour la transmettre au modèle : le test voit ce que verrait un vrai modèle.
    """

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        last = llm_request.contents[-1].parts[0]
        if last.function_response is not None:
            text = _safe_json_serialize(last.function_response.response)
        elif (last.text or "").startswith("?"):
            yield LlmResponse(content=types.Content(role="model", parts=[
                types.Part(function_call=types.FunctionCall(
                    name="load_memory", args={"query": last.text.lstrip("? ")}
                ))
            ]))
            return
        else:
            text = "noté"
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=text)]))


@pytest.fixture()
async def memory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def factory():
        async with maker() as s:
            yield s

    yield PersistentMemoryService(session_factory=factory, schema=None, retention_days=90)
    await engine.dispose()


def _runner(app_name: str, memory_service) -> Runner:
    agent = LlmAgent(
        name=app_name,
        model=_ScriptedLlm(model="scripted"),
        tools=[load_memory],
        # Exactement ce que to_agent pose quand la case Mémoire est cochée.
        after_agent_callback=with_memory_callback(None),
    )
    return Runner(
        app_name=app_name,
        agent=agent,
        session_service=InMemorySessionService(),
        memory_service=memory_service,
    )


async def _say(runner: Runner, user: str, session: str, text: str) -> str:
    await runner.session_service.create_session(app_name=runner.app_name, user_id=user, session_id=session)
    final = ""
    async for event in runner.run_async(
        user_id=user,
        session_id=session,
        new_message=types.Content(role="user", parts=[types.Part(text=text)]),
    ):
        if event.content and event.content.parts and event.content.parts[0].text:
            final = event.content.parts[0].text
    return final


async def test_une_nouvelle_conversation_retrouve_ce_qui_a_ete_dit_avant(memory):
    runner = _runner("agent1", memory)

    await _say(runner, "alice", "conv-1", "mon code client est 4521")
    reponse = await _say(runner, "alice", "conv-2", "? code client")

    assert "4521" in reponse


async def test_un_autre_utilisateur_du_meme_agent_ne_le_retrouve_pas(memory):
    runner = _runner("agent1", memory)

    await _say(runner, "alice", "conv-1", "mon code client est 4521")
    reponse = await _say(runner, "bob", "conv-2", "? code client")

    assert "4521" not in reponse
    # Pas un vide trompeur : le souvenir existe bel et bien, pour alice.
    assert "4521" in await _say(runner, "alice", "conv-3", "? code client")


async def test_un_autre_agent_du_meme_utilisateur_ne_le_retrouve_pas(memory):
    await _say(_runner("agent1", memory), "alice", "conv-1", "mon code client est 4521")
    reponse = await _say(_runner("agent2", memory), "alice", "conv-2", "? code client")

    assert "4521" not in reponse
    assert "4521" in await _say(_runner("agent1", memory), "alice", "conv-3", "? code client")
