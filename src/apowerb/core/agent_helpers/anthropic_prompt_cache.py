"""Cache de prompt Claude : LiteLLM marque le préfixe stable de chaque requête.

Pourquoi
    Anthropic ne met rien en cache sans marqueur ``cache_control``. Les agents
    apowerb tournent par défaut sur Claude, et chaque appel d'un tour repayait
    l'en-tête fixe -- déclarations d'outils et instruction, 5 à 17 k jetons selon
    le template -- au plein tarif, alors qu'un tour enchaîne plusieurs appels.

Ce que fait ce module
    Il demande à LiteLLM deux points de cache, ceux qu'ADK pose lui-même quand
    une application a un ``context_cache_config`` :

    * le message système. Anthropic range les outils AVANT le système dans
      l'ordre du cache : ce point couvre donc outils et instruction ;
    * le dernier message, qui met en cache la conversation et avance tout seul
      à chaque appel du tour.

    Les points ne sont posés que sur ``anthropic/``. Pour Gemini, LiteLLM les
    transformerait en cache explicite (``cachedContents``), plus lent au premier
    octet : c'est ``gemini_prompt_cache`` qui en décide. OpenAI et les autres
    cachent seuls ou pas du tout.

Le prix
    Une écriture coûte 1,25 fois le tarif d'entrée, une lecture 0,1 fois, pour
    5 minutes. Un tour à un seul appel paie donc 25 % de plus sur le préfixe ;
    dès deux appels en 5 minutes, le cache est gagnant. ``APOWERB_ANTHROPIC_PROMPT_CACHE=0``
    le coupe. Effet observable dans ``llm_usage.cached_tokens``.
"""
from __future__ import annotations

import os
from typing import Any

ENV_FLAG = "APOWERB_ANTHROPIC_PROMPT_CACHE"
_FALSY = {"0", "false", "no", "off"}

CACHE_POINTS: list[dict[str, Any]] = [
    {"location": "message", "role": "system"},
    {"location": "message", "index": -1},
]


def anthropic_cache_enabled() -> bool:
    """Allumé par défaut ; seule une valeur explicitement fausse l'éteint."""
    return os.environ.get(ENV_FLAG, "").strip().lower() not in _FALSY


def with_cache_points(kwargs: dict[str, Any]) -> dict[str, Any]:
    """*kwargs* avec les points de cache si le modèle est Claude.

    Rend l'objet reçu quand rien n'est à faire, sinon une copie : l'appelant
    n'est jamais muté. Des points déjà fournis par l'appelant sont respectés.
    """
    model = kwargs.get("model") or ""
    if (
        not model.startswith("anthropic/")
        or "cache_control_injection_points" in kwargs
        or not anthropic_cache_enabled()
    ):
        return kwargs
    return {**kwargs, "cache_control_injection_points": [dict(p) for p in CACHE_POINTS]}
