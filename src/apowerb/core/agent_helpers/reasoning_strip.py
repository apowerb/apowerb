"""Ce qu'un modèle de raisonnement écrit, il ne l'accepte pas toujours en retour.

Mesuré le 11/09/26 sur `k8s.apowerb.com`, avec `openai/gpt-oss-20b` chez Groq.
Premier tour : l'agent répond. Deuxième tour de la MÊME conversation :

    GroqException - 'messages.3' : for 'role:assistant' the following must be
    satisfied[('messages.3' : property 'reasoning_content' is unsupported)]

Groq place `reasoning_content` dans sa réponse ; ADK le garde dans l'historique ;
au tour suivant l'historique repart tel quel — et Groq refuse ce champ en
ENTRÉE. Le premier échange passe donc toujours, et c'est ce qui rend le défaut
traître : il ne se voit qu'à partir du deuxième message, quand on croit
l'installation validée.

La même famille avait mordu en juillet avec Gemini et ses suffixes
``__thought__`` sur les identifiants d'appels d'outil. Le filet d'alors vivait
dans ``helpers/litellm_config.py`` et monkey-patchait ``litellm.acompletion`` —
un correctif global, invisible depuis le code appelant, et disparu du noyau
depuis. ``llm_model_builder`` en garde encore la trace écrite, mais plus le
code.

Ici on passe par le point d'injection qu'ADK prévoit : le champ ``llm_client``
de ``LiteLlm``, documenté « for better testability ». Rien de global, rien de
patché, et le nettoyage s'observe dans un test sans toucher au réseau.

Portée délibérément étroite :

* seuls les messages de rôle ``assistant`` sont touchés — ailleurs, un champ du
  même nom viendrait de l'utilisateur et le retirer serait une perte ;
* seul le champ de raisonnement part, jamais ``content`` ;
* l'entrée n'est pas mutée : ADK garde son historique complet, seul ce qui
  voyage sur le réseau est allégé. Le raisonnement reste donc lisible dans les
  journaux et les rejeux ;
* aucune exception ne remonte : ce code s'exécute à chaque appel de modèle, il
  doit dégrader plutôt que casser une conversation.
"""

from __future__ import annotations

from logging import getLogger
from typing import Any

from google.adk.models.lite_llm import LiteLLMClient

logger = getLogger(__name__)

__all__ = ["CleaningLiteLLMClient", "REASONING_FIELDS", "strip_reasoning_content"]

# Les fournisseurs ne s'accordent pas sur le nom. Groq et DeepSeek écrivent
# `reasoning_content`, d'autres `reasoning`. On retire les deux plutôt que de
# parier sur celui du jour : en ajouter un coûte une ligne, en oublier un coûte
# une conversation.
REASONING_FIELDS = ("reasoning_content", "reasoning")


def strip_reasoning_content(messages: Any) -> Any:
    """Rend *messages* sans le raisonnement des réponses d'assistant.

    Rend l'objet reçu tel quel si ce n'est pas une liste, et ne recopie que les
    messages réellement modifiés : sur une conversation sans raisonnement, le
    résultat est la même liste, aux mêmes objets.
    """
    if not isinstance(messages, list):
        return messages

    nettoyes = []
    retires = 0
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            nettoyes.append(message)
            continue
        presents = [champ for champ in REASONING_FIELDS if champ in message]
        if not presents:
            nettoyes.append(message)
            continue
        copie = {k: v for k, v in message.items() if k not in presents}
        nettoyes.append(copie)
        retires += len(presents)

    if retires:
        logger.debug(
            "[REASONING] %d champ(s) de raisonnement retirés avant envoi", retires
        )
    return nettoyes


class CleaningLiteLLMClient(LiteLLMClient):
    """Client ADK qui allège l'historique juste avant de le confier à LiteLLM.

    L'héritage n'est pas décoratif : ``LiteLlm.llm_client`` est un champ
    pydantic typé ``LiteLLMClient``, et un simple objet compatible en canard
    est refusé à la construction (``Input should be an instance of
    LiteLLMClient``). Un test qui n'exercerait que ``acompletion`` passerait
    au vert sans que le client soit posable sur un vrai modèle.
    """

    async def _appel_reel(self, **kwargs: Any) -> Any:
        """Délègue au client d'ADK. Surchargé dans les tests."""
        return await super().acompletion(**kwargs)

    async def acompletion(self, **kwargs: Any) -> Any:
        messages = kwargs.get("messages")
        if messages is not None:
            kwargs = {**kwargs, "messages": strip_reasoning_content(messages)}
        return await self._appel_reel(**kwargs)
