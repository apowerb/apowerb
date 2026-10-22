"""Décision humaine : prévenir qui doit répondre, rappeler, annoncer la fin.

Un run arrêté sur un nœud ``approval`` (``run_main.pause_run``) attend une
personne : l'``assignee`` du nœud, sinon le propriétaire. Ce module la
prévient (mail, et Teams si elle a configuré son webhook), lui rappelle la
décision avant l'échéance, puis annonce au propriétaire une fin sans réponse
(expirée, abandonnée, instantané illisible) — et la fait passer par
``workflow_triggers.notify_run_finished``, pour que les déclencheurs « End of
a workflow » la voient comme toute autre fin.

Quand : aussitôt (``deliver``, appelé par le flux qui s'arrête et par
l'abandon) et à chaque passage du planificateur (``tick``, depuis
``flow_scheduler``), qui reprend ce qu'un process arrêté entre-temps n'a pas
envoyé et applique les échéances.

Une seule fois : chaque avis est réservé sur sa colonne d'``agent_runs``
(``notified_at``, ``reminded_at``, ``end_notice``) par un ``UPDATE ... WHERE``
qui ne réussit qu'une fois, entre deux passages comme entre réplicas, avant
de partir. Un envoi en échec n'est donc pas retenté : au plus une fois,
jamais en boucle.

Ce que dit un message : la question, le nom du workflow, l'échéance et un
lien vers la page Workflows, qui demande la connexion. Aucun jeton dans le
lien, aucune autre donnée du run.
"""

from __future__ import annotations

import asyncio
import html
import re
from datetime import datetime, timedelta, timezone
from logging import getLogger
from typing import Any, Optional

logger = getLogger(__name__)

_DEADLINE_FORMAT = "%d/%m/%Y à %H:%M UTC"

# Ce qui fait un lien, une emphase ou un titre dans le Markdown d'une carte
# Adaptive : « [texte](url) » venu d'un run deviendrait un lien déguisé.
_MARKDOWN = re.compile(r"([\\`*_~>#\[\]()])")

# Envois lancés par ``deliver`` : ils finissent même si le flux qui les a
# demandés est annulé (client parti).
_inflight: set[asyncio.Task] = set()


def _app_url() -> str:
    from apowerb.configs.settings import get_settings

    return get_settings().app_public_url.rstrip("/")


async def _send_mail(*, to: str, subject: str, html: str, text: str) -> None:
    from apowerb.helpers.system_mailer import send_system_email

    await send_system_email(to=to, subject=subject, html=html, text=text)


async def _send_teams(*, to: str, subject: str, body: str) -> None:
    """Carte sur le webhook Teams de ``to``, s'il en a configuré un.

    Une adresse qui n'est pas un compte (assigné externe) ou sans webhook :
    rien à envoyer. URL revalidée à l'envoi, sans redirection, comme le nœud
    ``notify`` (``workflow_runtime._notify_teams``).
    """
    import httpx

    from apowerb.core.workflow_runtime import _teams_card
    from apowerb.integrations.teams import (
        get_teams_webhook_url_for_owner,
        validate_teams_webhook_url,
    )
    from apowerb.users.exceptions import UserNotFoundException

    try:
        url = await get_teams_webhook_url_for_owner(to)
    except UserNotFoundException:
        return
    if not url:
        return
    url = validate_teams_webhook_url(url)
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
        resp = await client.post(url, json=_teams_card(subject, body))
    resp.raise_for_status()


# --- Lecture et réservation ---------------------------------------------------


def _rows(*where, run_id: Optional[str]) -> list[dict]:
    from apowerb.core import run_main

    t = run_main.run_store.run_table
    if run_id is not None:
        where = (*where, t.c.run_id == run_id)
    with run_main.run_store.engine.begin() as conn:
        # Jamais ``paused_state`` : l'instantané peut peser des Mio.
        rows = conn.execute(
            t.select()
            .with_only_columns(
                t.c.run_id,
                t.c.owner_id,
                t.c.assignee,
                t.c.config,
                t.c.decision,
                t.c.status,
                t.c.error_message,
                t.c.expires_at,
                t.c.remind_at,
            )
            .where(*where)
        ).fetchall()
    return [row._asdict() for row in rows]


def _reserve(run_id: str, *where, **values) -> bool:
    """``UPDATE ... SET values WHERE run_id AND where`` : vrai pour un seul."""
    from apowerb.core import run_main

    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        done = conn.execute(
            t.update().where(t.c.run_id == run_id, *where).values(**values)
        )
    return done.rowcount == 1


# --- Messages -----------------------------------------------------------------


def _workflow_name(row: dict) -> str:
    import json

    from apowerb.core import workflow_main

    try:
        workflow_id = json.loads(row.get("config") or "{}").get("workflow_id")
    except ValueError:
        workflow_id = None
    wf = (
        workflow_main.get_workflow(workflow_id, owner_id=row["owner_id"])
        if workflow_id
        else None
    )
    return (wf or {}).get("name") or "Workflow"


def _markdown_text(text: str) -> str:
    return _MARKDOWN.sub(r"\\\1", text)


def _deadline(stamp: Optional[str]) -> Optional[str]:
    if not stamp:
        return None
    moment = datetime.fromisoformat(stamp)
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment.astimezone(timezone.utc).strftime(_DEADLINE_FORMAT)


async def _send(to: str, subject: str, lines: list[str], link: str) -> None:
    """Mail et Teams ; un canal en panne n'éteint pas l'autre.

    ``lines`` sont du texte (la question vient du run) : échappées pour le
    HTML du mail et pour le Markdown de la carte Teams, jamais injectées
    telles quelles. Seul ``link`` reste un lien.
    """
    from apowerb.helpers.system_mailer import render_branded_email

    page = render_branded_email(
        heading=html.escape(subject),
        intro="<br>".join(html.escape(line) for line in lines),
        cta_label="Ouvrir la page Workflows",
        cta_url=html.escape(link, quote=True),
        note="La page te demande de te connecter avec le compte qui doit répondre.",
    )
    text = "\n".join([*lines, "", link])
    try:
        await _send_mail(to=to, subject=subject, html=page, text=text)
    except Exception:  # noqa: BLE001 - l'autre canal part quand même
        logger.exception("[decisions] mail non envoyé à %s (%s)", to, subject)
    card = "\n".join([*(_markdown_text(line) for line in lines), "", link])
    try:
        await _send_teams(to=to, subject=_markdown_text(subject), body=card)
    except Exception:  # noqa: BLE001
        logger.exception("[decisions] carte Teams non envoyée à %s (%s)", to, subject)


async def _ask(row: dict, *, reminder: bool) -> None:
    import json

    decision = json.loads(row.get("decision") or "{}")
    name = _workflow_name(row)
    subject = f"{'Rappel : décision' if reminder else 'Décision'} attendue — {name}"
    lines = [
        f"Le workflow « {name} » attend ta décision :",
        decision.get("question") or "",
    ]
    deadline = _deadline(decision.get("expires_at"))
    if deadline:
        lines.append(f"Sans réponse le {deadline}, le run s'arrêtera.")
    link = f"{_app_url()}/workflows?decision={row['run_id']}"
    await _send(row.get("assignee") or row["owner_id"], subject, lines, link)


async def _tell_end(row: dict) -> None:
    name = _workflow_name(row)
    word, sentence = _end_wording(row)
    subject, link = f"Décision {word} — {name}", f"{_app_url()}/workflows"
    owner = row["owner_id"]
    await _send(owner, subject, [f"Workflow « {name} ».", sentence], link)
    assignee = row.get("assignee")
    if assignee and assignee.lower() != owner.lower():
        # Elle avait été sollicitée : elle sait qu'elle n'a plus à répondre.
        await _send(
            assignee,
            subject,
            [f"Le workflow « {name} » n'attend plus ta décision.", sentence],
            link,
        )


def _end_wording(row: dict) -> tuple[str, str]:
    from apowerb.core import run_main

    if row["status"] == run_main.STATUS_CANCELLED:
        return "abandonnée", "La décision a été abandonnée : le run est arrêté."
    if row.get("error_message") == run_main.EXPIRED_MESSAGE:
        return "expirée", "Personne n'a répondu avant l'échéance : le run est arrêté."
    return "illisible", "La décision ne peut plus être reprise : le run est arrêté."


async def _announce_end(row: dict) -> None:
    """Avis de fin, puis la fin elle-même aux déclencheurs, quoi qu'il
    arrive à l'avis (workflow introuvable, envoi en panne)."""
    from apowerb.core import workflow_triggers

    try:
        await _tell_end(row)
    except Exception:  # noqa: BLE001 - les déclencheurs doivent voir la fin
        logger.exception("[decisions] avis de fin en échec (run_id=%s)", row["run_id"])
    await workflow_triggers.notify_run_finished(
        run_id=row["run_id"],
        owner_id=row["owner_id"],
        status=row["status"],
        output=None,
        error_message=row.get("error_message"),
    )


# --- Passages -----------------------------------------------------------------


def _remind_moment(row: dict) -> Optional[datetime]:
    """L'heure du rappel ; recalculée pour une pause d'avant ``remind_at``.

    ``None`` si l'échéance est illisible : la ligne est sautée, les autres
    passent (``_expire_due`` la saute de même).
    """
    import json

    from apowerb.core import run_main
    from apowerb.core.workflow_graph import DEFAULT_DECISION_HOURS

    try:
        if row.get("remind_at"):
            return run_main._instant(row["remind_at"])
        if not row.get("expires_at"):
            return None
        expires = run_main._instant(row["expires_at"])
        paused_at = json.loads(row.get("decision") or "{}").get("paused_at")
        paused = (
            datetime.fromisoformat(paused_at)
            if paused_at
            else expires - timedelta(hours=DEFAULT_DECISION_HOURS)
        )
        return run_main.remind_moment(paused, expires)
    except ValueError:
        logger.error("[decisions] échéance illisible (run_id=%s)", row.get("run_id"))
        return None


async def tick(now: Optional[datetime] = None, *, run_id: Optional[str] = None) -> int:
    """Un passage : échéances appliquées, puis avis, rappels et fins dus.

    Renvoie le nombre d'avis réservés (donc tentés). ``run_id`` borne le
    passage à un run (``deliver``).
    """
    from apowerb.core import run_main

    now = now or datetime.now(timezone.utc)
    t = run_main.run_store.run_table
    stamp = now.isoformat()
    with run_main.run_store.engine.begin() as conn:
        run_main._expire_due(conn, now=now)

    count = 0
    awaiting = t.c.status == run_main.STATUS_AWAITING
    for row in _rows(awaiting, t.c.notified_at.is_(None), run_id=run_id):
        if _reserve(
            row["run_id"], awaiting, t.c.notified_at.is_(None), notified_at=stamp
        ):
            count += 1
            await _sending(
                _ask(row, reminder=False),
                row,
                t.c.notified_at == stamp,
                notified_at=None,
            )

    for row in _rows(
        awaiting,
        t.c.notified_at.isnot(None),
        t.c.reminded_at.is_(None),
        run_id=run_id,
    ):
        moment = _remind_moment(row)
        if moment is None or moment > now:
            continue
        if _reserve(
            row["run_id"], awaiting, t.c.reminded_at.is_(None), reminded_at=stamp
        ):
            count += 1
            await _sending(
                _ask(row, reminder=True),
                row,
                t.c.reminded_at == stamp,
                reminded_at=None,
            )

    pending = t.c.end_notice == run_main.END_NOTICE_PENDING
    for row in _rows(pending, run_id=run_id):
        if _reserve(row["run_id"], pending, end_notice="sent"):
            count += 1
            await _sending(
                _announce_end(row),
                row,
                t.c.end_notice == "sent",
                end_notice=run_main.END_NOTICE_PENDING,
            )
    return count


async def deliver(run_id: str) -> int:
    """Ce qui est dû pour ce run, tout de suite (pause, abandon, arrêt).

    Appelé dans le ``finally`` d'un flux : si le client part pendant l'envoi,
    l'annulation n'atteint pas l'envoi, qui finit dans sa propre tâche.
    """
    task = asyncio.create_task(tick(run_id=run_id))
    _inflight.add(task)
    task.add_done_callback(_inflight.discard)
    return await asyncio.shield(task)


async def _sending(job: Any, row: dict, *still_mine, **release) -> None:
    """Envoie un avis réservé ; s'il est annulé en route (extinction), la
    réservation est rendue pour que le passage suivant l'envoie."""
    try:
        await _guarded(job, row)
    except asyncio.CancelledError:
        _reserve(row["run_id"], *still_mine, **release)
        raise


async def _guarded(job: Any, row: dict) -> None:
    """Un avis en échec n'arrête ni le passage ni les autres runs."""
    try:
        await job
    except Exception:  # noqa: BLE001
        logger.exception("[decisions] avis en échec (run_id=%s)", row.get("run_id"))
