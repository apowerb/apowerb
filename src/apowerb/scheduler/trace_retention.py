"""Purge quotidienne des déroulés de runs (``agent_runs_traces``).

Chaque run de graphe garde ses événements (entrées, sorties, appels d'outils)
pour être rouvert depuis l'historique. Ils sont chiffrés, mais rien ne les
supprimait : cette boucle retire ceux qui ont plus de
``RUN_TRACE_RETENTION_DAYS`` jours (30 par défaut), une fois par jour. Le run
reste listé ; seul son déroulé devient indisponible.

Même contrat que ``events_retention`` : un échec est journalisé et avalé, la
tâche de fond ne fait jamais tomber l'application.
"""

from __future__ import annotations

import asyncio
from logging import getLogger

from apowerb.core import run_main

logger = getLogger(__name__)

CHECK_INTERVAL_SECONDS = 24 * 3600


async def trace_retention_loop() -> None:
    """Boucle quotidienne : purge, puis attente."""
    while True:
        try:
            # Le store des runs est synchrone : hors de la boucle d'événements.
            await asyncio.to_thread(run_main.purge_old_traces)
        except Exception as exc:  # noqa: BLE001 - la tâche de fond doit survivre
            logger.warning("[TRACE RETENTION] purge échouée : %r", exc)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
