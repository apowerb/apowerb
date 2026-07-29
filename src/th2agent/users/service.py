from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from th2agent.models import User
from th2agent.helpers.pagination import PageParams, PageResponse, paginate
from th2agent.users import exceptions, schemas
from th2agent.helpers.security import get_password_hash
from th2agent.configs.settings import get_settings


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
