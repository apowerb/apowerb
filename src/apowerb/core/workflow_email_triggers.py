"""``email`` (T2) — un mail reçu (Outlook/Gmail) lance un run.

Réutilise les abonnements webhook existants de l'intégration de
l'utilisateur (``models.WebhookSubscription``, gérés par
``routers/webhook_handlers/{outlook,gmail}.py``) — ce module n'écoute rien
lui-même, il est appelé DEPUIS ces handlers une fois l'e-mail récupéré (voir
le point d'insertion documenté dans chaque fichier).

Séparé en deux fonctions pour rester testable SANS réseau ni abonnement live
(la réception réelle d'une notification Microsoft Graph / Gmail Pub/Sub
n'est PAS exercée par la suite de tests — voir
``tests/test_workflow_triggers_t2_email.py``) :

* ``matches_email_trigger`` — pur, sans I/O : filtre ``from_filter``/
  ``subject_filter`` (sous-chaîne insensible à la casse, ``None`` = toujours
  vrai).
* ``dispatch_email_triggers`` — interroge les triggers actifs du propriétaire
  pour ce ``provider``, filtre par ``matches_email_trigger``, lance un run
  par correspondance. Une erreur de lancement pour UN trigger ne doit jamais
  empêcher les autres (chacun est indépendant) ni casser le pipeline agent
  appelant — c'est à L'APPELANT (le handler webhook) d'envelopper cet appel
  en best-effort, comme les autres hooks post-run de ce pipeline
  (``_emit_run_outcome``, ``_augment_agent_response``).
"""

from __future__ import annotations

import json
from logging import getLogger

from apowerb.core import workflow_triggers as wt

logger = getLogger(__name__)


def matches_email_trigger(cfg: dict, *, from_addr: str, subject: str) -> bool:
    """``True`` si les filtres (sous-chaîne, insensibles à la casse) du
    trigger correspondent à cet e-mail. Un filtre absent (``None``/vide)
    correspond toujours."""
    from_filter = cfg.get("from_filter")
    if from_filter and from_filter.lower() not in (from_addr or "").lower():
        return False
    subject_filter = cfg.get("subject_filter")
    if subject_filter and subject_filter.lower() not in (subject or "").lower():
        return False
    return True


async def dispatch_email_triggers(
    *, provider: str, owner_id: str, envelope: dict
) -> list[str]:
    """Lance un run pour chaque trigger ``email`` actif de ``owner_id`` dont
    le ``provider`` et les filtres correspondent. Renvoie les ``run_id``
    effectivement lancés.

    ``envelope`` est transmis TEL QUEL comme payload du run — sa forme
    (``from``, ``to``, ``subject``, ``body``, ``received_at``,
    ``attachments``) est celle du contrat, jamais le contenu brut des pièces
    jointes (l'appelant ne doit passer que les métadonnées).
    """
    started: list[str] = []
    for row in wt.list_active_triggers("email", owner_id=owner_id):
        cfg = json.loads(row["config"] or "{}")
        if cfg.get("provider") != provider:
            continue
        if not matches_email_trigger(
            cfg, from_addr=envelope.get("from", ""), subject=envelope.get("subject", "")
        ):
            continue
        workflow_id = row["workflow_id"]
        try:
            run_id, _task = await wt.launch_triggered_run(
                workflow_id=workflow_id,
                owner_id=owner_id,
                kind="email",
                detail={"provider": provider},
                payload=envelope,
            )
        except wt.TriggerNotActive:
            logger.info(
                "[email_triggers] workflow %s non publié — notification ignorée",
                workflow_id,
            )
            continue
        started.append(run_id)
    return started
