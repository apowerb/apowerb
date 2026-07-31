"""Pydantic schemas for webhook subscription management.

Covers the create/read/list lifecycle for Microsoft Graph (and future
provider) webhook subscriptions, as well as the inbound notification
payload that Microsoft POSTs to our endpoint.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict


class WebhookSubscriptionCreate(BaseModel):
    """Request to create a new webhook subscription."""

    provider: Literal["microsoft_outlook", "google_gmail"] = "microsoft_outlook"
    resource: str = "me/mailFolders('Inbox')/messages"
    change_type: str = "created"  # "created", "created,updated", "updated", "deleted"
    agent_id: int  # Agent to trigger
    agent_message_template: str | None = None  # Optional template


class WebhookSubscriptionUpdate(BaseModel):
    """Request to update an existing webhook subscription (all fields optional)."""

    agent_id: int | None = None
    agent_message_template: str | None = None
    change_type: str | None = None


class WebhookSubscriptionResponse(BaseModel):
    """Response after creating a subscription."""

    id: int
    provider: str
    subscription_id: str | None
    resource: str
    change_type: str
    agent_id: int
    agent_message_template: str | None
    status: str
    expiration_datetime: str | None
    created_at: str | None

    model_config = ConfigDict(from_attributes=True)


class WebhookSubscriptionList(BaseModel):
    """List of subscriptions."""

    subscriptions: list[WebhookSubscriptionResponse]


class MicrosoftGraphNotification(BaseModel):
    """A single notification from Microsoft Graph."""

    subscriptionId: str
    changeType: str
    clientState: str | None = None
    resource: str
    resourceData: dict | None = None
    tenantId: str | None = None


class MicrosoftGraphNotificationPayload(BaseModel):
    """The full notification payload from Microsoft Graph."""

    value: list[MicrosoftGraphNotification]


class GmailPubSubMessage(BaseModel):
    """The inner message from a Pub/Sub push delivery."""

    data: str  # base64-encoded JSON: {"emailAddress": "...", "historyId": "..."}
    messageId: str | None = None
    publishTime: str | None = None


class GmailPubSubNotification(BaseModel):
    """Full Pub/Sub push notification payload.

    Google Cloud Pub/Sub wraps the Gmail notification in an envelope:
    ``{ "message": { "data": "<base64>", ... }, "subscription": "..." }``
    """

    message: GmailPubSubMessage
    subscription: str | None = None  # e.g. "projects/my-project/subscriptions/gmail-push"
