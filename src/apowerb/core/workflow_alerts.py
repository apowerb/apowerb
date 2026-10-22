"""Alerte d'échec d'un workflow : « Me prévenir en cas d'échec ».

Réglage du workflow ``settings.notify_on_failure`` (dans son graphe) : absent,
l'alerte suit le statut — active sur un workflow publié, muette sur un
brouillon ; ``true`` ou ``false`` l'imposent. Elle part par email au
propriétaire, et sur son webhook Teams s'il en a configuré un (mêmes canaux
que le nœud notification).

Quand :

* un run de graphe persisté finit en ``error`` (``notify_run_finished``,
  point d'accroche commun à toutes les origines) — jamais un « Tester
  jusqu'ici » (``config.until``) ni un run annulé ;
* une décision humaine expire (``alert_expired_decisions``, appelé par la
  boucle du planificateur : l'échéance est appliquée à la lecture et ne
  passe pas par la fin d'un flux).

Anti-tempête :

* une alerte par run : ``agent_runs.failure_alert`` est réservé par un
  ``UPDATE … WHERE failure_alert IS NULL`` avant tout envoi, ce qui tient
  entre réplicas et entre les deux chemins ci-dessus ;
* une alerte par workflow et par heure (``ALERT_COOLDOWN``) : les échecs
  suivants sont comptés (``workflow_failure_alerts.grouped``) et la
  prochaine alerte dit combien ont été regroupés. Un workflow planifié qui
  échoue en boucle envoie donc au plus 24 alertes par jour.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from apowerb.core import run_main

logger = logging.getLogger(__name__)

ALERT_COOLDOWN = timedelta(hours=1)
# Une décision close depuis plus longtemps (découverte après un arrêt du
# serveur, ou au déploiement de cette alerte) ne réveille plus personne.
EXPIRED_LOOKBACK = timedelta(days=1)
_MAX_ERROR_CHARS = 500
_TEAMS_NODE_ID = "failure_alert"
_STAMP = "%Y-%m-%d %H:%M:%S.%f"  # celui de run_main._now : triable

# Issue de la réservation, écrite dans agent_runs.failure_alert.
CLAIMED, SENT, GROUPED, OFF, FAILED = "claimed", "sent", "grouped", "off", "failed"


def _clock() -> datetime:
    return datetime.now()


def alert_enabled(workflow: dict) -> bool:
    settings = (workflow.get("graph") or {}).get("settings") or {}
    value = settings.get("notify_on_failure")
    if value is None:
        return workflow.get("status") == "published"
    return bool(value)


def _claim(run_id: str) -> bool:
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        taken = conn.execute(
            t.update()
            .where(t.c.run_id == run_id, t.c.failure_alert.is_(None))
            .values(failure_alert=CLAIMED)
        )
    return taken.rowcount == 1


def _settle(run_id: str, outcome: str) -> None:
    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        conn.execute(t.update().where(t.c.run_id == run_id).values(failure_alert=outcome))


def _take_slot(workflow_id: str, owner_id: str) -> Optional[int]:
    """Le droit d'alerter maintenant pour ce workflow.

    Renvoie le nombre d'échecs regroupés depuis la dernière alerte, ou
    ``None`` si une alerte est partie il y a moins d'``ALERT_COOLDOWN`` (cet
    échec est alors compté). Mise à jour conditionnelle sur l'horodatage lu :
    deux réplicas ne prennent jamais le même créneau.
    """
    t = run_main.run_store.failure_alert_table
    engine = run_main.run_store.engine
    now = _clock()
    stamp, cutoff = now.strftime(_STAMP), (now - ALERT_COOLDOWN).strftime(_STAMP)
    for _ in range(3):
        with engine.begin() as conn:
            row = conn.execute(select(t).where(t.c.workflow_id == workflow_id)).fetchone()
        if row is None:
            try:
                with engine.begin() as conn:
                    conn.execute(
                        t.insert().values(
                            workflow_id=workflow_id,
                            owner_id=owner_id,
                            last_sent_at=stamp,
                            grouped=0,
                        )
                    )
                return 0
            except IntegrityError:
                continue  # un autre réplica vient de l'écrire : relire
        row = row._mapping
        with engine.begin() as conn:
            if row["last_sent_at"] <= cutoff:
                won = conn.execute(
                    t.update()
                    .where(
                        t.c.workflow_id == workflow_id,
                        t.c.last_sent_at == row["last_sent_at"],
                    )
                    .values(last_sent_at=stamp, grouped=0)
                )
                if won.rowcount == 1:
                    return row["grouped"] or 0
                continue
            conn.execute(
                t.update()
                .where(t.c.workflow_id == workflow_id)
                .values(grouped=t.c.grouped + 1)
            )
            return None
    return None


async def _send_mail(to: str, subject: str, body: str) -> None:
    from apowerb.helpers.email_sender import send_email

    await send_email(to=to, subject=subject, body=body)


async def _send_teams(owner_id: str, subject: str, body: str) -> None:
    from apowerb.core.workflow_runtime import _notify_teams

    await _notify_teams(owner_id, _TEAMS_NODE_ID, subject, body)


def _message(workflow: dict, run_id: str, error_message: str, grouped: int) -> tuple[str, str]:
    name = workflow.get("name") or workflow.get("workflow_id")
    error = (error_message or "erreur sans message").strip()[:_MAX_ERROR_CHARS]
    lines = [
        f"Le workflow « {name} » a échoué.",
        f"Run : {run_id}",
        f"Erreur : {error}",
    ]
    if grouped:
        lines.append(
            f"{grouped} autre(s) échec(s) de ce workflow depuis la dernière alerte "
            "ont été regroupés (une alerte par heure au plus)."
        )
    lines.append(
        "Pour ne plus recevoir ces alertes : réglages du workflow, "
        "« Me prévenir en cas d'échec »."
    )
    return f"Échec du workflow « {name} »", "\n".join(lines)


async def alert_run_failed(
    *, run_id: str, owner_id: str, workflow_id: str, error_message: Optional[str]
) -> Optional[str]:
    """Alerte pour ce run en échec ; renvoie l'issue, ``None`` si déjà traité."""
    from apowerb.core import workflow_main

    if not _claim(run_id):
        return None
    workflow = workflow_main.get_workflow(workflow_id, owner_id=owner_id)
    if workflow is None or not alert_enabled(workflow):
        _settle(run_id, OFF)
        return OFF
    grouped = _take_slot(workflow_id, owner_id)
    if grouped is None:
        _settle(run_id, GROUPED)
        return GROUPED
    subject, body = _message(workflow, run_id, error_message or "", grouped)
    outcome = SENT
    try:
        await _send_mail(owner_id, subject, body)
    except Exception:  # noqa: BLE001 - une alerte ratée ne doit rien casser
        logger.exception("[alerts] email d'échec non envoyé (run_id=%s)", run_id)
        outcome = FAILED
    try:
        await _send_teams(owner_id, subject, body)
    except Exception as exc:  # noqa: BLE001
        if getattr(exc, "code", None) != "teams_not_configured":
            logger.exception("[alerts] alerte Teams non envoyée (run_id=%s)", run_id)
    _settle(run_id, outcome)
    return outcome


async def alert_expired_decisions() -> int:
    """Alerte pour chaque décision expirée récemment ; renvoie le nombre traité."""
    run_main.expire_due_decisions()
    t = run_main.run_store.run_table
    since = (_clock() - EXPIRED_LOOKBACK).strftime(_STAMP)
    with run_main.run_store.engine.begin() as conn:
        rows = conn.execute(
            select(t.c.run_id, t.c.owner_id, t.c.config).where(
                t.c.status == run_main.STATUS_ERROR,
                t.c.error_message == run_main.EXPIRED_MESSAGE,
                t.c.failure_alert.is_(None),
                t.c.finished_at >= since,
            )
        ).fetchall()
    done = 0
    for row in rows:
        config = _config(row.config)
        if not config.get("workflow_id") or config.get("until"):
            continue
        try:
            await alert_run_failed(
                run_id=row.run_id,
                owner_id=row.owner_id,
                workflow_id=config["workflow_id"],
                error_message=run_main.EXPIRED_MESSAGE,
            )
            done += 1
        except Exception:  # noqa: BLE001 - un run en panne n'arrête pas les autres
            logger.exception("[alerts] décision expirée non signalée (run_id=%s)", row.run_id)
    return done


def _config(raw: Any) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
