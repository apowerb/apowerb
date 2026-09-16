"""Ce qui est arrivé à chaque signalement, et ce qui attend trop longtemps.

Demande du 14/09/2026 : pouvoir retracer la vie d'un signalement, et être
prévenu quand un bug attend depuis plus de deux jours sans être corrigé.

Ce module sépare deux choses :

- **la décision**, en fonctions pures — ce qui a changé lors d'une mise à jour,
  ce qui doit passer « résolu », ce qui est en retard. Elles se testent sans
  base, sans GitHub et sans horloge ;
- **l'exécution** — lire la base, interroger GitHub, écrire le journal,
  prévenir. Elle ne fait qu'appliquer le plan.

« Corrigé » veut dire résolu, rejeté ou doublon. Trier un signalement ou en
faire une issue n'est pas le corriger : ces deux états restent en retard.

Le suivi des issues n'est pas un à-côté de l'alerte, il en est la moitié. Sans
lui, un signalement transmis en issue n'aurait aucun moyen de passer « résolu »
quand l'issue est fermée, et l'alerte sonnerait chaque jour sur un bug réglé.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence

from apowerb.configs.th2logger import setup_logging

logger = setup_logging(__name__)

# Sortis du flux : plus rien à corriger, donc plus rien à alerter.
CLOSED_STATUSES = frozenset({"resolved", "rejected", "duplicate"})

OVERDUE_AFTER = timedelta(hours=48)
# Une relance par jour tant que rien ne bouge : assez pour ne pas oublier,
# pas assez pour qu'on finisse par couper la cloche.
REALERT_EVERY = timedelta(hours=24)
CHECK_INTERVAL_SECONDS = 3600

EVENT_CREATED = "created"
EVENT_DUPLICATE_RECEIVED = "duplicate_received"
EVENT_STATUS_CHANGED = "status_changed"
EVENT_SEVERITY_CHANGED = "severity_changed"
EVENT_AREA_CHANGED = "area_changed"
EVENT_NOTE_CHANGED = "note_changed"
EVENT_ISSUE_CREATED = "issue_created"
EVENT_ISSUE_CLOSED = "issue_closed"
EVENT_OVERDUE_ALERTED = "overdue_alerted"


@dataclass(frozen=True)
class PendingEvent:
    """Un événement décidé, pas encore écrit."""

    kind: str
    from_value: Optional[str] = None
    to_value: Optional[str] = None
    detail: Optional[str] = None


@dataclass
class TickPlan:
    to_resolve: list[Any] = field(default_factory=list)
    to_alert: list[Any] = field(default_factory=list)


@dataclass(frozen=True)
class Notice:
    """Ce que l'auteur d'un signalement doit apprendre, décidé avant d'être envoyé."""

    user_id: int
    title: str
    message: str
    type: str


def _aware(moment: Optional[datetime]) -> Optional[datetime]:
    """La colonne est `TIMESTAMPTZ`, mais une doublure ou un pilote peut rendre
    une date sans fuseau ; comparer naïf et conscient lève en Python."""
    if moment is None:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Décision
# ---------------------------------------------------------------------------


def is_overdue(report: Any, *, last_alert_at: Optional[datetime], now: datetime) -> bool:
    """Non corrigé depuis plus de 48 h, et pas déjà alerté dans les dernières 24 h."""
    if report.status in CLOSED_STATUSES:
        return False
    created = _aware(report.created_at)
    if created is None or now - created < OVERDUE_AFTER:
        return False
    last = _aware(last_alert_at)
    return last is None or now - last >= REALERT_EVERY


def plan_tick(
    reports_with_last_alert: Iterable[tuple[Any, Optional[datetime]]],
    *,
    issue_states: dict[int, str],
    now: datetime,
) -> TickPlan:
    """Ce qu'une passe doit faire.

    Une issue fermée résout son signalement, et ce signalement n'est pas alerté
    dans la même passe. Un état d'issue inconnu — GitHub indisponible, jeton
    sans droit — ne résout rien : on ne déclare pas un bug corrigé faute de
    réponse.
    """
    plan = TickPlan()
    for report, last_alert_at in reports_with_last_alert:
        if (
            report.status not in CLOSED_STATUSES
            and report.issue_number is not None
            and issue_states.get(report.issue_number) == "closed"
        ):
            plan.to_resolve.append(report)
            continue
        if is_overdue(report, last_alert_at=last_alert_at, now=now):
            plan.to_alert.append(report)
    plan.to_alert.sort(key=lambda r: _aware(r.created_at))
    return plan


def diff_update(report: Any, **new_values: Optional[str]) -> list[PendingEvent]:
    """Les événements qu'une mise à jour d'administrateur doit laisser.

    Seuls les champs qui changent vraiment laissent une trace. Le contenu d'une
    note n'y figure jamais : le journal est lu par d'autres, et une note peut
    contenir n'importe quoi — il dit qu'elle a changé, pas ce qu'elle dit.
    """
    kinds = {
        "status": EVENT_STATUS_CHANGED,
        "severity": EVENT_SEVERITY_CHANGED,
        "area": EVENT_AREA_CHANGED,
    }
    events: list[PendingEvent] = []
    for name, kind in kinds.items():
        if name not in new_values or new_values[name] is None:
            continue
        before = getattr(report, name, None)
        after = new_values[name]
        if before != after:
            events.append(PendingEvent(kind, from_value=before, to_value=after))
    if "admin_note" in new_values and new_values["admin_note"] is not None:
        if (getattr(report, "admin_note", None) or "") != (new_values["admin_note"] or ""):
            events.append(PendingEvent(EVENT_NOTE_CHANGED))
    return events


def closure_notice(
    report: Any, *, to_status: str, actor_user_id: Optional[int] = None
) -> Optional[Notice]:
    """Ce qu'il faut dire à l'auteur quand son signalement passe à ``to_status``.

    Constat du 15/09/2026 : seule la fermeture d'une issue prévenait l'auteur,
    jamais le triage, jamais un rejet ni un doublon.

    Rien n'est dit hors d'un passage d'un état ouvert à un état clos : c'est ce
    qui garantit une seule notification par clôture, que le signalement soit
    clos par le triage puis ignoré par la veille, ou l'inverse. Personne à
    prévenir pour un signalement anonyme, ni pour l'administrateur qui clôt le
    sien. La note de triage n'accompagne que le rejet, seul cas où l'auteur
    attend une explication ; les journaux joints ne partent jamais.
    """
    author = getattr(report, "user_id", None)
    if (
        to_status not in CLOSED_STATUSES
        or getattr(report, "status", None) in CLOSED_STATUSES
        or not author
        or author == actor_user_id
    ):
        return None

    number = f"#{report.id}"
    subject = f"« {report.title} »"
    if to_status == "resolved":
        return Notice(author, f"Votre signalement {number} est corrigé",
                      f"{subject} est marqué corrigé.", "success")
    if to_status == "rejected":
        message = f"{subject} a été examiné et ne sera pas traité comme un bug."
        note = (getattr(report, "admin_note", None) or "").strip()
        if note:
            message += f"\n\nNote : {note}"
        return Notice(author, f"Votre signalement {number} a été examiné", message, "info")
    original = getattr(report, "duplicate_of", None)
    if original:
        return Notice(author, f"Votre signalement {number} rejoint le #{original}, déjà suivi",
                      f"{subject} décrit un problème déjà signalé, suivi sous le #{original}.", "info")
    return Notice(author, f"Votre signalement {number} est déjà suivi",
                  f"{subject} décrit un problème déjà signalé.", "info")


def _age_label(report: Any, now: datetime) -> str:
    hours = int((now - _aware(report.created_at)).total_seconds() // 3600)
    return f"{hours // 24} j" if hours >= 48 else f"{hours} h"


def build_overdue_digest(reports: Sequence[Any], *, now: datetime) -> tuple[str, str]:
    """Un seul message pour tous les retards d'une passe.

    Un message par bug, chaque jour, à chaque superadministrateur, transformerait
    la cloche en bruit de fond — et un bruit de fond, on cesse de l'entendre.
    """
    count = len(reports)
    noun = "signalement" if count == 1 else "signalements"
    verb = "attend" if count == 1 else "attendent"
    title = f"{count} {noun} {verb} depuis plus de 48 h"
    lines = [
        f"- #{r.id} {r.title} — {_age_label(r, now)}"
        + (" (bloquant)" if getattr(r, "severity", None) == "blocker" else "")
        for r in reports
    ]
    body = (
        "Ces signalements ne sont ni résolus, ni rejetés, ni marqués doublons :\n\n"
        + "\n".join(lines)
        + "\n\nÉcran de triage : panneau d'administration, onglet Bugs."
    )
    return title, body


# ---------------------------------------------------------------------------
# Exécution
# ---------------------------------------------------------------------------


def record_events(
    db: Any,
    report_id: Optional[int],
    events: Iterable[PendingEvent],
    *,
    actor_user_id: Optional[int] = None,
) -> None:
    """Ajoute les événements à la session, sans commit : ils partent avec la
    transaction de la transition qu'ils décrivent, ou pas du tout."""
    from apowerb.models import BugReportEvent

    for event in events:
        db.add(
            BugReportEvent(
                bug_report_id=report_id,
                kind=event.kind,
                actor_user_id=actor_user_id,
                from_value=(event.from_value or None) and str(event.from_value)[:60],
                to_value=(event.to_value or None) and str(event.to_value)[:60],
                detail=event.detail,
            )
        )


async def list_events(db: Any, report_id: int) -> list[dict[str, Any]]:
    """La frise d'un signalement, de la plus ancienne à la plus récente."""
    from sqlalchemy import select

    from apowerb.models import BugReportEvent, User

    rows = (
        await db.execute(
            select(BugReportEvent, User.email)
            .outerjoin(User, User.user_id == BugReportEvent.actor_user_id)
            .where(BugReportEvent.bug_report_id == report_id)
            .order_by(BugReportEvent.created_at.asc(), BugReportEvent.id.asc())
        )
    ).all()
    return [
        {
            "kind": event.kind,
            "from_value": event.from_value,
            "to_value": event.to_value,
            "detail": event.detail,
            "actor_email": email,
            "created_at": event.created_at,
        }
        for event, email in rows
    ]


async def _superadmins(db: Any) -> list[tuple[int, Optional[str]]]:
    """Les superadministrateurs et leur adresse. Vide si la table n'existe pas :
    un déploiement sans panneau d'administration n'a personne à qui l'envoyer
    dans l'application, et l'e-mail se rabat alors sur `super_admin_email`."""
    from sqlalchemy import text

    from apowerb.configs.settings import get_settings

    schema = get_settings().db_schema
    try:
        rows = (
            await db.execute(
                text(
                    f'SELECT u.user_id, u.email FROM {schema}.admin_superadmin s '
                    f'JOIN {schema}."user" u ON u.user_id = s.user_id'
                )
            )
        ).all()
    except Exception as exc:  # noqa: BLE001
        logger.warning("bug_report_watch: superadministrateurs illisibles (%s)", exc)
        return []
    return [(row[0], row[1]) for row in rows]


async def close_with_duplicates(
    db: Any,
    canonical: Any,
    to_status: str,
    *,
    actor_user_id: Optional[int] = None,
    record_canonical: bool = True,
) -> list[Notice]:
    """Clôt un signalement ET les doublons qui le suivent.

    Vu en dev le 16/09/2026 : deux doublons affichés « Issue créée » alors que
    leur issue était fermée depuis deux jours. La veille les écarte de sa
    sélection — les alerter compterait deux fois le même défaut — donc elle ne
    les résolvait jamais, et le triage ne regardait que le signalement ouvert.
    Une seule clôture ici, pour les deux chemins.

    Rend les avis à envoyer APRÈS le commit : l'auteur d'un doublon attend une
    réponse à son envoi, sans avoir à savoir qu'il a été rattaché à un autre.
    """
    from sqlalchemy import select

    from apowerb.models import BugReport

    notices = [closure_notice(canonical, to_status=to_status, actor_user_id=actor_user_id)]
    # Le triage a déjà tracé son changement de statut via `diff_update` : le
    # retracer ici ferait deux lignes pour une seule décision.
    if record_canonical:
        record_events(
            db,
            canonical.id,
            [PendingEvent(EVENT_STATUS_CHANGED, from_value=canonical.status, to_value=to_status)],
            actor_user_id=actor_user_id,
        )
    canonical.status = to_status

    duplicates = list(
        (
            await db.execute(
                select(BugReport).where(
                    BugReport.duplicate_of == canonical.id,
                    BugReport.status.notin_(sorted(CLOSED_STATUSES)),
                )
            )
        ).scalars()
    )
    for duplicate in duplicates:
        # La requête les écarte déjà ; la garde tient si l'appelant fournit
        # sa propre liste.
        if duplicate.status in CLOSED_STATUSES:
            continue
        notices.append(
            closure_notice(duplicate, to_status=to_status, actor_user_id=actor_user_id)
        )
        record_events(
            db,
            duplicate.id,
            [
                PendingEvent(
                    EVENT_STATUS_CHANGED,
                    from_value=duplicate.status,
                    to_value=to_status,
                    detail=f"suit le signalement #{canonical.id}",
                )
            ],
            actor_user_id=actor_user_id,
        )
        duplicate.status = to_status

    return [n for n in notices if n is not None]


async def send_notice(notice: Notice) -> None:
    """Prévient l'auteur. Sans lien : il n'a aucune page qui liste ses
    signalements, et un lien vers l'accueil ne mène à rien."""
    await _notify_in_app(notice.user_id, notice.title, notice.message, None, type_=notice.type)


async def _notify_in_app(
    user_id: int, title: str, message: str, link: Optional[str], *, type_: str = "warning"
) -> None:
    """Une notification en base, poussée en temps réel. Ne lève jamais."""
    from apowerb.helpers.database import sessionmanager
    from apowerb.helpers.notification_bus import notify as push_notification
    from apowerb.models import Notification

    try:
        async with sessionmanager.session() as db:
            notification = Notification(
                user_id=user_id,
                title=title[:255],
                message=message,
                type=type_,
                link=link,
                metadata_json=json.dumps({"source": "bug_reports"}),
                is_read=False,
            )
            db.add(notification)
            await db.commit()
            await db.refresh(notification)
            await push_notification(
                user_id,
                {
                    "id": notification.id,
                    "title": notification.title,
                    "message": notification.message,
                    "type": notification.type,
                    "link": notification.link,
                    "is_read": False,
                    "created_at": notification.created_at.isoformat()
                    if notification.created_at
                    else None,
                },
            )
    except Exception as exc:  # noqa: BLE001 — une notification ne vaut pas la veille
        logger.error("bug_report_watch: notification à %s échouée : %s", user_id, exc)


async def run_tick(now: Optional[datetime] = None) -> dict[str, int]:
    """Une passe : résoudre ce que GitHub a fermé, alerter ce qui traîne."""
    from sqlalchemy import func, select

    from apowerb.bug_reports.service import build_sink
    from apowerb.configs.settings import get_settings
    from apowerb.core.config_admin.store import read_posed
    from apowerb.helpers import email_sender
    from apowerb.helpers.database import sessionmanager
    from apowerb.models import BugReport, BugReportEvent

    now = now or datetime.now(timezone.utc)

    async with sessionmanager.session() as db:
        # Un doublon suit son signalement canonique : l'alerter aussi
        # compterait deux fois le même défaut dans le récapitulatif.
        reports = list(
            (
                await db.execute(
                    select(BugReport).where(
                        BugReport.status.notin_(sorted(CLOSED_STATUSES)),
                        BugReport.duplicate_of.is_(None),
                    )
                )
            ).scalars()
        )
        if not reports:
            return {"resolved": 0, "alerted": 0}

        ids = [r.id for r in reports]
        last_alerts = dict(
            (
                await db.execute(
                    select(BugReportEvent.bug_report_id, func.max(BugReportEvent.created_at))
                    .where(
                        BugReportEvent.kind == EVENT_OVERDUE_ALERTED,
                        BugReportEvent.bug_report_id.in_(ids),
                    )
                    .group_by(BugReportEvent.bug_report_id)
                )
            ).all()
        )

        issue_states: dict[int, str] = {}
        with_issue = [r for r in reports if r.issue_number]
        if with_issue:
            try:
                posed = await read_posed(
                    db,
                    ("BUG_REPORT_GITHUB_REPO", "BUG_REPORT_GITHUB_TOKEN"),
                )
            except Exception:  # noqa: BLE001
                posed = {}
            sink = build_sink(posed)
            if sink is not None:
                for report in with_issue:
                    state = await asyncio.to_thread(sink.get_issue_state, report.issue_number)
                    if state:
                        issue_states[report.issue_number] = state

        plan = plan_tick(
            [(r, last_alerts.get(r.id)) for r in reports],
            issue_states=issue_states,
            now=now,
        )

        # Tout ce qui sert après le commit est lu AVANT : `commit()` expire les
        # objets, et les relire en asyncio lève `MissingGreenlet` (vécu le
        # 10/09/2026 sur ce même domaine).
        notices = []
        for report in plan.to_resolve:
            record_events(
                db,
                report.id,
                [PendingEvent(EVENT_ISSUE_CLOSED, from_value=report.status, to_value="resolved",
                              detail=f"issue #{report.issue_number} fermée")],
            )
            notices.extend(await close_with_duplicates(db, report, "resolved"))

        digest = build_overdue_digest(plan.to_alert, now=now) if plan.to_alert else None
        for report in plan.to_alert:
            record_events(db, report.id, [PendingEvent(EVENT_OVERDUE_ALERTED)])

        superadmins = await _superadmins(db) if digest else []
        await db.commit()

    for notice in notices:
        await send_notice(notice)

    if digest:
        title, body = digest
        for user_id, _email in superadmins:
            await _notify_in_app(user_id, title, body, "/admin")
        recipients = [email for _uid, email in superadmins if email]
        if not recipients and get_settings().super_admin_email:
            recipients = [get_settings().super_admin_email]
        for recipient in recipients:
            try:
                await email_sender.send_email(to=recipient, subject=title, body=body)
            except Exception as exc:  # noqa: BLE001
                logger.error("bug_report_watch: e-mail à %s échoué : %s", recipient, exc)

    logger.info(
        "bug_report_watch: %d résolu(s) par issue fermée, %d en retard alerté(s)",
        len(plan.to_resolve), len(plan.to_alert),
    )
    return {"resolved": len(plan.to_resolve), "alerted": len(plan.to_alert)}


async def bug_report_watch_loop() -> None:
    """Boucle horaire. Ne meurt jamais : une passe qui lève est journalisée,
    la suivante repart."""
    logger.info("bug_report_watch: boucle démarrée (intervalle=%ds)", CHECK_INTERVAL_SECONDS)
    await asyncio.sleep(90)  # laisser le démarrage et les migrations se poser
    while True:
        try:
            await run_tick()
        except Exception as exc:  # noqa: BLE001
            logger.error("bug_report_watch: passe en échec : %s", exc)
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
