from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from apowerb.models import User, UserRole
from apowerb.helpers.pagination import PageParams, PageResponse, paginate
from apowerb.users import exceptions, schemas
from apowerb.helpers.default_superadmin import is_designated_superadmin
from apowerb.helpers.security import get_password_hash
from apowerb.configs.settings import get_settings


async def get_all_users(
    page_params: PageParams, db: AsyncSession
) -> PageResponse[schemas.User]:
    stmt = select(User)
    return await paginate(db, stmt, page_params, schemas.User)


async def create_user(user_in: schemas.UserCreate, db: AsyncSession) -> schemas.User:
    stmt = select(User).where(User.email == user_in.email)
    result = await db.execute(stmt)
    existing_user = result.scalar_one_or_none()

    if existing_user:
        raise exceptions.EmailAlreadyExists("Email already in use.")

    if user_in.password is not None:
        hashed_password = get_password_hash(user_in.password)
    else:
        hashed_password = None

    user_in.password = hashed_password

    new_user = User(**user_in.model_dump())
    # Flag OFF -> compte verifie d office (pas de friction). Flag ON -> doit verifier.
    new_user.email_verified = not get_settings().auth_email_verification_enabled

    # The operator names the first administrator by e-mail alone, and whoever
    # signs up with it is that administrator -- with their own password.
    # Promoting only at startup was not enough: on a new install the designated
    # person has not signed up yet, so there was nobody to promote and
    # DEFAULT_SUPERADMIN_EMAIL appeared to do nothing.
    if is_designated_superadmin(new_user.email):
        new_user.role = UserRole.ADMIN

    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    return schemas.User.model_validate(new_user)


async def get_user_by_id(user_id: int, db: AsyncSession) -> schemas.User:
    stmt = select(User).where(User.user_id == user_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        raise exceptions.UserNotFoundException("User not found")

    return schemas.User.model_validate(user)


async def get_user_by_email(user_email: str, db: AsyncSession) -> schemas.User:
    stmt = select(User).where(User.email == user_email)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        raise exceptions.UserNotFoundException("User not found")

    return schemas.User.model_validate(user)


async def delete_user_by_id(user_id: int, db: AsyncSession) -> None:
    stmt = select(User).where(User.user_id == user_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        raise exceptions.UserNotFoundException("User not found")

    await db.delete(user)
    await db.commit()


async def delete_user_by_email(user_email: str, db: AsyncSession) -> None:
    stmt = select(User).where(User.email == user_email)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()

    if not user:
        raise exceptions.UserNotFoundException("User not found")

    await db.delete(user)
    await db.commit()
