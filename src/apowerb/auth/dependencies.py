from apowerb.configs.th2logger import setup_logging
import os
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.configs.settings import get_settings
from apowerb.helpers.database import get_db
from apowerb.helpers.security import get_algorithm, get_secret_key
from apowerb.models import User as UserModel
from apowerb.users import schemas as user_schemas

logger = setup_logging(__name__)

BYPASS_AUTH = os.environ.get("BYPASS_AUTH", "").lower() == "true"

settings = get_settings()
# Make authentication optional
security = HTTPBearer(auto_error=False)
DBSessionDep = Annotated[AsyncSession, Depends(get_db)]


# Routes a user with an unsatisfied MFA demand may still reach: the ones
# that let them enrol, and the one the front reads to know who they are.
# Anything else is refused until the second factor exists — a demand that
# only the screen enforces is a suggestion.
_MFA_ENROLMENT_PATHS = (
    "/api/auth/mfa/",
    "/api/users/me",
    "/api/auth/logout",
)


def _mfa_enrolment_route(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in _MFA_ENROLMENT_PATHS)


async def get_current_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> user_schemas.User:
    if BYPASS_AUTH:
        logger.warning("AUTH BYPASS ACTIVE - Using fake user")

        # Return fake user with ALL required fields
        return user_schemas.User(
            user_id=1,
            email="test@example.com",
            role="USER",
            first_name="Test",
            last_name="User",
            onboarding_completed=True,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )

    if credentials is None:
        logger.debug("No credentials provided for request")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    # La clé est résolue AVANT le try : une configuration manquante doit
    # remonter telle quelle, pas se faire convertir en « identifiants
    # invalides » par le except ci-dessous.
    secret = get_secret_key()
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[get_algorithm()],
        )
        # H1 — Reject any token that isn't explicitly an access token.
        # Refresh tokens (30-day cookie) and long-lived agent_refresh tokens
        # (90-day Mage schedule tokens) MUST NOT unlock user-scope endpoints.
        token_type = payload.get("type")
        if token_type != "access":
            logger.warning(
                "Rejected non-access token on protected endpoint (type=%s)",
                token_type,
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token type. Access token required.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        email: str = payload.get("sub")
        if email is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Could not validate credentials",
                headers={"WWW-Authenticate": "Bearer"},
            )
    except JWTError as e:
        logger.warning("JWT decode failed: %s", type(e).__name__)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Could not validate credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    result = await db.execute(select(UserModel).where(UserModel.email == email))
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="User not found",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Revoked? A token minted before the cut-off is refused whatever its
    # expiry says. `iat` is absent from tokens minted before this shipped,
    # and those are treated as older than any cut-off — which is the safe
    # reading: an administrator who revokes wants them gone.
    cutoff = getattr(user, "sessions_valid_from", None)
    if cutoff is not None:
        issued_at = payload.get("iat")
        issued = (
            datetime.fromtimestamp(issued_at, tz=timezone.utc)
            if isinstance(issued_at, (int, float))
            else None
        )
        if issued is None or issued < cutoff:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="session_revoked",
                headers={"WWW-Authenticate": "Bearer"},
            )

    # A second factor was demanded and never set up: everything is refused
    # except enrolling. Refusing the login itself would deadlock — enrolling
    # requires being signed in.
    if (
        getattr(user, "mfa_required", False)
        and not getattr(user, "mfa_enabled", False)
        and not _mfa_enrolment_route(request.url.path)
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="mfa_enrolment_required",
        )

    return user_schemas.User(
        user_id=user.user_id,
        email=user.email,
        role=user.role.value if hasattr(user.role, "value") else str(user.role),
        first_name=user.first_name,
        last_name=user.last_name,
        username=user.username,
        full_name=user.full_name,
        avatar_url=user.avatar_url,
        plan=user.plan,
        stripe_customer_id=user.stripe_customer_id,
        mfa_enabled=getattr(user, "mfa_enabled", False) or False,
        onboarding_completed=getattr(user, "onboarding_completed", False) or False,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


async def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: AsyncSession = Depends(get_db),
) -> user_schemas.User | None:
    """Like get_current_user but returns None instead of 401 for unauthenticated requests."""
    if credentials is None:
        return None
    token = credentials.credentials
    secret = get_secret_key()
    try:
        payload = jwt.decode(
            token,
            secret,
            algorithms=[get_algorithm()],
        )
        # H1 — Only accept explicit access tokens in optional-auth paths too.
        if payload.get("type") != "access":
            return None
        email: str | None = payload.get("sub")
        if email is None:
            return None
    except JWTError:
        return None
    result = await db.execute(select(UserModel).where(UserModel.email == email))
    user = result.scalar_one_or_none()
    if user is None:
        return None
    return user_schemas.User(
        user_id=user.user_id,
        email=user.email,
        role=user.role.value if hasattr(user.role, "value") else str(user.role),
        first_name=user.first_name,
        last_name=user.last_name,
        username=user.username,
        full_name=user.full_name,
        avatar_url=user.avatar_url,
        plan=user.plan,
        stripe_customer_id=user.stripe_customer_id,
        mfa_enabled=getattr(user, "mfa_enabled", False) or False,
        onboarding_completed=getattr(user, "onboarding_completed", False) or False,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


async def get_admin_user(
    current_user: user_schemas.User = Depends(get_current_user),
) -> user_schemas.User:
    if BYPASS_AUTH:
        logger.warning("ADMIN BYPASS ACTIVE")
        current_user.role = "ADMIN"
        return current_user

    if getattr(current_user, "role", None) != "ADMIN":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Not enough permissions"
        )
    return current_user
