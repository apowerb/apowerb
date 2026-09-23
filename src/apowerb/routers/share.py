from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import or_, select
from datetime import datetime, timedelta, timezone
from nanoid import generate                          # pip install nanoid

from apowerb.auth.dependencies import get_current_user, get_optional_user
from apowerb.configs.settings import get_settings
from apowerb.helpers.database import get_db
from apowerb.models import SharedConversation
from apowerb.schema.share_schema import (
    ShareCreateRequest, ShareCreateResponse, ShareListItem,
    SharedConversationResponse,
)
from apowerb.users import schemas as user_schemas

router = APIRouter(prefix="/conversations/share", tags=["share"])


@router.post("", response_model=ShareCreateResponse)
async def create_share(
    payload: ShareCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
):
    share_id = generate(size=21)
    expires  = datetime.now(timezone.utc) + timedelta(days=30)
    created  = (
        datetime.fromtimestamp(payload.createdAt / 1000, tz=timezone.utc)
        if payload.createdAt else datetime.now(timezone.utc)
    )

    record = SharedConversation(
        id         = share_id,
        title      = payload.title,
        agent_name = payload.agentName,
        messages   = [m.model_dump() for m in payload.messages],
        created_at = created,
        expires_at = expires,
        owner_id   = current_user.email,
        is_public  = bool(payload.isPublic),
    )
    db.add(record)
    await db.commit()
    return ShareCreateResponse(shareId=share_id, isPublic=bool(payload.isPublic))


@router.get("", response_model=list[ShareListItem])
async def list_my_shares(
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """The caller's own snapshots that still lead somewhere.

    Scoped by owner only: ``owner_id`` is the e-mail, and the organisation is
    its domain (``get_domain_from_email``), so the owner filter already keeps
    the list inside one organisation. A revoked share is deleted, so it cannot
    appear; an expired one answers 410 and is left out.
    """
    result = await db.execute(
        select(SharedConversation)
        .where(SharedConversation.owner_id == current_user.email)
        .where(
            or_(
                SharedConversation.expires_at.is_(None),
                SharedConversation.expires_at > datetime.now(timezone.utc),
            )
        )
        .order_by(SharedConversation.created_at.desc())
    )
    base = get_settings().app_public_url.rstrip("/")
    return [
        ShareListItem(
            id        = r.id,
            title     = r.title,
            agentName = r.agent_name,
            createdAt = r.created_at,
            expiresAt = r.expires_at,
            isPublic  = bool(r.is_public),
            url       = f"{base}/share/{r.id}",
        )
        for r in result.scalars().all()
    ]


@router.get("/{share_id}", response_model=SharedConversationResponse)
async def get_share(
    share_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User | None = Depends(get_optional_user),
):
    result = await db.execute(
        select(SharedConversation).where(SharedConversation.id == share_id)
    )
    record = result.scalars().first()

    if not record:
        raise HTTPException(status_code=404, detail="Conversation not found")

    if record.expires_at and record.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="This shared link has expired")

    # Public share: readable by anyone (the unguessable share_id is the secret).
    if not getattr(record, "is_public", False):
        if current_user is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Authentication required",
                headers={"WWW-Authenticate": "Bearer"},
            )
        owner_id = getattr(record, "owner_id", None)
        if owner_id and owner_id != current_user.email:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not own this shared conversation",
            )

    return SharedConversationResponse(
        title      = record.title,
        agentName  = record.agent_name,
        messages   = record.messages,
        createdAt  = record.created_at,
    )


@router.delete("/{share_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_share(
    share_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
):
    result = await db.execute(
        select(SharedConversation).where(SharedConversation.id == share_id)
    )
    record = result.scalars().first()

    if not record:
        raise HTTPException(status_code=404, detail="Conversation not found")

    owner_id = getattr(record, "owner_id", None)
    if owner_id != current_user.email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not own this shared conversation",
        )

    await db.delete(record)
    await db.commit()
    return None
