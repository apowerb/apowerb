"""Abonnement au resume periodique et aux alertes KPI d'un tableau de bord.

PUT    /dashboards/{id}/digest   cree ou remplace l'abonnement de l'utilisateur
GET    /dashboards/{id}/digest   lit l'abonnement
DELETE /dashboards/{id}/digest   le supprime

Reserve au proprietaire d'un tableau de bord spec v2 (404 sinon, comme les
autres routes de tableau de bord).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.auth.dependencies import get_current_user
from apowerb.bi.dashboard_digest import DigestStore, load_dashboard
from apowerb.helpers.database import get_db
from apowerb.models import BIDashboardDigest
from apowerb.users import schemas as user_schemas

router = APIRouter(tags=["dashboard-digest"])

CurrentUser = Annotated[user_schemas.User, Depends(get_current_user)]
Db = Annotated[AsyncSession, Depends(get_db)]


class DigestSettings(BaseModel):
    frequency: Literal["weekly", "daily"] = "weekly"
    weekday: int | None = Field(default=None, ge=0, le=6, description="0 = lundi ; ignoré en quotidien")
    hour: int = Field(default=8, ge=0, le=23)
    timezone: str = "Europe/Paris"
    alerts_enabled: bool = True

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            raise ValueError("fuseau horaire inconnu")
        return value


class DigestResponse(DigestSettings):
    dashboard_id: str
    next_digest_at: datetime
    last_sent_at: datetime | None = None


def _out(row: BIDashboardDigest) -> DigestResponse:
    def aware(dt: datetime | None) -> datetime | None:
        return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt

    return DigestResponse(
        dashboard_id=row.dashboard_id,
        frequency=row.frequency,
        weekday=row.weekday,
        hour=row.hour,
        timezone=row.timezone,
        alerts_enabled=row.alerts_enabled,
        next_digest_at=aware(row.next_digest_at),
        last_sent_at=aware(row.last_sent_at),
    )


def _not_found() -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dashboard not found")


@router.put("/dashboards/{dashboard_id}/digest", response_model=DigestResponse)
async def put_digest(dashboard_id: str, body: DigestSettings, user: CurrentUser, db: Db) -> DigestResponse:
    dashboard = await load_dashboard(db, dashboard_id, user.email)
    if dashboard is None:
        raise _not_found()
    if dashboard.spec_version < 2:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Le résumé périodique n'est disponible que pour les tableaux de bord spec v2.",
        )
    row = await DigestStore(db).upsert(
        dashboard_id=dashboard_id,
        owner_email=user.email,
        now=datetime.now(timezone.utc),
        **body.model_dump(),
    )
    return _out(row)


@router.get("/dashboards/{dashboard_id}/digest", response_model=DigestResponse)
async def get_digest(dashboard_id: str, user: CurrentUser, db: Db) -> DigestResponse:
    row = await DigestStore(db).get(dashboard_id, user.email)
    if row is None:
        raise _not_found()
    return _out(row)


@router.delete("/dashboards/{dashboard_id}/digest", status_code=status.HTTP_204_NO_CONTENT)
async def delete_digest(dashboard_id: str, user: CurrentUser, db: Db) -> Response:
    if not await DigestStore(db).delete(dashboard_id, user.email):
        raise _not_found()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
