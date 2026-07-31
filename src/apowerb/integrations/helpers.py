import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional

from cryptography.fernet import InvalidToken
from sqlalchemy import MetaData, Table, create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from apowerb.configs.settings import get_settings
from apowerb.configs.th2logger import setup_logging
from apowerb.helpers import encryptor as _encryptor
from apowerb.helpers.database_connection import DBConfig
from apowerb.models import Integration, User

logger = setup_logging(__name__)
_settings = get_settings()


_INTEGRATIONS_TABLE = "integrations"


# ---------------------------------------------------------------------------
# Unified encrypted token helpers (B7)
# ---------------------------------------------------------------------------


def _build_engine() -> Engine:
    """Sync engine for the integrations table.

    Exposed as a module-level function so tests can monkeypatch it to redirect
    the helpers to an in-memory SQLite without going through the real DBConfig.
    """
    async_url = DBConfig().get_db_url()
    sync_url = async_url.replace("postgresql+asyncpg://", "postgresql+psycopg2://")
    return create_engine(sync_url, echo=False, future=True)


def _integrations_table(engine: Engine) -> Table:
    # The table lives in settings.db_schema (e.g. "th2agent_dev"), not the default
    # search_path — we must pass schema= explicitly so SQLAlchemy finds it.
    schema = _settings.db_schema if _settings.db_schema != "public" else None
    return Table(
        _INTEGRATIONS_TABLE,
        MetaData(),
        autoload_with=engine,
        schema=schema,
    )


def _require_fernet() -> None:
    """Abort if no encryption key is configured — never fall back to plaintext."""
    if getattr(_encryptor, "fernet", None) is None:
        raise RuntimeError(
            "ENCRYPT_KEY is not configured — refusing to persist OAuth tokens "
            "without Fernet encryption. Set ENCRYPT_KEY and restart the app."
        )


def _encrypt_optional(value: Optional[str]) -> Optional[str]:
    """Encrypt a non-empty string. Pass None and "" through untouched."""
    if value is None or value == "":
        return value
    _require_fernet()
    return _encryptor.encrypt_value(value)


def _decrypt_optional(value: Optional[str]) -> Optional[str]:
    """Decrypt a stored token. Pass None/"" through untouched.

    Raises RuntimeError with a human-readable message if the ciphertext cannot
    be decrypted with the active Fernet key (wrong key, tampered value, ...).
    """
    if value is None or value == "":
        return value
    _require_fernet()
    try:
        return _encryptor.decrypt_value(value)
    except InvalidToken as exc:
        raise RuntimeError(
            "Failed to decrypt integration token — the ENCRYPT_KEY does not "
            "match the one used at write time (invalid token)."
        ) from exc


def save_integration_tokens(
    *,
    user_id: int,
    provider: str,
    access_token: Optional[str],
    refresh_token: Optional[str],
    provider_username: Optional[str] = None,
    provider_user_id: Optional[str] = None,
    scopes: str = "",
    meta: Optional[dict] = None,
) -> None:
    """Upsert an Integration row with Fernet-encrypted access/refresh tokens.

    Raises
    ------
    RuntimeError
        If ``ENCRYPT_KEY`` is not configured (no silent plaintext fallback).
    """
    _require_fernet()

    enc_access = _encrypt_optional(access_token)
    enc_refresh = _encrypt_optional(refresh_token)

    engine = _build_engine()
    table = _integrations_table(engine)
    now = datetime.now(timezone.utc)

    with engine.begin() as conn:
        existing = conn.execute(
            select(table.c.id).where(
                (table.c.user_id == user_id) & (table.c.provider == provider)
            )
        ).first()

        payload = {
            "access_token":      enc_access,
            "refresh_token":     enc_refresh,
            "provider_username": provider_username,
            "provider_user_id":  provider_user_id,
            "scopes":            scopes,
            "meta":              meta or {},
            "updated_at":        now,
        }

        if existing:
            conn.execute(
                table.update()
                .where(table.c.id == existing[0])
                .values(**payload)
            )
        else:
            conn.execute(
                table.insert().values(
                    user_id=user_id,
                    provider=provider,
                    created_at=now,
                    **payload,
                )
            )


# ---------------------------------------------------------------------------
# ORM guard — block plaintext tokens before they hit the DB
# ---------------------------------------------------------------------------
#
# The DB-level CheckConstraint on ``integrations`` is the strong barrier
# (it covers raw psql / Core INSERTs too). This ORM listener gives a
# clean, early Python error for code that creates ``Integration(...)``
# instances directly instead of going through ``save_integration_tokens``.


def _assert_token_is_fernet(value: Optional[str], field: str) -> None:
    """Raise RuntimeError if ``value`` is not a Fernet-encrypted ciphertext."""
    if value is None or value == "":
        return
    _require_fernet()
    try:
        _encryptor.decrypt_value(value)
    except InvalidToken as exc:
        raise RuntimeError(
            f"integrations.{field} must be Fernet-encrypted; got plaintext "
            f"or ciphertext that does not decrypt with the active ENCRYPT_KEY. "
            f"Use save_integration_tokens() to write tokens."
        ) from exc


def _validate_integration_tokens_before_write(_mapper, _connection, target) -> None:
    _assert_token_is_fernet(getattr(target, "access_token", None), "access_token")
    _assert_token_is_fernet(getattr(target, "refresh_token", None), "refresh_token")


event.listen(Integration, "before_insert", _validate_integration_tokens_before_write)
event.listen(Integration, "before_update", _validate_integration_tokens_before_write)


def get_integration_tokens(
    *, user_id: int, provider: str
) -> Optional[dict]:
    """Return the decrypted tokens for ``(user_id, provider)`` or ``None``.

    Returned dict (when present):
        {
            "access_token":      str | None,
            "refresh_token":     str | None,
            "provider_username": str | None,
            "provider_user_id":  str | None,
            "scopes":            str | None,
            "meta":              dict,
        }
    """
    engine = _build_engine()
    table = _integrations_table(engine)

    with engine.connect() as conn:
        row = conn.execute(
            select(
                table.c.access_token,
                table.c.refresh_token,
                table.c.provider_username,
                table.c.provider_user_id,
                table.c.scopes,
                table.c.meta,
            ).where(
                (table.c.user_id == user_id) & (table.c.provider == provider)
            )
        ).first()

    if row is None:
        return None

    return {
        "access_token":      _decrypt_optional(row.access_token),
        "refresh_token":     _decrypt_optional(row.refresh_token),
        "provider_username": row.provider_username,
        "provider_user_id":  row.provider_user_id,
        "scopes":            row.scopes,
        "meta":              row.meta or {},
    }


def _looks_encrypted(value: str) -> bool:
    """Cheap probe: try to decrypt with the active key.

    - True  → value decrypts cleanly → already encrypted, skip.
    - False → InvalidToken → value is plaintext and must be re-encrypted.
    """
    if value is None or value == "":
        return True  # nothing to do
    try:
        _encryptor.decrypt_value(value)
        return True
    except InvalidToken:
        return False
    except Exception:
        # Any other decoding failure — err on the safe side and do not
        # silently rewrite the row.  The admin will see the row as "skipped"
        # and can investigate.
        return True


def encrypt_legacy_integration_tokens() -> int:
    """Re-encrypt any integration row whose access/refresh token is plaintext.

    Idempotent — rows already encrypted with the active Fernet key are left
    untouched.

    Returns
    -------
    int
        Number of rows that were re-encrypted in place.
    """
    _require_fernet()

    engine = _build_engine()
    table = _integrations_table(engine)

    migrated = 0
    with engine.begin() as conn:
        rows = conn.execute(
            select(table.c.id, table.c.access_token, table.c.refresh_token)
        ).fetchall()

        for row in rows:
            new_values: dict[str, Any] = {}

            access_raw = row.access_token
            if access_raw not in (None, "") and not _looks_encrypted(access_raw):
                new_values["access_token"] = _encryptor.encrypt_value(access_raw)

            refresh_raw = row.refresh_token
            if refresh_raw not in (None, "") and not _looks_encrypted(refresh_raw):
                new_values["refresh_token"] = _encryptor.encrypt_value(refresh_raw)

            if new_values:
                new_values["updated_at"] = datetime.now(timezone.utc)
                conn.execute(
                    table.update()
                    .where(table.c.id == row.id)
                    .values(**new_values)
                )
                migrated += 1

    logger.info("encrypt_legacy_integration_tokens: migrated %s row(s).", migrated)
    return migrated


# ---------------------------------------------------------------------------
# Legacy async fetcher (kept unchanged for runtime callers)
# ---------------------------------------------------------------------------


async def _fetch_integration_configs_async(
    provider: str = "microsoft_outlook",
    user: str | int | None = None,
) -> dict:
    """Fetch integration tokens for a given user (the invoker by default).

    The user is resolved in this order:
        1. ``user`` argument when provided (numeric id or email).
        2. The current invoker bound on the request ContextVar — this is
           the right choice for user-personal integrations (Outlook,
           Gmail, Drive, ...).
        3. ``AGENT_OWNER`` env var — fallback for background runs and
           agent-shared resources.

    The resolved value may be either:
    - An integer user_id (e.g. "1")  → query Integration directly
    - An email address (e.g. "ib@thaink2.com") → resolve User first, then Integration
    """
    if user is None:
        from apowerb.core.invocation_context import resolve_integration_user
        owner_raw = resolve_integration_user(prefer_invoker=True)
    else:
        owner_raw = str(user)
    if not owner_raw:
        raise RuntimeError(
            "No invoker bound on the request and AGENT_OWNER env var is not set."
        )

    url = DBConfig().get_db_url()
    engine = create_async_engine(url, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    try:
        # Determine if AGENT_OWNER is a numeric user_id or an email
        try:
            owner_id = int(owner_raw)
        except ValueError:
            # It's an email — resolve to user_id first
            async with async_session() as db:
                result = await db.execute(
                    select(User).where(User.email == owner_raw)
                )
                user = result.scalar_one_or_none()
            if not user:
                raise RuntimeError(
                    f"No user found with email='{owner_raw}'."
                )
            owner_id = user.user_id
            logger.debug("Resolved AGENT_OWNER email '%s' → user_id=%s", owner_raw, owner_id)

        # Fetch the integration by user_id + provider
        async with async_session() as db:
            result = await db.execute(
                select(Integration).where(
                    Integration.user_id == owner_id,
                    Integration.provider == provider,
                )
            )
            integration = result.scalar_one_or_none()

    finally:
        await engine.dispose()

    if not integration:
        raise RuntimeError(
            f"No {provider} integration found for user_id={owner_id}. "
            "The user must connect via the Integrations page first."
        )

    access = _decrypt_optional(integration.access_token)
    refresh = _decrypt_optional(integration.refresh_token)

    return {
        "access_token":      access,
        "refresh_token":     refresh,
        "meta":              integration.meta or {},
        "provider_user_id":  integration.provider_user_id,
        "provider_username": integration.provider_username,
    }


def fetch_integration_configs(
    provider: str = "microsoft_outlook",
    user: str | int | None = None,
) -> dict:
    """Sync wrapper — safe to call from within a running FastAPI/uvicorn event loop.

    Spawns a background thread with its own isolated event loop. The
    invoker ContextVar is captured upfront so it survives the loop swap.
    Returns dict with 'access_token' and 'refresh_token'.
    """
    if user is None:
        from apowerb.core.invocation_context import resolve_integration_user
        # Captured before the worker thread starts (ContextVar lookup
        # would return None inside the new thread otherwise).
        user = resolve_integration_user(prefer_invoker=True)

    def _run_in_thread():
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                _fetch_integration_configs_async(provider, user=user)
            )
        finally:
            loop.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_run_in_thread)
        return future.result()


async def _persist_refreshed_tokens_async(
    provider: str,
    access_token: str | None,
    refresh_token: str | None,
    scope: str | None = None,
    user: str | int | None = None,
) -> bool:
    """Encrypt + persist a freshly refreshed token pair to the Integration
    row of the resolved user (invoker by default).

    Critical: the token must be persisted on the **invoker's** row, not
    the agent owner's — otherwise a refresh triggered by user A
    overwrites user B's tokens.

    Microsoft rotates refresh tokens on every refresh — NOT persisting the
    new one silently invalidates the next refresh with a stale token
    (``invalid_grant``), which looks to the user like "my integration
    expires all the time".
    """
    if user is None:
        from apowerb.core.invocation_context import resolve_integration_user
        owner_raw = resolve_integration_user(prefer_invoker=True)
    else:
        owner_raw = str(user)
    if not owner_raw:
        return False
    if not access_token and not refresh_token:
        return False

    url = DBConfig().get_db_url()
    engine = create_async_engine(url, echo=False)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        try:
            owner_id = int(owner_raw)
        except ValueError:
            async with async_session() as db:
                result = await db.execute(
                    select(User).where(User.email == owner_raw)
                )
                user = result.scalar_one_or_none()
            if not user:
                return False
            owner_id = user.user_id

        async with async_session() as db:
            result = await db.execute(
                select(Integration).where(
                    Integration.user_id == owner_id,
                    Integration.provider == provider,
                )
            )
            integration = result.scalar_one_or_none()
            if not integration:
                return False
            if access_token:
                integration.access_token = _encryptor.encrypt_value(access_token)  # type: ignore[assignment]
            if refresh_token:
                integration.refresh_token = _encryptor.encrypt_value(refresh_token)  # type: ignore[assignment]
            if scope:
                integration.scopes = scope  # type: ignore[assignment]
            await db.commit()
            return True
    finally:
        await engine.dispose()


def persist_refreshed_tokens(
    provider: str,
    *,
    access_token: str | None = None,
    refresh_token: str | None = None,
    scope: str | None = None,
    user: str | int | None = None,
) -> bool:
    """Sync wrapper for ``_persist_refreshed_tokens_async``. Returns True
    when the Integration row was updated, False otherwise. Best-effort —
    swallows errors. The invoker is captured upfront so the worker thread
    persists on the right row."""

    if user is None:
        from apowerb.core.invocation_context import resolve_integration_user
        user = resolve_integration_user(prefer_invoker=True)

    def _run_in_thread():
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                _persist_refreshed_tokens_async(
                    provider,
                    access_token=access_token,
                    refresh_token=refresh_token,
                    scope=scope,
                    user=user,
                )
            )
        finally:
            loop.close()

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(_run_in_thread)
            return future.result()
    except Exception as exc:  # pragma: no cover — defensive
        logger.warning("persist_refreshed_tokens failed for %s: %s", provider, exc)
        return False
