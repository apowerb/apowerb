"""Resume periodique et alertes KPI d'un tableau de bord spec v2.

Un abonnement (`BIDashboardDigest`) lie un tableau de bord a son proprietaire.
A l'echeance, le tableau de bord est recalcule avec les droits du proprietaire
(`compute_dashboard(..., owner=...)`, chargement scope a ce proprietaire) puis :

* le resume (valeurs et statut des KPI, textes de synthese des visuels `text`,
  lien `/bi/<id>`) part par mail (`send_system_email`) et en notification ;
* un KPI qui passe en `bad` declenche une alerte, une seule fois : l'etat
  « deja signale » est memorise dans l'abonnement et ne se reinitialise que
  lorsque le KPI revient a `good` / `warn`. Un statut inconnu (calcul en
  erreur) ne reinitialise rien.

La boucle de tick (`scheduler/bi_digest.py`) appelle `tick_once`. Le verrou
entre repliques est la ligne elle-meme (UPDATE conditionnel sur `next_*_at`),
comme pour les triggers `schedule` de workflow.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as _tz
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.bi.dashboards.core import ComponentType, Dashboard
from apowerb.bi.db_stores import DatabaseDashboardStore
from apowerb.bi.spec.engine import compute_dashboard
from apowerb.bi.spec.models import DashboardDataRequest, DashboardDataResponse
from apowerb.configs.settings import get_settings
from apowerb.helpers.notification_bus import notify as push_notification
from apowerb.helpers.system_mailer import send_system_email
from apowerb.models import BIDashboardDigest, Notification, User

logger = logging.getLogger(__name__)

UTC = _tz.utc
# Frequence de verification des alertes entre deux resumes.
ALERT_CHECK_INTERVAL = timedelta(hours=6)
FREQUENCIES = ("weekly", "daily")

_STATUS_LABEL = {"good": "OK", "warn": "Attention", "bad": "Critique", None: "—"}
_STATUS_COLOR = {"good": "#15803d", "warn": "#b45309", "bad": "#b91c1c", None: "#6b7280"}


def _aware(dt: datetime | None) -> datetime | None:
    """SQLite rend des datetimes naifs (UTC) ; Postgres des datetimes avec fuseau."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


# --------------------------------------------------------------------------- planification


def next_run(frequency: str, *, weekday: int | None, hour: int, tz: str, after: datetime) -> datetime:
    """Prochaine echeance (UTC) strictement apres `after`, a `hour` h locale."""
    zone = ZoneInfo(tz)
    local = after.astimezone(zone)
    candidate = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    if frequency == "daily":
        if candidate <= local:
            candidate += timedelta(days=1)
    else:
        candidate += timedelta(days=((weekday or 0) - local.weekday()) % 7)
        if candidate <= local:
            candidate += timedelta(days=7)
    return candidate.astimezone(UTC)


# --------------------------------------------------------------------------- extraction et rendu


@dataclass(frozen=True)
class KpiLine:
    id: str
    label: str
    formatted: str
    status: str | None


def extract(dashboard: Dashboard, response: DashboardDataResponse) -> tuple[list[KpiLine], list[str]]:
    """KPI calcules (ordre du tableau de bord) et textes de synthese rendus."""
    kpis: list[KpiLine] = []
    texts: list[str] = []
    for comp in dashboard.components:
        visual = comp.visual
        if comp.component_type != ComponentType.VISUAL or visual is None:
            continue
        result = response.components.get(comp.id)
        if result is None or result.error:
            continue
        if visual.type == "kpi" and result.value is not None and visual.kpi is not None:
            kpis.append(
                KpiLine(comp.id, visual.title or visual.kpi.measure, result.value.formatted, result.value.status)
            )
        elif visual.type == "text" and result.text:
            texts.append(result.text)
    return kpis, texts


def _kpi_ids(dashboard: Dashboard) -> set[str]:
    return {
        c.id
        for c in dashboard.components
        if c.component_type == ComponentType.VISUAL and c.visual is not None and c.visual.type == "kpi"
    }


def _page(dashboard: Dashboard, heading: str, intro: str, kpis: list[KpiLine], texts: list[str], link: str) -> str:
    rows = "".join(
        '<tr><td style="padding:8px 12px;border-bottom:1px solid #eef0f3;">'
        f"{escape(k.label)}</td>"
        '<td style="padding:8px 12px;border-bottom:1px solid #eef0f3;text-align:right;font-weight:600;">'
        f"{escape(k.formatted)}</td>"
        f'<td style="padding:8px 12px;border-bottom:1px solid #eef0f3;color:{_STATUS_COLOR[k.status]};">'
        f"{_STATUS_LABEL[k.status]}</td></tr>"
        for k in kpis
    )
    table = f'<table style="border-collapse:collapse;width:100%;font-size:14px;">{rows}</table>' if rows else ""
    summary = "".join(f'<p style="margin:12px 0;color:#374151;">{escape(t)}</p>' for t in texts)
    return (
        '<!DOCTYPE html><html><body style="margin:0;padding:24px;background:#f4f5f7;'
        'font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">'
        '<div style="max-width:560px;margin:0 auto;background:#fff;border-radius:12px;padding:24px;">'
        f'<h1 style="margin:0 0 8px;color:#061551;font-size:20px;">{escape(heading)}</h1>'
        f'<p style="margin:0 0 16px;color:#6b7280;font-size:14px;">{escape(intro)}</p>'
        f"{table}{summary}"
        f'<p style="margin:20px 0 0;"><a href="{escape(link)}" style="color:#3b82f6;">'
        "Ouvrir le tableau de bord</a></p>"
        '<p style="margin:20px 0 0;color:#9ca3af;font-size:12px;">E-mail automatique, merci de ne pas y répondre.</p>'
        "</div></body></html>"
    )


def _plain(intro: str, kpis: list[KpiLine], texts: list[str], link: str) -> str:
    lines = [intro, ""]
    lines += [f"{k.label} : {k.formatted} ({_STATUS_LABEL[k.status]})" for k in kpis]
    lines += ["", *texts, "", f"Ouvrir : {link}"]
    return "\n".join(lines)


def render_digest(
    dashboard: Dashboard, kpis: list[KpiLine], texts: list[str], *, link: str, frequency: str = "weekly"
) -> tuple[str, str, str]:
    """(objet, HTML, texte brut) du resume."""
    period = "quotidien" if frequency == "daily" else "hebdomadaire"
    subject = f"Résumé {period} : {dashboard.title}"
    intro = f"Voici le point {'du jour' if frequency == 'daily' else 'de la semaine'} sur vos indicateurs."
    return subject, _page(dashboard, dashboard.title, intro, kpis, texts, link), _plain(intro, kpis, texts, link)


def render_alert(dashboard: Dashboard, bad: list[KpiLine], *, link: str) -> tuple[str, str, str]:
    subject = f"Alerte indicateur : {dashboard.title}"
    intro = "Un indicateur vient de passer en statut critique."
    return subject, _page(dashboard, dashboard.title, intro, bad, [], link), _plain(intro, bad, [], link)


# --------------------------------------------------------------------------- stockage


class DigestStore:
    def __init__(self, db: AsyncSession) -> None:
        self._db = db

    async def get(self, dashboard_id: str, owner_email: str) -> BIDashboardDigest | None:
        q = select(BIDashboardDigest).where(
            BIDashboardDigest.dashboard_id == dashboard_id,
            func.lower(BIDashboardDigest.owner_email) == owner_email.lower(),
        )
        return (await self._db.execute(q)).scalars().first()

    async def upsert(
        self,
        *,
        dashboard_id: str,
        owner_email: str,
        now: datetime,
        frequency: str = "weekly",
        weekday: int | None = None,
        hour: int = 8,
        timezone: str = "Europe/Paris",
        alerts_enabled: bool = True,
    ) -> BIDashboardDigest:
        """Cree l'abonnement ou remplace ses reglages (un par tableau de bord
        et proprietaire). Reprogramme le resume ; reactiver les alertes
        repart d'un etat vierge."""
        weekday = (weekday or 0) if frequency == "weekly" else None
        row = await self.get(dashboard_id, owner_email)
        digest_at = next_run(frequency, weekday=weekday, hour=hour, tz=timezone, after=now)
        if row is None:
            row = BIDashboardDigest(
                dashboard_id=dashboard_id,
                owner_email=owner_email,
                next_alert_check_at=now + ALERT_CHECK_INTERVAL,
            )
            self._db.add(row)
        elif alerts_enabled and not row.alerts_enabled:
            row.bad_components = None
            row.next_alert_check_at = now + ALERT_CHECK_INTERVAL
        row.frequency = frequency
        row.weekday = weekday
        row.hour = hour
        row.timezone = timezone
        row.alerts_enabled = alerts_enabled
        row.next_digest_at = digest_at
        await self._db.commit()
        await self._db.refresh(row)
        return row

    async def delete(self, dashboard_id: str, owner_email: str) -> bool:
        row = await self.get(dashboard_id, owner_email)
        if row is None:
            return False
        await self._db.delete(row)
        await self._db.commit()
        return True


# --------------------------------------------------------------------------- execution


async def load_dashboard(db: AsyncSession, dashboard_id: str, owner: str) -> Dashboard | None:
    """Charge le tableau de bord avec le perimetre du proprietaire."""
    return await DatabaseDashboardStore(db, owner=owner).get(dashboard_id)


async def _user_id(db: AsyncSession, email: str) -> int | None:
    q = select(User.user_id).where(func.lower(User.email) == email.lower())
    return (await db.execute(q)).scalars().first()


def _notification(user_id: int, *, title: str, message: str, kind: str, link: str, meta: dict) -> Notification:
    return Notification(
        user_id=user_id,
        title=title,
        message=message,
        type=kind,
        link=link,
        metadata_json=json.dumps(meta),
        is_read=False,
    )


def _loads(raw: str | None) -> set[str]:
    try:
        return {str(i) for i in json.loads(raw)} if raw else set()
    except (TypeError, ValueError):
        return set()


async def process_subscription(
    db: AsyncSession, sub: BIDashboardDigest, *, now: datetime, send_digest: bool
) -> bool:
    """Recalcule le tableau de bord, emet les alertes et, si `send_digest`, le
    resume. Renvoie False si le tableau de bord n'existe plus."""
    owner = sub.owner_email
    dashboard = await load_dashboard(db, sub.dashboard_id, owner)
    if dashboard is None:
        logger.warning("bi digest: dashboard %s introuvable pour %s", sub.dashboard_id, owner)
        return False
    response = await compute_dashboard(dashboard, DashboardDataRequest(), owner=owner)
    kpis, texts = extract(dashboard, response)

    rel_link = f"/bi/{dashboard.id}"
    abs_link = f"{get_settings().app_public_url.rstrip('/')}{rel_link}"
    user_id = await _user_id(db, owner)

    newly_bad: list[KpiLine] = []
    if sub.alerts_enabled:
        previous = _loads(sub.bad_components)
        known = {k.id for k in kpis if k.status is not None}
        current = {k.id for k in kpis if k.status == "bad"}
        # Un statut inconnu garde l'etat precedent : il ne reinitialise pas l'alerte.
        keep = (previous & _kpi_ids(dashboard)) - known
        newly_bad = [k for k in kpis if k.id in current and k.id not in previous]
        sub.bad_components = json.dumps(sorted(current | keep))

    pending: list[Notification] = []
    if newly_bad and user_id is not None:
        names = ", ".join(k.label for k in newly_bad)
        pending.append(
            _notification(
                user_id,
                title=f"Indicateur critique : {dashboard.title}",
                message=f"{names} est passé en statut critique.",
                kind="warning",
                link=rel_link,
                meta={"dashboard_id": dashboard.id, "kpis": [k.id for k in newly_bad], "kind": "kpi_alert"},
            )
        )
    if send_digest:
        sub.last_sent_at = now
        if user_id is not None:
            pending.append(
                _notification(
                    user_id,
                    title=f"Résumé : {dashboard.title}",
                    message=" · ".join(f"{k.label} {k.formatted}" for k in kpis) or "Résumé disponible.",
                    kind="info",
                    link=rel_link,
                    meta={"dashboard_id": dashboard.id, "kind": "dashboard_digest"},
                )
            )
    # Etat d'alerte et notifications valides par le MEME commit.
    db.add_all(pending)
    await db.commit()
    for n in pending:
        await db.refresh(n)
        await push_notification(
            n.user_id,
            {
                "id": n.id,
                "title": n.title,
                "message": n.message,
                "type": n.type,
                "link": n.link,
                "is_read": False,
                "created_at": n.created_at.isoformat() if n.created_at else None,
            },
        )

    if newly_bad:
        subject, html, plain = render_alert(dashboard, newly_bad, link=abs_link)
        await _mail(owner, subject, html, plain)
    if send_digest:
        subject, html, plain = render_digest(dashboard, kpis, texts, link=abs_link, frequency=sub.frequency)
        await _mail(owner, subject, html, plain)
    return True


async def _mail(to: str, subject: str, html: str, plain: str) -> None:
    try:
        await send_system_email(to=to, subject=subject, html=html, text=plain)
    except Exception:  # noqa: BLE001 - un mail en echec ne doit pas arreter la boucle
        logger.exception("bi digest: envoi du mail en echec (%s)", subject)


async def tick_once(db: AsyncSession, *, now: datetime | None = None) -> int:
    """Traite les abonnements echus. Renvoie le nombre de traitements aboutis.

    Le creneau est reserve AVANT le calcul (UPDATE conditionnel sur les deux
    `next_*_at`) : une seule replique le gagne, et un calcul en echec ne
    rejoue pas a chaque tick.
    """
    moment = now or datetime.now(UTC)
    t = BIDashboardDigest
    due = (
        (
            await db.execute(
                select(t).where(or_(t.next_digest_at <= moment, and_(t.alerts_enabled.is_(True), t.next_alert_check_at <= moment)))
            )
        )
        .scalars()
        .all()
    )
    done = 0
    for row in due:
        sub_id = row.id
        old_digest, old_check = row.next_digest_at, row.next_alert_check_at
        do_digest = _aware(old_digest) <= moment
        values: dict = {}
        if do_digest:
            values["next_digest_at"] = next_run(
                row.frequency, weekday=row.weekday, hour=row.hour, tz=row.timezone, after=moment
            )
        if row.alerts_enabled and (do_digest or _aware(old_check) <= moment):
            values["next_alert_check_at"] = moment + ALERT_CHECK_INTERVAL
        try:
            claim = await db.execute(
                update(t)
                .where(t.id == sub_id, t.next_digest_at == old_digest, t.next_alert_check_at == old_check)
                .values(**values)
            )
            await db.commit()
            if claim.rowcount != 1:
                continue  # une autre replique a pris ce creneau
            await db.refresh(row)
            if await process_subscription(db, row, now=moment, send_digest=do_digest):
                done += 1
        except Exception:  # noqa: BLE001 - un abonnement en panne ne doit pas arreter les autres
            await db.rollback()
            logger.exception("bi digest: echec pour l'abonnement %s", sub_id)
    return done
