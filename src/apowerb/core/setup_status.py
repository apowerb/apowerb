"""What this installation has configured, feature by feature -- without ever
saying what the values are.

David, 08/09/26: the interface must show which features need extra
configuration, a checklist of what is not configured yet and, for the
administrator, how to configure it; users must never meet a 4xx/5xx for a
feature that is simply not set up -- they see « not configured yet, contact
your administrator ».

Two consumers, one truth:

- ``GET /api/config/setup`` serves the list -- keys, flags, the *names* of
  the missing variables for administrators, never a value;
- ``require_configured(key)`` is what a route raises before starting an
  OAuth dance or reaching an orchestrator that cannot exist: a 503 carrying
  ``{"code": "NOT_CONFIGURED", "capability": key}`` the front maps to that
  quiet state instead of an error.

Every predicate here is the one the feature itself uses (artifact store,
orchestrator, system mailer, shared model): the checklist cannot drift from
the behaviour it describes.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from apowerb.configs.artifact_service_config import is_s3_artifact_storage_configured
from apowerb.configs.settings import get_settings
from apowerb.core.agent_helpers.default_llm import default_llm_available
from apowerb.helpers.email_sender import _smtp_configured
from apowerb.scheduler.outage import orchestrator_is_configured

NOT_CONFIGURED_CODE = "NOT_CONFIGURED"
DOCS_BASE = "https://docs.apowerb.com/configuration"


class Capability(BaseModel):
    """One line of the checklist. ``missing`` names variables, never values,
    and is only served to administrators."""

    model_config = ConfigDict(extra="forbid")

    key: str
    configured: bool
    # Free-form mode when a feature works in several ways (storage: local | s3).
    mode: Optional[str] = None
    # True when the feature works without it (storage on a local directory):
    # the line is informative, not blocking.
    optional: bool = False
    # Feature keys the front translates ("outlook_webhooks", ...).
    blocks: list[str] = []
    # Environment variable NAMES still unset. Administrators only.
    missing: list[str] = []
    docs_url: str


class SetupStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[Capability]
    # How many blocking features are not configured -- the sidebar badge.
    missing_count: int


def _blank(value: Any) -> bool:
    return not str(value or "").strip() or str(value).strip().lower() == "tbd"


def _env_blank(name: str) -> bool:
    return not (os.environ.get(name) or "").strip()


def capabilities(settings=None, env=None) -> list[Capability]:
    """The checklist, computed from the same predicates the features use."""
    s = settings or get_settings()
    e = env if env is not None else os.environ
    items: list[Capability] = []

    # -- shared model ------------------------------------------------------
    missing = [
        n
        for n, v in (
            ("DEFAULT_LLM_MODEL", s.default_llm_model),
            ("DEFAULT_LLM_API_KEY", s.default_llm_api_key),
        )
        if _blank(v)
    ]
    items.append(
        Capability(
            key="default_llm",
            configured=default_llm_available() if settings is None else not missing,
            blocks=["shared_model"],
            missing=missing,
            docs_url=f"{DOCS_BASE}/default-llm",
        )
    )

    # -- object storage: local by default, S3 when fully configured --------
    s3 = is_s3_artifact_storage_configured(s)
    s3_missing = [
        n
        for n, v in (
            ("STORAGE_MODE=S3", "S3" if s.storage_mode == "S3" else ""),
            ("S3_BUCKET_NAME", s.s3_bucket_name),
            ("S3_ACCESS_KEY", s.s3_access_key),
            ("S3_ACCESS_KEY_SECRET", s.s3_access_key_secret),
            ("S3_ENDPOINT", s.s3_endpoint),
            ("S3_REGION", s.s3_region),
        )
        if _blank(v)
    ]
    items.append(
        Capability(
            key="object_storage",
            configured=True,
            mode="s3" if s3 else "local",
            optional=True,
            blocks=[],
            missing=[] if s3 else s3_missing,
            docs_url=f"{DOCS_BASE}/storage",
        )
    )

    # -- Microsoft (Outlook integration, Outlook webhooks, emailing) -------
    missing = [
        n
        for n, v in (
            ("MICROSOFT_INTEGRATION_CLIENT_ID", s.microsoft_integration_client_id),
            (
                "MICROSOFT_INTEGRATION_CLIENT_SECRET",
                s.microsoft_integration_client_secret,
            ),
        )
        if _blank(v)
    ]
    items.append(
        Capability(
            key="microsoft_integration",
            configured=not missing,
            blocks=["outlook_integration", "outlook_webhooks", "emailing"],
            missing=missing,
            docs_url=f"{DOCS_BASE}/microsoft",
        )
    )

    # -- Google (Drive, Gmail, Calendar... integrations and webhooks) ------
    missing = [
        n
        for n, v in (
            ("GOOGLE_INTEGRATION_CLIENT_ID", s.google_integration_client_id),
            ("GOOGLE_INTEGRATION_CLIENT_SECRET", s.google_integration_client_secret),
        )
        if _blank(v)
    ]
    items.append(
        Capability(
            key="google_integration",
            configured=not missing,
            blocks=["google_integrations", "google_webhooks"],
            missing=missing
            + (
                ["GOOGLE_WEBHOOK_AUDIENCE"] if _blank(s.google_webhook_audience) else []
            ),
            docs_url=f"{DOCS_BASE}/google",
        )
    )

    # -- orchestration (th2etl) ---------------------------------------------
    configured = orchestrator_is_configured(s) and not _blank(
        getattr(s, "th2etl_api_key", None)
    )
    items.append(
        Capability(
            key="orchestration",
            configured=configured,
            blocks=["orchestrator"],
            missing=[]
            if configured
            else [
                n
                for n in ("TH2ETL_BASE_URL", "TH2ETL_API_KEY")
                if n == "TH2ETL_API_KEY"
                and _blank(getattr(s, "th2etl_api_key", None))
                or n == "TH2ETL_BASE_URL"
                and not orchestrator_is_configured(s)
            ],
            docs_url=f"{DOCS_BASE}/orchestration",
        )
    )

    # -- observability (the Logging screen) --------------------------------
    otel_missing = [
        n for n in ("OTEL_EXPORTER_OTLP_ENDPOINT",) if not (e.get(n) or "").strip()
    ]
    items.append(
        Capability(
            key="observability",
            configured=not otel_missing,
            blocks=["logging_screen"],
            missing=otel_missing,
            docs_url=f"{DOCS_BASE}/observability",
        )
    )

    # -- system mail (e-mail verification, password reset) -----------------
    smtp_missing = [
        n
        for n in ("SMTP_HOST", "SMTP_PORT", "SMTP_FROM")
        if not (e.get(n) or "").strip()
    ]
    items.append(
        Capability(
            key="system_mail",
            configured=_smtp_configured() if env is None else not smtp_missing,
            blocks=["email_verification", "password_reset"],
            missing=smtp_missing,
            docs_url=f"{DOCS_BASE}/mail",
        )
    )
    # -- signalement de bug : la sortie vers un traqueur de tickets --------
    # `optional=True` : sans dépôt configuré, le signalement fonctionne
    # entièrement — il reste dans l'écran de triage, ce qui est le mode par
    # défaut assumé. Cette ligne n'est donc pas un défaut à corriger, elle
    # existe pour qu'un administrateur DÉCOUVRE la possibilité. Sans elle,
    # on ne l'apprend qu'en cliquant sur « Créer l'issue » et en tombant
    # sur un refus.
    # `getattr` et non un accès direct : `capabilities(settings=...)` accepte
    # n'importe quel objet, et cinq suites de tests lui passent leur propre
    # doublure. Exiger d'elles un attribut apparu après leur écriture les
    # casse toutes — mesuré, le job `unit` a rougi sur cinq fichiers que
    # cette fonctionnalité ne concerne pas. Une capacité nouvelle se lit
    # avec un défaut ; c'est le prix d'un paramètre ouvert.
    bug_report_missing = [
        n
        for n, v in (
            ("BUG_REPORT_GITHUB_REPO", getattr(s, "bug_report_github_repo", "")),
            ("BUG_REPORT_GITHUB_TOKEN", getattr(s, "bug_report_github_token", "")),
        )
        if _blank(v)
    ]
    items.append(
        Capability(
            key="bug_reports",
            configured=not bug_report_missing,
            mode="github" if not bug_report_missing else "in_app",
            optional=True,
            blocks=[],
            missing=bug_report_missing,
            docs_url=f"{DOCS_BASE}/bug-reports",
        )
    )
    return items


def setup_status(*, for_admin: bool, settings=None, env=None) -> SetupStatus:
    """The checklist as served: administrators get the variable names, everyone
    else only what works and what does not."""
    items = capabilities(settings, env)
    if not for_admin:
        items = [c.model_copy(update={"missing": []}) for c in items]
    return SetupStatus(
        items=items,
        missing_count=sum(1 for c in items if not c.configured and not c.optional),
    )


def is_configured(key: str, settings=None, env=None) -> bool:
    return next(
        (c.configured for c in capabilities(settings, env) if c.key == key), False
    )


def not_configured(key: str, message: str | None = None) -> HTTPException:
    """The refusal a route raises for a feature that is not set up: a 503 the
    front recognises by its code, and turns into « not configured yet,
    contact your administrator » rather than an error."""
    return HTTPException(
        status_code=503,
        detail={
            "code": NOT_CONFIGURED_CODE,
            "capability": key,
            "message": message or f"{key} is not configured on this server.",
        },
    )


def require_configured(key: str) -> None:
    if not is_configured(key):
        raise not_configured(key)
