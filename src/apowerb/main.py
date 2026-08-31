import sys
# Force UTF-8 stdout/stderr on Windows to avoid UnicodeEncodeError with emojis
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from fastapi import APIRouter
from fastapi.responses import FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
import typer
import uvicorn
import os
from jose import JWTError, jwt

from apowerb.core.invocation_context import set_current_invoker

# load th2agent routers
from apowerb.routers.tools import router as tools_router
from apowerb.routers.agents import router as agents_router
from apowerb.routers.agent_reload import router as agent_reload_router
from apowerb.routers.runs import router as runs_router
from apowerb.routers.adk_runner import router as adk_runner_router
from apowerb.routers.artifacts import router as artifacts_router
from apowerb.auth.router import router as auth_router
# Scheduler integration is optional — controlled by settings.scheduler_enabled
# (env: SCHEDULER_ENABLED). Disabled deployments (e.g. a client overlay) skip Mage entirely.
from apowerb.configs.settings import get_settings as _settings_for_sched
if _settings_for_sched().scheduler_enabled:
    from apowerb.routers.scheduler import router as scheduler_router
else:
    scheduler_router = None

from apowerb.routers.files import router as files_router
from apowerb.users.router import router as user_router
from apowerb.routers.superagents import router as superagents_router
from apowerb.routers.hub import router as hub_router
from apowerb.routers.rag import router as rag_router
from apowerb.routers.data_lake import router as data_lake_router
from apowerb.routers.config import router as config_router
from apowerb.routers.api_keys import router as api_keys_router
from apowerb.routers.emailing import router as emailing_router
from apowerb.routers.integrations import router as integrations_router
from apowerb.routers.webhooks import router as webhooks_router
from apowerb.routers.notifications import router as notifications_router
from apowerb.routers.onedrive_browser import router as onedrive_browser_router
from apowerb.routers.google_drive_browser import router as google_drive_browser_router
from apowerb.routers.share import router as share_router
from apowerb.routers.skills import router as skills_router
from apowerb.routers.audio_stream import router as audio_stream_router
from apowerb.routers.workflows import router as workflows_router
from apowerb.routers.health import router as health_router
from apowerb.helpers.integrations_migration import ensure_integrations_table
from apowerb.helpers.webhook_migration import ensure_webhook_subscriptions_table, ensure_webhook_subscriptions_columns
from apowerb.helpers.webhook_log_migration import ensure_webhook_logs_table
from apowerb.helpers.notification_migration import ensure_notifications_table
from apowerb.helpers.business_intelligence_migration import ensure_business_intelligence_table
from apowerb.helpers.share_migration import ensure_shared_conversations_columns
from apowerb.helpers.oauth_states_migration import ensure_oauth_states_table
from apowerb.helpers.security import get_algorithm, get_secret_key
from apowerb.bi.charts.router import (
    _load_charts_on_startup,
    register_exception_handlers ,
    router as charts_router
)
from apowerb.bi.dashboards.router import router as dashboards_router
from apowerb.bi.data.router import router as data_router
from apowerb.bi.stats_router import router as bi_stats_router
from apowerb.bi.data.upload_router import router as bi_upload_router
from apowerb.bi.data.dataset_router import router as bi_dataset_router
from apowerb.routers.models import router as models_router
from apowerb.bi.refresh_router import router as bi_refresh_router
from apowerb.bi.chart_refresh_router import router as bi_chart_refresh_router

from apowerb.configs.settings import get_settings
from apowerb.helpers.api_schema import hide_api_schema, publishes_api_schema
import logging as _logging
from apowerb.helpers.safe_paths import PathEscape
from apowerb.configs.paths import (
    agents_pool_dir,
    artifacts_store_dir,
    ensure_runtime_dirs,
)
from apowerb.configs.artifact_service_config import resolve_artifact_service_uri
from apowerb.artifacts.s3_artifact_service import register_s3_artifact_service
from apowerb.helpers import encryptor as _encryptor
from apowerb.helpers.user_migration import ensure_user_columns
from apowerb.helpers.core_tables import ensure_core_tables
from apowerb.helpers.default_superadmin import ensure_default_superadmin
from apowerb.admin.migration import ensure_admin_tables, ensure_superadmin_grant
from apowerb.helpers.store_migrations import ensure_store_tables
from apowerb.core.adk_agent_builder import ensure_agent_modules


_BOOTSTRAPPED = False


def bootstrap(force: bool = False) -> None:
    """Effets de bord du démarrage : validation, migrations, auto-réparation.

    Tout ceci s'exécutait auparavant au *niveau module* : importer
    ``apowerb.main`` déclenchait 10 migrations sur une vraie base et écrivait
    dans ``agents_pool/``. Un simple ``import`` valait donc un boot — et rendait
    le paquet inutilisable comme dépendance. C'est désormais branché sur le
    lifespan ASGI : l'objet ``app`` reste importable, les effets de bord ne se
    produisent qu'au démarrage réel du serveur.

    Idempotent : une seule exécution par processus, comme l'ancien code au
    niveau module (un import ne s'exécute qu'une fois).
    """
    global _BOOTSTRAPPED
    if _BOOTSTRAPPED and not force:
        return

    # La configuration doit être complète pour servir (elle ne l'est plus
    # à l'import : cf Settings.assert_runtime_ready).
    get_settings().assert_runtime_ready()

    # Dossiers de travail (agents_pool, artifacts_store, uploads).
    ensure_runtime_dirs()

    # B7 — Fail fast if no Fernet encryption key is configured. OAuth tokens
    # persisted in the `integrations` table MUST be encrypted at rest; we refuse
    # to boot rather than silently fall back to plaintext. The only escape hatch
    # is ENV=test for unit tests that stub `encryptor.fernet` themselves.
    if _encryptor.fernet is None and os.getenv("ENV", "").lower() != "test":
        print(
            "[FATAL] ENCRYPT_KEY is not configured — refusing to start. "
            "OAuth integration tokens must be Fernet-encrypted at rest.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Tables des stores (agents, tool configs, skills, hub, api keys) — leur
    # DDL tournait à l'import des modules concernés.
    ensure_store_tables()

    # Tables of the core model (user, integrations, webhooks, notifications…).
    # Must run before the ensure_* migrations below: they add columns to those
    # tables, or reference them, but none of them ever creates `user`. On an
    # existing deployment this is a no-op.
    ensure_core_tables()

    # Auto-migrate user table columns (OAuth + Billing)
    ensure_user_columns()
    ensure_integrations_table()
    ensure_webhook_subscriptions_table()
    ensure_webhook_subscriptions_columns()
    ensure_webhook_logs_table()
    ensure_notifications_table()
    ensure_business_intelligence_table()
    ensure_shared_conversations_columns()
    ensure_oauth_states_table()

    # A brand-new database has no ADMIN at all: `role` defaults to USER and no
    # route exposes it, so the first administrator used to require hand-written
    # SQL -- impossible on a hosted deployment with no database console.
    # Reads DEFAULT_SUPERADMIN_EMAIL / DEFAULT_SUPERADMIN_PASSWORD, does nothing
    # when they are unset, and runs BEFORE the brick hooks: the commercial admin
    # brick carries the same bootstrap plus its own table, and finds the account
    # already there.
    ensure_default_superadmin()

    # Le panneau d'administration : ses six tables, puis la ligne qui accorde
    # le rang de superadmin. Dans cet ordre -- la seconde ecrit dans les
    # premieres. `ensure_default_superadmin` ci-dessus pose le ROLE sur la
    # ligne utilisateur ; celle-ci pose le RANG dans `admin_superadmin`. Meme
    # variable d'environnement, deux gestes distincts, d'ou les deux noms.
    ensure_admin_tables()
    ensure_superadmin_grant()

    # Qui peut lire les sessions d'autrui. Le noyau porte desormais la notion
    # de superadmin, donc il repond lui-meme au lieu d'attendre une brique.
    # L'ecran de supervision, lui, reste commercial : il interroge cette
    # reponse depuis sa brique.
    from apowerb.core.extensions.registry import registry as _registre
    from apowerb.admin.guard import is_superadmin

    _registre.register_supervision_scope(is_superadmin)

    # Migrations apportees par les briques branchees. Ici et pas a l'import :
    # importer th2agent ne doit jamais toucher la base.
    from apowerb.core.extensions.registry import registry as _reg
    for _crochet in _reg.bootstrap_hooks():
        _crochet()

    # B7 — Optional dev/staging hook: re-encrypt plaintext integration tokens at
    # boot when ENCRYPT_LEGACY_ON_BOOT=true. No-op by default; MUST remain false
    # in production (the migration is driven explicitly via the CLI there).
    if get_settings().encrypt_legacy_on_boot:
        try:
            from apowerb.integrations.helpers import encrypt_legacy_integration_tokens
            _migrated = encrypt_legacy_integration_tokens()
            print(
                f"[WARNING] ENCRYPT_LEGACY_ON_BOOT=true — migrated {_migrated} "
                "integration row(s) to encrypted tokens."
            )
        except Exception as _exc:
            print(f"[WARNING] Legacy integration token migration failed: {_exc}")

    # Auto-repair missing agent.py files in the agents pool
    ensure_agent_modules()

    # Charts BI : lecture en base, donc au boot et pas à l'import.
    _load_charts_on_startup()

    _BOOTSTRAPPED = True

# load Google ADK FastAPI app
from google.adk.cli.fast_api import get_fast_api_app
# ApiServer, not AdkWebServer: in ADK 2.x the latter is a deprecated empty
# subclass of DevServer, and `get_fast_api_app(web=False)` -- the call below
# -- instantiates ApiServer directly. Patching the deprecated class would
# capture nothing and fail silently: the hot-reload endpoint would answer 503
# and app.state would hold None, with no error anywhere. ApiServer exposes the
# same `agent_loader` / `runner_dict` / `get_runner_async` surface.
from google.adk.cli.api_server import ApiServer as _AdkServer

# Capture the AdkWebServer instance created inside get_fast_api_app() so the
# hot-reload endpoint can reach its agent_loader + runners_to_clean set.
_ADK_HANDLES: dict = {}
_orig_adk_init = _AdkServer.__init__


def _capture_adk_init(self, *args, **kwargs):
    _orig_adk_init(self, *args, **kwargs)
    _ADK_HANDLES["web_server"] = self
    _ADK_HANDLES["agent_loader"] = self.agent_loader


_AdkServer.__init__ = _capture_adk_init

# Configure LiteLLM for OVHCloud compatibility
from apowerb.helpers.litellm_config import configure_litellm_for_ovhcloud

# Apply LiteLLM configuration for OVHCloud and providers without system message support
configure_litellm_for_ovhcloud()

settings = get_settings()

# ── ADK Auth Middleware ────────────────────────────────────────────────────────
# Google ADK's get_fast_api_app() injects /run and /run_sse directly into the
# app before our routers are added, so Depends(get_current_user) never runs on
# those paths.  This middleware closes that gap by validating the JWT for every
# request that hits those two ADK-native endpoints.
'''
ADK_PROTECTED_PATHS = {
    "/run", "/run_sse", "/run_live", "/list-apps", "/version",
    "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json",
}
'''
ADK_PROTECTED_PATHS = {
    "/run", "/run_sse", "/run_live", "/list-apps", "/version",
}
ADK_PROTECTED_PREFIXES = ("/apps/", "/builder/", "/debug/")


class ADKAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if path in ADK_PROTECTED_PATHS or any(path.startswith(p) for p in ADK_PROTECTED_PREFIXES):
            auth_header = request.headers.get("Authorization", "")
            if not auth_header.startswith("Bearer "):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Not authenticated"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            token = auth_header.split(" ", 1)[1].strip()
            # Résolue hors du try : sans ENCRYPT_KEY, on ne répond pas 401
            # (« ton jeton est mauvais »), on échoue — le serveur est cassé,
            # pas l'appelant.
            secret = get_secret_key()
            try:
                payload = jwt.decode(
                    token,
                    secret,
                    algorithms=[get_algorithm()],
                )
                # H1 — ADK-native endpoints only accept real user access
                # tokens. Long-lived agent_refresh tokens (90-day) and normal
                # refresh tokens MUST NOT be usable here; they have a
                # dedicated /run_from_refresh_token endpoint.
                if payload.get("type") != "access":
                    raise JWTError("Invalid token type")
                if payload.get("sub") is None:
                    raise JWTError("Missing identity claim")
            except JWTError:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Could not validate credentials"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            # Bind the run's identity (the token ``sub`` is always the user
            # email) for THIS request so user-personal integrations (Outlook,
            # Gmail, ...) resolve the actual invoker. This is the ONLY hook that
            # covers the ADK-native /run path used by scheduled and webhook runs
            # — they POST straight to google-adk's endpoint, bypassing the
            # /api/adk wrapper where the interactive binding lives, so without
            # this the send falls back to the racy global AGENT_OWNER and mails
            # go out from the wrong mailbox (incident 2026-07-03).
            set_current_invoker(payload["sub"])
        return await call_next(request)

api_host = "127.0.0.1"
api_port = 8000


# Emplacements des dossiers runtime, issus de la configuration (défaut :
# CWD, comportement historique). Leur *création* est faite par
# ``bootstrap()`` au démarrage : la calculer ici ne coûte rien, mais
# l'exécuter ferait de l'import de ce module une écriture disque.
agents_dir = str(agents_pool_dir())
artifacts_dir = str(artifacts_store_dir())

# Persistance des sessions : sans ``session_service_uri``, ADK retombe sur
# InMemorySessionService -> conversations perdues a CHAQUE restart (cf audit
# 2026-06-08 : ~8 restarts/j, aucun historique). On branche DatabaseSessionService
# sur la base (URL construite depuis l'env, jamais de secret hardcode) ->
# conversations persistees + auditables (indispensable pour le produit revendable).
from apowerb.helpers.database_connection import DBConfig as _DBConfig

_dbc = _DBConfig()
_SESSION_DB_URI = (
    f"postgresql+asyncpg://{_dbc.db_user}:{_dbc.db_password}"
    f"@{_dbc.db_host}:{_dbc.db_port}/{_dbc.db_name}"
)

# Artefacts : un seul stockage, sur S3, quand il est configure (dev ET prod
# le sont) ; repli sur le disque local sinon, pour qu'un deploiement sans S3
# demarre quand meme (cf apowerb.configs.artifact_service_config). ADK ne
# fournit nativement que file://, gs:// et la memoire -- le scheme "s3" est
# enregistre a la main via register_s3_artifact_service().
register_s3_artifact_service()


def _split_csv(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


_artifact_service_uri = resolve_artifact_service_uri(
    get_settings(), artifacts_dir=artifacts_dir
)

app = get_fast_api_app(
    host=api_host,
    port=api_port,
    agents_dir=agents_dir,
    web=False,  # True
    logo_text="welcome to thaink2 agentic platform",
    logo_image_url="https://raw.githubusercontent.com/thaink2/thaink2publicimages/main/thaink2_logo_circle.png",
    # memory_service_uri="rag://",  # Disabled: RAG corpus was empty
    session_service_uri=_SESSION_DB_URI,
    # Borne le pool ADK (sinon 15 conns/worker via create_async_engine) : la
    # base defaultdb est PARTAGEE (celle d'un client incluse, max 100 conns). 5/worker.
    session_db_kwargs={
        "pool_size": 3, "max_overflow": 2, "pool_timeout": 10,
        "pool_recycle": 300, "pool_pre_ping": True,
        # ADK cree ses tables NON qualifiees par un schema -> elles tombent
        # sur "public", ou le user DB n'a pas le droit CREATE (durcissement
        # PG15) => InsufficientPrivilegeError / HTTP 500 sur create_session.
        # On pose le search_path sur le schema applicatif (th2agent_dev en
        # dev, DB_SCHEMA en prod) pour qu'ADK y cree/lise ses tables.
        "connect_args": {"server_settings": {"search_path": _dbc.db_schema}},
    },
    artifact_service_uri=_artifact_service_uri,
    # B8 — the CORS whitelist lives HERE, not in a CORSMiddleware of our own.
    #
    # ADK 2.6 added `_OriginCheckMiddleware`, which rejects every
    # state-changing request (POST/PUT/PATCH/DELETE) carrying an `Origin` it
    # was not told about -- with a 403 raised *before* authentication. Our
    # front-end and our API live on separate domains by design
    # (agent-dev.thaink2.fr -> api-agent-dev.thaink2.fr), so every direct
    # browser call is cross-origin: artifact execution, chat attachments and
    # BI CSV uploads all broke the moment 2.6.2 was deployed, while GETs kept
    # working. Passing the origins here is what makes that guard aware of
    # them.
    #
    # It also means ADK installs its own CORSMiddleware. Keeping ours as well
    # would emit `Access-Control-Allow-Origin` twice, which browsers reject
    # outright -- so ours is gone, and this list is the single source of
    # truth. What we lose is the explicit method/header enumeration ADK
    # replaces with "*"; the origin whitelist, which is what actually gates
    # access, is unchanged.
    allow_origins=_split_csv(settings.cors_allowed_origins),
)
# Levier 3 : cap d'appels LLM (adapte au contexte 32k OVHcloud)
# Patche AdkWebServer.RunConfig pour injecter max_llm_calls=LLM_MAX_CALLS (defaut 25)
from apowerb.core.agent_helpers.run_config_patch import apply_adk_run_config_patch
apply_adk_run_config_patch()

# Expose the captured ADK handles on app.state so routers can reach them.
app.state.adk_web_server = _ADK_HANDLES.get("web_server")
app.state.adk_agent_loader = _ADK_HANDLES.get("agent_loader")

# The API does not publish its own route inventory unless a deployment asks
# for it. FastAPI adds /openapi.json, /docs and /redoc itself, ahead of
# ADKAuthMiddleware's path list, so the 401 that guards everything else never
# applies to them -- measured from the internet on 2026-08-06: 215 routes
# readable with no credentials on one deployment, 216 on another.
#
# Removed rather than guarded: Swagger UI cannot send a bearer token on its
# first load, so requiring one yields a broken page, not a protected one.
# That is why these paths were commented out of ADK_PROTECTED_PATHS above.
#
# Closed by default rather than keyed on WORKING_MODE: that variable lives in
# each VM's hand-written .env, the deploy workflow never sets it, and there is
# no way to read production's from outside. A guard keyed on a value we cannot
# observe is a guard that does nothing on the one host that matters. A
# deployment that wants a browsable Swagger sets PUBLISH_API_SCHEMA=true.
if not publishes_api_schema(settings):
    _hidden = hide_api_schema(app)
    # Same reason as the PathEscape handler below: this module's logger is
    # configured further down the file, well after the app is built.
    _logging.getLogger(__name__).info(
        "[SCHEMA] not published, routes removed: %s", _hidden
    )
# Câblage pur (routes + tools de l'overlay client) : doit rester à la
# construction de l'app, avant l'inclusion des routers. Aucun accès base.
from apowerb.core.extensions.loader import load_overlay  # noqa: E402
load_overlay()
register_exception_handlers(app)


# A path that resolved outside its base directory is a bad request, not a
# server fault. Without this handler `contained_path` would surface as a 500
# with a traceback, which is both the wrong status and more than the caller
# should learn. The detail is fixed text: the exception carries the offending
# components, and echoing those back tells a prober exactly what was parsed.
@app.exception_handler(PathEscape)
async def _path_escape_handler(request, exc: PathEscape):  # noqa: ANN001
    # Resolved at call time: this module's own logger is configured further
    # down the file, after this handler is registered.
    _logging.getLogger(__name__).warning(
        "[SECURITY] Path escaped its base directory on %s: %s", request.url.path, exc
    )
    return JSONResponse(status_code=400, content={"detail": "Invalid path"})
# ``bootstrap()`` (validation, migrations, auto-réparation, charts) est appelé
# depuis ``_wrapped_lifespan`` plus bas — le wrapper de lifespan que ce module
# installe déjà pour contourner celui d'ADK. Surtout pas à l'import.

# B19 — Structured logging (JSON in prod/staging, text in dev). Configured
# once at boot; all subsequent ``setup_logging(__name__)`` calls simply
# return a logger bound to this root configuration.
from apowerb.configs.th2logger import configure_structured_logging
configure_structured_logging()

# B19 — Metrics middleware (Prometheus). Added BEFORE SecurityHeaders so
# the latency histogram captures the full middleware stack cost.
from apowerb.helpers.metrics_middleware import MetricsMiddleware
from apowerb.helpers.metrics import make_metrics_asgi_app
app.add_middleware(MetricsMiddleware)

# B8 — Security headers on every response (CSP, HSTS, XFO, CT, Referrer-Policy).
# Added BEFORE CORS so its headers survive OPTIONS pre-flight responses.
from apowerb.helpers.security_headers import SecurityHeadersMiddleware
app.add_middleware(SecurityHeadersMiddleware)

# B19 — Request-ID middleware is the outermost wrapper: it must see every
# request before any other middleware logs or branches, and its header must
# end up on every response. Starlette executes middlewares in reverse
# registration order, so the LAST ``add_middleware`` call wins as outermost.
from apowerb.helpers.request_id_middleware import RequestIdMiddleware

# Protect Google ADK's native /run and /run_sse endpoints
app.add_middleware(ADKAuthMiddleware)
app.add_middleware(RequestIdMiddleware)

# B19 — Prometheus scrape endpoint (ASGI sub-app, outside /api on purpose).
app.mount("/metrics", make_metrics_asgi_app())

# Create API router
api_router = APIRouter()
api_router.include_router(auth_router, prefix="/api")
api_router.include_router(user_router, prefix="/api")
api_router.include_router(agents_router, prefix="/api")
api_router.include_router(agent_reload_router, prefix="/api")
api_router.include_router(tools_router, prefix="/api")
api_router.include_router(runs_router, prefix="/api")
api_router.include_router(adk_runner_router, prefix="/api/adk")
api_router.include_router(artifacts_router, prefix="/api")
if scheduler_router is not None:
    api_router.include_router(scheduler_router, prefix="/api")
api_router.include_router(files_router, prefix="/api")
api_router.include_router(superagents_router, prefix="/api")
api_router.include_router(hub_router, prefix="/api")
api_router.include_router(rag_router, prefix="/api")
api_router.include_router(data_lake_router, prefix="/api")
api_router.include_router(config_router, prefix="/api")
api_router.include_router(api_keys_router, prefix="/api")
api_router.include_router(integrations_router, prefix="/api")
api_router.include_router(webhooks_router, prefix="/api")
api_router.include_router(notifications_router, prefix="/api")
api_router.include_router(emailing_router, prefix="/api")
api_router.include_router(onedrive_browser_router)
api_router.include_router(google_drive_browser_router)
api_router.include_router(charts_router, prefix="/api/v1")
api_router.include_router(dashboards_router, prefix="/api/v1")
api_router.include_router(data_router, prefix="/api/v1")
api_router.include_router(bi_stats_router, prefix="/api/v1")
api_router.include_router(bi_upload_router, prefix="/api/v1")
api_router.include_router(bi_dataset_router, prefix="/api/v1")
api_router.include_router(bi_refresh_router, prefix="/api/v1")
api_router.include_router(bi_chart_refresh_router, prefix="/api/v1")
api_router.include_router(share_router, prefix="/api")
api_router.include_router(skills_router, prefix="/api")
api_router.include_router(models_router, prefix="/api")
api_router.include_router(audio_stream_router, prefix="/api")
api_router.include_router(workflows_router, prefix="/api")

# `/api/admin`: users, groups, organisations, permissions. Every route is
# admin-only, through the core's own `is_admin`, which normalises the role's
# casing -- see `apowerb/admin/guard.py`.
from apowerb.admin.router import router as admin_router

api_router.include_router(admin_router, prefix="/api")

# Routeurs apportes par les briques branchees (TH2_EXTENSIONS). Montes apres
# ceux du noyau, donc une brique ajoute des routes sans pouvoir masquer les
# siennes. Aucun nom de module tiers n'apparait ici : c'est ce qui permet de
# publier ce fichier tel quel dans le noyau open source.
from apowerb.core.extensions.registry import registry as _ext_registry  # noqa: E402
from apowerb.configs.th2logger import setup_logging as _setup_logging  # noqa: E402

_ext_logger = _setup_logging(__name__)
for _spec in _ext_registry.routers():
    api_router.include_router(_spec.router, prefix=_spec.prefix)
    _ext_logger.info("[EXT] routeur monte: %s (%s)", _spec.name or "anonyme", _spec.prefix)

# Include API router in main app
app.include_router(api_router)
# Start background webhook subscription renewal loop + backlog drain
from apowerb.scheduler.webhook_renewal import webhook_renewal_loop
from apowerb.scheduler.events_retention import events_retention_loop
from apowerb.scheduler import backlog_worker
from apowerb.scheduler.notifier_watch import notifier_watch_loop
from apowerb.routers.webhook_handlers.outlook import process_webhook_log_row


async def _start_webhook_renewal():
    import asyncio
    # Debug breadcrumbs (cf. one deployment, 2026-05-07 where this hook was
    # apparently never reaching backlog_worker.start_in_background and
    # there were no logs to triage). prints survive any logger config
    # so we can confirm in journalctl which step the hook actually
    # reached.
    print("[STARTUP HOOK] _start_webhook_renewal entered", flush=True)
    try:
        _load_charts_on_startup()
        print("[STARTUP HOOK] _load_charts_on_startup ok", flush=True)
    except Exception as e:
        print(f"[STARTUP HOOK] _load_charts_on_startup raised: {e!r}", flush=True)

    try:
        renewal_task = asyncio.create_task(webhook_renewal_loop())

        def _on_renewal_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                print(
                    f"[STARTUP HOOK] webhook_renewal_loop crashed: {exc!r}",
                    flush=True,
                )

        renewal_task.add_done_callback(_on_renewal_done)
        print("[STARTUP HOOK] webhook_renewal_loop scheduled", flush=True)
    except Exception as e:
        print(f"[STARTUP HOOK] webhook_renewal_loop schedule raised: {e!r}", flush=True)

    try:
        asyncio.create_task(notifier_watch_loop())
        print("[STARTUP HOOK] notifier_watch_loop scheduled", flush=True)
    except Exception as e:
        print(f"[STARTUP HOOK] notifier_watch_loop schedule raised: {e!r}", flush=True)

    try:
        retention_task = asyncio.create_task(events_retention_loop())

        def _on_retention_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                print(
                    f"[STARTUP HOOK] events_retention_loop crashed: {exc!r}",
                    flush=True,
                )

        retention_task.add_done_callback(_on_retention_done)
        print("[STARTUP HOOK] events_retention_loop scheduled", flush=True)
    except Exception as e:
        print(f"[STARTUP HOOK] events_retention_loop schedule raised: {e!r}", flush=True)

    try:
        # Drain webhook_logs queue one row at a time. Serialising agent
        # runs here is what keeps the Gemini per-minute input-token
        # quota from saturating when several Graph notifications land
        # in the same window.
        backlog_worker.start_in_background(process_webhook_log_row)
        print("[STARTUP HOOK] backlog_worker.start_in_background returned", flush=True)
    except Exception as e:
        print(f"[STARTUP HOOK] backlog_worker.start_in_background raised: {e!r}", flush=True)

    try:
        from apowerb.core.agent_seeds import ensure_seed_agents
        ensure_seed_agents()
        print("[STARTUP HOOK] ensure_seed_agents ok", flush=True)
    except Exception as e:
        print(f"[WARNING] Seed agent import failed (non-fatal): {e}", flush=True)

    print("[STARTUP HOOK] _start_webhook_renewal done", flush=True)


# The two legacy registrations that used to sit here -- an
# ``@app.on_event("startup")`` decorator and an ``app.add_event_handler``
# call -- are gone. Starlette 1.0 removed both APIs, and they had never
# fired anyway: ADK installs its own lifespan, Starlette honours only that,
# and the comments here already recorded it (one deployment, 2026-05-07: neither
# hook ever printed, the backlog worker never started). The lifespan wrapper
# below is the one that works, and it is now the only one.


# Final fallback: ADK builds the FastAPI app with its own lifespan
# context manager (``get_fast_api_app(...)`` wraps the app's startup
# in an ``@asynccontextmanager``). When that pattern is in use, both
# ``@app.on_event("startup")`` and ``app.add_event_handler("startup",
# ...)`` are silently *ignored* — Starlette only honours the lifespan
# context. Verified on one deployment, 2026-05-07: with both hooks above
# defined, journalctl never showed the ``[STARTUP HOOK] entered``
# print, so the worker never started and the webhook backlog stalled.
#
# Patch the lifespan in place: keep ADK's original wrapped, then run
# our startup callback after the inner lifespan has yielded — that
# matches Starlette semantics where the context body equals the
# "running" phase of the application.
import contextlib as _stdlib_contextlib

_inner_lifespan = getattr(app.router, "lifespan_context", None)


def _preflight_validate_templates() -> None:
    """Boot-time guard: reject any shipped template whose agent_instruction
    contains an unescaped ``{xxx}`` ADK placeholder that cannot be a real
    session.state variable.

    Background: one deployment, 2026-05-13 → 2026-05-18, 53 documents
    dropped to ``error`` because a client template shipped with 5 illustrative
    ``{xxx}`` examples that ADK tried to resolve at runtime. PR #172
    escaped those placeholders; this check stops the next regression
    from booting at all.
    """
    from apowerb.core.superagents.templates import SUPERAGENT_TEMPLATES
    from apowerb.core.validation.prompt_safety import assert_templates_safe

    print("[STARTUP HOOK] _preflight_validate_templates entered", flush=True)
    assert_templates_safe(SUPERAGENT_TEMPLATES)
    print("[STARTUP HOOK] _preflight_validate_templates passed", flush=True)


@_stdlib_contextlib.asynccontextmanager
async def _wrapped_lifespan(scope_app):
    print("[STARTUP HOOK via lifespan wrapper] entering", flush=True)
    # Effets de bord du boot (migrations, agents_pool, charts) : ils
    # tournaient au niveau module, ce qui faisait d'un simple import un
    # démarrage complet. Ici, ils ne partent qu'au vrai lancement.
    bootstrap()
    print("✅ Manual startup complete!", flush=True)
    _preflight_validate_templates()
    if _inner_lifespan is not None:
        try:
            async with _inner_lifespan(scope_app):
                await _start_webhook_renewal()
                yield
        except TypeError:
            # ADK's lifespan signature can be either ``(app)`` or
            # ``()`` depending on its version — try the no-arg form
            # before giving up.
            async with _inner_lifespan():
                await _start_webhook_renewal()
                yield
    else:
        await _start_webhook_renewal()
        yield
    print("[STARTUP HOOK via lifespan wrapper] exited", flush=True)


# ``app.router.lifespan_context`` is the canonical attribute Starlette
# reads on every request cycle, so swapping it is the only way to make
# our startup callback actually fire when ADK installs its own.
app.router.lifespan_context = _wrapped_lifespan
print(
    "[STARTUP MODULE] lifespan_context wrapped (had_inner="
    f"{_inner_lifespan is not None})",
    flush=True,
)


# B19 — Real health checks (live + ready). The router provides
# ``/health/live`` and ``/health/ready``; a legacy ``/health`` alias is
# preserved for external probes that were scraping the old endpoint.
app.include_router(health_router)


@app.get("/health")
async def health_legacy_alias():
    """Backward-compatible alias for the old ``/health`` endpoint.

    Prefer ``/health/live`` (fast) or ``/health/ready`` (dependency
    aware) for new probes.
    """
    return {"status": "healthy", "service": "th2agent"}

# Google domain verification (unprotected)
@app.get("/google3c3f80c61f9d38d4.html")
async def google_verification():
    return FileResponse(
        os.path.join(os.path.dirname(__file__), "static", "google3c3f80c61f9d38d4.html"),
        media_type="text/html",
    )


# CLI Application
cli_app = typer.Typer()


@cli_app.command()
def serve(
    host: str = typer.Option(
        api_host, "--host", "-h", help="Host to bind the server to"
    ),
    port: int = typer.Option(
        api_port, "--port", "-p", help="Port to bind the server to"
    ),
    reload: bool = typer.Option(
        True, "--reload/--no-reload", help="Enable auto-reload"
    ),
):
    """Start the th2agent FastAPI server."""
    typer.echo(f"Starting th2agent server on {host}:{port}")
    uvicorn.run("apowerb.main:app", host=host, port=port, reload=reload)


def main():
    """Main CLI entry point."""
    from apowerb.cli.main import app as cli_main_app

    cli_main_app()


__main__ = "apowerb.main"
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("apowerb.main:app", host=api_host, port=api_port, reload=True)
