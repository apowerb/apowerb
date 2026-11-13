"""Génération d'un titre court de conversation à partir du premier message.

Réutilise le modèle + les credentials de l'agent de la conversation
(``load_agent_model_params``) — pas de clé LLM globale à maintenir. Tout échec
(modèle indisponible, LLM en erreur, réponse vide) retombe sur un fallback
déterministe (les premiers mots du message) : la génération de titre ne doit
JAMAIS casser l'envoi de message côté front (appel best-effort).
"""

from __future__ import annotations

import logging
import re

from apowerb.core.agent_helpers.agent_utils import load_agent_model_params

logger = logging.getLogger(__name__)

# Endpoint OVH du Mistral-Small (litellm route sinon ``mistral/`` vers
# api.mistral.ai — mauvais hôte pour une clé OVH). Même valeur que le draft
# de prospection.

_TITLE_SYSTEM = (
    "Tu génères un titre court pour une conversation, à partir du tout premier "
    "message de l'utilisateur. Règles STRICTES : 3 à 6 mots maximum ; dans la "
    "langue du message ; pas de guillemets ; pas de ponctuation finale ; pas de "
    "préfixe du type 'Titre :'. Réponds UNIQUEMENT par le titre, rien d'autre."
)

_MAX_TITLE_CHARS = 60
_MAX_TITLE_WORDS = 8


def _fallback_title(message: str) -> str:
    """Titre déterministe : les premiers mots du message."""
    words = (message or "").strip().split()
    if not words:
        return "Nouvelle conversation"
    return " ".join(words[:6])[:_MAX_TITLE_CHARS].strip()


def clean_title(raw: str | None) -> str:
    """Nettoie la sortie LLM : retire guillemets, préfixes, ponctuation finale,
    sauts de ligne, et borne la longueur."""
    if not raw:
        return ""
    t = raw.strip()
    # Première ligne non vide seulement (le modèle peut bavarder).
    for line in t.splitlines():
        if line.strip():
            t = line.strip()
            break
    # Préfixe "Titre :" / "Title:" éventuel.
    t = re.sub(r"^(titre|title)\s*[:\-]\s*", "", t, flags=re.IGNORECASE)
    # Guillemets entourants.
    t = t.strip().strip("\"'«»“”").strip()
    # Ponctuation finale.
    t = t.rstrip(".…!?,;: ").strip()
    # Borne mots + caractères.
    words = t.split()
    if len(words) > _MAX_TITLE_WORDS:
        t = " ".join(words[:_MAX_TITLE_WORDS])
    return t[:_MAX_TITLE_CHARS].strip()


def _resolve_model_creds(agent_id) -> tuple[str | None, dict]:
    """(model, params) de l'agent, ou (None, {}) si indispo."""
    if agent_id is None:
        return None, {}
    try:
        num = int(str(agent_id).replace("agent", "").strip())
    except (TypeError, ValueError):
        return None, {}
    try:
        return load_agent_model_params(num)
    except Exception as e:  # noqa: BLE001
        logger.warning("[title] creds agent %s indisponibles: %s", agent_id, e)
        return None, {}


async def generate_session_title(message: str, agent_id=None) -> str:
    """Génère un titre court depuis le 1er message. Best-effort : jamais d'exception."""
    msg = (message or "").strip()
    if not msg:
        return "Nouvelle conversation"
    msg = msg[:600]

    model, params = _resolve_model_creds(agent_id)
    if not model:
        return _fallback_title(msg)

    import litellm

    from apowerb.core.agent_helpers.llm_model_builder import litellm_call_kwargs

    # Same route as the agent itself (endpoint, key, api_version).
    kwargs: dict = {
        **litellm_call_kwargs(model, params),
        "messages": [
            {"role": "system", "content": _TITLE_SYSTEM},
            {"role": "user", "content": msg},
        ],
        "temperature": 0.3,
        "max_tokens": 24,
        "timeout": 20,
        "num_retries": 1,
        "drop_params": True,
    }
    if kwargs["model"].startswith("gemini/"):
        kwargs["reasoning_effort"] = "disable"
        kwargs["thinking"] = {"type": "disabled"}

    try:
        resp = await litellm.acompletion(**kwargs)
        title = clean_title(resp.choices[0].message.content)
        return title or _fallback_title(msg)
    except Exception as e:  # noqa: BLE001
        logger.warning("[title] génération LLM échouée: %s", e)
        return _fallback_title(msg)
