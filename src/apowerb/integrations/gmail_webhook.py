"""Service layer for managing Gmail push notification subscriptions via Google Cloud Pub/Sub.

Handles the full lifecycle of Gmail mail watch subscriptions:
- Token acquisition (refresh_token -> access_token exchange)
- Watch subscription creation and cancellation via the Gmail API
- History fetching for detecting new messages
- Email metadata fetching for notification payloads

All HTTP calls use ``httpx.AsyncClient`` to stay consistent with the
other integration services (``outlook_webhook.py``, ``google.py``).
"""

import secrets
from logging import getLogger

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.configs.settings import get_settings
from apowerb.models import Integration

logger = getLogger(__name__)

_GMAIL_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


class GmailWebhookService:
    """Manages Gmail push notification subscriptions via Google Cloud Pub/Sub."""

    # ------------------------------------------------------------------
    # Token helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def get_access_token_for_user(db: AsyncSession, user_id: int) -> str:
        """Get a fresh access token for the user's Gmail integration.

        Reads the ``refresh_token`` stored in the Integration table and
        exchanges it for a short-lived ``access_token`` via the Google
        OAuth token endpoint.

        Args:
            db:      Async database session.
            user_id: ID of the user whose token we need.

        Returns:
            A valid Google Gmail access token string.

        Raises:
            RuntimeError: If no integration exists, the refresh token is
                missing, or the token exchange fails.
        """
        settings = get_settings()

        result = await db.execute(
            select(Integration).where(
                Integration.user_id == user_id,
                Integration.provider == "google_gmail",
            )
        )
        integration = result.scalar_one_or_none()

        if not integration:
            raise RuntimeError(
                f"No Google Gmail integration found for user_id={user_id}. "
                "The user must connect their Gmail account first."
            )

        from cryptography.fernet import InvalidToken
        from apowerb.helpers.encryptor import decrypt_value
        if not integration.refresh_token:
            raise RuntimeError(
                f"Google Gmail integration for user_id={user_id} has no refresh token. "
                "The user must reconnect their Gmail account."
            )
        try:
            refresh_token = decrypt_value(integration.refresh_token)
        except InvalidToken:
            logger.warning(
                "Gmail refresh_token for user_id=%s is plaintext (pre-B7) — "
                "run the integration token migration CLI.",
                user_id,
            )
            refresh_token = integration.refresh_token

        client_id = settings.google_integration_client_id
        client_secret = settings.google_integration_client_secret

        if not client_id or not client_secret:
            raise RuntimeError(
                "Google Integration OAuth credentials are not configured "
                "(google_integration_client_id / google_integration_client_secret)."
            )

        async with httpx.AsyncClient() as client:
            response = await client.post(
                _GOOGLE_TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": refresh_token,
                    "grant_type": "refresh_token",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15.0,
            )

        if response.status_code != 200:
            body = response.text
            logger.error(
                "Gmail token refresh failed for user_id=%s: HTTP %s - %s",
                user_id,
                response.status_code,
                body,
            )
            if "invalid_grant" in body.lower():
                raise RuntimeError(
                    "The Gmail refresh token has expired or been revoked. "
                    "The user must reconnect their Gmail account."
                )
            raise RuntimeError(
                f"Failed to refresh Gmail access token (HTTP {response.status_code})."
            )

        data = response.json()
        access_token = data.get("access_token")
        if not access_token:
            raise RuntimeError(
                "Google token endpoint did not return an access_token."
            )

        # Persist the new refresh_token (encrypted) if Google rotated it
        new_refresh_token = data.get("refresh_token")
        if new_refresh_token and new_refresh_token != refresh_token:
            from apowerb.helpers.encryptor import encrypt_value
            integration.refresh_token = encrypt_value(new_refresh_token)  # type: ignore[assignment]
            await db.commit()
            logger.info(
                "Rotated Gmail refresh token (encrypted) persisted for user_id=%s",
                user_id,
            )

        return access_token

    # ------------------------------------------------------------------
    # Watch subscription management
    # ------------------------------------------------------------------

    @staticmethod
    async def watch_mailbox(
        access_token: str,
        topic_name: str,
        label_ids: list[str] | None = None,
    ) -> dict:
        """Create a Gmail push notification watch.

        Calls ``POST /gmail/v1/users/me/watch`` to register push
        notifications via Google Cloud Pub/Sub.

        Args:
            access_token: Valid Bearer token for the Gmail API.
            topic_name:   Full Pub/Sub topic name
                (e.g. ``projects/my-project/topics/gmail-notifications``).
            label_ids:    Gmail labels to watch. Defaults to ``["INBOX"]``.

        Returns:
            Dict with ``historyId`` and ``expiration`` (epoch milliseconds).

        Raises:
            RuntimeError: If the Gmail API call fails.
        """
        payload = {
            "topicName": topic_name,
            "labelIds": label_ids or ["INBOX"],
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{_GMAIL_BASE}/watch",
                json=payload,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                timeout=15.0,
            )

        if response.status_code not in (200, 201):
            logger.error(
                "Failed to create Gmail watch: HTTP %s - %s",
                response.status_code,
                response.text,
            )
            raise RuntimeError(
                f"Gmail watch creation failed "
                f"(HTTP {response.status_code}): {response.text[:500]}"
            )

        result = response.json()
        logger.info(
            "Created Gmail watch: historyId=%s, expiration=%s",
            result.get("historyId"),
            result.get("expiration"),
        )
        return result

    @staticmethod
    async def stop_watch(access_token: str) -> bool:
        """Stop Gmail push notifications for the user.

        Calls ``POST /gmail/v1/users/me/stop`` to cancel all active
        push notification watches.

        Args:
            access_token: Valid Bearer token for the Gmail API.

        Returns:
            ``True`` on success.

        Raises:
            RuntimeError: If the Gmail API call fails.
        """
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{_GMAIL_BASE}/stop",
                headers={
                    "Authorization": f"Bearer {access_token}",
                },
                timeout=15.0,
            )

        # Gmail stop returns 204 on success, but also 200 in some cases
        if response.status_code not in (200, 204):
            logger.error(
                "Failed to stop Gmail watch: HTTP %s - %s",
                response.status_code,
                response.text,
            )
            raise RuntimeError(
                f"Gmail watch stop failed "
                f"(HTTP {response.status_code}): {response.text[:500]}"
            )

        logger.info("Stopped Gmail watch for user")
        return True

    # ------------------------------------------------------------------
    # History and email fetching (for notification processing)
    # ------------------------------------------------------------------

    @staticmethod
    async def fetch_history(
        access_token: str,
        start_history_id: str,
        label_id: str | None = "INBOX",
    ) -> list[dict]:
        """Fetch mailbox history since a given historyId.

        Calls ``GET /gmail/v1/users/me/history`` to retrieve history
        records of newly added messages.  Handles pagination via
        ``nextPageToken``.

        Args:
            access_token:      Valid Bearer token for the Gmail API.
            start_history_id:  The historyId to start from (exclusive).
            label_id:          Optional label filter (e.g. ``"INBOX"``).

        Returns:
            Flat list of history records containing added messages.
        """
        all_records: list[dict] = []
        page_token: str | None = None

        while True:
            params: dict[str, str] = {
                "startHistoryId": start_history_id,
                "historyTypes": "messageAdded",
            }
            if label_id:
                params["labelId"] = label_id
            if page_token:
                params["pageToken"] = page_token

            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{_GMAIL_BASE}/history",
                    headers={
                        "Authorization": f"Bearer {access_token}",
                        "Accept": "application/json",
                    },
                    params=params,
                    timeout=15.0,
                )

            if response.status_code == 404:
                # historyId is too old -- Gmail has purged the history
                logger.warning(
                    "Gmail history not found for startHistoryId=%s (purged). "
                    "Returning empty history.",
                    start_history_id,
                )
                return []

            if response.status_code != 200:
                logger.error(
                    "Failed to fetch Gmail history: HTTP %s - %s",
                    response.status_code,
                    response.text,
                )
                raise RuntimeError(
                    f"Gmail history fetch failed "
                    f"(HTTP {response.status_code}): {response.text[:500]}"
                )

            data = response.json()
            history = data.get("history", [])
            all_records.extend(history)

            page_token = data.get("nextPageToken")
            if not page_token:
                break

        return all_records

    @staticmethod
    async def fetch_email(access_token: str, message_id: str) -> dict:
        """Fetch email metadata from the Gmail API.

        Retrieves the message with metadata format including Subject,
        From, Date, and To headers plus the snippet.

        Args:
            access_token: Valid Bearer token for the Gmail API.
            message_id:   The Gmail message ID.

        Returns:
            Email data dict from Gmail API.

        Raises:
            RuntimeError: If the Gmail API call fails.
        """
        params = {
            "format": "full",
        }

        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{_GMAIL_BASE}/messages/{message_id}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
                params=params,
                timeout=15.0,
            )

        if response.status_code != 200:
            logger.error(
                "Failed to fetch Gmail email %s: HTTP %s - %s",
                message_id,
                response.status_code,
                response.text,
            )
            raise RuntimeError(
                f"Failed to fetch email from Gmail API "
                f"(HTTP {response.status_code}): {response.text[:500]}"
            )

        email_data = response.json()
        headers = email_data.get("payload", {}).get("headers", [])
        subject = ""
        for h in headers:
            if h.get("name", "").lower() == "subject":
                subject = h.get("value", "")
                break

        logger.info(
            "Fetched Gmail email id=%s subject=%r",
            email_data.get("id"),
            subject,
        )
        return email_data

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    @staticmethod
    def generate_client_state() -> str:
        """Generate a cryptographically secure random ``client_state`` string.

        Used for internal tracking since Gmail Pub/Sub does not have a
        native client_state verification mechanism like Microsoft Graph.

        Returns:
            A URL-safe base64 string of 32 random bytes.
        """
        return secrets.token_urlsafe(32)
