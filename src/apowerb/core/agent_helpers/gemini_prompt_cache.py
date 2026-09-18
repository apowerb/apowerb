"""Cache de prompt explicite pour Gemini, derrière un drapeau DÉSACTIVÉ par défaut.

Pourquoi
    Mesuré sur une requête de production le 17/09/2026 : 98,7 % du prompt est un
    en-tête fixe (instruction système + déclarations d'outils, ~6 000 jetons) pour
    une conversation de 361 caractères. Le cache implicite de Gemini n'en a absorbé
    que 22 %, et seulement quand deux appels se suivaient à moins de ~30 s.

Ce que fait ce module
    Il pose, sur le message système, le marqueur ``cache_control`` que LiteLLM sait
    transformer en cache explicite Gemini (``cachedContents``). LiteLLM fait ensuite
    tout le reste avec SA PROPRE conversion : il déplace l'instruction et les outils
    dans le cache, vérifie le seuil de 1 024 jetons, réutilise un cache existant.

Pourquoi le cœur ne gère pas le cache lui-même
    Passer ``cached_content`` à LiteLLM lui fait retirer outils et instruction de la
    requête (``litellm.modify_params`` est à True) : le modèle ne verrait alors QUE la
    version mise en cache. La construire nous-mêmes imposerait de reconvertir les
    schémas d'outils au format Gemini, et toute divergence avec la conversion de
    LiteLLM casserait les appels d'outils en silence. Poser le marqueur garantit que
    le cache contient exactement ce que LiteLLM aurait envoyé.

Le prix, à connaître avant d'allumer
    LiteLLM 1.95.0 interroge la liste des caches de Google à CHAQUE requête avant de
    générer, et crée le cache s'il manque. Au trafic du 18/09/2026 (aucun tour en
    18 h), la plupart des tours manqueraient le cache et paieraient cet aller-retour
    en plus : le TTFT empirerait. N'allumer que lorsqu'un même agent reçoit des tours
    plus souvent que la durée de vie du cache, puis vérifier l'effet sur l'attribut
    ``gen_ai.server.time_to_first_token``.
"""
from __future__ import annotations

import os
from typing import Any

ENV_FLAG = "APOWERB_GEMINI_EXPLICIT_CACHE"
ENV_TTL = "APOWERB_GEMINI_CACHE_TTL_SECONDS"
DEFAULT_TTL_SECONDS = 3600
_TRUTHY = {"1", "true", "yes", "on"}


def explicit_cache_enabled() -> bool:
    """Vrai seulement si le drapeau est explicitement allumé. Absent = éteint."""
    return os.environ.get(ENV_FLAG, "").strip().lower() in _TRUTHY


def _ttl_seconds() -> int:
    """Durée de vie demandée à Google ; toute valeur absente ou invalide vaut une heure."""
    try:
        value = int(os.environ.get(ENV_TTL, "").strip())
    except ValueError:
        return DEFAULT_TTL_SECONDS
    return value if value > 0 else DEFAULT_TTL_SECONDS


def mark_system_prefix_for_cache(model: str, messages: list) -> list:
    """Marque le message système pour le cache explicite de Gemini.

    Renvoie la liste REÇUE, inchangée et identique (``is``), dès que rien n'est à
    faire : drapeau éteint, modèle hors Gemini, pas de message système. L'appelant
    peut s'en servir pour savoir qu'aucune requête marquée n'a été produite.

    Sinon renvoie une NOUVELLE liste ; l'original n'est jamais muté. LiteLLM ne lit la
    durée de vie que sur un bloc de contenu, pas sur une chaîne : le contenu système
    est donc converti en blocs, et le marqueur posé sur le dernier bloc texte pour
    couvrir tout le préfixe.
    """
    if not explicit_cache_enabled() or not (model or "").startswith("gemini/"):
        return messages

    index = next(
        (i for i, m in enumerate(messages) if isinstance(m, dict) and m.get("role") == "system"),
        None,
    )
    if index is None:
        return messages

    marker = {"type": "ephemeral", "ttl": f"{_ttl_seconds()}s"}
    content: Any = messages[index].get("content")
    if isinstance(content, str):
        blocks = [{"type": "text", "text": content, "cache_control": marker}]
    elif isinstance(content, list):
        blocks = [dict(b) if isinstance(b, dict) else b for b in content]
        last_text = next(
            (i for i in range(len(blocks) - 1, -1, -1)
             if isinstance(blocks[i], dict) and blocks[i].get("type") == "text"),
            None,
        )
        if last_text is None:
            return messages
        blocks[last_text]["cache_control"] = marker
    else:
        return messages

    marked = list(messages)
    marked[index] = {**messages[index], "content": blocks}
    return marked
