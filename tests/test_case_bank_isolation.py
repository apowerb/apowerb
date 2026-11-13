"""Banque de cas : aucun cas ne franchit la frontière d'une organisation ni d'un utilisateur.

Contrat : un cas appartient à (organisation, agent). Un cas tiré d'une
conversation appartient en plus à son utilisateur ; un cas tiré d'un run
planifié est partagé dans l'organisation. Sans organisation posée par
l'invocation, on n'écrit ni ne lit rien : le repli sur ``os.environ`` est
exactement la voie par laquelle un agent a déjà agi au nom d'un autre client.

Chaque test pose une valeur reconnaissable et vérifie qui peut la relire.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from google.adk.models.llm_request import LlmRequest
from google.genai import types
from sqlalchemy import create_engine

from apowerb.core import invocation_context as ic
from apowerb.memory.case_bank import (
    CaseBank,
    case_from_run,
    make_case_bank_before_model_callback,
    record_run_case,
    scope_user_key,
)

AGENT = 7


@pytest.fixture()
def bank(tmp_path):
    # Fichier plutôt que mémoire : les lectures concurrentes passent par des
    # threads, et une connexion SQLite unique ne se partage pas entre eux.
    engine = create_engine(f"sqlite:///{tmp_path / 'cases.db'}")
    b = CaseBank(engine=engine, schema=None)
    b.create_table()
    yield b
    engine.dispose()


def _record(bank, org, summary, *, user=None, outcome=1, agent=AGENT, run_id=None):
    return bank.record(
        organization_id=org,
        agent_id=agent,
        scope_user=scope_user_key(user) if user else None,
        source="chat" if user else "schedule",
        run_id=run_id or f"run-{org}-{summary}",
        task_summary=summary,
        tool_sequence=["tool_lire_facture"],
        outcome=outcome,
    )


def _texts(cases):
    return [c["task_summary"] for c in cases]


# --------------------------------------------------------------------------- #
# Organisations
# --------------------------------------------------------------------------- #


def test_une_organisation_ne_relit_pas_les_cas_d_une_autre(bank):
    assert _record(bank, "org-a", "facture fournisseur ALPHA")
    assert (
        _texts(
            bank.search(
                organization_id="org-b",
                agent_id=AGENT,
                user_key=None,
                query="facture fournisseur",
            )
        )
        == []
    )
    assert _texts(
        bank.search(
            organization_id="org-a",
            agent_id=AGENT,
            user_key=None,
            query="facture fournisseur",
        )
    ) == ["facture fournisseur ALPHA"]


def test_meme_identifiant_d_agent_dans_deux_organisations(bank):
    _record(bank, "org-a", "facture ALPHA")
    _record(bank, "org-b", "facture BRAVO")
    assert _texts(
        bank.search(
            organization_id="org-b", agent_id=AGENT, user_key=None, query="facture"
        )
    ) == ["facture BRAVO"]


def test_un_agent_ne_relit_pas_les_cas_d_un_autre_agent(bank):
    _record(bank, "org-a", "facture ALPHA", agent=8)
    assert (
        bank.search(
            organization_id="org-a", agent_id=AGENT, user_key=None, query="facture"
        )
        == []
    )


# --------------------------------------------------------------------------- #
# Utilisateurs
# --------------------------------------------------------------------------- #


def test_le_cas_de_conversation_d_alice_reste_a_alice(bank):
    _record(bank, "org-a", "conversation ALICE facture", user="alice@a.test")
    alice = scope_user_key("alice@a.test")
    bob = scope_user_key("bob@a.test")
    assert (
        _texts(
            bank.search(
                organization_id="org-a", agent_id=AGENT, user_key=bob, query="facture"
            )
        )
        == []
    )
    assert (
        _texts(
            bank.search(
                organization_id="org-a", agent_id=AGENT, user_key=None, query="facture"
            )
        )
        == []
    )
    assert _texts(
        bank.search(
            organization_id="org-a", agent_id=AGENT, user_key=alice, query="facture"
        )
    ) == ["conversation ALICE facture"]


def test_un_cas_partage_de_l_organisation_est_lu_par_ses_deux_utilisateurs(bank):
    _record(bank, "org-a", "run planifié facture PARTAGE")
    for user in ("alice@a.test", "bob@a.test"):
        got = bank.search(
            organization_id="org-a",
            agent_id=AGENT,
            user_key=scope_user_key(user),
            query="facture",
        )
        assert _texts(got) == ["run planifié facture PARTAGE"]


def test_la_cle_utilisateur_ne_revele_pas_l_adresse(bank):
    key = scope_user_key("alice@a.test")
    assert "alice" not in key and "@" not in key and len(key) == 16


# --------------------------------------------------------------------------- #
# Organisation non posée
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("org", [None, "", "  "])
def test_sans_organisation_on_n_ecrit_rien(bank, org):
    assert _record(bank, org, "facture ORPHELINE") is False
    assert bank.count() == 0


@pytest.mark.parametrize("org", [None, ""])
def test_sans_organisation_on_ne_lit_rien(bank, org):
    _record(bank, "org-a", "facture ALPHA")
    assert (
        bank.search(organization_id=org, agent_id=AGENT, user_key=None, query="facture")
        == []
    )


# --------------------------------------------------------------------------- #
# Écriture depuis un run : l'organisation vient de l'agent en base, jamais de l'env
# --------------------------------------------------------------------------- #


def _resolver(org, owner=None):
    def resolve(ref):
        return {
            "agent_id": AGENT,
            "organization_id": org,
            "owner_id": owner,
            "case_bank_enabled": True,
        }

    return resolve


def _run(trigger="schedule", status="success", owner="alice@a.test", **extra):
    run = {
        "run_id": "r1",
        "trigger": trigger,
        "owner_id": owner,
        "organization_id": None,
        "agent_ids": ["7"],
        "config": {
            "agent_name": "agent7",
            "new_message": {"role": "user", "parts": [{"text": "trier les factures"}]},
        },
        "status": status,
        "error_message": None,
        "tools_executed": ["tool_lire_facture"],
    }
    run.update(extra)
    return run


def test_l_organisation_du_cas_vient_de_l_agent_en_base_pas_de_l_environnement(
    bank, monkeypatch
):
    monkeypatch.setenv("AGENT_ORGANIZATION_ID", "org-b")
    resolve = _resolver("org-a", owner="alice@a.test")
    assert record_run_case(_run(), bank=bank, resolve_agent=resolve)
    assert (
        bank.search(
            organization_id="org-b", agent_id=AGENT, user_key=None, query="factures"
        )
        == []
    )
    assert (
        len(
            bank.search(
                organization_id="org-a", agent_id=AGENT, user_key=None, query="factures"
            )
        )
        == 1
    )


def test_agent_sans_organisation_en_base_aucun_cas(bank, monkeypatch):
    monkeypatch.setenv("AGENT_ORGANIZATION_ID", "org-a")
    resolve = _resolver(None)
    assert record_run_case(_run(), bank=bank, resolve_agent=resolve) is False
    assert bank.count() == 0


def test_le_cas_d_une_conversation_est_range_chez_son_utilisateur(bank):
    case = case_from_run(_run(trigger="chat", owner="alice@a.test"))
    assert case["scope_user"] == scope_user_key("alice@a.test")
    assert case_from_run(_run(trigger="schedule"))["scope_user"] is None


def test_le_message_d_erreur_n_est_jamais_stocke(bank):
    case = case_from_run(
        _run(status="error", error_message="ValueError: IBAN FR76 1234 de Mme Durand")
    )
    assert case["outcome"] == 0
    assert case["failure_kind"] == "ValueError"
    assert "IBAN" not in str(case) and "Durand" not in str(case)


# --------------------------------------------------------------------------- #
# Lecture dans l'agent : seule la ContextVar fait foi, même sous concurrence
# --------------------------------------------------------------------------- #


def _request(text: str) -> LlmRequest:
    return LlmRequest(
        contents=[types.Content(role="user", parts=[types.Part(text=text)])]
    )


def _ctx(invocation_id="inv-1"):
    return SimpleNamespace(invocation_id=invocation_id, state={})


def _injected(req: LlmRequest) -> str:
    return "\n".join(p.text or "" for c in req.contents for p in (c.parts or []))


async def test_organisation_seulement_dans_l_environnement_aucune_injection(
    bank, monkeypatch
):
    _record(bank, "org-a", "facture ALPHA")
    monkeypatch.setenv("AGENT_ORGANIZATION_ID", "org-a")
    monkeypatch.setenv("ROOT_AGENT_ID", str(AGENT))

    async def run():
        # Tâche neuve : aucune ContextVar posée, seul l'environnement l'est.
        cb = make_case_bank_before_model_callback(agent_id=AGENT, bank=bank)
        req = _request("facture du mois")
        await cb(callback_context=_ctx(), llm_request=req)
        return _injected(req)

    text = await asyncio.create_task(run(), context=__import__("contextvars").Context())
    assert "ALPHA" not in text


async def test_deux_invocations_concurrentes_ne_se_melangent_pas(bank):
    _record(bank, "org-a", "facture ALPHA")
    _record(bank, "org-b", "facture BRAVO")
    cb = make_case_bank_before_model_callback(agent_id=AGENT, bank=bank)

    async def invoke(org, user, inv):
        ic.bind_agent_identity(
            owner=user,
            organization_id=org,
            project_id="p",
            agent_id=str(AGENT),
            invocation_id=inv,
        )
        ic.set_current_invoker(user)
        await asyncio.sleep(0)  # laisse l'autre invocation poser sa propre identité
        req = _request("facture du mois")
        await cb(callback_context=_ctx(inv), llm_request=req)
        return _injected(req)

    a, b = await asyncio.gather(
        asyncio.create_task(invoke("org-a", "alice@a.test", "inv-a")),
        asyncio.create_task(invoke("org-b", "bob@b.test", "inv-b")),
    )
    assert "ALPHA" in a and "BRAVO" not in a
    assert "BRAVO" in b and "ALPHA" not in b


# --------------------------------------------------------------------------- #
# Revue : un run planifié par quelqu'un d'autre que le propriétaire de l'agent
# --------------------------------------------------------------------------- #


def test_un_run_planifie_par_un_autre_que_le_proprietaire_ne_devient_pas_un_cas_partage(
    bank,
):
    # L'agent appartient à org-a (propriétaire alice) ; bob, d'ailleurs, le planifie.
    resolve = _resolver("org-a", owner="alice@a.test")
    assert (
        record_run_case(_run(owner="bob@b.test"), bank=bank, resolve_agent=resolve)
        is False
    )
    assert bank.count() == 0
    assert record_run_case(_run(owner="alice@a.test"), bank=bank, resolve_agent=resolve)


async def test_le_cache_d_invocation_ne_sert_pas_une_autre_organisation(bank):
    _record(bank, "org-a", "facture ALPHA")
    cb = make_case_bank_before_model_callback(agent_id=AGENT, bank=bank)

    async def invoke(org):
        ic.bind_agent_identity(
            owner="x",
            organization_id=org,
            project_id="p",
            agent_id=str(AGENT),
            invocation_id="meme-id",
        )
        req = _request("facture du mois")
        await cb(callback_context=_ctx("meme-id"), llm_request=req)
        return _injected(req)

    assert "ALPHA" in await asyncio.create_task(invoke("org-a"))
    assert "ALPHA" not in await asyncio.create_task(invoke("org-b"))
