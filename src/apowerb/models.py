import enum
from datetime import datetime
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
import uuid
from sqlalchemy.orm import relationship
from apowerb.helpers.database import Base  # or wherever your Base is defined
from apowerb.configs.settings import get_settings


settings = get_settings()


class Sender(enum.Enum):
    USER = "USER"
    SYSTEM = "SYSTEM"


class UserRole(enum.Enum):
    ADMIN = "ADMIN"
    USER = "USER"


class Status(enum.Enum):
    PENDING = "PENDING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class User(Base):
    __tablename__ = "user"
    user_id = Column(Integer, primary_key=True)
    first_name = Column(String(100), nullable=False)
    last_name = Column(String(100), nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    username = Column(String(150), nullable=True)
    full_name = Column(String(255), nullable=True)
    password = Column(String(255), nullable=True)
    avatar_url = Column(String(500), nullable=True)
    plan = Column(String(50), nullable=True, default="free")
    role = Column(
        Enum(UserRole, name="role_enum", schema=settings.db_schema),
        nullable=False,
        default=UserRole.USER,
    )
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # GitHub OAuth
    github_id = Column(String, unique=True, index=True, nullable=True)
    github_login = Column(String, nullable=True)
    github_access_token = Column(String, nullable=True)

    # Google OAuth
    google_id = Column(String, unique=True, index=True, nullable=True)
    google_access_token = Column(String, nullable=True)

    # Microsoft OAuth (NEW)
    microsoft_id = Column(String, unique=True, index=True, nullable=True)
    microsoft_access_token = Column(String, nullable=True)

    # LinkedIn OAuth
    linkedin_id = Column(String, unique=True, index=True, nullable=True)
    linkedin_access_token = Column(String, nullable=True)

    # MFA fields
    mfa_enabled = Column(Boolean, default=False, nullable=False, server_default="false")
    # An administrator can demand a second factor. Separate from
    # `mfa_enabled`, which says whether the user has one: the whole point is
    # the state where it is required and not yet set up.
    mfa_required = Column(Boolean, default=False, nullable=False, server_default="false")
    # Tokens minted before this instant are refused. NULL means never
    # revoked, which is every account until an administrator says otherwise.
    sessions_valid_from = Column(DateTime(timezone=True), nullable=True)
    mfa_secret = Column(String(255), nullable=True)
    onboarding_completed = Column(Boolean, default=False, nullable=False, server_default="false")
    email_verified = Column(Boolean, default=True, nullable=False, server_default="true")
    mfa_backup_codes = Column(String(1000), nullable=True)

    # Billing fields (NEW)
    credits = Column(Numeric(10, 2), default=0.00)  # User's current credit balance
    stripe_customer_id = Column(String, unique=True, nullable=True, index=True)

    # Relationships
    # conversations = relationship("Conversation", back_populates="user")
    # knowledges = relationship("Knowledge", back_populates="user")
    # datasets = relationship("Dataset", back_populates="user")
    # Relationships
    transactions = relationship("Transaction", back_populates="user")
    credit_packages = relationship("CreditPurchase", back_populates="user")
    integrations = relationship("Integration", back_populates="user", cascade="all, delete-orphan")
    webhook_subscriptions = relationship("WebhookSubscription", back_populates="user", cascade="all, delete-orphan")
    webhook_logs = relationship("WebhookLog", back_populates="user", cascade="all, delete-orphan")
    notifications = relationship("Notification", back_populates="user", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<User(user_id={self.user_id}, email={self.email})>"


class Transaction(Base):
    """Track all credit transactions (purchases, usage, refunds)"""

    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.user_id"), nullable=False)

    # Transaction details
    type = Column(String, nullable=False)  # 'purchase', 'usage', 'refund', 'bonus'
    amount = Column(
        Numeric(10, 2), nullable=False
    )  # Positive for credit, negative for debit
    balance_after = Column(
        Numeric(10, 2), nullable=False
    )  # Balance after this transaction

    # Description
    description = Column(String, nullable=True)
    extra_metadata = Column("metadata", JSON, nullable=True)  # Store additional context

    # Stripe reference (if applicable)
    stripe_payment_intent_id = Column(String, nullable=True, index=True)
    stripe_charge_id = Column(String, nullable=True)

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    user = relationship("User", back_populates="transactions")


class CreditPurchase(Base):
    """Track credit purchase sessions"""

    __tablename__ = "credit_purchases"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.user_id"), nullable=False)

    # Stripe details
    stripe_checkout_session_id = Column(String, unique=True, index=True)
    stripe_payment_intent_id = Column(String, nullable=True)

    # Purchase details
    credits_amount = Column(Numeric(10, 2), nullable=False)
    price_paid = Column(Numeric(10, 2), nullable=False)  # Amount in USD
    currency = Column(String, default="usd")

    # Status
    status = Column(String, default="pending")  # pending, completed, failed, refunded

    # Timestamps
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    user = relationship("User", back_populates="credit_packages")

class Integration(Base):
    """Stores third-party integration tokens per user (GitHub, Google, etc.)"""

    __tablename__ = "integrations"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.user_id"), nullable=False)

    # Provider identity
    provider = Column(String(50), nullable=False)          # e.g. "github", "google"
    provider_user_id = Column(String, nullable=True)       # GitHub numeric user id
    provider_username = Column(String, nullable=True)      # GitHub login handle

    # Tokens
    access_token = Column(String, nullable=True)
    refresh_token = Column(String, nullable=True)
    scopes = Column(String, nullable=True)                 # comma-separated granted scopes

    # Extra provider data 
    meta = Column(JSON, nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    # Relationships
    user = relationship("User", back_populates="integrations")
    webhook_subscriptions = relationship("WebhookSubscription", back_populates="integration")

    # Unique constraint: one integration per provider per user
    __table_args__ = (
        UniqueConstraint("user_id", "provider", name="uq_integration_user_provider"),
        # Defense in depth: any token persisted here MUST be Fernet ciphertext
        # (prefix gAAAAA, stable until year 2106). Blocks raw INSERT/UPDATE
        # bypassing save_integration_tokens() — root cause of the 2026-05-07
        # plaintext-token incident on one deployment.
        CheckConstraint(
            "access_token IS NULL OR access_token = '' OR access_token LIKE 'gAAAAA%'",
            name="ck_integrations_access_token_fernet",
        ),
        CheckConstraint(
            "refresh_token IS NULL OR refresh_token = '' OR refresh_token LIKE 'gAAAAA%'",
            name="ck_integrations_refresh_token_fernet",
        ),
    )

    def __repr__(self):
        return f"<Integration(user_id={self.user_id}, provider={self.provider}, username={self.provider_username})>"


class WebhookSubscription(Base):
    """Stores Microsoft Graph (and future provider) webhook subscriptions.

    Each row represents an active subscription that, upon receiving a
    notification from the provider (e.g. Outlook new-mail event), will
    trigger the linked agent with an optional message template.

    Lifecycle:
        active   -> subscription registered on Microsoft Graph, receiving events
        expired  -> expiration_datetime reached; must be renewed or deleted
        disabled -> manually paused by the user
        error    -> last renewal / registration attempt failed
    """

    __tablename__ = "webhook_subscriptions"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # Owner
    user_id = Column(Integer, ForeignKey("user.user_id"), nullable=False)

    # Optional link to the OAuth integration that authorised the subscription
    integration_id = Column(Integer, ForeignKey("integrations.id"), nullable=True)

    # Provider identity — "microsoft_outlook" today, extensible to "google_gmail" etc.
    provider = Column(String(50), nullable=False, default="microsoft_outlook")

    # Microsoft Graph subscription ID returned after a successful POST /subscriptions
    subscription_id = Column(String(255), nullable=True, unique=True)

    # Graph resource path being watched, e.g. "me/mailFolders('Inbox')/messages"
    resource = Column(String(500), nullable=False)

    # Comma-separated change types: "created", "updated", "deleted", "created,updated"
    change_type = Column(String(100), nullable=False, default="created")

    # Full URL of our webhook endpoint that Microsoft will POST notifications to
    notification_url = Column(String(1000), nullable=False)

    # Random secret echoed back by Microsoft so we can verify notification authenticity
    client_state = Column(String(255), nullable=False)

    # Microsoft Graph mail subscriptions expire after at most 3 days; must be renewed
    expiration_datetime = Column(DateTime(timezone=True), nullable=True)

    # Agent to invoke when a matching notification arrives
    agent_id = Column(Integer, nullable=False)

    # Jinja-style template rendered into the agent's input message, e.g.:
    # "New email from {sender}: {subject}\n\n{body_preview}"
    agent_message_template = Column(Text, nullable=True)

    # Subscription health
    status = Column(String(20), nullable=False, default="active")  # active/expired/disabled/error

    # Gmail-specific: history cursor used to fetch incremental changes via the
    # Gmail History API.  NULL for non-Gmail providers (e.g. Outlook).
    last_history_id = Column(String(50), nullable=True)

    # Observability: timestamp of the last notification successfully processed
    last_notification_at = Column(DateTime(timezone=True), nullable=True)

    # Audit timestamps (DB-side defaults so they are always set, even via raw SQL)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # Relationships
    user = relationship("User", back_populates="webhook_subscriptions")
    integration = relationship("Integration", back_populates="webhook_subscriptions")

    # Table-level constraints and indexes
    __table_args__ = (
        # Fast lookup when filtering a user's subscriptions by provider and status
        # (the most common query pattern when dispatching incoming notifications)
        Index("ix_webhook_subscriptions_user_provider_status", "user_id", "provider", "status"),
    )

    def __repr__(self):
        return (
            f"<WebhookSubscription("
            f"id={self.id}, "
            f"user_id={self.user_id}, "
            f"provider={self.provider!r}, "
            f"subscription_id={self.subscription_id!r}, "
            f"status={self.status!r}"
            f")>"
        )


class WebhookLog(Base):
    """Backlog + execution log for webhook-triggered agent runs.

    A row is created the moment a Microsoft Graph notification (or
    equivalent) is received and survives the full lifecycle:

      pending     enqueued, not picked yet
      in_progress worker has picked it and is running the agent
      retrying    failed once with a recoverable error (rate limit, transient
                  network error). next_attempt_at gates when the worker
                  picks it again.
      success     agent completed successfully
      error       failed permanently (max attempts exhausted or hard error)
      skipped     duplicate notification or stale event

    Picking rule for the worker:
      WHERE status IN ('pending', 'retrying')
        AND (next_attempt_at IS NULL OR next_attempt_at <= NOW())
      ORDER BY id ASC
      LIMIT 1

    Dedup is enforced via the unique (subscription_id, resource_id) index.
    Microsoft Graph re-delivers the same message_id on retries, so a
    duplicate INSERT raises IntegrityError and the second handler call is
    silently ignored — the original log is the source of truth.
    """

    __tablename__ = "webhook_logs"

    # Statuses used by the queue + log lifecycle. Listed here so callers
    # can import the symbol instead of free-form strings.
    STATUS_PENDING = "pending"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_RETRYING = "retrying"
    STATUS_SUCCESS = "success"
    STATUS_ERROR = "error"
    STATUS_SKIPPED = "skipped"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("user.user_id", ondelete="CASCADE"), nullable=False)
    subscription_id = Column(
        Integer,
        ForeignKey("webhook_subscriptions.id", ondelete="CASCADE"),
        nullable=False,
    )
    agent_id = Column(Integer, nullable=False)
    trigger_event = Column(String(50), nullable=False)  # "created", "updated", etc.
    # Microsoft Graph message resource id (or equivalent for other providers).
    # Used both for idempotent dedup and for the worker to re-fetch the
    # email when it picks the row.
    resource_id = Column(String(500))
    # Raw provider notification payload preserved verbatim so the worker
    # can re-pick the row after a restart without re-receiving from the
    # provider. Stored as JSON so SQLite test backends remain compatible.
    payload_json = Column(Text)
    email_subject = Column(String(500))
    email_sender = Column(String(500))
    agent_message = Column(Text)  # The message sent to the agent
    agent_response = Column(Text)  # The agent's response
    status = Column(String(20), nullable=False, default=STATUS_PENDING)
    # Deliberate operator re-processing (set by retrigger_webhook_log).
    # When True, the recorder REPLACES the existing AR row instead of the
    # anti-duplicate no-op, then is reset to False after a successful run.
    force_reprocess = Column(Boolean, nullable=False, default=False, server_default="false")
    error_message = Column(Text)
    attempts = Column(Integer, nullable=False, default=0, server_default="0")
    next_attempt_at = Column(DateTime(timezone=True))
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    duration_ms = Column(Integer)  # How long the agent took to respond
    # Email content captured at webhook reception time so we can
    # rebuild the case file without re-fetching Graph (the message
    # is often deleted by the operator within days, cf incident
    # 2026-05-19 "replay 52 ARs -> 100% 404 ErrorItemNotFound").
    email_body_html = Column(Text)  # raw HTML body from Graph
    email_body_text = Column(Text)  # plain-text strip for search
    # JSONB list of {filename, path, content_type, size} for each PJ
    # stored under the attachment root.
    attachments = Column(JSON)

    # Relationships
    user = relationship("User", back_populates="webhook_logs")
    subscription = relationship("WebhookSubscription")

    __table_args__ = (
        Index("ix_webhook_logs_user_sub", "user_id", "subscription_id"),
        # Worker pick scan: find the next pending/retrying row whose
        # next_attempt_at has elapsed. The (status, next_attempt_at, id)
        # composite keeps the oldest-first FIFO ordering cheap.
        Index(
            "ix_webhook_logs_pick",
            "status", "next_attempt_at", "id",
        ),
        # Dedup Graph re-deliveries on the same (subscription, message).
        # NULL resource_id rows (legacy) are allowed to coexist by leaving
        # the constraint plain — Postgres treats NULLs as distinct.
        Index(
            "ux_webhook_logs_sub_resource",
            "subscription_id", "resource_id",
            unique=True,
            postgresql_where=text("resource_id IS NOT NULL"),
            sqlite_where=text("resource_id IS NOT NULL"),
        ),
    )

    def __repr__(self):
        return (
            f"<WebhookLog("
            f"id={self.id}, "
            f"user_id={self.user_id}, "
            f"subscription_id={self.subscription_id}, "
            f"status={self.status!r}, "
            f"attempts={self.attempts}"
            f")>"
        )


class Notification(Base):
    """User notifications for webhook events, system alerts, etc.

    Each notification can optionally link to a specific chat/page via
    the ``link`` field and carry extra context in ``metadata_json``.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_user_read", "user_id", "is_read"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("user.user_id", ondelete="CASCADE"),
        nullable=False,
    )
    title = Column(String(255), nullable=False)
    message = Column(Text)
    type = Column(String(50), nullable=False, default="info")  # "webhook", "info", "warning", "error"
    # Link to open when notification is clicked
    link = Column(String(500))  # e.g. "/chat?agent=agent201&session=webhook_2_xxx"
    # Metadata for webhook notifications
    metadata_json = Column(Text)  # JSON string with extra data (agent_id, email_subject, etc.)
    is_read = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    user = relationship("User", back_populates="notifications")

    def __repr__(self):
        return (
            f"<Notification("
            f"id={self.id}, "
            f"user_id={self.user_id}, "
            f"type={self.type!r}, "
            f"is_read={self.is_read}"
            f")>"
        )
    


class BIItemType(str, enum.Enum):
    CHART = "chart"
    DATA = "data"
    DASHBOARD = "dashboard"


class BIItemStatus(str, enum.Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"
    DRAFT = "draft"
    DELETED = "deleted"


class BusinessIntelligence(Base):
    __tablename__ = "business_intelligence"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(255), nullable=False)
    type = Column(String(20), nullable=False)

    children = Column(JSON, nullable=False, default=list)
    parents = Column(JSON, nullable=False, default=list)

    owner = Column(String(255), nullable=False)
    organization_id = Column(String(255), nullable=False)
    project_id = Column(String(255), nullable=False, default="thaink2")

    permissions = Column(JSON, nullable=False, default=list)
    config = Column(JSON, nullable=True)  # Full chart/dashboard JSON config

    status = Column(String(30), nullable=False, default=BIItemStatus.ACTIVE.value)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "name",
            "type",
            "organization_id",
            "project_id",
            name="uq_bi_name_type_org_project",
        ),
        Index("idx_bi_org_project", "organization_id", "project_id"),
        Index("idx_bi_type_org_project", "type", "organization_id", "project_id"),
        Index("idx_bi_owner", "owner"),
        {"schema": settings.db_schema},
    )


class OAuthState(Base):
    """One-time CSRF state tokens for OAuth authorisation flows.

    Each row is created when the user hits an ``/integrations/*/connect``
    endpoint and consumed (deleted) when the matching ``/callback`` endpoint
    validates it. Rows expire after ``expires_at`` and a periodic cleanup
    removes stale ones to keep the table bounded.
    """

    __tablename__ = "oauth_states"

    state = Column(String(255), primary_key=True)
    user_id = Column(
        Integer,
        ForeignKey("user.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider = Column(String(100), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)

    __table_args__ = (
        Index("ix_oauth_states_user_provider", "user_id", "provider"),
    )

    def __repr__(self):
        return (
            f"<OAuthState(state={self.state[:8]}…, "
            f"user_id={self.user_id}, provider={self.provider!r})>"
        )


class SharedConversation(Base):
    __tablename__ = "shared_conversations"

    id         = Column(String, primary_key=True)
    title      = Column(String, nullable=False)
    agent_name = Column(String, nullable=True)
    messages   = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=True)
    # Email of the authenticated user who created the share. Used to enforce
    # ownership on read/delete when the share is not explicitly public.
    owner_id   = Column(String(255), nullable=True, index=True)
    # When True, the share can be fetched by anyone knowing the (unguessable)
    # share id — intended for "public link" sharing with external recipients.
    # When False, only the owner can fetch / delete the share.
    is_public  = Column(Boolean, nullable=False, default=False, server_default="false")

class LlmUsage(Base):
    """One row per completed LLM model turn (see
    ``apowerb.core.agent_helpers.usage_recorder``). Best-effort
    accounting: rows can be missing on DB hiccups, never guaranteed
    exhaustive — used for cost/usage reporting, not billing-grade audit.
    Rows are lost only on DB failure or a process kill mid-write; the
    write task's reference is held in
    ``usage_recorder._pending_writes`` (a done-callback removes it once
    the INSERT completes) so it cannot be silently garbage-collected
    before landing.
    """

    __tablename__ = "llm_usage"

    id = Column(Integer, primary_key=True, autoincrement=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    agent_id = Column(Integer, nullable=False)
    agent_name = Column(String(255), nullable=False)
    owner_id = Column(String(255), nullable=True)
    session_id = Column(String(255), nullable=True)
    # ADK invocation id -- groups the successive model turns of ONE user
    # request together. Null on rows written before the drivers
    # instrumentation shipped (2026-07-20), hence nullable forever.
    invocation_id = Column(String(255), nullable=True)
    # Comma-separated names of the tools this turn ASKED to call. The
    # results of those calls land in the NEXT turn's prompt, which is how
    # a tool's token cost is attributed (cf. the per_tool driver query).
    tool_names = Column(Text, nullable=True)
    invocation_source = Column(String(255), nullable=True)
    model = Column(String(255), nullable=False)
    input_tokens = Column(Integer, nullable=False, default=0)
    output_tokens = Column(Integer, nullable=False, default=0)
    thoughts_tokens = Column(Integer, nullable=False, default=0)
    cached_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)
    # True quand le tour a consommé la clé mutualisée thaink2 (agent en
    # `thaink2/default`) : c'est la seule consommation que le quota
    # plafonne, une clé API perso étant payée par son propriétaire.
    # `model` reste le modèle RÉELLEMENT appelé (gemini/…), pas la
    # sentinelle — sinon le calcul de coût par modèle perdrait sa clé de
    # jointure avec la grille tarifaire.
    # False pour les lignes antérieures : aucune ne passait par ce modèle.
    billed_to_thaink2 = Column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    __table_args__ = (
        Index("ix_llm_usage_agent_created", "agent_id", "created_at"),
        Index("ix_llm_usage_owner_created", "owner_id", "created_at"),
        # The drivers queries (turn_profile, per_tool, top_invocations)
        # window over the turns of one invocation, ordered by id.
        Index("ix_llm_usage_invocation", "invocation_id", "id"),
    )

    def __repr__(self):
        return (
            f"<LlmUsage(id={self.id}, agent_id={self.agent_id}, "
            f"model={self.model!r}, total_tokens={self.total_tokens})>"
        )
