"""Service layer for managing Microsoft Graph webhook subscriptions.

Handles the full lifecycle of Outlook mail subscriptions:
- Token acquisition (refresh_token -> access_token exchange)
- Subscription creation, renewal, and deletion via the Graph API
- Email fetching for notification payloads
- Secure ``client_state`` generation for notification verification

All HTTP calls use ``httpx.AsyncClient`` to stay consistent with the
other integration services (``microsoft.py``, ``google.py``).
"""

import secrets
from datetime import datetime, timedelta, timezone
from logging import getLogger

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.configs.settings import get_settings
from apowerb.models import Integration

logger = getLogger(__name__)

_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_GRAPH_SUBSCRIPTIONS_URL = f"{_GRAPH_BASE}/subscriptions"


class OutlookWebhookService:
    """Manages Microsoft Graph webhook subscriptions for Outlook."""

    # ------------------------------------------------------------------
    # Token helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def get_access_token_for_user(db: AsyncSession, user_id: int) -> str:
        """Get a fresh access token for the user's Outlook integration.

        Reads the ``refresh_token`` stored in the Integration table and
        exchanges it for a short-lived ``access_token`` via the Microsoft
        OAuth token endpoint.

        Args:
            db:      Async database session.
            user_id: ID of the user whose token we need.

        Returns:
            A valid Microsoft Graph access token string.

        Raises:
            RuntimeError: If no integration exists, the refresh token is
                missing, or the token exchange fails.
        """
        settings = get_settings()

        result = await db.execute(
            select(Integration).where(
                Integration.user_id == user_id,
                Integration.provider == "microsoft_outlook",
            )
        )
        integration = result.scalar_one_or_none()

        if not integration:
            raise RuntimeError(
                f"No Microsoft Outlook integration found for user_id={user_id}. "
                "The user must connect their Outlook account first."
            )

        from cryptography.fernet import InvalidToken
        from apowerb.helpers.encryptor import decrypt_value
        if not integration.refresh_token:
            raise RuntimeError(
                f"Microsoft Outlook integration for user_id={user_id} has no refresh token. "
                "The user must reconnect their Outlook account."
            )
        try:
            refresh_token = decrypt_value(integration.refresh_token)
        except InvalidToken:
            logger.warning(
                "Outlook refresh_token for user_id=%s is plaintext (pre-B7) — "
                "run the integration token migration CLI.",
                user_id,
            )
            refresh_token = integration.refresh_token

        client_id = settings.microsoft_integration_client_id
        client_secret = settings.microsoft_integration_client_secret
        tenant_id = settings.microsoft_integration_tenant_id

        if not client_id or not client_secret:
            raise RuntimeError(
                "Microsoft Integration OAuth credentials are not configured "
                "(microsoft_integration_client_id / microsoft_integration_client_secret)."
            )

        token_url = (
            f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        )

        async with httpx.AsyncClient() as client:
            response = await client.post(
                token_url,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                    "scope": "offline_access Mail.Read",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15.0,
            )

        if response.status_code != 200:
            body = response.text
            logger.error(
                "Outlook token refresh failed for user_id=%s: HTTP %s - %s",
                user_id,
                response.status_code,
                body,
            )
            if "invalid_grant" in body.lower():
                raise RuntimeError(
                    "The Outlook refresh token has expired or been revoked. "
                    "The user must reconnect their Outlook account."
                )
            raise RuntimeError(
                f"Failed to refresh Outlook access token (HTTP {response.status_code})."
            )

        data = response.json()
        access_token = data.get("access_token")
        if not access_token:
            raise RuntimeError(
                "Microsoft token endpoint did not return an access_token."
            )

        # Persist the new refresh_token (encrypted) if Microsoft rotated it
        new_refresh_token = data.get("refresh_token")
        if new_refresh_token and new_refresh_token != refresh_token:
            from apowerb.helpers.encryptor import encrypt_value
            integration.refresh_token = encrypt_value(new_refresh_token)  # type: ignore[assignment]
            await db.commit()
            logger.info(
                "Rotated refresh token (encrypted) persisted for user_id=%s",
                user_id,
            )

        return access_token

    # ------------------------------------------------------------------
    # Subscription CRUD
    # ------------------------------------------------------------------

    @staticmethod
    async def create_subscription(
        access_token: str,
        notification_url: str,
        resource: str = "me/mailFolders('Inbox')/messages",
        change_type: str = "created",
        client_state: str = "",
        expiration_minutes: int = 4230,  # ~2.9 days (max is 3 days = 4320 min)
    ) -> dict:
        """Create a Microsoft Graph subscription.

        Registers a push-notification subscription so Microsoft will POST
        to ``notification_url`` whenever the specified ``resource`` changes.

        Args:
            access_token:       Valid Bearer token for Microsoft Graph.
            notification_url:   Public HTTPS URL that Microsoft will call.
            resource:           Graph resource path to watch.
            change_type:        Comma-separated change types.
            client_state:       Secret echoed back by Microsoft for verification.
            expiration_minutes: Subscription TTL in minutes (max 4320 for mail).

        Returns:
            The subscription dict from Microsoft Graph (includes ``id``,
            ``expirationDateTime``, etc.).

        Raises:
            RuntimeError: If the Graph API call fails.
        """
        expiration_dt = datetime.now(timezone.utc) + timedelta(
            minutes=expiration_minutes
        )

        payload = {
            "changeType": change_type,
            "notificationUrl": notification_url,
            "resource": resource,
            "expirationDateTime": expiration_dt.isoformat(),
            "clientState": client_state,
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                _GRAPH_SUBSCRIPTIONS_URL,
                json=payload,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                timeout=15.0,
            )

        if response.status_code not in (200, 201):
            logger.error(
                "Failed to create Graph subscription: HTTP %s - %s",
                response.status_code,
                response.text,
            )
            raise RuntimeError(
                f"Microsoft Graph subscription creation failed "
                f"(HTTP {response.status_code}): {response.text[:500]}"
            )

        result = response.json()
        logger.info(
            "Created Graph subscription id=%s for resource=%s (expires %s)",
            result.get("id"),
            resource,
            result.get("expirationDateTime"),
        )
        return result

    @staticmethod
    async def renew_subscription(
        access_token: str,
        subscription_id: str,
        expiration_minutes: int = 4230,
    ) -> dict:
        """Renew / extend a Microsoft Graph subscription.

        Must be called before the subscription expires (max lifetime for
        mail subscriptions is 3 days = 4320 minutes).

        Args:
            access_token:       Valid Bearer token for Microsoft Graph.
            subscription_id:    The Graph subscription ID to renew.
            expiration_minutes: New TTL in minutes from now.

        Returns:
            Updated subscription dict from Microsoft Graph.

        Raises:
            RuntimeError: If the Graph API call fails.
        """
        expiration_dt = datetime.now(timezone.utc) + timedelta(
            minutes=expiration_minutes
        )

        async with httpx.AsyncClient() as client:
            response = await client.patch(
                f"{_GRAPH_SUBSCRIPTIONS_URL}/{subscription_id}",
                json={"expirationDateTime": expiration_dt.isoformat()},
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                timeout=15.0,
            )

        if response.status_code != 200:
            logger.error(
                "Failed to renew Graph subscription %s: HTTP %s - %s",
                subscription_id,
                response.status_code,
                response.text,
            )
            if response.status_code == 404:
                raise LookupError(
                    f"Microsoft Graph subscription {subscription_id} "
                    f"no longer exists (ResourceNotFound)."
                )
            raise RuntimeError(
                f"Microsoft Graph subscription renewal failed "
                f"(HTTP {response.status_code}): {response.text[:500]}"
            )

        result = response.json()
        logger.info(
            "Renewed Graph subscription id=%s (new expiration %s)",
            subscription_id,
            result.get("expirationDateTime"),
        )
        return result

    @staticmethod
    async def delete_subscription(
        access_token: str,
        subscription_id: str,
    ) -> bool:
        """Delete a Microsoft Graph subscription.

        After deletion, Microsoft will stop sending notifications for this
        subscription.

        Args:
            access_token:    Valid Bearer token for Microsoft Graph.
            subscription_id: The Graph subscription ID to delete.

        Returns:
            ``True`` on success.

        Raises:
            RuntimeError: If the Graph API call fails (except 404 which is
                treated as already-deleted and returns ``True``).
        """
        async with httpx.AsyncClient() as client:
            response = await client.delete(
                f"{_GRAPH_SUBSCRIPTIONS_URL}/{subscription_id}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                },
                timeout=15.0,
            )

        if response.status_code == 404:
            logger.warning(
                "Graph subscription %s already deleted (404).",
                subscription_id,
            )
            return True

        if response.status_code != 204:
            logger.error(
                "Failed to delete Graph subscription %s: HTTP %s - %s",
                subscription_id,
                response.status_code,
                response.text,
            )
            raise RuntimeError(
                f"Microsoft Graph subscription deletion failed "
                f"(HTTP {response.status_code}): {response.text[:500]}"
            )

        logger.info("Deleted Graph subscription id=%s", subscription_id)
        return True

    # ------------------------------------------------------------------
    # Email fetching (for notification processing)
    # ------------------------------------------------------------------

    @staticmethod
    async def fetch_email(
        access_token: str,
        message_resource: str,
    ) -> dict:
        """Fetch email details from Microsoft Graph.

        The notification only provides a resource path like
        ``Users/{user-id}/Messages/{msg-id}``. This method fetches the
        actual email content so it can be forwarded to the agent.

        Args:
            access_token:     Valid Bearer token for Microsoft Graph.
            message_resource: Graph resource path from the notification
                (e.g. ``Users/abc-123/Messages/xyz-456``).

        Returns:
            Email data dict with subject, sender, body, recipients, etc.

        Raises:
            RuntimeError: If the Graph API call fails.
        """
        # Build the full URL -- the resource path from the notification
        # is relative to the Graph API base.
        url = f"{_GRAPH_BASE}/{message_resource}"

        params = {
            "$select": (
                "id,subject,from,receivedDateTime,bodyPreview,body,"
                "toRecipients,ccRecipients,hasAttachments"
            ),
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(
                url,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
                params=params,
                timeout=15.0,
            )

        if response.status_code != 200:
            logger.error(
                "Failed to fetch email %s: HTTP %s - %s",
                message_resource,
                response.status_code,
                response.text,
            )
            raise RuntimeError(
                f"Failed to fetch email from Graph API "
                f"(HTTP {response.status_code}): {response.text[:500]}"
            )

        email_data = response.json()
        logger.info(
            "Fetched email id=%s subject=%r",
            email_data.get("id"),
            email_data.get("subject"),
        )
        return email_data

    # ------------------------------------------------------------------
    # Attachments
    # ------------------------------------------------------------------

    @staticmethod
    async def fetch_attachments(
        access_token: str,
        message_resource: str,
    ) -> list[dict]:
        """Fetch all file attachments of a Graph message as raw bytes.

        Returns a list of ``{"name": str, "contentType": str,
        "content": bytes}``. Inline attachments (``isInline=True`` —
        typically images embedded in the HTML body for display) are
        skipped: the body itself already carries them as ``cid:``
        references and they are not what the operator means by "PJ".

        Args:
            access_token:     Valid Bearer token for Microsoft Graph.
            message_resource: Graph resource path (e.g.
                ``Users/abc-123/Messages/xyz-456``).

        Raises:
            RuntimeError: If the metadata call to ``/attachments`` fails.

        Note: failures to download an *individual* attachment body
        (``$value``) are logged but do NOT raise — we still want to
        capture the rest of the email. The webhook handler decides what
        to do with a partial set.
        """
        url = f"{_GRAPH_BASE}/{message_resource}/attachments"
        # Graph rejects "@odata.type" in $select (HTTP 400 BadRequest,
        # cf live error 2026-05-20). The discriminator is emitted in
        # the payload regardless, so we filter on it below.
        params = {
            "$select": "id,name,contentType,size,isInline",
        }
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=headers, params=params, timeout=15.0)
            if response.status_code != 200:
                logger.error(
                    "Failed to list attachments %s: HTTP %s - %s",
                    message_resource, response.status_code, response.text,
                )
                raise RuntimeError(
                    f"Failed to list attachments from Graph "
                    f"(HTTP {response.status_code}): {response.text[:500]}"
                )
            listing = response.json().get("value", [])

            out: list[dict] = []
            for att in listing:
                if att.get("isInline"):
                    continue
                # Only handle file attachments. Item/reference
                # attachments (forwarded mails, SharePoint refs) are
                # not files we can dump to disk in this PR.
                if att.get("@odata.type") != "#microsoft.graph.fileAttachment":
                    logger.info(
                        "Skipping non-file attachment %r (type=%s)",
                        att.get("name"), att.get("@odata.type"),
                    )
                    continue
                att_id = att["id"]
                value_url = (
                    f"{_GRAPH_BASE}/{message_resource}/attachments/{att_id}/$value"
                )
                # $value streams the raw bytes — no base64 wrapping,
                # so memory pressure scales with one attachment at a
                # time, not the whole batch.
                try:
                    body = await client.get(value_url, headers=headers, timeout=60.0)
                except httpx.HTTPError as exc:
                    logger.warning(
                        "Failed to download attachment %s/%s: %s",
                        message_resource, att_id, exc,
                    )
                    continue
                if body.status_code != 200:
                    logger.warning(
                        "Failed to download attachment %s/%s: HTTP %s",
                        message_resource, att_id, body.status_code,
                    )
                    continue
                out.append({
                    "name": att.get("name") or att_id,
                    "contentType": att.get("contentType") or "application/octet-stream",
                    "content": body.content,
                })
        logger.info(
            "Fetched %d file attachment(s) for %s", len(out), message_resource,
        )
        return out

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def generate_client_state() -> str:
        """Generate a cryptographically secure random ``client_state`` string.

        Microsoft echoes this value back in every notification so we can
        verify the notification truly originated from Graph.

        Returns:
            A URL-safe base64 string of 32 random bytes.
        """
        return secrets.token_urlsafe(32)
