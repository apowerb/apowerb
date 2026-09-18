"""Ce que le chat affiche après une carte d'action, à travers le vrai Runner ADK (roadmap#79).

Une carte qui attend l'utilisateur arrête le run avec ``skip_summarization``.
Dans ce cas, ADK (``flows/llm_flows/functions.py``, ``__build_response_event``)
ajoute au même événement une partie TEXTE : ``json.dumps`` du retour de
l'outil, pour les interfaces qui n'affichent pas les ``functionResponse``. Le
front range toute partie texte dans le message : le retour de l'outil
apparaissait donc sous la carte. ADK ne l'ajoute pas quand l'outil renvoie
``None``.

Le faux modèle ci-dessous demande une carte au premier appel, puis répond un
texte repérable s'il est rappelé.
"""

import pytest
from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from apowerb.core.agent_helpers import chat_action_tools as t

REPONSE_DU_MODELE = "REPONSE-DU-MODELE"

CARTES_EN_PAUSE = [
    (
        "request_user_input",
        {
            "question": "Quelle période ?",
            "input_type": "chips",
            "choices": ["mois", "trimestre"],
        },
    ),
    ("confirm_destructive", {"action": "Supprimer", "impact": "définitif"}),
    ("request_payment", {"amount": 42.0, "currency": "EUR", "reason": "abonnement"}),
    ("schedule_followup", {"when_iso": "2026-09-20T10:00:00", "recap": "relance"}),
    ("propose_artifact_edit", {"filename": "a.md", "diff": "-a\n+b"}),
    ("request_file_from_user", {"purpose": "facture"}),
    ("propose_agent_upgrade", {"capability": "mails", "reason": "envoyer"}),
    ("request_location", {"reason": "météo"}),
]


class FauxModele(BaseLlm):
    model: str = "faux"
    outil: str = ""
    args: dict = {}
    appels: int = 0

    async def generate_content_async(self, llm_request, stream: bool = False):
        self.appels += 1
        if self.appels == 1:
            part = types.Part(
                function_call=types.FunctionCall(name=self.outil, args=self.args)
            )
        else:
            part = types.Part(text=REPONSE_DU_MODELE)
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


async def _derouler(outil: str, args: dict):
    modele = FauxModele(outil=outil, args=args)
    agent = LlmAgent(
        name="carte", model=modele, instruction="x", tools=[getattr(t, outil)]
    )
    sessions = InMemorySessionService()
    await sessions.create_session(app_name="a", user_id="u", session_id="s")
    runner = Runner(agent=agent, app_name="a", session_service=sessions)
    message = types.Content(role="user", parts=[types.Part(text="go")])
    evenements = [
        ev
        async for ev in runner.run_async(
            user_id="u", session_id="s", new_message=message
        )
    ]
    textes = [
        p.text
        for ev in evenements
        for p in (ev.content.parts if ev.content else [])
        if p.text
    ]
    reponses = [
        p.function_response
        for ev in evenements
        for p in (ev.content.parts if ev.content else [])
        if p.function_response
    ]
    return modele.appels, textes, reponses


@pytest.mark.parametrize(
    "outil,args", CARTES_EN_PAUSE, ids=[c[0] for c in CARTES_EN_PAUSE]
)
async def test_une_carte_en_pause_n_ajoute_aucun_texte_au_chat(outil, args):
    appels, textes, reponses = await _derouler(outil, args)
    assert len(reponses) == 1, "l'outil n'a pas été exécuté"
    assert appels == 1, "le run devait s'arrêter sur la carte, sans rappeler le modèle"
    assert textes == [], f"{outil} fait apparaître ce texte sous la carte : {textes}"


async def test_un_graphique_integre_laisse_le_modele_repondre():
    # embed_chart n'arrête pas le run : le modèle est rappelé et son texte seul s'affiche.
    appels, textes, _ = await _derouler("embed_chart", {"chart_id": "c1"})
    assert appels == 2
    assert textes == [REPONSE_DU_MODELE]
