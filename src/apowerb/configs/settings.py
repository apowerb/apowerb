import os
import re

from apowerb.configs.th2logger import setup_logging

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from functools import lru_cache

_logger = setup_logging(__name__)

_ENV_FILE = ".env"

# Shipped placeholder for RAG_WEBHOOK_SECRET. It is a literal in a public
# repository, so it is a known credential rather than a secret: production
# refuses to boot on it (see _refuse_default_webhook_secret).
_DEFAULT_RAG_WEBHOOK_SECRET = "th2-webhook-default-secret"

# The value this codebase uses to mean "not configured yet" -- see the OAuth
# client ids below, which all default to it. A setting left on it is unset,
# whatever a `grep` on the .env file suggests.
_UNSET_PLACEHOLDER = "tbd"

# The settings that say where THIS deployment is reached, or where it sends
# people. Not the addresses of *other* services: Mage (`base_url`) and th2etl
# may legitimately sit on localhost beside the API, and flagging them would
# cry wolf on a perfectly sound install.
#
# Read from the class rather than duplicated here, so a default that moves off
# localhost one day cannot leave this list quietly protecting nothing --
# `test_public_urls.py` asserts exactly that.
_PUBLIC_URL_SETTINGS = (
    "root_path",
    "public_base_url",
    "app_public_url",
    "frontend_urls",
    "cors_allowed_origins",
    "github_redirect_uri",
    "github_integration_redirect_uri",
    "google_redirect_uri",
    "google_integration_redirect_uri",
    "outlook_mail_redirect_uri",
)

# What each one breaks when it is left behind, so the warning is actionable
# rather than a list of names. Checked against the code that reads them, not
# against what their names suggest: `cors_allowed_origins` is the one CORS
# uses (`main.py`), `frontend_urls` is not, and `root_path` is the base of the
# HTTP calls this API makes to ITSELF, not an ASGI mount path.
#
# ⚠️ Blind spot, named rather than hidden: this guard only sees settings whose
# shipped default mentions localhost. `microsoft_integration_redirect_uri`
# defaults to the empty string, so forgetting it stays silent here.
_PUBLIC_URL_CONSEQUENCE = {
    "app_public_url": "password-reset and e-mail-verification links",
    "public_base_url": "the callback URLs this API hands out to webhooks",
    "root_path": "the base of the HTTP calls this API makes to itself",
    "frontend_urls": "the origins the browser is allowed to be served from",
    "cors_allowed_origins": "which origins the browser is allowed to call from",
    "github_redirect_uri": "where GitHub sends the user back after sign-in",
    "github_integration_redirect_uri": "where GitHub sends the user back after connecting the integration",
    "google_redirect_uri": "where Google sends the user back after sign-in",
    "google_integration_redirect_uri": "where Google sends the user back after connecting the integration",
    "outlook_mail_redirect_uri": "where Microsoft sends the user back after connecting Outlook Mail",
}


# Public URLs that are not independent facts about a deployment. A callback
# path is fixed by the code that serves it; only the origin varies. So these
# are deduced from the two settings that ARE facts -- where this API answers,
# and where the front is served -- rather than asking for each of them and
# offering nine separate chances to forget one.
#
# Explicit always wins: deducing fills a blank, it never corrects anybody.
#
# ⚠️ `root_path` is deliberately absent. Measured across three deployments, it
# differs from the public URLs on all three: it is the base of the HTTP calls
# this API makes to *itself*, where going out through the public domain would
# be a detour through DNS, TLS and a proxy. Deducing it from the public URL
# would have been a plausible guess and a wrong one.
#
# `microsoft_integration_redirect_uri` is left alone too: it ships empty
# rather than localhost, so it is already the honest kind of default.
# Public URLs that are not independent facts about a deployment: a callback
# path is fixed by the page that serves it, only the origin varies. Deduced
# from `APP_PUBLIC_URL` rather than asked for one by one, which offered as
# many chances to forget one.
#
# Only settings something actually reads. `github_redirect_uri` and
# `google_redirect_uri` are absent although their names fit: nothing reads
# them -- not this core, not the commercial authentication brick, not the
# front, which computes its own callback from `window.location.origin` and
# sends it with the code exchange. Deducing a value nobody consults would
# have dressed up dead configuration as a feature.
#
# `public_base_url` is not a base here either: it names where this API
# answers, which webhooks need, and no browser-facing callback lives there.
_DERIVED_FROM_FRONT = {
    "frontend_urls": "",
    "cors_allowed_origins": "",
    "github_integration_redirect_uri": "/integrations/github/callback",
    "google_integration_redirect_uri": "/integrations/google/callback",
    "outlook_mail_redirect_uri": "/emailing/microsoft/callback",
}
_URL_BASES = (("app_public_url", _DERIVED_FROM_FRONT),)


def _usable_as_a_base(value: str) -> bool:
    """A base worth deducing from: one absolute URL, and only one.

    An environment variable that is declared but empty is the oldest trap in
    this file -- it reads as configured and behaves as nothing. Deducing from
    it produced a schemeless `/auth/callback`, and the guard stayed quiet
    because the name *was* in `model_fields_set`.

    A comma is refused: `frontend_urls` is documented as a list, and pasting
    one into a base would build a URI with a comma in the middle.

    A scheme is required and inner whitespace refused: `//host/x` and
    `https://ho st/x` are both URL-shaped enough to pass a careless check, and
    both yield a redirect_uri no OAuth provider accepts.
    """
    v = value.strip()
    if not v or "," in v or any(c.isspace() for c in v):
        return False
    scheme, sep, rest = v.partition("://")
    return bool(sep) and scheme.isalpha() and bool(rest)


def _deduced_url_names(fields_set: set[str], bases: dict[str, str]) -> set[str]:
    """Which URLs got filled in from a base rather than left at their default.

    Stateless on purpose: the answer follows from what the environment
    provided, so nothing has to be recorded and nothing can drift out of step
    with what was actually deduced.
    """
    out: set[str] = set()
    for base_name, table in _URL_BASES:
        if base_name in fields_set and _usable_as_a_base(bases.get(base_name, "")):
            out |= {name for name in table if name not in fields_set}
    return out


def _public_urls_left_behind(
    fields_set: set[str], defaults: dict[str, object]
) -> list[str]:
    """Which guarded settings were never provided, among those shipping a
    localhost default.

    Pure and fed with data rather than reading the class, for two reasons.
    The decision can then be exercised on shapes the class does not currently
    have -- in particular a guarded setting whose default is *not* localhost,
    which must never be reported, and which no test could otherwise reach
    while all nine happen to be localhost.

    And `defaults.get` rather than `[...]`: the list is a hard-coded tuple, so
    renaming one of those fields in an unrelated change would otherwise raise
    `KeyError` inside a validator that runs before anything else in the
    process -- turning a rename into a service that will not start.
    """
    return [
        name
        for name in _PUBLIC_URL_SETTINGS
        if name not in fields_set and "localhost" in str(defaults.get(name, ""))
    ]


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


def _warn_env_keys_dropped_by_the_parser(env_path: str = _ENV_FILE) -> None:
    """Name every variable the file declares that the parser does not return.

    The symmetric case of the duplicate check above, and the one that bites
    harder. When a line does not parse, python-dotenv drops the whole
    statement and logs `could not parse statement starting at line N` — one
    line, once, at boot, among hundreds. The variable is simply absent, and
    a `grep` on the file says it is present. Both readings disagree, and the
    quiet one wins.

    Production carried this for an unknown stretch (found 2026-08-07): a
    missing newline had crammed two assignments together,
    `GOOGLE_WEBHOOK_AUDIENCE="tbd"NOTIFICATION_EMAIL="..."`. The first key
    took the placeholder as its value, the second did not exist at all —
    notifications had no recipient — and nothing said so.

    Note that dotenv's own warning counts *statements*, not file lines, so
    the number it prints does not point at the offending line. This one
    reports keys, which do.

    Best-effort, like its neighbour: never raises, never blocks boot.
    """
    if not os.path.isfile(env_path):
        return

    try:
        from dotenv import dotenv_values
    except ImportError:  # pragma: no cover - dotenv ships with the settings dep
        return

    declared: dict[str, int] = {}
    source_lines: dict[int, str] = {}
    try:
        with open(env_path, encoding="utf-8") as f:
            # A value may span several lines when it is quoted -- a PEM key, a
            # JSON blob, base64 with its `=` padding. Those continuation lines
            # are not declarations, and reading them as such invented a warning
            # per line: exactly the noise that teaches operators to skip this
            # message. Track the open quote and skip until it closes.
            open_quote: str | None = None
            for lineno, raw in enumerate(f, start=1):
                line = raw.rstrip("\n")
                if open_quote is not None:
                    if open_quote in line:
                        open_quote = None
                    continue

                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export "):].lstrip()
                if "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if len(key) >= 2 and key[0] == key[-1] and key[0] in ('"', "'"):
                    key = key[1:-1]
                if not key or any(c.isspace() for c in key):
                    continue

                value = value.lstrip()
                if value[:1] in ('"', "'") and value.count(value[0]) < 2:
                    open_quote = value[0]

                declared.setdefault(key, lineno)
                source_lines[lineno] = line

        parsed = set(dotenv_values(env_path))
    except (OSError, UnicodeDecodeError) as exc:
        _logger.debug("Could not scan %s for dropped keys: %s", env_path, exc)
        return

    for key, lineno in declared.items():
        if key not in parsed:
            # The scan records the first key of a line. When two assignments
            # were crammed together, the second one is the more damaging of
            # the two -- it is absent entirely rather than merely wrong -- so
            # name it too instead of leaving it to be found by reading.
            others = [
                name
                for name in re.findall(
                    r"([A-Za-z_][A-Za-z0-9_]*)=", source_lines.get(lineno, "")
                )
                if name != key
            ]
            _logger.warning(
                "variable %r is written in %s (line %d) but the parser does "
                "not return it — the statement did not parse, so the setting "
                "is ABSENT at runtime however present it looks in the file. "
                "Check that line for a missing newline between two "
                "assignments, or an unbalanced quote.%s",
                key,
                env_path,
                lineno,
                f" The same line also assigns {', '.join(others)}." if others else "",
            )


# Champs indispensables au *runtime serveur* mais pas à l'import du paquet.
# Ils ont une valeur par défaut vide pour que ``import apowerb.<module>``
# fonctionne sans ``.env`` (th2agent doit être consommable comme library) ;
# le refus explicite se fait au boot via ``assert_runtime_ready()``.
# `test_token` is deliberately NOT here. It is read by a single middleware,
# ``apowerb/middleware/auth.py``, which is mounted nowhere -- so requiring it
# forced every deployment to invent a "test token" in order to run in
# production. Its guard already refuses an empty value, so a build without one
# rejects every request that middleware would see rather than letting them
# through. See tests/test_runtime_required_fields.py.
RUNTIME_REQUIRED_FIELDS: tuple[str, ...] = (
    "db_host",
    "db_name",
    "db_user",
    "db_password",
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
    # Mode TLS de la connexion Postgres. `require` par defaut : c'est ce que
    # le code imposait en dur, et tous les deploiements existants tournent
    # contre un Postgres gere qui l'exige. Un auto-hebergeur avec une base
    # locale sans TLS pose `DB_SSLMODE=disable` — sans ce reglage, il ne
    # pouvait tout simplement pas demarrer.
    db_sslmode: str = "require"
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

    # Monthly token cap on the shared model, per user.
    # Counts ONLY consumption paid for by thaink2: a personal API key is
    # never capped. 0 (or negative) = unlimited, which serves as a
    # kill-switch without redeploying if the guard blocks incorrectly.
    default_llm_monthly_token_quota: int = 1_000_000
    # What one purchased credit is worth in tokens on the shared model.
    # 100,000 tokens per credit puts the Starter package (10 credits, $10)
    # at the equivalent of one month of the default quota. Commercial
    # value: to be confirmed, hence the setting rather than a constant.
    credit_token_value: int = 100_000
    # Override per plan, as JSON — e.g.
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

    # ── Agent evaluation (POC) ──────────────────────────────────────
    # Set EVALUATION_ENABLED=true to allow the evaluation brick's routes
    # to run. Off by default: this is offline scaffolding (a library plus a
    # manually-run script), not a route, and never touches the agent
    # request path. The feature itself lives in the `th2agent-evaluation`
    # extension since 14/08/2026; these settings stay here because the
    # core owns the configuration surface and the brick reads it.
    evaluation_enabled: bool = False
    # Judge model for the LLM-judge evaluator (litellm model string, e.g.
    # "gemini/gemini-2.5-flash"). Must differ from the model being judged —
    # `evaluate_task_completion` refuses to run otherwise (self-preference
    # bias). No default: an unconfigured judge must fail loudly, not silently
    # score everything with whatever happens to be `default_llm_model`.
    evaluation_judge_model: str = ""
    evaluation_judge_api_key: str = ""
    # Pass/fail thresholds, one per LLM-judge evaluator -- deliberately not
    # a single shared constant: task completion, coherence, completeness
    # and hallucination are four different dimensions with no reason to
    # agree on what "good enough" means. Each defaults to the pre-existing
    # hardcoded 0.7 so behaviour is unchanged until an operator tunes one.
    evaluation_pass_threshold_task_completion: float = 0.7
    evaluation_pass_threshold_coherence: float = 0.7
    evaluation_pass_threshold_completeness: float = 0.7
    evaluation_pass_threshold_hallucination: float = 0.7
    # Minimum delay between two evaluation runs of the SAME session, to
    # absorb the "the screen looks broken, I'll click again" reflex --
    # POST /evaluations/run has no other rate limit (see routers/evaluations.py).
    evaluation_min_rerun_interval_seconds: int = 15
    # 0 disables the tool_usage resource-efficiency penalty entirely
    # (default: unchanged behaviour, the score reflects only duplicate
    # calls). Set to a positive token count to start discounting the
    # score once total_tokens / tool_calls exceeds it -- there is no
    # universal "normal" resource use across every possible agent, so
    # this is an opt-in, not a built-in guess.
    evaluation_tool_usage_tokens_per_call_soft_cap: int = 0

    # ── Notifications / system mailer ───────────────────────────────
    # All three are deployment identities and have no sensible default: a
    # hardcoded address would make every installation mail a third party.
    # Left empty, the system mailer is simply off — no alert, no 503, one
    # log line saying so.
    # Shared mailbox used as the From for all system emails.
    notification_email: str = ""
    notification_email_provider: str = "outlook"
    # Login (in DB) that owns the mailbox integration above. Its refresh_token
    # sends headless mail.
    notification_integration_owner: str = ""
    # Principal admin — recipient of ETL/system alerts.
    super_admin_email: str = ""
    # Public base URL of the front app, for verify/reset links.
    app_public_url: str = "http://localhost:3000"
    # Email-verification gate. OFF by default; never enable on SCEI until
    # the email_verified backfill is confirmed in prod.
    auth_email_verification_enabled: bool = False
    email_verify_token_expire_hours: int = 24
    # Extra recipients of the ETL alerts, on top of super_admin_email, comma
    # separated. Deployment identity: no default, or every installation would
    # mail its alerts to whoever is named here.
    etl_alert_recipients: str = ""

    # Scheduler configuration (Mage) - matches .env variable names
    base_url: str = "http://localhost:6789"
    api_key: str = "your_mage_api_key_here"
    oauth_token: str = "your_mage_oauth_token_here"
    project_name: str = "default_repo"
    mage_pipeline_uuid: str = "agents"

    # Orchestrator selection: "mage" (default, no behaviour change) or "th2etl".
    orchestrator: str = "mage"
    th2etl_base_url: str = "http://localhost:8009"
    # th2etl requires `Authorization: Bearer <key>` on its business routes and
    # compares it in constant time. Without this the client is answered 401 and
    # scheduling stops -- quietly, since the client swallows request errors.
    # Must equal the `API_KEY` of the th2etl instance being addressed.
    th2etl_api_key: str | None = None

    # Authentication settings
    frontend_urls: str = "http://localhost:3000"  # Allowed origins for CORS
    # B8 — Explicit CORS whitelist (comma-separated). When set, supersedes
    # ``frontend_urls``. Lets us restrict allowed origins independently of the
    # frontend_urls plumbing in the codebase.
    # Deployment-specific: only the local frontend is a sensible default. A
    # third-party domain here would be trusted by every installation, and
    # localhost stays allowed in production for no reason.
    cors_allowed_origins: str = "http://localhost:3000"
    # `cors_allowed_methods` / `cors_allowed_headers` were dropped when the
    # CORS layer moved to ADK (main.py), which enumerates neither -- keeping
    # them would have been two settings that read as configurable and changed
    # nothing. Deployments that still define them are unaffected: the model
    # ignores unknown env vars.

    # oauth settings
    ## Github
    github_client_id: str = "tbd"
    github_client_secret: str = "tbd"
    # ⚠️ Read by nothing: not this core, not the commercial authentication
    # brick, not the front, which computes its own callback and sends it with
    # the code exchange. The value below is inert, and its shape has never
    # matched anything served -- kept only because removing a published
    # setting is a decision of its own.
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

    # Where Microsoft sends the user back after connecting Outlook Mail, which
    # is not the integration callback above: that one lands on
    # /integrations/microsoft/<service>/callback, this one on the path below.
    # Deduced from `app_public_url` like its neighbours, and shipped on
    # localhost rather than empty so the guard can say it was forgotten.
    outlook_mail_redirect_uri: str = "http://localhost:3000/emailing/microsoft/callback"

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
    rag_webhook_secret: str = _DEFAULT_RAG_WEBHOOK_SECRET

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
    def _refuse_default_webhook_secret(self) -> "Settings":
        """Refuse to boot in production on an unset RAG webhook secret.

        ``POST /rag/webhook`` sits outside the auth middleware and trusts an
        HMAC-SHA256 signature instead, so this value is the only credential
        guarding it. The shipped placeholder is a literal in a public
        repository, and a blank value makes the HMAC computable by anyone --
        either one lets a forged call rewrite indexing status and push events
        onto a subscriber stream.

        Warning and continuing was not enough. Both machines carried
        ``RAG_WEBHOOK_SECRET="th2-webhook-default-secret"`` -- the placeholder,
        quoted -- so the key read as configured while the public default was
        live, and the warning had become part of the startup noise. A setting
        that guards an unauthenticated endpoint has to fail closed, like the
        other production refusals above.
        """
        if self.rag_webhook_secret.strip() not in {"", _DEFAULT_RAG_WEBHOOK_SECRET}:
            return self

        if self.working_mode.lower() in {"prod", "production"}:
            raise ValueError(
                "RAG_WEBHOOK_SECRET is empty or still the shipped placeholder "
                "when WORKING_MODE=production. Refusing to start -- it is the "
                "only credential checked by /rag/webhook. Generate one with: "
                "openssl rand -hex 32"
            )
        _logger.warning(
            "SECURITY: rag_webhook_secret is empty or using the default value. "
            "Set a strong, unique RAG_WEBHOOK_SECRET environment variable in production."
        )
        return self

    @model_validator(mode="after")
    def _validate_gmail_webhook_security(self) -> "Settings":
        """Refuse to boot in production without a Gmail webhook audience,
        or with the dev signature-skip flag enabled."""
        is_prod = self.working_mode.lower() in {"prod", "production"}
        audience = (self.google_webhook_audience or "").strip()
        # Emptiness was the only test, so the placeholder walked through it.
        # Production carried GOOGLE_WEBHOOK_AUDIENCE="tbd" for an unknown
        # stretch (found 2026-08-07): the guard passed, the key read as
        # configured in the file, and Gmail push notifications were being
        # verified against a value that means nothing. Same shape as the RAG
        # secret above -- a placeholder in quotes looks like configuration.
        # Case-folded on purpose: `TBD` and `Tbd` are the same non-answer as
        # `tbd`, and a guard whose subject is "a placeholder is not a
        # configuration" cannot be defeated by a capital letter.
        if is_prod and audience.lower() in {"", _UNSET_PLACEHOLDER}:
            raise ValueError(
                "GOOGLE_WEBHOOK_AUDIENCE is empty or still the placeholder "
                f"({_UNSET_PLACEHOLDER!r}) when WORKING_MODE=production. "
                "Refusing to start -- it is what Gmail Pub/Sub push "
                "notifications are verified against."
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

    @model_validator(mode="after")
    def _deduce_public_urls(self) -> "Settings":
        """Fill the callback URLs from the two origins that were declared.

        Runs before the warning below, and `object.__setattr__` rather than a
        plain assignment on purpose: `model_fields_set` must keep meaning
        "what the environment provided". Several guards read it to tell a
        default from a choice, and a deduced value silently joining that set
        would make it lie about the thing it exists to answer.
        """
        provided = self.model_fields_set
        for base_name, table in _URL_BASES:
            if base_name not in provided:
                continue
            base = str(getattr(self, base_name)).strip().rstrip("/")
            if not _usable_as_a_base(base):
                # Left alone on purpose: the guard below then names what was
                # not filled in, which is the honest outcome for a base that
                # is present but unusable.
                continue
            for name, path in table.items():
                if name not in provided:
                    object.__setattr__(self, name, f"{base}{path}")
        return self

    @model_validator(mode="after")
    def _warn_public_url_left_behind(self) -> "Settings":
        """One public URL still on its shipped localhost default, in a
        deployment that configured its neighbours.

        Found by inspecting deployed environments rather than reading this
        file. One of them ran `WORKING_MODE=prod` with its public URLs on a
        real domain and `APP_PUBLIC_URL` simply absent, so it fell back to
        localhost. That setting is what `auth/service.py` builds password-reset
        and verification links from: every such mail carried a link to the
        recipient's own machine. Nothing failed, nothing logged -- once the
        object is built, a default is indistinguishable from a choice.

        The question asked here is deliberately narrow. Not "is this
        localhost" -- localhost is the right answer on a laptop, and this
        codebase already learned that a warning firing on every boot becomes
        part of the noise (see the RAG webhook secret above). The question is
        **was this one forgotten while its neighbours were configured**, which
        `model_fields_set` answers exactly: it holds what the environment
        actually provided. An install that set none of them is somebody's
        machine; one that set six out of nine has a hole.

        A warning, not a refusal -- and the reason is measured, not timid.
        A running deployment is in this state, so refusing to boot would take
        it down at its next restart to punish a link that has been wrong for a
        while. Fail-closed is the right end state once deployments carry the
        value, and `test_public_urls.py` pins that decision so changing it is
        deliberate.
        """
        fields = type(self).model_fields
        # A deduced URL counts as provided: it now points where it should,
        # and naming it would be exactly the noise this guard exists to avoid.
        bases = {name: str(getattr(self, name, "")) for name, _ in _URL_BASES}
        deduced = _deduced_url_names(self.model_fields_set, bases)
        forgotten = _public_urls_left_behind(
            self.model_fields_set | deduced,
            {
                name: getattr(fields.get(name), "default", None)
                for name in _PUBLIC_URL_SETTINGS
            },
        )
        # Nothing configured at all is a coherent local setup, not a hole.
        if not forgotten or len(forgotten) == len(_PUBLIC_URL_SETTINGS):
            return self

        _logger.warning(
            "PUBLIC URL left at its localhost default while this deployment "
            "configured its neighbours: %s. Each one is handed to a browser, "
            "written into a mail, or dialled by this server, so it resolves "
            "somewhere other than this installation.",
            "; ".join(
                f"{name} ({_PUBLIC_URL_CONSEQUENCE.get(name, 'a public URL')})"
                for name in forgotten
            ),
        )
        return self

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
    _warn_env_keys_dropped_by_the_parser()
    return Settings()
