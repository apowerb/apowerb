from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.auth import exceptions, schemas, service
from apowerb.auth.dependencies import get_current_user, get_db
from apowerb.configs.settings import get_settings
from apowerb.users import schemas as user_schemas
from apowerb.users.exceptions import UserNotFoundException

router = APIRouter(prefix="/auth", tags=["auth"])


def basic_auth_enabled() -> None:
    """Dependency: returns 404 when AUTH_BASIC_ENABLED is false.

    Used to gate email/password login, registration and password reset.
    OAuth flows must NOT depend on this — they are independent of basic auth.
    """
    if not get_settings().auth_basic_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Not Found"
        )


def registration_enabled() -> None:
    """Dependency: returns 404 when AUTH_REGISTER_ENABLED is false.

    Stacks on top of basic_auth_enabled — both must be true for the
    registration endpoint to be reachable. One deployment uses this to keep login
    open while closing self-registration.
    """
    if not get_settings().auth_register_enabled:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Not Found"
        )


# ── Standard auth endpoints ──────────────────────────────────────────────────


@router.post("/token", status_code=status.HTTP_200_OK, dependencies=[Depends(basic_auth_enabled)])
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
    response: Response = Response(),
):
    request = schemas.LoginRequest(
        email=form_data.username, password=form_data.password
    )
    try:
        return await service.login(request, response, db)
    except exceptions.EmailNotVerified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="email_not_verified",
        )
    except (exceptions.InvalidCredentials, UserNotFoundException):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
        )


@router.post(
    "/refresh-token", response_model=schemas.Token, status_code=status.HTTP_200_OK
)
async def refresh_token(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        return await service.refresh(request, db)
    except exceptions.InvalidCredentials as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))


@router.post("/logout", status_code=status.HTTP_200_OK)
async def logout(response: Response):
    """Clear the refresh token cookie."""
    response.delete_cookie(
        key="refresh_token",
        path="/",
        httponly=True,
        samesite="lax",
    )
    return {"status": "ok"}


# ── Forgot / reset password (B18) ────────────────────────────────────────────


@router.post("/forgot-password", status_code=status.HTTP_200_OK, dependencies=[Depends(basic_auth_enabled)])
async def forgot_password(
    body: schemas.ForgotPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Kick off a password-reset email for ``body.email``.

    Always returns 200 — success/failure is indistinguishable to avoid
    enumerating accounts.
    """
    base_url = str(request.base_url)
    await service.request_password_reset(body.email, db, base_url=base_url)
    return {"status": "ok"}


@router.post("/reset-password", status_code=status.HTTP_200_OK, dependencies=[Depends(basic_auth_enabled)])
async def reset_password(
    body: schemas.ResetPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    """Consume a reset token and rotate the user's password hash."""
    try:
        await service.reset_password(body.token, body.new_password, db)
    except exceptions.InvalidCredentials as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        )
    return {"status": "ok"}


@router.post("/verify-email", status_code=status.HTTP_200_OK, dependencies=[Depends(basic_auth_enabled)])
async def verify_email(
    body: schemas.VerifyEmailRequest,
    db: AsyncSession = Depends(get_db),
):
    """Consume an email-verification token and mark the account verified."""
    try:
        await service.verify_email_token(body.token, db)
    except exceptions.InvalidCredentials as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc))
    return {"status": "ok"}


@router.post("/resend-verification", status_code=status.HTTP_200_OK, dependencies=[Depends(basic_auth_enabled)])
async def resend_verification(
    body: schemas.ResendVerificationRequest,
    db: AsyncSession = Depends(get_db),
):
    """Re-send a verification email. Always 200 (no account-existence leak)."""
    await service.send_verification_email(body.email, db)
    return {"status": "ok"}
