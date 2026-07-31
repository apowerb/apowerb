"""Création des tables des stores (agents, tools, skills, hub, api keys).

Ces ``create_table()`` étaient appelés en colonne 1 dans les modules des
stores : importer ``apowerb.core.agent_main`` ou
``apowerb.tools_store.tools_helpers`` ouvrait une connexion et exécutait du
DDL sur la base — l'effet de bord le plus violent du paquet, et un obstacle
net à son usage comme library.

Ils sont regroupés ici et appelés depuis ``main.bootstrap()``, au même titre
que les autres ``ensure_*``. Comportement inchangé pour le serveur : les
tables sont toujours garanties avant de servir la première requête.
"""

from __future__ import annotations

from apowerb.configs.th2logger import setup_logging

_logger = setup_logging(__name__)


def ensure_store_tables() -> None:
    """Crée les tables des stores manquantes (no-op si elles existent)."""
    from apowerb.core.agent_main import agent_store
    from apowerb.core.api_key_main import api_key_store
    from apowerb.core.hub_main import hub_store
    from apowerb.skills_store.skill_manager import skill_store
    from apowerb.tools_store.tools_helpers import tool_config_store

    for store in (agent_store, tool_config_store, skill_store, hub_store, api_key_store):
        try:
            store.create_table()
        except Exception as exc:  # pragma: no cover - dépend de la base
            _logger.error(
                "création de table impossible pour %s: %s",
                type(store).__name__,
                exc,
            )
            raise
