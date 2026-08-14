from datetime import datetime, timedelta, timezone

from jose import JWTError, jwt
from fastapi import Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.auth import exceptions, schemas
from apowerb.core.extensions.registry import registry as _registry
from apowerb.configs.settings import get_settings
from apowerb.models import User
from apowerb.users.exceptions import UserNotFoundException
from apowerb.helpers.security import (
    get_algorithm,
    get_secret_key,
    create_access_token,
    get_password_hash,
    verify_password,
)


settings = get_settings()

PASSWORD_RESET_TOKEN_EXPIRE_MINUTES = 30
PASSWORD_RESET_TOKEN_TYPE = "password_reset"
EMAIL_VERIFY_TOKEN_TYPE = "email_verify"


def _issue_tokens(user, response: Response) -> schemas.Token:
    """Create access + refresh tokens and set refresh cookie.

    Partagé entre la connexion normale et la vérification d'un second
    facteur, que la brique d'auth avancée appelle après son propre défi.
    """
    access_token_expires = timedelta(minutes=settings.access_token_expire_minutes)
    access_token = create_access_token(
        data={"sub": user.email, "role": user.role.name, "type": "access"},
        expires_delta=access_token_expires,
    )

    refresh_token_expires = timedelta(days=30)
    refresh_token = create_access_token(
        data={"sub": user.email, "role": "USER", "type": "refresh"},
        expires_delta=refresh_token_expires,
    )

    response.set_cookie(
        key="refresh_token",
        value=refresh_token,
        httponly=True,
        max_age=30 * 24 * 60 * 60,
        secure=(settings.working_mode != "development"),
        samesite="lax",
        path="/",
    )

    return schemas.Token(access_token=access_token, token_type="bearer")


async def login(
    request: schemas.LoginRequest, response: Response, db: AsyncSession
) -> schemas.Token | schemas.MfaLoginResponse:
    stmt = select(User).where(User.email == request.email)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        raise UserNotFoundException("User does not exist")

    if user.password is None:
        raise exceptions.InvalidCredentials("Invalid Credentials")

    if not verify_password(request.password, user.password):
        raise exceptions.InvalidCredentials("Invalid credentials")

    if settings.auth_email_verification_enabled and not user.email_verified:
        raise exceptions.EmailNotVerified("Email not verified")

    # Le mot de passe est bon. Reste-t-il une étape ? Le noyau ne le sait pas :
    # il le demande au registre. Sans brique branchée, il n'y en a aucune et on
    # délivre les jetons — c'est le comportement complet du noyau open source.
    second_facteur = _registry.second_factor()
    if second_facteur is not None:
        réponse = second_facteur(user)
        if réponse is not None:
            return réponse

    return _issue_tokens(user, response)


async def refresh(request: Request, db: AsyncSession):
    refresh_token = request.cookies.get("refresh_token")
    if not refresh_token:
        raise exceptions.InvalidCredentials("Missing refresh token")

    # Impérativement hors du try : celui-ci attrape `Exception`, il
    # convertirait une clé manquante en « jeton invalide ou expiré » — un 401
    # qui accuserait l'appelant d'une panne de configuration serveur.
    secret = get_secret_key()
    try:
        payload = jwt.decode(
            refresh_token, secret, algorithms=[get_algorithm()]
        )
        if payload.get("type") != "refresh":
            raise exceptions.InvalidCredentials("Invalid token type")
    except Exception:
        raise exceptions.InvalidCredentials("Invalid or expired token")

    user_email = payload.get("sub")
    if not user_email:
        raise exceptions.InvalidCredentials("Invalid token")

    stmt = select(User).where(User.email == user_email)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        raise exceptions.InvalidCredentials("User not found")

    if settings.auth_email_verification_enabled and not user.email_verified:
        raise exceptions.InvalidCredentials("Email not verified")

    # The cut-off applies here as well. Revoking access tokens while leaving
    # a thirty-day cookie able to mint fresh ones would turn "sign in again"
    # into "wait a few minutes".
    # Same two helpers as the request path: one reading of "revoked", not two.
    from apowerb.auth.dependencies import _revocation_cutoff, _token_predates

    cutoff = _revocation_cutoff(user)
    if cutoff is not None and _token_predates(payload, cutoff):
        raise exceptions.InvalidCredentials("Session revoked")

    access_token_expires = timedelta(minutes=settings.access_token_expire_minutes)
    access_token = create_access_token(
        data={"sub": user.email, "role": "USER", "type": "access"},
        expires_delta=access_token_expires,
    )

    return schemas.Token(access_token=access_token, token_type="bearer")


# ── Forgot / reset password (B18) ────────────────────────────────────────────


def generate_reset_token(
    email: str,
    expires_delta: timedelta = timedelta(
        minutes=PASSWORD_RESET_TOKEN_EXPIRE_MINUTES
    ),
) -> str:
    """Mint a JWT scoped to ``type=password_reset`` for ``email``.

    The token is keyed on the user email (``sub``) and is valid for 30
    minutes by default. Callers who need a custom TTL can override
    ``expires_delta`` (mostly for tests).
    """
    payload = {
        "sub": email,
        "type": PASSWORD_RESET_TOKEN_TYPE,
        "exp": datetime.now(timezone.utc) + expires_delta,
    }
    return jwt.encode(payload, get_secret_key(), algorithm=get_algorithm())


async def request_password_reset(
    email: str, db: AsyncSession, base_url: str
) -> None:
    """Look up the user and email them a reset link.

    The response to this call must NOT differ between known and unknown
    emails — handle the "user not found" branch as a silent no-op.

    ``base_url`` is kept for signature compatibility but the reset link now
    points to the front (``app_public_url``), not the API.
    """
    from apowerb.helpers import system_mailer

    stmt = select(User).where(User.email == email)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        # Do not leak account existence.
        return

    token = generate_reset_token(user.email)
    reset_url = (
        f"{settings.app_public_url.rstrip('/')}/auth/reset-password?token={token}"
    )
    html = system_mailer.render_branded_email(
        heading="Réinitialise ton mot de passe",
        intro="Tu as demandé à réinitialiser ton mot de passe apowerb. "
        "Clique sur le bouton ci-dessous pour en choisir un nouveau.",
        cta_label="Réinitialiser mon mot de passe",
        cta_url=reset_url,
        note="Ce lien est valide 30 minutes. Si tu n’es pas à l’origine de "
        "cette demande, ignore ce message — ton mot de passe reste inchangé.",
    )
    text = f"Réinitialise ton mot de passe th2agent (valide 30 min) : {reset_url}"
    await system_mailer.send_system_email(
        to=user.email,
        subject="Réinitialise ton mot de passe th2agent",
        html=html,
        text=text,
    )


async def reset_password(
    token: str, new_password: str, db: AsyncSession
) -> None:
    """Validate a password-reset token and update the user's password.

    Raises :class:`exceptions.InvalidCredentials` for any failure (invalid
    signature, wrong token type, expired, unknown user) so the router can
    blanket-401 without leaking which branch triggered.
    """
    try:
        payload = jwt.decode(token, get_secret_key(), algorithms=[get_algorithm()])
    except JWTError as exc:
        raise exceptions.InvalidCredentials("Invalid or expired token") from exc

    if payload.get("type") != PASSWORD_RESET_TOKEN_TYPE:
        raise exceptions.InvalidCredentials("Invalid token type")

    email = payload.get("sub")
    if not email:
        raise exceptions.InvalidCredentials("Invalid token")

    stmt = select(User).where(User.email == email)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if user is None:
        raise exceptions.InvalidCredentials("Invalid token")

    user.password = get_password_hash(new_password)
    await db.commit()
    await db.refresh(user)



# -- Email verification (B-notif) -------------------------------------------

def generate_verify_token(email: str, expires_delta: timedelta | None = None) -> str:
    """Mint a JWT scoped to type=email_verify (distinct from password_reset)."""
    if expires_delta is None:
        expires_delta = timedelta(hours=settings.email_verify_token_expire_hours)
    payload = {
        "sub": email,
        "type": EMAIL_VERIFY_TOKEN_TYPE,
        "exp": datetime.now(timezone.utc) + expires_delta,
    }
    return jwt.encode(payload, get_secret_key(), algorithm=get_algorithm())


async def send_verification_email(email: str, db: AsyncSession) -> None:
    """Email a verification link. Silent no-op when the user is unknown or
    already verified (no account-existence leak)."""
    from apowerb.helpers import system_mailer

    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None or user.email_verified:
        return
    token = generate_verify_token(user.email)
    verify_url = f"{settings.app_public_url.rstrip("/")}/auth/verify-email?token={token}"
    hours = settings.email_verify_token_expire_hours
    html = system_mailer.render_branded_email(
        heading="Confirme ton adresse e-mail",
        intro="Bienvenue sur apowerb. Confirme ton adresse pour activer ton compte.",
        cta_label="Confirmer mon e-mail",
        cta_url=verify_url,
        note=f"Ce lien est valide {hours} h. Si tu n\u2019es pas \u00e0 l\u2019origine de cette inscription, ignore ce message.",
    )
    text = f"Confirme ton e-mail th2agent (valide {hours} h) : {verify_url}"
    await system_mailer.send_system_email(
        to=user.email, subject="Confirme ton adresse e-mail th2agent", html=html, text=text,
    )


async def verify_email_token(token: str, db: AsyncSession) -> None:
    """Consume an email_verify token -> set email_verified=True. Idempotent."""
    try:
        payload = jwt.decode(token, get_secret_key(), algorithms=[get_algorithm()])
    except JWTError as exc:
        raise exceptions.InvalidCredentials("Invalid or expired token") from exc
    if payload.get("type") != EMAIL_VERIFY_TOKEN_TYPE:
        raise exceptions.InvalidCredentials("Invalid token type")
    email = payload.get("sub")
    if not email:
        raise exceptions.InvalidCredentials("Invalid token")
    user = (await db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if user is None:
        raise exceptions.InvalidCredentials("Invalid token")
    if not user.email_verified:
        user.email_verified = True
        await db.commit()
        await db.refresh(user)
