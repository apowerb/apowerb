import os

from th2agent.configs.th2logger import setup_logging

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache

_logger = setup_logging(__name__)

_ENV_FILE = ".env"


def _warn_duplicate_env_keys(env_path: str = _ENV_FILE) -> None:
    """Log a warning for any variable declared more than once in *env_path*.

    pydantic-settings (and python-dotenv under the hood) silently keep the
    last definition when a key appears multiple times. This makes copy-paste
    or merge mistakes invisible — exactly what produced the 2026-05-07 SCEI
    incident, where ``JWT_SECRET_KEY`` ended up with conflicting values.

    The function is best-effort: it only flags exact duplicates of the same
    key. Unknown keys, malformed lines, comments and ``export FOO=bar``
    prefixes are tolerated. It never raises and never blocks boot.

    Edge cases the helper *does not* try to solve (intentional):

    - **Quoted keys** (``"FOO"=bar``). Some ``.env`` parsers accept them,
      others reject them; we strip surrounding quotes from the key so a
      mix of quoted and unquoted forms of the same name still flags as a
      duplicate. Quoted *values* (``FOO="bar=baz"``) are unaffected — only
      the key side is normalised.
    - **Multi-line values** (``FOO="line1\\nline2"``). pydantic-settings
      handles those, but the parser here treats every newline as a line
      boundary. False negatives are possible if a duplicate of the
      *outer* key appears between the lines of a multi-line value, which
      is so unusual we accept it and document it here.
    - **CRLF line endings**. ``.strip()`` already handles ``\\r``; the
      double-strip in the body (raw and key) protects against trailing
      ``\\r`` even if the outer one is removed.
    """
    if not os.path.isfile(env_path):
        return

    seen: dict[str, list[int]] = {}
    try:
        with open(env_path, encoding="utf-8") as f:
            for lineno, raw in enumerate(f, start=1):
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                if "=" not in line:
                    continue
                key = line.split("=", 1)[0].strip()
                # Normalise quoted keys ("FOO"=bar or 'FOO'=bar) so a mixed
                # quoted / unquoted use of the same name still trips the
                # duplicate detector.
                if len(key) >= 2 and key[0] == key[-1] and key[0] in ('"', "'"):
                    key = key[1:-1]
                if not key or any(c.isspace() for c in key):
                    continue
                seen.setdefault(key, []).append(lineno)
    except OSError as exc:
        _logger.debug("Could not scan %s for duplicates: %s", env_path, exc)
        return

    for key, lines in seen.items():
        if len(lines) > 1:
            _logger.warning(
                "duplicate variable %r in %s on lines %s — pydantic-settings "
                "silently keeps the last one. Remove the earlier definitions "
                "to avoid surprises.",
                key,
                env_path,
                ", ".join(str(n) for n in lines),
            )


# Champs indispensables au *runtime serveur* mais pas à l'import du paquet.
# Ils ont une valeur par défaut vide pour que ``import th2agent.<module>``
# fonctionne sans ``.env`` (th2agent doit être consommable comme library) ;
# le refus explicite se fait au boot via ``assert_runtime_ready()``.
RUNTIME_REQUIRED_FIELDS: tuple[str, ...] = (
    "db_host",
    "db_name",
    "db_user",
    "db_password",
    "test_token",
    "encrypt_key",
)


class Settings(BaseSettings):
    """Application settings."""

    db_host: str = ""
    db_name: str = ""
    db_user: str = ""
    db_password: str = ""
    db_port: int = 5432  # Default PostgreSQL port
    db_schema: str = "public"  # Default schema
    db_type: str = "postgresql"  # Default to PostgreSQL
    db_agent_store_table_name: str = "th2agents_store"
    # sqlalchemy config
    echo_sql: bool = False
    #s3 ovh configuration
    storage_mode : str = "S3"
    s3_region: str = ""
    s3_access_key: str = ""
    s3_access_key_secret: str = ""
    s3_endpoint: str = ""
    s3_bucket_name: str = ""
    # working mode
    working_mode: str = "development"  # or "production"
    test_token: str = ""  # For authentication
    encrypt_key: str = ""
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 120
    # ADK configuration
    root_path: str = "http://localhost:8000"

    # Scheduler feature flag — set SCHEDULER_ENABLED=false to disable Mage scheduler integration
    scheduler_enabled: bool = True

    # ── Modèle « thaink2 par défaut » ───────────────────────────────
    # Credentials mutualisées servant les agents dont agent_model vaut
    # `thaink2/default` : l'utilisateur n'a ni clé ni modèle à saisir, et
    # ne peut pas les lire (cf core/agent_helpers/default_llm.py).
    # Le modèle par défaut n'est proposé dans l'UI que si MODEL **et** KEY
    # sont renseignés ici — sinon l'option n'existe simplement pas.
    # L'indirection est volontaire : basculer Mistral -> Gemini se fait ici,
    # sans toucher aux agents déjà créés.
    default_llm_model: str = ""
    default_llm_api_key: str = ""
    # Optionnel — endpoint OpenAI-compat (ex. OVH) quand le modèle n'est pas
    # servi par l'API publique du provider.
    default_llm_api_base: str = ""

    # Plafond mensuel de tokens sur le modèle mutualisé, par utilisateur.
    # Ne compte QUE la consommation payée par thaink2 : une clé API perso
    # n'est jamais plafonnée. 0 (ou négatif) = illimité, ce qui sert de
    # kill-switch sans redéploiement si le garde-fou bloque à tort.
    default_llm_monthly_token_quota: int = 1_000_000
    # Override par plan, en JSON — ex.
    # DEFAULT_LLM_PLAN_QUOTAS={"free": 1000000, "pro": 50000000}
    default_llm_plan_quotas: dict[str, int] = {}

    # Registration feature flag — set AUTH_REGISTER_ENABLED=false to keep
    # the login endpoint open while disabling user self-registration. SCEI
    # uses this so internal users can sign in but the public POST /users/
    # endpoint stays closed.
    auth_register_enabled: bool = True

    # Basic auth (email + password) feature flag — set AUTH_BASIC_ENABLED=false
    # to disable email/password login, registration and password reset endpoints.
    # OAuth flows (Google/Microsoft/etc.) are unaffected.
    auth_basic_enabled: bool = True

    # ── Notifications / system mailer ───────────────────────────────
    # Shared Outlook mailbox used as the From for all system emails.
    notification_email: str = "notifications@thaink2.com"
    notification_email_provider: str = "outlook"
    # Login (in DB) that owns the Outlook integration with access to the
    # shared mailbox above. Its refresh_token sends headless mail.
    notification_integration_owner: str = "farid.azouaou@thaink2.com"
    # Principal admin — recipient of ETL/system alerts.
    super_admin_email: str = "farid.azouaou@thaink2.com"
    # Public base URL of the front app, for verify/reset links.
    app_public_url: str = "http://localhost:3000"
    # Email-verification gate. OFF by default; never enable on SCEI until
    # the email_verified backfill is confirmed in prod.
    auth_email_verification_enabled: bool = False
    email_verify_token_expire_hours: int = 24
    # Destinataires additionnels des alertes ETL (en plus de super_admin_email),
    # séparés par des virgules.
    etl_alert_recipients: str = "support@thaink2.com,david.gnaglo@thaink2.com"

    # Scheduler configuration (Mage) - matches .env variable names
    base_url: str = "http://localhost:6789"
    api_key: str = "your_mage_api_key_here"
    oauth_token: str = "your_mage_oauth_token_here"
    project_name: str = "default_repo"
    mage_pipeline_uuid: str = "agents"

    # Orchestrator selection: "mage" (default, no behaviour change) or "th2etl".
    orchestrator: str = "mage"
    th2etl_base_url: str = "http://localhost:8009"

    # Authentication settings
    frontend_urls: str = "http://localhost:3000"  # Allowed origins for CORS
    # B8 — Explicit CORS whitelist (comma-separated). When set, supersedes
    # ``frontend_urls``. Lets us restrict allowed origins independently of the
    # frontend_urls plumbing in the codebase.
    cors_allowed_origins: str = (
        "http://localhost:3000,https://th2ai.thaink2.com"
    )
    cors_allowed_methods: str = "GET,POST,PUT,PATCH,DELETE,OPTIONS"
    cors_allowed_headers: str = (
        "Authorization,Content-Type,Accept,X-Requested-With,Agent-Id,X-Webhook-Signature,X-Webhook-Timestamp"
    )

    # oauth settings
    ## Github
    github_client_id: str = "tbd"
    github_client_secret: str = "tbd"
    github_redirect_uri: str = "http://localhost:8000/auth/github/callback"
    # GitHub Integration OAuth
    # connect a user's GitHub account as a workspace integration.
    github_integration_client_id: str = ""
    github_integration_client_secret: str = ""
    github_integration_redirect_uri: str = (
        "http://localhost:3000/integrations/github/callback"
    )
    ## Google
    google_client_id: str = "tbd"
    google_client_secret: str = "tbd"
    google_redirect_uri: str = "http://localhost:8000/auth/google/callback"

    # Microsoft OAuth (NEW)
    microsoft_client_id: str = "tbd"
    microsoft_client_secret: str = "tbd"
    microsoft_tenant_id: str = (
        "common"  # Use "common" for multi-tenant, or specific tenant ID
    )

    # LinkedIn OAuth
    linkedin_client_id: str = "tbd"
    linkedin_client_secret: str = "tbd"

    # Stripe (NEW)
    stripe_secret_key: str = ""
    stripe_publishable_key: str = ""
    stripe_webhook_secret: str = ""

    # Microsoft Integration OAuth (used for all Microsoft workspace integrations)
    microsoft_integration_client_id: str = ""
    microsoft_integration_client_secret: str = ""
    microsoft_integration_tenant_id: str = "common"
    microsoft_integration_redirect_uri: str = ""

    # Google Integration OAuth (single app, scopes vary per service)
    google_integration_client_id: str = ""
    google_integration_client_secret: str = ""
    google_integration_redirect_uri: str = "http://localhost:3000/integrations/google/callback"

    # Gmail Pub/Sub webhook settings
    gmail_pubsub_project_id: str = ""  # Google Cloud project ID
    gmail_pubsub_topic: str = ""       # e.g. "gmail-notifications" (just the topic name, not full path)
    # Expected `aud` claim on the OIDC JWT signed by Google Pub/Sub when
    # pushing notifications to our webhook. Must match the audience
    # configured on the Pub/Sub subscription (typically the public webhook
    # URL). MANDATORY in production — refused at boot if empty while
    # working_mode == "production".
    google_webhook_audience: str = ""
    # DEV-ONLY escape hatch to bypass signature verification on the Gmail
    # webhook. Keeps local testing simple without a Google Cloud setup.
    # MUST remain false in production (boot refuses otherwise).
    webhook_dev_skip_sig: bool = False

    # RAG webhook / SSE streaming
    public_base_url: str = "http://localhost:8000"
    rag_webhook_secret: str = "th2-webhook-default-secret"

    # Integration token encryption migration (B7)
    # Dev/staging flag: when true, the backend runs
    # ``encrypt_legacy_integration_tokens()`` at boot.  Never enable in
    # production — the migration CLI must be invoked explicitly instead.
    encrypt_legacy_on_boot: bool = False

    # ------------------------------------------------------------------ #
    # B8 — production refusals                                           #
    # ------------------------------------------------------------------ #
    @model_validator(mode="after")
    def _refuse_dangerous_prod_flags(self) -> "Settings":
        """Refuse to boot in production with dev-only escape hatches enabled.

        - ``BYPASS_AUTH=true`` fabricates a fake ADMIN user in
          ``auth/dependencies.py`` — catastrophic if toggled in prod.
        - ``ENCRYPT_LEGACY_ON_BOOT=true`` re-encrypts DB tokens at boot;
          the migration must be invoked explicitly via the CLI in prod.
        - ``WEBHOOK_DEV_SKIP_SIG`` is already covered by
          ``_validate_gmail_webhook_security`` below; kept here for
          symmetry in the error message.
        """
        import os as _os

        is_prod = self.working_mode.lower() in {"prod", "production"}
        if not is_prod:
            return self

        bypass_auth = _os.environ.get("BYPASS_AUTH", "").lower() == "true"
        if bypass_auth:
            raise ValueError(
                "BYPASS_AUTH=true is forbidden when WORKING_MODE=production. "
                "Refusing to start — this flag fabricates an ADMIN user."
            )
        if self.encrypt_legacy_on_boot:
            raise ValueError(
                "ENCRYPT_LEGACY_ON_BOOT=true is forbidden when "
                "WORKING_MODE=production. Invoke the migration CLI "
                "explicitly instead."
            )
        return self

    @model_validator(mode="after")
    def _warn_default_webhook_secret(self) -> "Settings":
        if self.rag_webhook_secret == "th2-webhook-default-secret":
            _logger.warning(
                "SECURITY: rag_webhook_secret is using the default value. "
                "Set a strong, unique RAG_WEBHOOK_SECRET environment variable in production."
            )
        return self

    @model_validator(mode="after")
    def _validate_gmail_webhook_security(self) -> "Settings":
        """Refuse to boot in production without a Gmail webhook audience,
        or with the dev signature-skip flag enabled."""
        is_prod = self.working_mode.lower() in {"prod", "production"}
        if is_prod and not self.google_webhook_audience:
            raise ValueError(
                "GOOGLE_WEBHOOK_AUDIENCE must be set in production "
                "(required to verify Gmail Pub/Sub push notifications)."
            )
        if is_prod and self.webhook_dev_skip_sig:
            raise ValueError(
                "WEBHOOK_DEV_SKIP_SIG cannot be enabled in production."
            )
        if self.webhook_dev_skip_sig:
            _logger.warning(
                "SECURITY: WEBHOOK_DEV_SKIP_SIG=true — Gmail webhook signature "
                "verification is DISABLED. This MUST only be used for local "
                "development."
            )
        return self

    # ============================================================
    # Per-org template visibility — generic mapping from email domain
    # to org slug. Templates may declare `visible_to_orgs` to restrict
    # who can see them; user membership is computed via this map.
    # Empty by default. Override via ORG_DOMAIN_SLUGS env var, e.g.
    # ORG_DOMAIN_SLUGS={"example.com": "example"} (JSON).
    # ============================================================
    org_domain_slugs: dict[str, str] = {}

    # ============================================================
    # Répertoires de travail. Historiquement codés en relatif
    # ("agents_pool", "./uploads/…"), donc résolus contre le CWD :
    # th2agent ne fonctionnait que lancé depuis la racine du repo.
    # Les défauts ci-dessous reproduisent exactement l'ancien
    # comportement (racine = CWD) ; un consommateur externe pose
    # TH2AGENT_RUNTIME_ROOT (ou les chemins un par un) et n'a plus
    # aucune contrainte sur son répertoire courant.
    #
    # Ces réglages ne concernent que le disque : les clés S3 gardent
    # leur propre espace de noms (`uploads/{agent}/…` dans le bucket).
    # ============================================================
    runtime_root: str = ""  # vide => CWD, comme avant
    agents_pool_dir: str = "agents_pool"
    artifacts_store_dir: str = "artifacts_store"
    uploads_dir: str = "uploads"
    toolbox_dir: str = ""  # vide => runtime_root

    # Accept extra values
    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        extra="ignore",
    )

    # ------------------------------------------------------------------ #
    # Validation runtime (boot), distincte de la validation d'import     #
    # ------------------------------------------------------------------ #
    def missing_runtime_fields(self) -> list[str]:
        """Retourne les champs indispensables au serveur restés vides."""
        return [f for f in RUNTIME_REQUIRED_FIELDS if not getattr(self, f, "")]

    def assert_runtime_ready(self) -> None:
        """Refuse de démarrer le serveur avec une configuration incomplète.

        Remplace le ``ValidationError`` que pydantic levait auparavant à
        l'import de n'importe quel module. Même exigence, mais déclenchée
        au boot et avec un message qui nomme les variables d'environnement
        manquantes plutôt que les champs pydantic.
        """
        missing = self.missing_runtime_fields()
        if missing:
            raise RuntimeError(
                "th2agent ne peut pas démarrer : variable(s) d'environnement "
                "manquante(s) — " + ", ".join(f.upper() for f in missing) + ". "
                "Renseignez-les dans le .env du déploiement (cf .env.example)."
            )


@lru_cache()
def get_settings() -> Settings:
    """Get application settings from environment variables or .env file."""
    _warn_duplicate_env_keys()
    return Settings()
