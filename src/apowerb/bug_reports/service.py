"""Cycle de vie d'un signalement : réception, regroupement, sortie.

Le point d'entrée est ``create_bug_report``. Il fait, dans cet ordre,
quatre choses qui ne sont pas interchangeables :

1. **expurger** ce qui arrive du navigateur — avant toute écriture, pour
   qu'il n'existe aucun chemin par lequel un secret entre en base ;
2. **calculer l'empreinte** et chercher un signalement identique, pour
   que le vingtième témoin d'un défaut connu enrichisse un ticket au
   lieu d'en ouvrir un vingtième ;
3. **récolter les logs serveur** des requêtes citées, tant qu'ils sont
   encore en mémoire — c'est la seule étape qui a une date de
   péremption, d'où sa place avant le stockage de la capture ;
4. **ranger la capture** dans le stockage, jamais en base.

La création d'issue n'est pas ici : elle est déclenchée par un
administrateur, depuis ``create_issue_for``.
"""

from __future__ import annotations

import json
from logging import getLogger
from collections.abc import Mapping
from typing import Any, Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.bug_reports.areas import BugArea, infer_area
from apowerb.bug_reports.fingerprint import compute_fingerprint
from apowerb.bug_reports.issue_body import render_issue_body, render_issue_title
from apowerb.bug_reports.log_buffer import get_buffer
from apowerb.bug_reports.redaction import (
    redact_mapping,
    redact_text,
    redact_url,
)
from apowerb.bug_reports.screenshot import ScreenshotRejected, decode_screenshot
from apowerb.configs.settings import get_settings
from apowerb.models import BugReport
from apowerb.schema.bug_report_schema import (
    BugReportCreate,
    BugReportCreated,
    BugReportDetail,
    BugReportStatus,
    BugReportSummary,
)

logger = getLogger(__name__)

# Combien de lignes de log serveur on garde AVEC le signalement. Au-delà,
# ce n'est plus une preuve, c'est un export.
MAX_SERVER_LOG_LINES = 200


def _dumps(value: Any) -> Optional[str]:
    if value in (None, [], {}):
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def _loads(value: Optional[str], fallback: Any) -> Any:
    if not value:
        return fallback
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        # Une ligne illisible ne doit pas casser l'écran de triage : le
        # reste du signalement garde sa valeur.
        return fallback


# --------------------------------------------------------------------------
# Réception
# --------------------------------------------------------------------------


def _sanitised_calls(payload: BugReportCreate) -> list[dict[str, Any]]:
    calls = []
    for call in payload.api_calls:
        calls.append(
            {
                "method": call.method,
                "path": redact_url(call.path),
                "status": call.status,
                "request_id": call.request_id,
                "duration_ms": call.duration_ms,
                "error": redact_text(call.error, max_length=1000),
                "at": call.at.isoformat() if call.at else None,
            }
        )
    return calls


def _sanitised_console(payload: BugReportCreate) -> list[dict[str, Any]]:
    return [
        {
            "level": entry.level,
            "message": redact_text(entry.message, max_length=2000),
            "source": redact_url(entry.source),
            "at": entry.at.isoformat() if entry.at else None,
        }
        for entry in payload.console
    ]


def _failing_call(calls: list[dict[str, Any]]) -> dict[str, Any]:
    """L'appel qui a échoué, à défaut le dernier.

    L'empreinte se calcule sur celui-là : c'est lui qui identifie le
    défaut. Prendre « le dernier appel » sans regarder le statut ferait
    dépendre le regroupement de ce que le front a chargé après l'erreur.
    """
    failures = [c for c in calls if isinstance(c.get("status"), int) and c["status"] >= 400]
    if failures:
        return failures[-1]
    no_response = [c for c in calls if c.get("status") is None and c.get("error")]
    if no_response:
        return no_response[-1]
    return calls[-1] if calls else {}


async def create_bug_report(
    db: AsyncSession,
    *,
    user_id: Optional[int],
    reporter_email: Optional[str],
    payload: BugReportCreate,
) -> BugReportCreated:
    context = redact_mapping(payload.context.model_dump(mode="json")) or {}
    context["url"] = redact_url(context.get("url"))
    calls = _sanitised_calls(payload)
    console = _sanitised_console(payload)

    failing = _failing_call(calls)
    route = context.get("route") or failing.get("path")
    error_signature = (
        failing.get("error")
        or (console[-1]["message"] if console else None)
        or payload.observed
    )
    fingerprint = compute_fingerprint(
        route=route,
        status=failing.get("status"),
        error_signature=error_signature,
    )

    # Le chemin de l'appel fautif d'abord : c'est lui qui dit ce qui a
    # cassé, quand l'écran affiché dit seulement où on se trouvait.
    area = payload.area or infer_area(failing.get("path"), route, context.get("url"))

    request_ids = [c["request_id"] for c in calls if c.get("request_id")]
    server_logs = get_buffer().collect(request_ids, limit=MAX_SERVER_LOG_LINES)

    # Le doublon : première ligne canonique portant cette empreinte et pas
    # encore close. Un défaut re-signalé après correction rouvre un fil
    # neuf plutôt que de ressusciter l'ancien.
    canonical = (
        await db.execute(
            select(BugReport)
            .where(
                BugReport.fingerprint == fingerprint,
                BugReport.duplicate_of.is_(None),
                BugReport.status.notin_(
                    [BugReportStatus.RESOLVED.value, BugReportStatus.REJECTED.value]
                ),
            )
            .order_by(BugReport.id.asc())
            .limit(1)
        )
    ).scalar_one_or_none()

    report = BugReport(
        user_id=user_id,
        reporter_email=reporter_email,
        title=(payload.title or "").strip()[:200]
        or render_issue_title(
            {"title": None, "route": route, "observed": payload.observed,
             "what_i_did": payload.what_i_did}
        ),
        where_i_was=redact_text(payload.where_i_was, max_length=1000),
        what_i_did=redact_text(payload.what_i_did),
        expected=redact_text(payload.expected),
        observed=redact_text(payload.observed),
        area=(area.value if isinstance(area, BugArea) else str(area)),
        severity=payload.severity.value,
        status=BugReportStatus.NEW.value,
        fingerprint=fingerprint,
        occurrences=1,
        duplicate_of=canonical.id if canonical else None,
        route=(route or "")[:512] or None,
        run_id=_current_run_id(),
        server_version=_server_version(),
        context_json=_dumps(context),
        api_calls_json=_dumps(calls),
        console_json=_dumps(console),
        server_logs_json=_dumps(server_logs),
        request_ids_json=_dumps(request_ids),
    )

    if payload.screenshot and payload.screenshot_consent:
        report.screenshot_path = _store_screenshot(payload.screenshot, fingerprint)
    elif payload.screenshot and not payload.screenshot_consent:
        # Envoyée sans consentement : on ne la garde pas. Le cas existe —
        # un client tiers peut remplir le champ sans montrer d'aperçu.
        logger.info(
            "[BUG-REPORT] Capture reçue sans consentement explicite : ignorée."
        )

    db.add(report)
    # Les deux valeurs du signalement canonique sont lues AVANT le commit,
    # et gardées en variables locales. `commit()` expire les objets de la
    # session ; en asyncio, le rechargement paresseux qui suivrait un accès
    # à `canonical.occurrences` lève `MissingGreenlet` — l'attribut ne peut
    # pas déclencher d'entrée-sortie hors du contexte greenlet.
    # Mesuré le 10/09/2026 sur un parcours réel : le PREMIER signalement
    # passait (aucun canonique à relire), le SECOND échouait en 500. Les
    # tests unitaires ne pouvaient pas le voir, faute de base.
    duplicate_of = canonical.id if canonical else None
    occurrences = 1
    if canonical:
        occurrences = (canonical.occurrences or 1) + 1
        canonical.occurrences = occurrences
    await db.commit()
    await db.refresh(report)

    logger.info(
        "[BUG-REPORT] #%s enregistré (empreinte %s, %s ligne(s) de log, "
        "doublon de %s)",
        report.id,
        fingerprint,
        len(server_logs),
        duplicate_of if duplicate_of else "—",
    )

    return BugReportCreated(
        id=report.id,
        fingerprint=fingerprint,
        occurrences=occurrences,
        duplicate_of=duplicate_of,
        logs_attached=len(server_logs),
    )


def _server_version() -> Optional[str]:
    """Version du paquet installé.

    Lue depuis les métadonnées de distribution et non depuis un réglage :
    un champ de configuration se copie d'un environnement à l'autre et
    finit par mentir sur ce qui tourne vraiment.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("apowerb")
        except PackageNotFoundError:
            return None
    except Exception:  # pragma: no cover
        return None


def _current_run_id() -> Optional[str]:
    try:
        from apowerb.configs.th2logger import current_run_id

        return current_run_id()
    except Exception:  # pragma: no cover - le run id est un confort
        return None


def _store_screenshot(data_url: str, fingerprint: str) -> Optional[str]:
    """Range la capture dans le stockage configuré. ``None`` si refusée.

    Un refus ne fait pas échouer le signalement : le texte, les logs et
    les identifiants valent déjà le ticket, et perdre tout ça parce qu'une
    image dépasse de 200 Ko serait le mauvais arbitrage.
    """
    try:
        shot = decode_screenshot(data_url)
    except ScreenshotRejected as exc:
        logger.warning("[BUG-REPORT] Capture refusée : %s", exc)
        return None

    try:
        from uuid import uuid4

        from apowerb.storage.storage_service import StorageService

        storage = StorageService()
        path = f"bug-reports/{fingerprint}/{uuid4().hex}.{shot.extension}"
        upload = getattr(storage, "upload_bytes_to_storage", None) or getattr(
            storage, "upload_bytes_to_s3", None
        )
        if upload is None:  # pragma: no cover - dépend du mode de stockage
            logger.warning("[BUG-REPORT] Aucun uploader disponible : capture perdue.")
            return None
        upload(shot.content, path, shot.content_type)
        return path
    except Exception as exc:  # pragma: no cover - dépend de l'infra
        logger.warning("[BUG-REPORT] Capture non stockée : %s", exc)
        return None


# --------------------------------------------------------------------------
# Lecture
# --------------------------------------------------------------------------


def to_summary(report: BugReport) -> BugReportSummary:
    return BugReportSummary(
        id=report.id,
        title=report.title,
        area=report.area or BugArea.OTHER.value,
        severity=report.severity,
        status=report.status,
        fingerprint=report.fingerprint,
        occurrences=report.occurrences or 1,
        reporter_email=report.reporter_email,
        route=report.route,
        issue_url=report.issue_url,
        has_screenshot=bool(report.screenshot_path),
        created_at=report.created_at.isoformat() if report.created_at else None,
        updated_at=report.updated_at.isoformat() if report.updated_at else None,
    )


def to_detail(report: BugReport) -> BugReportDetail:
    return BugReportDetail(
        **to_summary(report).model_dump(),
        where_i_was=report.where_i_was,
        what_i_did=report.what_i_did,
        expected=report.expected,
        observed=report.observed,
        context=_loads(report.context_json, {}),
        api_calls=_loads(report.api_calls_json, []),
        console=_loads(report.console_json, []),
        server_logs=_loads(report.server_logs_json, []),
        request_ids=_loads(report.request_ids_json, []),
        run_id=report.run_id,
        admin_note=report.admin_note,
    )


async def list_bug_reports(
    db: AsyncSession,
    *,
    status: Optional[str] = None,
    severity: Optional[str] = None,
    area: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[BugReport], int]:
    query = select(BugReport)
    count_query = select(func.count()).select_from(BugReport)
    if status:
        query = query.where(BugReport.status == status)
        count_query = count_query.where(BugReport.status == status)
    if area:
        query = query.where(BugReport.area == area)
        count_query = count_query.where(BugReport.area == area)
    if severity:
        query = query.where(BugReport.severity == severity)
        count_query = count_query.where(BugReport.severity == severity)

    total = (await db.execute(count_query)).scalar_one()
    rows = (
        (
            await db.execute(
                query.order_by(BugReport.created_at.desc(), BugReport.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return list(rows), int(total)


async def get_bug_report(db: AsyncSession, report_id: int) -> Optional[BugReport]:
    return (
        await db.execute(select(BugReport).where(BugReport.id == report_id))
    ).scalar_one_or_none()


# --------------------------------------------------------------------------
# Sortie
# --------------------------------------------------------------------------


def build_sink(posed: Optional[Mapping[str, str]] = None):
    """Instancie la sortie configurée, ou ``None`` si aucune ne l'est.

    ``None`` n'est pas une erreur : un déploiement qui garde ses
    signalements dans son écran de triage est un déploiement valide, et
    c'est même le défaut.

    ``posed`` porte les valeurs posées depuis l'écran d'administration, lues
    au moment de l'usage. Elles ne comblent que ce que le déploiement laisse
    vide — la précédence que l'écran annonce, « imposée par le déploiement ».

    Pourquoi une lecture à chaud ici, quand `config_admin/overlay.py` la
    refuse partout ailleurs : ce refus vaut pour les 33 modules qui capturent
    ``Settings`` à l'import, où ne recharger qu'une partie donnerait un
    processus à moitié à jour. Ce domaine n'en fait pas partie — cette
    fonction appelle ``get_settings()`` dans son corps, à chaque création
    d'issue. Il n'y a donc aucune moitié à désynchroniser, et l'administrateur
    n'a pas à redémarrer un service pour changer un dépôt de destination.
    """
    settings = get_settings()
    posed = posed or {}

    def _value(attribut: str, nom_pose: str) -> str:
        return (getattr(settings, attribut, "") or "") or (posed.get(nom_pose) or "")

    repo = _value("bug_report_github_repo", "BUG_REPORT_GITHUB_REPO")
    token = _value("bug_report_github_token", "BUG_REPORT_GITHUB_TOKEN")
    if not repo or not token:
        return None

    # Une valeur abîmée en base ne doit pas empêcher de créer un ticket :
    # on range dans le tableau si on peut, on crée l'issue dans tous les cas.
    raw_project = _value("bug_report_github_project", "BUG_REPORT_GITHUB_PROJECT")
    try:
        project_number = int(str(raw_project).strip().lstrip("#")) or None
    except (TypeError, ValueError):
        project_number = None

    from apowerb.bug_reports.sinks.github import GitHubIssueSink

    return GitHubIssueSink(
        repo=repo,
        token=token,
        allow_public_repo=bool(
            getattr(settings, "bug_report_github_allow_public", False)
        ),
        project_number=project_number,
    )


def issue_payload(report: BugReport, *, app_url: Optional[str] = None) -> dict[str, Any]:
    """Le dictionnaire que consomme le rendu Markdown."""
    detail = to_detail(report)
    data = detail.model_dump()
    data["server_version"] = report.server_version
    return data


async def create_issue_for(
    db: AsyncSession, report: BugReport
) -> tuple[str, int]:
    """Crée l'issue (ou commente l'existante) et enregistre le lien.

    Retourne ``(url, numéro)``. Lève ``SinkConfigurationError``,
    ``SinkRefusal`` ou ``SinkDeliveryError`` — le routeur les traduit en
    codes HTTP distincts, parce qu'un refus de garde et une panne réseau
    n'appellent pas la même réaction de l'administrateur.
    """
    settings = get_settings()
    # Les valeurs posées depuis l'écran, lues maintenant plutôt qu'au
    # démarrage : l'administrateur qui change de dépôt n'a pas à faire
    # redémarrer un service pour cela. Le déploiement garde la main sur ce
    # qu'il porte — la préséance est appliquée dans `build_sink`.
    from apowerb.core.config_admin.store import read_posed

    try:
        posed = await read_posed(
            db,
            (
                "BUG_REPORT_GITHUB_REPO",
                "BUG_REPORT_GITHUB_TOKEN",
                "BUG_REPORT_GITHUB_PROJECT",
            ),
        )
    except Exception:  # noqa: BLE001 — une table absente n'est pas une panne
        posed = {}
    sink = build_sink(posed)
    if sink is None:
        from apowerb.bug_reports.sinks.github import SinkConfigurationError

        raise SinkConfigurationError(
            "Aucune sortie configurée : renseignez BUG_REPORT_GITHUB_REPO et "
            "BUG_REPORT_GITHUB_TOKEN, ou traitez le signalement dans l'écran "
            "de triage."
        )

    app_url = getattr(settings, "app_public_url", None)
    data = issue_payload(report, app_url=app_url)
    body = render_issue_body(data, app_url=app_url)
    title = render_issue_title(data)

    existing = sink.find_existing_issue(report.fingerprint)
    if existing:
        sink.comment_on_issue(
            existing["number"],
            f"Nouveau signalement pour la même empreinte "
            f"`{report.fingerprint}`.\n\n{body}",
        )
        url, number = existing["html_url"], existing["number"]
    else:
        created = sink.create_issue(
            title=title,
            body=body,
            labels=[
                "bug",
                f"severity:{report.severity}",
                f"area:{report.area or 'other'}",
                "from:app",
            ],
            # Le type d'issue de l'organisation. Les tableaux de projet
            # filtrent dessus : sans type, un bug n'y apparaît pas.
            issue_type="Bug",
        )
        url, number = created["html_url"], created["number"]

    report.issue_url = url
    report.issue_number = number
    report.status = BugReportStatus.ISSUE_CREATED.value
    await db.commit()
    await db.refresh(report)
    return url, number


__all__ = [
    "MAX_SERVER_LOG_LINES",
    "build_sink",
    "create_bug_report",
    "create_issue_for",
    "get_bug_report",
    "issue_payload",
    "list_bug_reports",
    "to_detail",
    "to_summary",
]
