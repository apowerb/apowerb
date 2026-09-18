"""Mémoire entre conversations : ce qu'un agent retient, et pour qui.

Contrat (apowerb/roadmap#82) : la mémoire est rangée par agent ET par
utilisateur, elle survit aux conversations, elle expire à 90 jours et un
utilisateur peut l'effacer. Chaque test pose une valeur reconnaissable et
vérifie qui peut la relire.

SQLite en mémoire : la CI tourne sans Postgres, et le service n'utilise que
du SQL portable.
"""

from __future__ import annotations

import time
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from google.adk.events import Event
from google.adk.sessions import Session
from google.genai import types
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from apowerb.memory.service import (
    PersistentMemoryService,
    memory_after_agent_callback,
    with_memory_callback,
)

DAY = 24 * 3600


@pytest.fixture()
async def service():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    maker = async_sessionmaker(engine, expire_on_commit=False)

    @asynccontextmanager
    async def factory():
        async with maker() as s:
            yield s

    yield PersistentMemoryService(session_factory=factory, schema=None, retention_days=90)
    await engine.dispose()


def _event(text: str, *, author="user", role="user", age_days: float = 0, eid=None, **part):
    return Event(
        id=eid or f"e-{abs(hash((text, age_days)))}",
        invocation_id="inv",
        author=author,
        timestamp=time.time() - age_days * DAY,
        content=types.Content(role=role, parts=[types.Part(text=text, **part)]),
    )


def _session(app, user, *events, sid="s1"):
    return Session(id=sid, app_name=app, user_id=user, events=list(events))


def _texts(resp):
    return [p.text for m in resp.memories for p in m.content.parts]


async def test_retrouve_d_une_conversation_a_l_autre(service):
    await service.add_session_to_memory(
        _session("agent1", "alice", _event("mon numero client est 4521"), sid="s1")
    )

    resp = await service.search_memory(app_name="agent1", user_id="alice", query="numero client")

    assert _texts(resp) == ["mon numero client est 4521"]


async def test_un_autre_utilisateur_du_meme_agent_ne_voit_rien(service):
    await service.add_session_to_memory(_session("agent1", "alice", _event("IBAN FR76 secret")))

    resp = await service.search_memory(app_name="agent1", user_id="bob", query="IBAN")

    assert resp.memories == []


async def test_un_autre_agent_du_meme_utilisateur_ne_voit_rien(service):
    await service.add_session_to_memory(_session("agent1", "alice", _event("regle metier interne")))

    resp = await service.search_memory(app_name="agent2", user_id="alice", query="regle metier")

    assert resp.memories == []


async def test_une_session_ajoutee_a_chaque_tour_ne_se_duplique_pas(service):
    e1 = _event("premier tour budget", eid="e1")
    await service.add_session_to_memory(_session("agent1", "alice", e1))
    e2 = _event("second tour budget", eid="e2")
    await service.add_session_to_memory(_session("agent1", "alice", e1, e2))
    await service.add_session_to_memory(_session("agent1", "alice", e1, e2))

    resp = await service.search_memory(app_name="agent1", user_id="alice", query="budget")

    assert sorted(_texts(resp)) == ["premier tour budget", "second tour budget"]


async def test_au_dela_de_90_jours_le_souvenir_n_est_plus_rendu(service):
    await service.add_session_to_memory(_session(
        "agent1", "alice",
        _event("vieux contrat fournisseur", age_days=91, eid="old"),
        _event("recent contrat fournisseur", age_days=89, eid="new"),
    ))

    resp = await service.search_memory(app_name="agent1", user_id="alice", query="contrat")

    assert _texts(resp) == ["recent contrat fournisseur"]


async def test_la_purge_supprime_seulement_ce_qui_a_expire(service):
    await service.add_session_to_memory(_session(
        "agent1", "alice",
        _event("vieux souvenir", age_days=120, eid="old"),
        _event("souvenir recent", age_days=1, eid="new"),
    ))

    assert await service.purge_expired() == 1
    assert await service.purge_expired() == 0
    assert _texts(await service.search_memory(app_name="agent1", user_id="alice", query="souvenir")) == [
        "souvenir recent"
    ]


async def test_l_effacement_ne_vise_que_cet_utilisateur_et_cet_agent(service):
    await service.add_session_to_memory(_session("agent1", "alice", _event("alice agent1 note", eid="a")))
    await service.add_session_to_memory(_session("agent2", "alice", _event("alice agent2 note", eid="b"), sid="s2"))
    await service.add_session_to_memory(_session("agent1", "bob", _event("bob agent1 note", eid="c"), sid="s3"))

    assert await service.delete_user_memory(app_name="agent1", user_id="alice") == 1

    assert (await service.search_memory(app_name="agent1", user_id="alice", query="note")).memories == []
    assert _texts(await service.search_memory(app_name="agent2", user_id="alice", query="note")) == ["alice agent2 note"]
    assert _texts(await service.search_memory(app_name="agent1", user_id="bob", query="note")) == ["bob agent1 note"]


async def test_seul_le_texte_echange_est_retenu(service):
    pensee = _event("raisonnement interne cache", eid="t", thought=True)
    appel = Event(
        id="f", invocation_id="inv", author="agent1", timestamp=time.time(),
        content=types.Content(role="model", parts=[
            types.Part(function_call=types.FunctionCall(name="tool_sql", args={"q": "cache"}))
        ]),
    )
    partiel = _event("cache fragment en cours", eid="p")
    partiel.partial = True
    reponse = _event("reponse visible cache", author="agent1", role="model", eid="r")

    await service.add_session_to_memory(_session("agent1", "alice", pensee, appel, partiel, reponse))

    resp = await service.search_memory(app_name="agent1", user_id="alice", query="cache")
    assert _texts(resp) == ["reponse visible cache"]
    assert resp.memories[0].author == "agent1"


async def test_un_joker_sql_dans_la_requete_ne_matche_pas_tout(service):
    await service.add_session_to_memory(_session("agent1", "alice", _event("axb sans rapport")))

    resp = await service.search_memory(app_name="agent1", user_id="alice", query="a_b")

    assert resp.memories == []


async def test_une_requete_vide_ne_rend_rien(service):
    await service.add_session_to_memory(_session("agent1", "alice", _event("quelque chose")))

    assert (await service.search_memory(app_name="agent1", user_id="alice", query="  ")).memories == []


async def test_le_declencheur_memorise_la_session_et_ne_modifie_pas_la_reponse():
    appels = []

    async def add():
        appels.append(1)

    assert await memory_after_agent_callback(SimpleNamespace(add_session_to_memory=add)) is None
    assert appels == [1]


async def test_un_echec_de_memorisation_ne_casse_pas_le_run():
    async def add():
        raise RuntimeError("base indisponible")

    assert await memory_after_agent_callback(SimpleNamespace(add_session_to_memory=add)) is None


def test_le_declencheur_passe_avant_un_callback_existant_sans_l_ecraser():
    def existant(callback_context):
        return None

    assert with_memory_callback(None) is memory_after_agent_callback
    assert with_memory_callback(existant) == [memory_after_agent_callback, existant]
    assert with_memory_callback([existant]) == [memory_after_agent_callback, existant]
