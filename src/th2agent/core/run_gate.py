"""Passage oblige de tout run d'agent devant les gardes enregistrees.

Il existe plusieurs portes d'entree vers un run : le chat (`/api/adk/run`,
`/api/adk/run_sse`), les runs planifies (Mage/th2etl via `/run_from_jwt` et
`/run_from_refresh_token`) et les webhooks. Chacune resolvait -- ou oubliait
de resoudre -- les gardes pour son compte : seules les deux premieres le
faisaient, les autres passaient au travers.

Ce module est le point unique. Une nouvelle porte doit l'appeler ; un test
de couverture (`tests/test_run_gate_couverture.py`) relit les sources et
echoue si un module appelle le runner ADK sans passer par ici.

Le noyau ne nomme aucune garde : il les demande au registre d'extensions.
Sans brique installee, la liste est vide et tout passe.
"""
from __future__ import annotations

from logging import getLogger
from typing import Optional

logger = getLogger(__name__)


async def resolve_owner_plan(owner_id: str) -> Optional[str]:
    """Plan commercial de *owner_id* (son email), ou ``None``.

    Best-effort : les chemins non interactifs (planifie, webhook) n'ont pas
    d'objet utilisateur sous la main, seulement un identifiant. Une panne de
    lecture rend ``None``, ce qui fait retomber la garde sur le plafond par
    defaut -- jamais sur « pas de plafond ».
    """
    if not owner_id:
        return None
    try:
        from sqlalchemy import select

        from th2agent.helpers.database import sessionmanager
        from th2agent.models import User

        async with sessionmanager.session() as db:
            resultat = await db.execute(
                select(User.plan).where(User.email == owner_id)
            )
            return resultat.scalar_one_or_none()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[RUN GATE] plan illisible pour %s: %s", owner_id, exc
        )
        return None


async def apply_run_guards(
    *, agent_name: str, owner_id: str, plan: Optional[str]
) -> None:
    """Fait passer un run devant toutes les gardes enregistrees.

    Leve ce que leve une garde (typiquement un 402 de quota depasse) : c'est
    le refus net, avant que quoi que ce soit ne commence.

    ``owner_id`` vide ne fait PAS sauter le controle en silence : il est
    trace en WARNING. Un plafond commercial doit tomber en marche ouverte
    plutot que rendre le produit muet, mais l'ouverture doit s'entendre --
    sinon elle devient le trou suivant.
    """
    from th2agent.core.extensions.registry import registry

    gardes = registry.run_guards()
    if not gardes:
        return

    if not owner_id:
        logger.warning(
            "[RUN GATE] run de %s sans proprietaire resolu : %d garde(s) "
            "non appliquee(s)",
            agent_name,
            len(gardes),
        )
        return

    for garde in gardes:
        await garde(agent_name, owner_id=owner_id, plan=plan)
