"""Branchement des outils einvoice dans l'application — contrôles 22 à 25.

Sans lui, des contrôles verts sur une fonction jamais appelée ne prouvent rien :
le catalogue réel (ToolsStore), la résolution réelle des outils d'un agent
(load_agent_tools_functions) et un vrai Runner ADK avec un faux modèle scripté
(aucun LLM, aucun réseau).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from tests.einvoice import einvoice_fixtures as fx

OUTILS = {
    "einvoice.tool_einvoice_extract": ["source"],
    "einvoice.tool_einvoice_parse": ["source"],
    "einvoice.tool_einvoice_check": ["source"],
    "einvoice.tool_einvoice_to_table": ["sources"],
}


@pytest.fixture(scope="module")
def store():
    from apowerb.tools_store.tool_manager import ToolsStore

    return ToolsStore()


@pytest.fixture(scope="module")
def catalogue(store):
    return store.get_all_tools()


@pytest.fixture(scope="module")
def docs(store):
    return store.get_all_tools_docs()


# ── Contrôle 22 ───────────────────────────────────────────────────────────────


def test_c22_les_quatre_outils_sont_dans_le_catalogue(catalogue):
    """Contrôle 22 : les quatre outils einvoice.* apparaissent dans le catalogue de ToolsStore."""
    assert sorted(catalogue.get("einvoice", [])) == sorted(OUTILS)


def test_c22_les_outils_se_resolvent_pour_un_agent():
    """Contrôle 22 : la résolution réelle des outils d'un agent trouve les quatre fonctions."""
    from apowerb.tools_store.tools_helpers import load_agent_tools_functions

    noms, fonctions = load_agent_tools_functions(list(OUTILS), owner_id="1")

    assert noms == list(OUTILS)
    assert [f.__name__ for f in fonctions] == [n.split(".")[1] for n in OUTILS]


# ── Contrôle 23 ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("nom", list(OUTILS))
def test_c23_aucune_variable_d_environnement_exigee(store, nom):
    """Contrôle 23 : aucun paramètre de configuration (variable d'environnement) n'est exigé."""
    import importlib

    # Un module absent rendrait aussi [] : l'outil doit d'abord exister.
    module = importlib.import_module("apowerb.tools_store.portfolio.einvoice")
    assert callable(getattr(module, nom.split(".")[1], None)), nom
    assert store.get_tool_expected_params(nom) == []


def test_c23_aucun_outil_einvoice_ne_demande_de_configuration(store):
    """Contrôle 23 : l'interface les marque prêts (needs_config faux), sans connexion OAuth."""
    statut = store.get_all_tools_with_status().get("einvoice", [])

    assert sorted(t["name"] for t in statut) == sorted(OUTILS)
    assert all(t["needs_config"] is False for t in statut), statut


@pytest.mark.parametrize("nom", list(OUTILS))
def test_c23_parametres_decouverts(docs, nom):
    """Contrôle 23 : les paramètres attendus (source / sources) sont découverts par la doc du catalogue."""
    outils = {t["name"]: t for t in docs.get("einvoice", {}).get("tools", [])}

    assert nom in outils, sorted(outils)
    assert [p["name"] for p in outils[nom]["parameters"]] == OUTILS[nom]
    assert outils[nom]["env_vars"] == []


# ── Contrôle 24 ───────────────────────────────────────────────────────────────

MOTS_CLES = {
    # (ce que l'utilisateur fournit, ce qu'il reçoit) : chaque groupe = au moins un des mots.
    "einvoice.tool_einvoice_extract": [("pdf",), ("factur-x",), ("xml",), ("profil",)],
    "einvoice.tool_einvoice_parse": [("cii",), ("ubl",), ("xml",), ("normalis",)],
    "einvoice.tool_einvoice_check": [("erreur", "error"), ("correction",), ("règle", "rule")],
    "einvoice.tool_einvoice_to_table": [("lot", "plusieurs", "batch", "list"),
                                        ("colonne", "column"), ("ligne", "row")],
}


@pytest.mark.parametrize("nom", list(OUTILS))
def test_c24_la_docstring_dit_quoi_fournir_et_ce_qu_on_recoit(docs, nom):
    """Contrôle 24 : docstring = ce que l'utilisateur fournit (nom de fichier envoyé) et ce qu'il reçoit."""
    outils = {t["name"]: t for t in docs.get("einvoice", {}).get("tools", [])}
    assert nom in outils, sorted(outils)
    doc = outils[nom]["full_docstring"].lower()
    description = outils[nom]["description"].strip()

    assert description and description != "No description available."
    assert "facture" in doc or "invoice" in doc
    for param in OUTILS[nom]:
        assert param in doc, param
    assert any(m in doc for m in ("upload", "envoy", "fichier", "file")), doc
    assert "status" in doc and ("error_message" in doc or "erreur" in doc), doc
    for groupe in MOTS_CLES[nom]:
        assert any(m in doc for m in groupe), (groupe, doc)


# ── Contrôle 25 ───────────────────────────────────────────────────────────────


def _outils_d_agent():
    """Les fonctions telles qu'un agent les reçoit (résolution réelle du catalogue)."""
    from apowerb.tools_store.tools_helpers import load_agent_tools_functions

    _noms, fonctions = load_agent_tools_functions(list(OUTILS), owner_id="1")
    return {f.__name__: f for f in fonctions}


def test_c25_chaque_outil_enregistre_dans_un_agent_adk_est_appelable(agent_dir):
    """Contrôle 25 : agent ADK de test avec les 4 outils ; chaque outil enregistré est appelé (sans LLM)."""
    from google.adk.agents import LlmAgent
    from google.adk.tools import FunctionTool

    fx.ecrire(agent_dir, "facture.pdf", fx.facturx(fx.cii(fx.facture())))
    fx.ecrire(agent_dir, "facture.xml", fx.ubl(fx.facture()))
    fonctions = _outils_d_agent()
    agent = LlmAgent(name="controleur_factures", model="gemini-2.5-flash", instruction="x",
                     tools=list(fonctions.values()))
    assert len(agent.tools) == 4

    appels = {
        "tool_einvoice_extract": {"source": "facture.pdf"},
        "tool_einvoice_parse": {"source": "facture.xml"},
        "tool_einvoice_check": {"source": "facture.xml"},
        "tool_einvoice_to_table": {"sources": ["facture.xml", "facture.pdf"]},
    }
    ctx = SimpleNamespace(session=SimpleNamespace(id="s-test"), invocation_id="i-test",
                          tool_confirmation=None)
    for outil in agent.tools:
        ft = outil if isinstance(outil, FunctionTool) else FunctionTool(outil)
        decl = ft._get_declaration()
        assert decl.name in appels
        sortie = asyncio.run(ft.run_async(args=appels[decl.name], tool_context=ctx))
        assert sortie["status"] == "success", (decl.name, sortie)


def test_c25_un_runner_adk_appelle_l_outil_et_rend_sa_reponse_au_modele(agent_dir):
    """Contrôle 25 : vrai Runner ADK, faux modèle scripté qui appelle tool_einvoice_check sur une fixture."""
    from google.adk.agents import LlmAgent
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    class FauxModele(BaseLlm):
        model: str = "faux"
        appels: int = 0

        async def generate_content_async(self, llm_request, stream: bool = False):
            self.appels += 1
            if self.appels == 1:
                part = types.Part(function_call=types.FunctionCall(
                    name="tool_einvoice_check", args={"source": "facture.xml"}))
            else:
                part = types.Part(text="FIN")
            yield LlmResponse(content=types.Content(role="model", parts=[part]))

    fx.ecrire(agent_dir, "facture.xml", fx.cii(fx.facture(iban="FR1420041010050500013M02607")))
    fonctions = _outils_d_agent()
    modele = FauxModele()
    agent = LlmAgent(name="controleur_factures", model=modele, instruction="x",
                     tools=[fonctions["tool_einvoice_check"]])

    async def derouler():
        sessions = InMemorySessionService()
        await sessions.create_session(app_name="a", user_id="u", session_id="s")
        runner = Runner(agent=agent, app_name="a", session_service=sessions)
        message = types.Content(role="user", parts=[types.Part(text="contrôle ma facture")])
        return [ev async for ev in runner.run_async(user_id="u", session_id="s", new_message=message)]

    evenements = asyncio.run(derouler())
    reponses = [p.function_response for ev in evenements
                for p in (ev.content.parts if ev.content else []) if p.function_response]

    assert len(reponses) == 1, evenements
    sortie = reponses[0].response
    assert sortie["status"] == "success", sortie
    assert {e["règle"] for e in sortie["errors"]} == {"IBAN"}
    assert modele.appels == 2, "la réponse de l'outil n'est pas revenue au modèle"
    for ev in evenements:
        ev.model_dump_json()  # l'événement (Decimal compris) reste sérialisable pour la session
