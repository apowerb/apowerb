"""Tests de la capture des DRIVERS par le usage_recorder :
``invocation_id`` (qui relie les tours d'une meme requete) et
``tool_names`` (les outils demandes par le tour).

Sans ces deux champs, une boucle multi-tours est indiscernable de N
appels one-shot -- or c'est precisement la boucle qui brule les tokens.

Pas de DB : sessionmanager est monkeypatche par des faux en memoire,
comme dans test_usage_recorder.py.
"""
from __future__ import annotations

import asyncio

import pytest

from apowerb.core.agent_helpers.usage_recorder import (
    _extract_tool_names,
    create_usage_recorder_callback,
)

USAGE = {
    "prompt_token_count": 100,
    "candidates_token_count": 10,
    "thoughts_token_count": 0,
    "cached_content_token_count": 0,
    "total_token_count": 110,
}


class FakeSession:
    def __init__(self, session_id="sess-1"):
        self.id = session_id


class FakeCallbackContext:
    def __init__(self, invocation_id="inv-1", session_id="sess-1"):
        self.session = FakeSession(session_id)
        self.invocation_id = invocation_id


class FakeCallbackContextWithoutInvocationId:
    """Un contexte ADK plus ancien / partiel : l'acces doit degrader en
    None, jamais lever -- une panne de comptabilite ne doit pas casser la
    reponse de l'agent."""

    def __init__(self):
        self.session = FakeSession()

    @property
    def invocation_id(self):
        raise AttributeError("no invocation_id here")


class FakeFunctionCall:
    def __init__(self, name):
        self.name = name


class FakePart:
    def __init__(self, function_call=None):
        self.function_call = function_call


class FakeContent:
    def __init__(self, parts):
        self.parts = parts


class FakeLlmResponse:
    def __init__(self, usage_metadata=None, partial=None, parts=None):
        self.usage_metadata = usage_metadata
        self.partial = partial
        self.content = FakeContent(parts) if parts is not None else None


class FakeAsyncSessionCM:
    def __init__(self, store):
        self.store = store

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, obj):
        self.store.append(obj)

    async def commit(self):
        return None


class FakeSessionManager:
    def __init__(self):
        self.store = []

    def session(self):
        return FakeAsyncSessionCM(self.store)


async def _run_and_wait(coro):
    import apowerb.core.agent_helpers.usage_recorder as ur_module

    before = set(ur_module._pending_writes)
    await coro
    new_tasks = set(ur_module._pending_writes) - before
    if new_tasks:
        await asyncio.gather(*new_tasks, return_exceptions=True)


@pytest.fixture
def fake_db(monkeypatch):
    import apowerb.helpers.database as db_module

    manager = FakeSessionManager()
    monkeypatch.setattr(db_module, "sessionmanager", manager)
    return manager


# ---------------------------------------------------------------------------
# _extract_tool_names -- fonction pure
# ---------------------------------------------------------------------------


def test_extract_tool_names_returns_none_on_a_plain_text_turn():
    assert _extract_tool_names(FakeLlmResponse(parts=[FakePart()])) is None


def test_extract_tool_names_returns_none_when_there_is_no_content():
    assert _extract_tool_names(FakeLlmResponse()) is None


def test_extract_tool_names_joins_calls_in_order():
    resp = FakeLlmResponse(
        parts=[
            FakePart(FakeFunctionCall("rag_search")),
            FakePart(FakeFunctionCall("get_order")),
        ]
    )
    assert _extract_tool_names(resp) == "rag_search,get_order"


def test_extract_tool_names_dedupes_repeated_calls():
    """Un tour qui appelle deux fois le meme outil ne doit pas le compter
    deux fois -- sinon l'attribution par outil est faussee."""
    resp = FakeLlmResponse(
        parts=[
            FakePart(FakeFunctionCall("rag_search")),
            FakePart(FakeFunctionCall("rag_search")),
        ]
    )
    assert _extract_tool_names(resp) == "rag_search"


def test_extract_tool_names_drops_a_name_containing_the_separator():
    """Les noms d'outils sont des identifiants de fonction ([A-Za-z0-9_-]),
    donc une virgule ne peut pas s'y trouver. Si ca arrivait quand meme,
    mieux vaut perdre l'outil que le couper en deux outils fantomes qui
    fausseraient l'attribution."""
    resp = FakeLlmResponse(
        parts=[
            FakePart(FakeFunctionCall("ok_tool")),
            FakePart(FakeFunctionCall("mechant,outil")),
        ]
    )
    assert _extract_tool_names(resp) == "ok_tool"


def test_extract_tool_names_never_raises_on_a_malformed_response():
    class Exploding:
        @property
        def content(self):
            raise RuntimeError("boom")

    assert _extract_tool_names(Exploding()) is None


# ---------------------------------------------------------------------------
# Persistance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recorded_row_carries_invocation_id_and_tools(fake_db):
    cb = create_usage_recorder_callback(
        agent_id=7, agent_name="Analyste AR", owner_id="alice@example.com",
        model_name="gemini/gemini-2.5-flash",
    )
    resp = FakeLlmResponse(
        usage_metadata=USAGE, parts=[FakePart(FakeFunctionCall("rag_search"))]
    )

    await _run_and_wait(
        cb(callback_context=FakeCallbackContext(invocation_id="inv-42"),
           llm_response=resp)
    )

    assert len(fake_db.store) == 1
    row = fake_db.store[0]
    assert row.invocation_id == "inv-42"
    assert row.tool_names == "rag_search"
    # Le nom metier, pas l'appName ADK.
    assert row.agent_name == "Analyste AR"


@pytest.mark.asyncio
async def test_two_turns_of_one_request_share_the_invocation_id(fake_db):
    """C'est ce partage qui rend la boucle multi-tours mesurable."""
    cb = create_usage_recorder_callback(
        agent_id=7, agent_name="A", owner_id="alice@example.com", model_name="m",
    )
    ctx = FakeCallbackContext(invocation_id="inv-99")

    await _run_and_wait(
        cb(callback_context=ctx,
           llm_response=FakeLlmResponse(
               usage_metadata=USAGE,
               parts=[FakePart(FakeFunctionCall("rag_search"))]))
    )
    await _run_and_wait(
        cb(callback_context=ctx,
           llm_response=FakeLlmResponse(usage_metadata=USAGE, parts=[FakePart()]))
    )

    assert [r.invocation_id for r in fake_db.store] == ["inv-99", "inv-99"]
    assert [r.tool_names for r in fake_db.store] == ["rag_search", None]


@pytest.mark.asyncio
async def test_missing_invocation_id_degrades_to_none_without_raising(fake_db):
    cb = create_usage_recorder_callback(
        agent_id=7, agent_name="A", owner_id="alice@example.com", model_name="m",
    )

    await _run_and_wait(
        cb(callback_context=FakeCallbackContextWithoutInvocationId(),
           llm_response=FakeLlmResponse(usage_metadata=USAGE))
    )

    assert len(fake_db.store) == 1
    assert fake_db.store[0].invocation_id is None


@pytest.mark.asyncio
async def test_partial_streaming_chunk_still_records_nothing(fake_db):
    """Garde de streaming inchangee : un chunk intermediaire ne doit pas
    creer de ligne, meme s'il porte des function calls."""
    cb = create_usage_recorder_callback(
        agent_id=7, agent_name="A", owner_id="alice@example.com", model_name="m",
    )

    await _run_and_wait(
        cb(callback_context=FakeCallbackContext(),
           llm_response=FakeLlmResponse(
               usage_metadata=USAGE, partial=True,
               parts=[FakePart(FakeFunctionCall("rag_search"))]))
    )

    assert fake_db.store == []
