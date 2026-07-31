from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.helpers.database import get_db
from apowerb.auth.dependencies import get_admin_user, get_current_user
from apowerb.helpers.pagination import PageParams, PageResponse
from apowerb.helpers.security import create_access_token
from apowerb.models import User as UserModel
from apowerb.users import dependencies, exceptions, schemas, service
from apowerb.users.schemas import UserUpdate

from apowerb.auth.schemas import Token
from apowerb.configs.settings import get_settings
from apowerb.auth.router import basic_auth_enabled, registration_enabled
from apowerb.auth import service as auth_service
from logging import getLogger

settings = get_settings()
logger = getLogger(__name__)


router = APIRouter(prefix="/users", tags=["users"])


@router.get(
    "/",
    response_model=PageResponse[schemas.User],
    dependencies=[Depends(get_admin_user)],
)
async def get_all_users(
    page_params: PageParams = Depends(), db: AsyncSession = Depends(get_db)
):
    return await service.get_all_users(page_params, db)


@router.post("/", response_model=schemas.User, status_code=status.HTTP_201_CREATED, dependencies=[Depends(basic_auth_enabled), Depends(registration_enabled)])
async def create_user(user_in: schemas.UserCreate, db: AsyncSession = Depends(get_db)):
    try:
        user = await service.create_user(user_in, db)
        if get_settings().auth_email_verification_enabled:
            await auth_service.send_verification_email(user.email, db)
        return user
    except exceptions.EmailAlreadyExists as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(e))


@router.get("/me", status_code=status.HTTP_200_OK)
async def get_current_user_route(user: schemas.User = Depends(get_current_user)):
    return user


@router.get("/{user_id}", response_model=schemas.User)
async def get_user_by_id(
    user: schemas.User = Depends(dependencies.require_user_owner_or_admin),
):
    return user


@router.patch("/{user_id}", response_model=schemas.User)
async def update_user(
    user_id: int,
    updates: UserUpdate,
    current_user: schemas.User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update user profile fields."""
    if current_user.user_id != user_id and getattr(current_user, "role", None) != "ADMIN":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized")

    result = await db.execute(select(UserModel).where(UserModel.user_id == user_id))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    update_data = updates.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(user, field, value)

    await db.commit()
    await db.refresh(user)
    return user


@router.get("/email/{user_email}", response_model=schemas.User)
async def get_user_by_email(
    user: schemas.User = Depends(dependencies.require_user_owner_or_admin_by_email),
):
    return user


@router.delete(
    "/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_user(
    user: schemas.User = Depends(dependencies.require_user_owner_or_admin),
    db=Depends(get_db),
):
    await service.delete_user_by_id(user.user_id, db)


@router.delete("/email/{user_email}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user_by_email(
    user: schemas.User = Depends(dependencies.require_user_owner_or_admin_by_email),
    db: AsyncSession = Depends(get_db),
):
    await service.delete_user_by_id(user.user_id, db)
