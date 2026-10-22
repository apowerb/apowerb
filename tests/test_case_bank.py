"""Banque de cas : ce qui s'écrit après un run, ce que l'agent relit, et combien.

L'isolation est dans ``test_case_bank_isolation.py`` ; ici, le comportement :
K cas au plus dont 2 échecs, au premier appel de l'agent racine seulement,
plafond par agent, et l'écriture branchée sur l'issue réelle du run.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from google.adk.models.llm_request import LlmRequest
from google.genai import types
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from apowerb.auth.dependencies import get_current_user
from apowerb.core import invocation_context as ic
from apowerb.core.agent_helpers.agent_utils import to_agent
from apowerb.memory import case_bank as cb
from apowerb.memory.case_bank import (
    CaseBank,
    make_case_bank_before_model_callback,
    scope_user_key,
)

AGENT = 7
ORG = "org-a"


@pytest.fixture()
def bank():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    b = CaseBank(engine=engine, schema=None, max_cases=5)
    b.create_table()
    yield b
    engine.dispose()


def _rec(bank, summary, outcome=1, run_id=None, user=None, tools=("tool_a",)):
    return bank.record(
        organization_id=ORG,
        agent_id=AGENT,
        scope_user=scope_user_key(user) if user else None,
        source="schedule",
        run_id=run_id or summary,
        task_summary=summary,
        tool_sequence=list(tools),
        outcome=outcome,
        failure_kind=None if outcome else "ValueError",
    )


def test_k_cas_au_plus_dont_deux_echecs(bank):
    # Les échecs, plus récents, passeraient tous devant : on n'en garde que deux.
    for i in range(3):
        _rec(bank, f"facture reussie {i}")
    for i in range(3):
        _rec(bank, f"facture echec {i}", outcome=0)
    got = bank.search(
        organization_id=ORG, agent_id=AGENT, user_key=None, query="facture", k=4
    )
    assert len(got) == 4
    assert sum(1 for c in got if not c["outcome"]) == 2


def test_les_cas_les_plus_proches_d_abord(bank):
    _rec(bank, "relance client impayé")
    _rec(bank, "facture fournisseur électricité")
    got = bank.search(
        organization_id=ORG,
        agent_id=AGENT,
        user_key=None,
        query="facture électricité mars",
        k=1,
    )
    assert got[0]["task_summary"] == "facture fournisseur électricité"


def test_plafond_par_agent_garde_les_plus_recents(bank):
    for i in range(8):
        _rec(bank, f"tache {i}")
    assert bank.count() == 5
    kept = {
        c["task_summary"]
        for c in bank.search(
            organization_id=ORG, agent_id=AGENT, user_key=None, query="tache", k=10
        )
    }
    assert kept == {f"tache {i}" for i in range(3, 8)}


def test_un_run_n_est_range_qu_une_fois(bank):
    assert _rec(bank, "facture", run_id="r1")
    assert _rec(bank, "facture", run_id="r1") is False
    assert bank.count() == 1


def test_la_meme_tache_avec_la_meme_issue_n_est_gardee_qu_une_fois(bank):
    _rec(bank, "facture", run_id="r1", tools=("ancien",))
    _rec(bank, "facture", run_id="r2", tools=("nouveau",))
    got = bank.search(
        organization_id=ORG, agent_id=AGENT, user_key=None, query="facture"
    )
    assert [c["tool_sequence"] for c in got] == [["nouveau"]]


def test_effacer_ses_cas_ne_touche_ni_ceux_des_autres_ni_les_partages(bank):
    _rec(bank, "facture alice", user="alice@a.test")
    _rec(bank, "facture bob", user="bob@a.test")
    _rec(bank, "facture partagée")
    assert bank.delete_user_cases(agent_id=AGENT, user="alice@a.test") == 1
    assert bank.count() == 2


# --------------------------------------------------------------------------- #
# Le callback
# --------------------------------------------------------------------------- #


def _bind(agent_id=str(AGENT), inv="inv-1"):
    ic.bind_agent_identity(
        owner="alice@a.test",
        organization_id=ORG,
        project_id="p",
        agent_id=agent_id,
        invocation_id=inv,
    )
    ic.set_current_invoker("alice@a.test")


def _text(req):
    return "\n".join(p.text or "" for c in req.contents for p in (c.parts or []))


async def test_les_cas_sont_inseres_juste_avant_le_message(bank):
    _rec(bank, "facture fournisseur")
    _rec(bank, "facture rejetée", outcome=0)
    _bind()
    req = LlmRequest(
        contents=[
            types.Content(role="user", parts=[types.Part(text="bonjour")]),
            types.Content(role="model", parts=[types.Part(text="salut")]),
            types.Content(role="user", parts=[types.Part(text="la facture du mois")]),
        ]
    )
    await make_case_bank_before_model_callback(agent_id=AGENT, bank=bank)(
        callback_context=None, llm_request=req
    )
    assert len(req.contents) == 4
    block = req.contents[2].parts[0].text
    assert block.startswith(cb.HEADER)
    assert "Succeeded:" in block and "Failed:" in block and "ValueError" in block
    assert req.contents[3].parts[0].text == "la facture du mois"


async def test_apres_un_outil_les_memes_cas_sans_relire_la_base(bank):
    # La requête est reconstruite depuis la session à chaque appel : sans
    # remontrer le bloc, le modèle l'a oublié dès l'outil suivant (banc).
    _rec(bank, "facture fournisseur")
    _bind(inv="inv-tool")
    searches = []
    original = bank.search
    bank.search = lambda **kw: searches.append(kw) or original(**kw)
    cb_ = make_case_bank_before_model_callback(agent_id=AGENT, bank=bank)
    ctx = SimpleNamespace(invocation_id="inv-tool")
    opening = types.Content(role="user", parts=[types.Part(text="la facture")])

    first = LlmRequest(contents=[opening])
    await cb_(callback_context=ctx, llm_request=first)
    after_tool = LlmRequest(
        contents=[
            opening,
            types.Content(
                role="model",
                parts=[
                    types.Part(function_call=types.FunctionCall(name="tool_a", args={}))
                ],
            ),
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="tool_a", response={}
                        )
                    )
                ],
            ),
        ]
    )
    await cb_(callback_context=ctx, llm_request=after_tool)

    assert len(searches) == 1
    assert len(after_tool.contents) == 4
    assert after_tool.contents[0].parts[0].text == first.contents[0].parts[0].text
    assert "facture fournisseur" in after_tool.contents[0].parts[0].text
    assert after_tool.contents[1] is opening


async def test_sans_message_d_ouverture_rien_n_est_insere(bank):
    _rec(bank, "facture fournisseur")
    _bind()
    req = LlmRequest(
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part(
                        function_response=types.FunctionResponse(
                            name="tool_a", response={}
                        )
                    )
                ],
            ),
        ]
    )
    await make_case_bank_before_model_callback(agent_id=AGENT, bank=bank)(
        callback_context=None, llm_request=req
    )
    assert len(req.contents) == 1


async def test_un_sous_agent_ne_relit_pas_les_cas(bank):
    _rec(bank, "facture fournisseur")
    _bind(agent_id="99")  # la racine de l'invocation est un autre agent
    req = LlmRequest(
        contents=[types.Content(role="user", parts=[types.Part(text="la facture")])]
    )
    await make_case_bank_before_model_callback(agent_id=AGENT, bank=bank)(
        callback_context=None, llm_request=req
    )
    assert "facture fournisseur" not in _text(req)


async def test_une_banque_en_panne_ne_casse_pas_l_appel():
    class Broken:
        def search(self, **_):
            raise RuntimeError("db down")

    _bind()
    req = LlmRequest(
        contents=[types.Content(role="user", parts=[types.Part(text="la facture")])]
    )
    assert (
        await make_case_bank_before_model_callback(agent_id=AGENT, bank=Broken())(
            callback_context=None, llm_request=req
        )
        is None
    )
    assert len(req.contents) == 1


# --------------------------------------------------------------------------- #
# Branchements (to_agent lit Postgres : on vérifie la pose, comme pour la mémoire)
# --------------------------------------------------------------------------- #


def test_to_agent_pose_le_callback_seulement_si_l_agent_l_active_et_en_dernier():
    src = inspect.getsource(to_agent)
    flag = src.index('agent_details.get("case_bank_enabled")')
    pose = src.index("make_case_bank_before_model_callback(agent_id=agent_id)")
    assigned = src.index('agent_kwargs["before_model_callback"] = before_model_cb')
    assert flag < pose < assigned
    assert (
        "[*before_model_cb, _case_cb]" in src and "[before_model_cb, _case_cb]" in src
    )


def test_settle_run_range_le_cas_apres_avoir_consigne_l_issue(monkeypatch):
    from apowerb.core import run_main

    calls = []
    monkeypatch.setattr(run_main, "finish_run", lambda *a, **k: calls.append("finish"))
    monkeypatch.setattr(
        run_main, "record_tools_executed", lambda *a, **k: calls.append("tools")
    )
    monkeypatch.setattr(
        run_main, "_learn_case", lambda run_id: calls.append(("learn", run_id))
    )
    run_main.settle_run("r1", run_main.STATUS_SUCCESS, None, [])
    assert calls == ["finish", "tools", ("learn", "r1")]


def test_issue_non_consignee_aucun_cas(monkeypatch):
    from apowerb.core import run_main

    learned = []

    def boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(run_main, "finish_run", boom)
    monkeypatch.setattr(run_main, "_learn_case", lambda run_id: learned.append(run_id))
    run_main.settle_run("r1", run_main.STATUS_SUCCESS, None, [])
    assert learned == []


def test_l_effacement_de_la_memoire_efface_aussi_les_cas_de_l_appelant(bank):
    from apowerb.routers import agent_memory

    _rec(bank, "facture alice", user="alice@a.test")
    _rec(bank, "facture partagée")

    class NoMemory:
        async def delete_user_memory(self, **_):
            return 0

    app = FastAPI()
    app.include_router(agent_memory.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        email="alice@a.test"
    )
    app.dependency_overrides[agent_memory._memory_service] = lambda: NoMemory()
    app.dependency_overrides[agent_memory._case_bank] = lambda: bank
    response = TestClient(app).delete(f"/api/agents/{AGENT}/memory")
    assert response.status_code == 204
    assert [
        c["task_summary"]
        for c in bank.search(
            organization_id=ORG,
            agent_id=AGENT,
            user_key=scope_user_key("alice@a.test"),
            query="facture",
        )
    ] == ["facture partagée"]


def test_le_modele_ne_voit_que_le_debut_d_un_cas_long():
    long = "facture " + "x" * 600
    block = cb.format_cases(
        [
            {
                "task_summary": long,
                "tool_sequence": ["tool_a"],
                "outcome": 1,
                "failure_kind": None,
            }
        ]
    )
    line = next(x for x in block.splitlines() if x.startswith("- Task:"))
    assert len(line.split(" | ")[0]) == len("- Task: ") + cb._SHOWN_SUMMARY_CHARS


def test_supprimer_l_agent_efface_ses_cas_et_seulement_les_siens(bank, monkeypatch):
    from apowerb.core import agent_main

    _rec(bank, "facture de l'agent 7")
    bank.record(
        organization_id=ORG,
        agent_id=8,
        scope_user=None,
        source="schedule",
        run_id="r8",
        task_summary="facture de l'agent 8",
        tool_sequence=[],
        outcome=1,
    )
    monkeypatch.setattr(cb, "default_case_bank", lambda: bank)
    agent_main._delete_agent_cases(str(AGENT))
    assert bank.count() == 1
    assert bank.search(organization_id=ORG, agent_id=8, user_key=None, query="facture")


def test_un_echec_sans_aucun_outil_appele_n_est_pas_un_cas():
    # Quota, fournisseur indisponible, garde-fou : l'agent n'a rien fait, il n'y a rien à apprendre.
    run = {
        "run_id": "r",
        "trigger": "schedule",
        "owner_id": "o",
        "status": "error",
        "error_message": "RateLimitError: quota",
        "tools_executed": [],
        "config": {"new_message": {"parts": [{"text": "trier les factures"}]}},
    }
    assert cb.case_from_run(run) is None
    assert cb.case_from_run({**run, "tools_executed": None}) is None
    assert cb.case_from_run({**run, "tools_executed": ["tool_a"]})["outcome"] == 0


def test_mettre_a_jour_un_agent_sans_le_drapeau_ne_l_eteint_pas():
    from apowerb.schema.agent_schema import AgentCreateSchema

    assert AgentCreateSchema.model_fields["case_bank_enabled"].default is None
    src = inspect.getsource(
        __import__("apowerb.core.agent_main", fromlist=["x"]).update_agent
    )
    assert "agent.case_bank_enabled is not None" in src
