"""Branche la comptabilisation des jetons et son plafond, côté noyau.

Jusqu'au 09/09/26 ces quatre points étaient câblés par une brique commerciale :
sans elle, rien n'écrivait ``llm_usage`` et la jauge restait muette. La
décision produit du 09/09 fait passer le compteur **et** la barre en open
source. Ce module est ce que la brique faisait, ramené dans le noyau.

Il est appelé au montage de l'application, **avant** ``load_overlay()`` : le
noyau se réserve ainsi les capacités ``llm_usage`` et ``llm_quota``, et une
brique restée sur l'ancien découpage voit son second enregistrement ignoré au
lieu de compter chaque jeton deux fois (cf. ``ExtensionRegistry._claim``).

Ce qui reste vendu : l'écran d'administration de la consommation. Compter et
plafonner sont désormais dans le produit ouvert ; l'analyser par agent, par
outil et par utilisateur ne l'est pas.

Le plafond se règle, il ne se retire plus : ``DEFAULT_LLM_USER_TOKEN_CAP=0``
rend les runs illimités. Une absence de brique n'est plus le kill-switch.
"""

from __future__ import annotations

from logging import getLogger
from typing import Any

logger = getLogger(__name__)

__all__ = ["register_core_usage"]


def _model_observer(**context: Any):
    """Fabrique appelée à la construction d'un agent.

    Le noyau a déjà résolu quel modèle a répondu ; ce qu'on en fait — le
    comptabiliser — commence ici.
    """
    from apowerb.core.agent_helpers.usage_recorder import (
        create_usage_recorder_callback,
    )

    return create_usage_recorder_callback(**context)


def register_core_usage(registry) -> None:
    """Enregistre les quatre points. Idempotent par capacité."""
    from apowerb.core.usage_quota import default_llm_cap
    from apowerb.helpers.llm_usage_migration import ensure_llm_usage_table
    from apowerb.helpers.quota_guard import enforce_run_quota

    registry.register_default_llm_cap(default_llm_cap)
    registry.register_run_guard(enforce_run_quota, provides="llm_quota")
    registry.register_model_observer(_model_observer, provides="llm_usage")
    registry.register_bootstrap_hook(
        ensure_llm_usage_table, provides="llm_usage_table"
    )

    logger.info("[USAGE] comptage des jetons et plafond câblés (noyau)")
