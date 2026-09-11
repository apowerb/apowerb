"""Un modèle de raisonnement ne doit pas rendre sa propre conversation invalide.

Mesuré en production le 11/09/26 sur `k8s.apowerb.com`. Premier tour :
l'agent répond. Deuxième tour, même conversation :

    GroqException - {"error":{"message":"'messages.3' : for 'role:assistant'
    the following must be satisfied[('messages.3' : property
    'reasoning_content' is unsupported)]"}}

Le mécanisme : `openai/gpt-oss-20b` est un modèle à raisonnement. Groq renvoie
`reasoning_content` dans sa réponse, ADK le conserve dans l'historique, et au
tour suivant l'historique repart tel quel — or Groq refuse ce champ en ENTRÉE.
Ce qu'un fournisseur écrit n'est pas forcément ce qu'il accepte à relire.

La même famille avait déjà mordu avec Gemini et ses `__thought__` (juillet 26).
Le filet d'alors vivait dans `helpers/litellm_config.py`, qui monkey-patchait
`litellm.acompletion` ; il a disparu du noyau. Ici, on passe par le point
d'injection qu'ADK prévoit — le champ `llm_client` de `LiteLlm` — donc rien de
global, rien de patché, et testable.
"""

from __future__ import annotations

import pytest

from apowerb.core.agent_helpers.reasoning_strip import (
    CleaningLiteLLMClient,
    strip_reasoning_content,
)


class TestCeQuiEstRetire:
    def test_le_champ_part_du_message_assistant(self):
        messages = [
            {"role": "user", "content": "bonjour"},
            {
                "role": "assistant",
                "content": "Bonjour !",
                "reasoning_content": "L'utilisateur salue, je salue.",
            },
            {"role": "user", "content": "qu'est-ce que tu sais faire ?"},
        ]
        nettoyes = strip_reasoning_content(messages)
        assert "reasoning_content" not in nettoyes[1]
        assert nettoyes[1]["content"] == "Bonjour !", "le texte de la réponse reste"

    def test_la_variante_reasoning_part_aussi(self):
        """Les fournisseurs ne s'accordent pas sur le nom du champ."""
        messages = [{"role": "assistant", "content": "ok", "reasoning": "…"}]
        assert "reasoning" not in strip_reasoning_content(messages)[0]

    def test_un_message_utilisateur_nest_jamais_touche(self):
        """La garde est bornée au rôle assistant : ailleurs, ce champ n'a pas
        le même sens et le retirer serait une perte de données."""
        messages = [{"role": "user", "content": "x", "reasoning_content": "garde-moi"}]
        assert strip_reasoning_content(messages)[0]["reasoning_content"] == "garde-moi"


class TestCeQuiNeBougePas:
    def test_sans_raisonnement_rien_ne_change(self):
        messages = [
            {"role": "user", "content": "bonjour"},
            {"role": "assistant", "content": "salut"},
        ]
        assert strip_reasoning_content(messages) == messages

    def test_l_appel_original_nest_pas_mute(self):
        """Le nettoyage rend une copie : l'historique d'ADK n'est pas amputé,
        seul ce qui part sur le réseau l'est. Sinon on perdrait le raisonnement
        dans les journaux et les rejeux."""
        messages = [{"role": "assistant", "content": "ok", "reasoning_content": "r"}]
        strip_reasoning_content(messages)
        assert messages[0]["reasoning_content"] == "r"

    @pytest.mark.parametrize("entree", [None, "pas une liste", 42, []])
    def test_une_entree_inattendue_ne_leve_jamais(self, entree):
        """Ce code tourne sur CHAQUE appel de modèle : il doit dégrader, pas
        casser."""
        assert strip_reasoning_content(entree) == entree

    def test_un_element_non_dict_est_laisse_tel_quel(self):
        messages = ["bizarre", {"role": "assistant", "reasoning_content": "r"}]
        nettoyes = strip_reasoning_content(messages)
        assert nettoyes[0] == "bizarre"
        assert "reasoning_content" not in nettoyes[1]


class TestLeClientQuiNettoie:
    @pytest.mark.asyncio
    async def test_il_nettoie_avant_de_deleguer(self):
        """Le contrôle qui compte : ce qui part chez le fournisseur."""
        recu = {}

        class _Espion(CleaningLiteLLMClient):
            async def _appel_reel(self, **kwargs):
                recu.update(kwargs)
                return "reponse"

        client = _Espion()
        resultat = await client.acompletion(
            model="groq/openai/gpt-oss-20b",
            messages=[
                {"role": "assistant", "content": "ok", "reasoning_content": "r"},
                {"role": "user", "content": "et maintenant ?"},
            ],
        )
        assert resultat == "reponse"
        assert "reasoning_content" not in recu["messages"][0]
        assert recu["messages"][1]["content"] == "et maintenant ?"

    @pytest.mark.asyncio
    async def test_sans_messages_il_delegue_sans_broncher(self):
        class _Espion(CleaningLiteLLMClient):
            async def _appel_reel(self, **kwargs):
                return kwargs

        rendu = await _Espion().acompletion(model="m")
        assert rendu == {"model": "m"}


class TestLeClientEstReellementPose:
    """Le contrôle qui compte le plus : un nettoyeur non branché ne nettoie rien.

    Les tests ci-dessus exercent la fonction et le client isolément ; ils
    passaient au vert alors que ``LiteLlm`` refusait encore le client, faute
    d'héritage (``Input should be an instance of LiteLLMClient``). Un défaut
    invisible pour eux, fatal en production.
    """

    def _modele(self, details):
        from apowerb.core.agent_helpers.llm_model_builder import build_litellm_model

        return build_litellm_model(details, None)

    def test_le_chemin_natif_porte_le_client(self):
        modele = self._modele(
            {"agent_model": "groq/openai/gpt-oss-20b", "agent_model_params": {}}
        )
        assert isinstance(modele.llm_client, CleaningLiteLLMClient)

    def test_le_chemin_openai_compat_le_porte_aussi(self):
        """Un endpoint OpenAI-compat prend une autre branche du builder : en
        câbler une seule laisserait le défaut vivant sur la moitié des
        installations."""
        modele = self._modele(
            {
                "agent_model": "mistral/x",
                "agent_model_params": {"model_api_base": "https://exemple.invalid/v1"},
            }
        )
        assert isinstance(modele.llm_client, CleaningLiteLLMClient)
