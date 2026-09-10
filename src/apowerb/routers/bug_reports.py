"""Signalement de bug depuis l'application.

    POST   /api/bug-reports               (auth)   Envoyer un signalement
    GET    /api/bug-reports/areas         (auth)   Liste des fonctionnalités
    GET    /api/bug-reports               (admin)  Triage
    GET    /api/bug-reports/{id}          (auth*)  Détail
    GET    /api/bug-reports/{id}/screenshot (auth*) Capture jointe
    PATCH  /api/bug-reports/{id}          (admin)  Statut, zone, note
    POST   /api/bug-reports/{id}/issue    (admin)  Créer l'issue

(*) l'auteur voit son propre signalement, l'administrateur voit tout.
Un utilisateur qui a envoyé une capture de son écran doit pouvoir
vérifier ce qu'il a envoyé ; le lui cacher serait le meilleur moyen de
lui apprendre à ne plus rien envoyer.

**L'envoi ne crée aucune issue.** Il écrit en base et rend la main. La
sortie vers GitHub est un geste d'administrateur, sur la route dédiée
ci-dessus, parce qu'un signalement contient des logs et une capture qui
méritent une relecture humaine avant de quitter le déploiement.
"""

from logging import getLogger
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.admin.guard import require_admin
from apowerb.auth.dependencies import get_current_user
from apowerb.bug_reports import service
from apowerb.bug_reports.areas import area_options
from apowerb.helpers.database import get_db
from apowerb.helpers.ownership import is_admin
from apowerb.schema.bug_report_schema import (
    AreaOption,
    BugReportCreate,
    BugReportCreated,
    BugReportDetail,
    BugReportListResponse,
    BugReportUpdate,
)
from apowerb.users import schemas as user_schemas

logger = getLogger(__name__)

router = APIRouter(prefix="/bug-reports", tags=["bug-reports"])


def _user_id(user) -> Optional[int]:
    return getattr(user, "user_id", None) or getattr(user, "id", None)


# ---------------------------------------------------------------------------
# 1. POST /api/bug-reports -- envoyer un signalement
# ---------------------------------------------------------------------------


@router.post("", response_model=BugReportCreated, status_code=status.HTTP_201_CREATED)
async def submit_bug_report(
    payload: BugReportCreate,
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
) -> BugReportCreated:
    return await service.create_bug_report(
        db,
        user_id=_user_id(current_user),
        reporter_email=getattr(current_user, "email", None),
        payload=payload,
    )


# ---------------------------------------------------------------------------
# 2. GET /api/bug-reports/areas -- peupler le menu déroulant
# ---------------------------------------------------------------------------


# Avant `/{report_id}` : sinon FastAPI teste la route paramétrée d'abord
# et « areas » part en identifiant, ce qui donne un 422 déroutant.
@router.get("/areas", response_model=list[AreaOption])
async def list_areas(
    _: user_schemas.User = Depends(get_current_user),
) -> list[AreaOption]:
    """Les fonctionnalités proposées dans le formulaire.

    Servie par l'API plutôt que codée en dur dans l'interface : les deux
    listes doivent rester identiques, et l'empreinte comme les libellés
    d'issue sont calculés côté serveur.
    """
    return [AreaOption(**option) for option in area_options()]


# ---------------------------------------------------------------------------
# 3. GET /api/bug-reports -- écran de triage (administrateurs)
# ---------------------------------------------------------------------------


@router.get("", response_model=BugReportListResponse)
async def list_bug_reports(
    report_status: Optional[str] = Query(default=None, alias="status"),
    severity: Optional[str] = None,
    area: Optional[str] = None,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
    _: user_schemas.User = Depends(require_admin),
) -> BugReportListResponse:
    rows, total = await service.list_bug_reports(
        db,
        status=report_status,
        severity=severity,
        area=area,
        limit=limit,
        offset=offset,
    )
    return BugReportListResponse(
        items=[service.to_summary(row) for row in rows], total=total
    )


# ---------------------------------------------------------------------------
# 4. GET /api/bug-reports/{id} -- détail
# ---------------------------------------------------------------------------


async def _readable_report(report_id: int, db: AsyncSession, user):
    report = await service.get_bug_report(db, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Signalement introuvable.")
    if not is_admin(user) and report.user_id != _user_id(user):
        # 404 et non 403 : répondre « il existe mais pas pour vous »
        # divulguerait l'existence du signalement d'un autre utilisateur,
        # et son numéro suffit à en deviner le volume.
        raise HTTPException(status_code=404, detail="Signalement introuvable.")
    return report


@router.get("/{report_id}", response_model=BugReportDetail)
async def get_bug_report(
    report_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
) -> BugReportDetail:
    report = await _readable_report(report_id, db, current_user)
    return service.to_detail(report)


# ---------------------------------------------------------------------------
# 5. GET /api/bug-reports/{id}/screenshot -- l'image jointe
# ---------------------------------------------------------------------------


@router.get("/{report_id}/screenshot")
async def get_bug_report_screenshot(
    report_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
) -> Response:
    report = await _readable_report(report_id, db, current_user)
    if not report.screenshot_path:
        raise HTTPException(status_code=404, detail="Aucune capture jointe.")

    from apowerb.storage.storage_service import StorageService

    storage = StorageService()
    download = getattr(storage, "download_file_from_storage", None) or getattr(
        storage, "download_file_from_s3", None
    )
    if download is None:
        raise HTTPException(
            status_code=501, detail="Le stockage configuré ne sait pas relire."
        )
    try:
        content = download(report.screenshot_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Capture introuvable.")
    except Exception as exc:
        logger.warning("[BUG-REPORT] Capture illisible (#%s) : %s", report_id, exc)
        raise HTTPException(status_code=502, detail="Capture illisible.")

    media_type = (
        "image/png"
        if report.screenshot_path.endswith(".png")
        else "image/webp"
        if report.screenshot_path.endswith(".webp")
        else "image/jpeg"
    )
    # `private` : une capture d'écran ne doit pas s'installer dans le
    # cache d'un proxy partagé.
    return Response(
        content=content,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=300"},
    )


# ---------------------------------------------------------------------------
# 6. PATCH /api/bug-reports/{id} -- triage
# ---------------------------------------------------------------------------


@router.patch("/{report_id}", response_model=BugReportDetail)
async def update_bug_report(
    report_id: int,
    payload: BugReportUpdate,
    db: AsyncSession = Depends(get_db),
    _: user_schemas.User = Depends(require_admin),
) -> BugReportDetail:
    report = await service.get_bug_report(db, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Signalement introuvable.")

    if payload.status is not None:
        report.status = payload.status.value
    if payload.severity is not None:
        report.severity = payload.severity.value
    if payload.area is not None:
        report.area = payload.area.value
    if payload.admin_note is not None:
        report.admin_note = payload.admin_note

    await db.commit()
    await db.refresh(report)
    return service.to_detail(report)


# ---------------------------------------------------------------------------
# 7. POST /api/bug-reports/{id}/issue -- publier après relecture
# ---------------------------------------------------------------------------


@router.post("/{report_id}/issue", response_model=BugReportDetail)
async def create_issue(
    report_id: int,
    db: AsyncSession = Depends(get_db),
    admin: user_schemas.User = Depends(require_admin),
) -> BugReportDetail:
    from apowerb.bug_reports.sinks.github import (
        SinkConfigurationError,
        SinkDeliveryError,
        SinkRefusal,
    )

    report = await service.get_bug_report(db, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="Signalement introuvable.")
    if report.issue_url:
        # Idempotent plutôt qu'erreur : deux clics sur un bouton lent ne
        # doivent pas produire deux issues.
        return service.to_detail(report)

    try:
        url, number = await service.create_issue_for(db, report)
    except SinkRefusal as refusal:
        # 409 et non 403 : ce n'est pas l'administrateur qui manque d'un
        # droit, c'est la configuration du dépôt qui est incompatible avec
        # ce qu'on s'apprête à y écrire. Le message porte le remède.
        raise HTTPException(status_code=409, detail=str(refusal))
    except SinkConfigurationError as misconfigured:
        raise HTTPException(status_code=501, detail=str(misconfigured))
    except SinkDeliveryError as failure:
        raise HTTPException(status_code=502, detail=str(failure))

    logger.info(
        "[BUG-REPORT] Issue %s (#%s) créée pour le signalement #%s par %s",
        url,
        number,
        report_id,
        getattr(admin, "email", "?"),
    )
    return service.to_detail(report)
