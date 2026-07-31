"""Odoo (SaaS) integration — non-OAuth, uses API key + URL + database.

Unlike Microsoft/Google integrations (OAuth), Odoo authenticates via
JSON-RPC against the instance's `/jsonrpc` endpoint. The user provides:

  - URL       (e.g. "https://mycompany.odoo.com")
  - database  (Odoo database name, visible in the URL on login)
  - login     (typically the user's email)
  - api_key   (personal API key created in Odoo > Preferences > Account Security)

We store the API key encrypted in ``access_token``, the login in
``provider_username``, the Odoo uid in ``provider_user_id`` and the
URL + database in ``meta``.
"""

from logging import getLogger
from typing import Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from apowerb.helpers.encryptor import decrypt_value, encrypt_value
from apowerb.models import Integration

logger = getLogger(__name__)

ODOO_PROVIDER = "odoo"
_JSONRPC_TIMEOUT = 20.0


class OdooConnectionError(Exception):
    """Raised when Odoo authentication or JSON-RPC call fails."""


def _normalize_url(url: str) -> str:
    """Strip trailing slash so we can safely append '/jsonrpc'."""
    return (url or "").rstrip("/")


async def _jsonrpc(url: str, payload: dict) -> dict:
    """POST a JSON-RPC 2.0 envelope to {url}/jsonrpc and return the parsed body.

    Raises OdooConnectionError on HTTP errors, transport errors, or
    Odoo-level errors (the "error" key in the response).
    """
    endpoint = f"{_normalize_url(url)}/jsonrpc"
    try:
        async with httpx.AsyncClient(timeout=_JSONRPC_TIMEOUT) as client:
            resp = await client.post(
                endpoint,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
    except httpx.RequestError as exc:
        raise OdooConnectionError(f"Could not reach Odoo instance at {endpoint}: {exc}") from exc

    if resp.status_code != 200:
        raise OdooConnectionError(
            f"Odoo returned HTTP {resp.status_code} for {endpoint}: {resp.text[:300]}"
        )

    try:
        body = resp.json()
    except ValueError as exc:
        raise OdooConnectionError(f"Odoo returned non-JSON response: {resp.text[:300]}") from exc

    if "error" in body:
        err = body["error"]
        # Odoo nests the real message under data.message in 2xx responses with error key
        data = err.get("data") or {}
        msg = data.get("message") or err.get("message") or str(err)
        raise OdooConnectionError(f"Odoo error: {msg}")

    return body


async def authenticate(url: str, database: str, login: str, api_key: str) -> int:
    """Authenticate against Odoo using api_key as the password.

    Returns the numeric Odoo uid. Raises OdooConnectionError on any
    failure (wrong instance, wrong db, wrong login, revoked key, ...).
    """
    payload = {
        "jsonrpc": "2.0",
        "method":  "call",
        "params": {
            "service": "common",
            "method":  "authenticate",
            "args":    [database, login, api_key, {}],
        },
    }
    body = await _jsonrpc(url, payload)
    uid = body.get("result")
    if not uid or not isinstance(uid, int):
        raise OdooConnectionError(
            "Odoo authentication failed — check the URL, database, login, and API key."
        )
    return uid


async def execute_kw(
    url: str,
    database: str,
    uid: int,
    api_key: str,
    model: str,
    method: str,
    args: list,
    kwargs: Optional[dict] = None,
):
    """Call object.execute_kw(db, uid, key, model, method, args, kwargs) via JSON-RPC.

    Returns whatever Odoo returns (list, dict, int, bool, etc.) or raises
    OdooConnectionError on a transport/protocol/Odoo error.
    """
    payload = {
        "jsonrpc": "2.0",
        "method":  "call",
        "params": {
            "service": "object",
            "method":  "execute_kw",
            "args":    [database, uid, api_key, model, method, args, kwargs or {}],
        },
    }
    body = await _jsonrpc(url, payload)
    return body.get("result")


async def save_integration(
    db: AsyncSession,
    user_id: int,
    url: str,
    database: str,
    login: str,
    api_key: str,
    uid: int,
    display_name: Optional[str] = None,
) -> Integration:
    """Upsert the Odoo integration row for ``user_id``.

    The api_key is Fernet-encrypted at rest (B7 — uniform with other
    providers). Kept on the provided ``AsyncSession`` so the caller's unit
    of work (transaction, pre-existing row, test mocks) is preserved.
    """
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == user_id,
            Integration.provider == ODOO_PROVIDER,
        )
    )
    integration = result.scalar_one_or_none()

    encrypted_key = encrypt_value(api_key)
    meta = {
        "url":          _normalize_url(url),
        "database":     database,
        "display_name": display_name or login,
    }

    if integration:
        integration.access_token     = encrypted_key        # type: ignore[assignment]
        integration.refresh_token    = ""                    # type: ignore[assignment]
        integration.provider_user_id = str(uid)              # type: ignore[assignment]
        integration.provider_username = login                # type: ignore[assignment]
        integration.scopes           = ""                    # type: ignore[assignment]
        integration.meta             = meta                  # type: ignore[assignment]
        flag_modified(integration, "meta")
        logger.info("Updated Odoo integration for user_id=%s (login=%s)", user_id, login)
    else:
        integration = Integration(
            user_id=user_id,
            provider=ODOO_PROVIDER,
            provider_user_id=str(uid),
            provider_username=login,
            access_token=encrypted_key,
            refresh_token="",
            scopes="",
            meta=meta,
        )
        db.add(integration)
        logger.info("Created Odoo integration for user_id=%s (login=%s)", user_id, login)

    await db.commit()
    await db.refresh(integration)
    return integration


async def get_credentials(db: AsyncSession, user_id: int) -> Optional[dict]:
    """Return decrypted Odoo credentials for the given user, or None if absent.

    Returned dict: ``{url, database, login, api_key, uid}``.
    """
    result = await db.execute(
        select(Integration).where(
            Integration.user_id == user_id,
            Integration.provider == ODOO_PROVIDER,
        )
    )
    integration = result.scalar_one_or_none()
    if not integration:
        return None

    meta = integration.meta or {}
    try:
        api_key = decrypt_value(integration.access_token) if integration.access_token else ""
    except Exception as exc:
        logger.warning("Failed to decrypt Odoo api_key for user_id=%s: %s", user_id, exc)
        return None

    return {
        "url":      meta.get("url") or "",
        "database": meta.get("database") or "",
        "login":    integration.provider_username or "",
        "api_key":  api_key,
        "uid":      int(integration.provider_user_id) if integration.provider_user_id else 0,
    }
