# src/users/dependencies.py
from fastapi import Depends, HTTPException, Path, status
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.auth.dependencies import get_current_user
from apowerb.helpers.database import get_db
from apowerb.users import exceptions, schemas, service


async def get_user_or_404(
    user_id: int,
    db: AsyncSession = Depends(get_db),
) -> schemas.User:
    try:
        return await service.get_user_by_id(user_id, db)
    except exceptions.UserNotFoundException:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")


async def require_user_owner_or_admin(
    requested_user: schemas.User = Depends(get_user_or_404),
    current: schemas.User = Depends(get_current_user),
) -> schemas.User:
    if current.role != "admin" and current.user_id != requested_user.user_id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed")
    return requested_user


async def get_user_by_email_or_404(
    user_email: str = Path(..., description="The email of the user"),
    db: AsyncSession = Depends(get_db),
) -> schemas.User:
    try:
        return await service.get_user_by_email(user_email, db)
    except exceptions.UserNotFoundException:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")


async def require_user_owner_or_admin_by_email(
    requested_user: schemas.User = Depends(get_user_by_email_or_404),
    current: schemas.User = Depends(get_current_user),
) -> schemas.User:
    if current.role != "admin" and current.email != requested_user.email:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not authorized")
    return requested_user
