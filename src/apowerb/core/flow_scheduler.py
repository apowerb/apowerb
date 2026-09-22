"""Boucle de tick pour les triggers ``schedule`` et ``file``.

Ce fichier était laissé vide pour ce lot (triggers de workflow, T1). Deux
ordonnanceurs existent déjà dans ce dépôt, et ni l'un ni l'autre ne convient
ici :

* ``routers/scheduler.py`` + ``scheduler/`` pilotent l'orchestrateur externe
  Mage/th2etl pour les AGENTS planifiés — un aller-retour HTTP vers un
  service tiers, pensé pour des pipelines de données, pas pour "vérifier une
  échéance en base et lancer un run" ;
* ``scheduler/backlog_worker.py`` draine une file (``webhook_logs``), pas une
  liste d'échéances futures.

Donner aux triggers ``schedule`` un aller-retour vers l'orchestrateur externe
ajouterait une dépendance et une panne possibles pour ce qui reste, au fond,
une boucle de tick toutes les N secondes sur une table locale — il en faut
une de toute façon quelque part, et ``core/flow_scheduler.py`` est cet
endroit-là.

Sûreté multi-réplica : ``_lock`` reste un ``asyncio.Lock`` DE PROCESSUS — il
n'empêche qu'un tick de se chevaucher avec le PRÉCÉDENT au sein du MÊME
processus (évite d'empiler des ticks derrière un tick lent), rien de plus.
Avec plusieurs workers uvicorn/gunicorn, chaque processus démarre sa propre
boucle et peut lire la même échéance échue au même tick — mais le
LANCEMENT effectif est protégé au niveau de la ligne, pas du processus :
``workflow_triggers.fire_schedule_trigger`` réserve le créneau par un
``UPDATE workflow_triggers SET next_run_at=... WHERE workflow_id=... AND
next_run_at=<valeur lue>`` avant de lancer quoi que ce soit. Une seule
réplique gagne cette comparaison-et-échange (``rowcount == 1``) ; les autres
se retirent sans rien lancer. Un double déclenchement inter-processus pour
le même créneau n'est donc plus possible, sans verrou distribué dédié
(Postgres advisory lock / Redis) : c'est la ligne elle-même, via son
``next_run_at``, qui sert de verrou.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from logging import getLogger
from typing import Optional

from apowerb.core import workflow_file_triggers as wft
from apowerb.core import workflow_triggers as wt

logger = getLogger(__name__)

# 30 s : assez réactif pour un intervalle minimal de 5 minutes (contrat),
# assez espacé pour ne pas marteler la base sur la VM de dev partagée.
TICK_SECONDS = 30

_lock = asyncio.Lock()
_task: Optional[asyncio.Task] = None


async def _tick_file_trigger(row: dict, *, moment: datetime) -> int:
    """Réserve CE sondage (CAS sur ``next_run_at``) puis sonde si gagné.

    Même principe que ``fire_schedule_trigger`` : la réservation avant
    l'action rend le double sondage inter-réplica impossible sans verrou
    distribué dédié (voir le docstring de module).
    """
    cfg = json.loads(row["config"] or "{}")
    interval = int(cfg.get("interval_min") or 15)
    next_run_at = (moment + timedelta(minutes=interval)).isoformat()
    reserved = wt.reserve_next_poll(
        row["workflow_id"],
        prior_next_run_at=row["next_run_at"],
        next_run_at=next_run_at,
    )
    if not reserved:
        return 0
    return await wft.poll_file_trigger(row, list_files=wft.list_files_for_trigger)


async def tick_once(*, now: Optional[datetime] = None) -> int:
    """Un passage : lance les triggers ``schedule`` échus et sonde les
    triggers ``file`` échus. Renvoie combien de runs ont effectivement
    démarré (pas le nombre de lignes échues examinées — un tick sauté pour
    chevauchement, ou un trigger désarmé en cours de route, ne compte pas).

    Si un tick précédent tourne encore (verrou déjà pris), celui-ci se
    retire immédiatement plutôt que d'attendre — le prochain passage
    régulier suffit, inutile d'empiler des ticks derrière un tick lent.
    """
    if _lock.locked():
        return 0
    async with _lock:
        moment = now or datetime.now(timezone.utc)
        fired = 0
        for row in wt.due_schedule_triggers(moment):
            try:
                if await wt.fire_schedule_trigger(row, now=moment):
                    fired += 1
            except Exception:  # noqa: BLE001 - un trigger en panne ne doit pas arrêter la boucle
                logger.exception(
                    "[flow_scheduler] échec du déclenchement schedule pour workflow=%s",
                    row.get("workflow_id"),
                )
        for row in wt.due_file_triggers(moment):
            try:
                fired += await _tick_file_trigger(row, moment=moment)
            except Exception:  # noqa: BLE001 - un trigger en panne ne doit pas arrêter la boucle
                logger.exception(
                    "[flow_scheduler] échec du sondage file pour workflow=%s",
                    row.get("workflow_id"),
                )
        return fired


async def _loop() -> None:
    logger.info("[flow_scheduler] boucle de tick démarrée (%ss)", TICK_SECONDS)
    while True:
        try:
            await tick_once()
        except Exception:  # noqa: BLE001 - la boucle doit survivre à un tick en échec
            logger.exception("[flow_scheduler] tick en échec")
        await asyncio.sleep(TICK_SECONDS)


def start() -> None:
    """Démarre la boucle une fois par processus (idempotent)."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop())


def stop() -> None:
    """Arrête la boucle (tests, extinction propre)."""
    global _task
    if _task is not None:
        _task.cancel()
        _task = None
