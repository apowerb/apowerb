"""Tests de vérification de signature OIDC Google Pub/Sub sur le webhook Gmail.

Vérifie que l'endpoint ``POST /api/webhooks/gmail/notifications`` exige
un JWT OIDC valide (`Authorization: Bearer <token>`) émis par Google et
destiné à l'audience configurée.

Matrice de cas couverts :
- pas d'Authorization → 401
- JWT avec mauvaise signature → 403
- JWT expiré → 403
- JWT avec mauvais `aud` → 403
- JWT valide (mock) → 200 et traitement normal

Références :
- ``routers/webhook_handlers/gmail.py:handle_gmail_notification``
- ``helpers/google_oidc.py:verify_gmail_push_jwt``
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


AUDIENCE = "https://webhook.th2ai.test/api/webhooks/gmail/notifications"


def _pubsub_body(email: str = "alice@example.com", history_id: str = "42") -> dict:
    """Build a valid Pub/Sub push payload."""
    inner = json.dumps({"emailAddress": email, "historyId": history_id})
    data_b64 = base64.b64encode(inner.encode("utf-8")).decode("ascii")
    return {
        "message": {
            "data": data_b64,
            "messageId": "msg-1",
            "publishTime": "2026-04-16T00:00:00Z",
        },
        "subscription": "projects/test/subscriptions/gmail-sub",
    }


@pytest.fixture()
def app_client():
    """Build a minimal FastAPI app wiring the webhooks router with mocked settings."""
    from apowerb.configs.settings import Settings, get_settings
    from apowerb.routers.webhooks import router as webhooks_router

    def _override_settings() -> Settings:
        return Settings(
            db_host="x",
            db_name="x",
            db_user="x",
            db_password="x",
            test_token="x",
            encrypt_key="x",
            google_webhook_audience=AUDIENCE,
            webhook_dev_skip_sig=False,
        )

    app = FastAPI()
    app.include_router(webhooks_router, prefix="/api")
    app.dependency_overrides[get_settings] = _override_settings

    with patch(
        "apowerb.routers.webhook_handlers.gmail.get_settings",
        side_effect=_override_settings,
    ):
        yield TestClient(app)


class TestGmailWebhookRejectsMissingAuth:
    def test_missing_authorization_header_returns_401(self, app_client):
        resp = app_client.post(
            "/api/webhooks/gmail/notifications",
            json=_pubsub_body(),
        )
        assert resp.status_code == 401, resp.text

    def test_non_bearer_authorization_returns_401(self, app_client):
        resp = app_client.post(
            "/api/webhooks/gmail/notifications",
            json=_pubsub_body(),
            headers={"Authorization": "Basic abc"},
        )
        assert resp.status_code == 401, resp.text


class TestGmailWebhookRejectsInvalidJwt:
    def test_invalid_signature_returns_403(self, app_client):
        with patch(
            "apowerb.helpers.google_oidc.id_token.verify_oauth2_token",
            side_effect=ValueError("Invalid token signature"),
        ):
            resp = app_client.post(
                "/api/webhooks/gmail/notifications",
                json=_pubsub_body(),
                headers={"Authorization": "Bearer bad.jwt.token"},
            )
        assert resp.status_code == 403, resp.text

    def test_expired_token_returns_403(self, app_client):
        with patch(
            "apowerb.helpers.google_oidc.id_token.verify_oauth2_token",
            side_effect=ValueError("Token expired, 1700000000 < 1799999999"),
        ):
            resp = app_client.post(
                "/api/webhooks/gmail/notifications",
                json=_pubsub_body(),
                headers={"Authorization": "Bearer expired.jwt.token"},
            )
        assert resp.status_code == 403, resp.text

    def test_wrong_audience_returns_403(self, app_client):
        with patch(
            "apowerb.helpers.google_oidc.id_token.verify_oauth2_token",
            side_effect=ValueError("Token has wrong audience"),
        ):
            resp = app_client.post(
                "/api/webhooks/gmail/notifications",
                json=_pubsub_body(),
                headers={"Authorization": "Bearer wrong-aud.jwt.token"},
            )
        assert resp.status_code == 403, resp.text

    def test_wrong_issuer_returns_403(self, app_client):
        """An otherwise well-formed token with non-Google issuer must be rejected."""
        with patch(
            "apowerb.helpers.google_oidc.id_token.verify_oauth2_token",
            return_value={
                "iss": "https://evil.example.com",
                "aud": AUDIENCE,
                "email": "attacker@example.com",
            },
        ):
            resp = app_client.post(
                "/api/webhooks/gmail/notifications",
                json=_pubsub_body(),
                headers={"Authorization": "Bearer forged.jwt.token"},
            )
        assert resp.status_code == 403, resp.text


class TestGmailWebhookAcceptsValidJwt:
    def test_valid_jwt_reaches_handler(self, app_client):
        """A valid OIDC token must pass the guard and reach the Gmail handler."""
        valid_claims = {
            "iss": "https://accounts.google.com",
            "aud": AUDIENCE,
            "email": "pubsub-sa@th2ai.iam.gserviceaccount.com",
            "email_verified": True,
        }

        # Mock sessionmanager so the handler doesn't touch the DB
        fake_session = MagicMock()
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=False)
        exec_result = MagicMock()
        exec_result.scalar_one_or_none = MagicMock(return_value=None)
        exec_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        fake_session.execute = AsyncMock(return_value=exec_result)

        with patch(
            "apowerb.helpers.google_oidc.id_token.verify_oauth2_token",
            return_value=valid_claims,
        ), patch(
            "apowerb.routers.webhook_handlers.gmail.sessionmanager"
        ) as mock_mgr:
            mock_mgr.session.return_value = fake_session

            resp = app_client.post(
                "/api/webhooks/gmail/notifications",
                json=_pubsub_body(),
                headers={"Authorization": "Bearer good.jwt.token"},
            )

        assert resp.status_code == 200, resp.text


class TestSettingsGmailWebhookBoot:
    """Boot-time validation of Gmail webhook security settings."""

    def _make(self, **overrides):
        from apowerb.configs.settings import Settings

        base = dict(
            db_host="x",
            db_name="x",
            db_user="x",
            db_password="x",
            test_token="x",
            encrypt_key="x",
        )
        base.update(overrides)
        return Settings(**base)

    def test_production_requires_audience(self):
        with pytest.raises(ValueError, match="GOOGLE_WEBHOOK_AUDIENCE"):
            self._make(working_mode="production", google_webhook_audience="")

    def test_production_refuses_dev_skip(self):
        with pytest.raises(ValueError, match="WEBHOOK_DEV_SKIP_SIG"):
            self._make(
                working_mode="production",
                google_webhook_audience="https://webhook.test/gmail",
                webhook_dev_skip_sig=True,
            )

    def test_development_allows_empty_audience(self):
        s = self._make(working_mode="development", google_webhook_audience="")
        assert s.google_webhook_audience == ""

    def test_production_accepts_proper_config(self):
        s = self._make(
            working_mode="production",
            google_webhook_audience="https://webhook.test/gmail",
            webhook_dev_skip_sig=False,
        )
        assert s.google_webhook_audience == "https://webhook.test/gmail"
        assert s.webhook_dev_skip_sig is False


class TestGmailWebhookDevSkipFlag:
    """When WEBHOOK_DEV_SKIP_SIG=true, signature is skipped (dev only)."""

    def test_dev_skip_allows_missing_auth(self):
        from apowerb.configs.settings import Settings, get_settings
        from apowerb.routers.webhooks import router as webhooks_router

        def _override_settings() -> Settings:
            return Settings(
                db_host="x",
                db_name="x",
                db_user="x",
                db_password="x",
                test_token="x",
                encrypt_key="x",
                google_webhook_audience="",
                webhook_dev_skip_sig=True,
            )

        app = FastAPI()
        app.include_router(webhooks_router, prefix="/api")
        app.dependency_overrides[get_settings] = _override_settings

        fake_session = MagicMock()
        fake_session.__aenter__ = AsyncMock(return_value=fake_session)
        fake_session.__aexit__ = AsyncMock(return_value=False)
        exec_result = MagicMock()
        exec_result.scalar_one_or_none = MagicMock(return_value=None)
        exec_result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        fake_session.execute = AsyncMock(return_value=exec_result)

        with patch(
            "apowerb.routers.webhook_handlers.gmail.get_settings",
            side_effect=_override_settings,
        ), patch(
            "apowerb.routers.webhook_handlers.gmail.sessionmanager"
        ) as mock_mgr:
            mock_mgr.session.return_value = fake_session
            client = TestClient(app)
            resp = client.post(
                "/api/webhooks/gmail/notifications",
                json=_pubsub_body(),
            )

        assert resp.status_code == 200, resp.text
