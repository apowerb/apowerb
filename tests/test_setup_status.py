"""The configuration checklist: keys and flags for everyone, variable names
for administrators, never a value.

David, 08/09/26: users must never meet a 4xx/5xx for a feature that is
simply not configured; administrators see what is missing and how to fix it.
"""

from __future__ import annotations

import pytest

from apowerb.core import setup_status as ss


class _Settings:
    """Distinctive values: the tests assert none of them ever leaks."""

    default_llm_model = "gemini/secret-model-name"
    default_llm_api_key = "sk-VALUE-MUST-NOT-LEAK"
    storage_mode = "S3"
    s3_bucket_name = "bucket-VALUE"
    s3_access_key = "AKIA-VALUE"
    s3_access_key_secret = "s3secret-VALUE"
    s3_endpoint = "https://s3.example-VALUE"
    s3_region = "gra-VALUE"
    microsoft_integration_client_id = ""
    microsoft_integration_client_secret = ""
    google_integration_client_id = "tbd"
    google_integration_client_secret = "tbd"
    google_webhook_audience = ""
    orchestrator = "th2etl"
    th2etl_base_url = "http://th2etl:8000"
    th2etl_api_key = ""
    model_fields_set = {"th2etl_base_url"}


ENV = {
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel:4318",
    "SMTP_HOST": "",
    "SMTP_PORT": "",
    "SMTP_FROM": "",
}


def _by_key(items):
    return {c.key: c for c in items}


def test_every_capability_is_listed_once():
    keys = [c.key for c in ss.capabilities(_Settings(), ENV)]
    assert keys == [
        "default_llm",
        "object_storage",
        "microsoft_integration",
        "google_integration",
        "orchestration",
        "observability",
        "system_mail",
        "bug_reports",
    ]


def test_flags_follow_the_features_own_predicates():
    c = _by_key(ss.capabilities(_Settings(), ENV))
    assert c["default_llm"].configured and c["default_llm"].missing == []
    assert c["object_storage"].mode == "s3" and c["object_storage"].optional
    assert not c["microsoft_integration"].configured
    assert c["microsoft_integration"].missing == [
        "MICROSOFT_INTEGRATION_CLIENT_ID",
        "MICROSOFT_INTEGRATION_CLIENT_SECRET",
    ]
    assert not c["google_integration"].configured  # "tbd" is not a value
    assert "GOOGLE_WEBHOOK_AUDIENCE" in c["google_integration"].missing
    assert not c["orchestration"].configured and c["orchestration"].missing == [
        "TH2ETL_API_KEY"
    ]
    assert c["observability"].configured
    assert not c["system_mail"].configured and c["system_mail"].missing == [
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_FROM",
    ]


def test_local_storage_is_the_default_and_names_what_s3_would_need():
    s = _Settings()
    s.s3_endpoint = ""
    c = _by_key(ss.capabilities(s, ENV))["object_storage"]
    assert c.configured and c.mode == "local" and c.optional
    assert c.missing == ["S3_ENDPOINT"]


def test_no_value_ever_leaks_even_to_an_administrator():
    body = ss.setup_status(
        for_admin=True, settings=_Settings(), env=ENV
    ).model_dump_json()
    for value in (
        "secret-model-name",
        "MUST-NOT-LEAK",
        "bucket-VALUE",
        "AKIA-VALUE",
        "s3secret-VALUE",
        "example-VALUE",
        "gra-VALUE",
        "th2etl:8000",
        "otel:4318",
    ):
        assert value not in body


def test_users_get_flags_but_never_the_variable_names():
    admin = ss.setup_status(for_admin=True, settings=_Settings(), env=ENV)
    user = ss.setup_status(for_admin=False, settings=_Settings(), env=ENV)
    assert any(c.missing for c in admin.items)
    assert all(c.missing == [] for c in user.items)
    assert [c.configured for c in user.items] == [c.configured for c in admin.items]
    assert (
        admin.missing_count == user.missing_count == 4
    )  # microsoft, google, orchestration, mail


def test_the_schemas_are_closed():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ss.Capability(key="x", configured=True, docs_url="u", api_key="leak")
    with pytest.raises(ValidationError):
        ss.SetupStatus(items=[], missing_count=0, secrets={})


def test_not_configured_is_a_503_the_front_can_recognise():
    exc = ss.not_configured("microsoft_integration")
    assert exc.status_code == 503
    assert exc.detail["code"] == "NOT_CONFIGURED"
    assert exc.detail["capability"] == "microsoft_integration"


def test_require_configured_raises_only_when_missing(monkeypatch):
    monkeypatch.setattr(ss, "get_settings", lambda: _Settings())
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel:4318")
    ss.require_configured("observability")
    with pytest.raises(Exception) as leve:
        ss.require_configured("microsoft_integration")
    assert (
        leve.value.status_code == 503 and leve.value.detail["code"] == "NOT_CONFIGURED"
    )


# ------------------------------------------------------------- route
@pytest.fixture
def mini_app(monkeypatch):
    from fastapi import FastAPI

    from apowerb.auth import dependencies as auth_deps
    from apowerb.routers.config import router

    monkeypatch.setattr(auth_deps, "BYPASS_AUTH", False, raising=False)
    monkeypatch.setattr(ss, "get_settings", lambda: _Settings())
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return app


async def _get(app, path, user=None):
    import httpx

    from apowerb.auth.dependencies import get_current_user

    if user is not None:
        app.dependency_overrides[get_current_user] = lambda: user
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as client:
        return await client.get(path)


class _User:
    def __init__(self, role):
        self.email = "a@b.fr"
        self.role = role
        self.plan = None


async def test_the_route_refuses_anonymous_callers(mini_app):
    r = await _get(mini_app, "/api/config/setup")
    assert r.status_code in (401, 403)


async def test_an_administrator_gets_the_names_a_user_does_not(mini_app):
    admin = (await _get(mini_app, "/api/config/setup", _User("ADMIN"))).json()
    user = (await _get(mini_app, "/api/config/setup", _User("USER"))).json()
    assert any(c["missing"] for c in admin["items"])
    assert all(c["missing"] == [] for c in user["items"])
    assert admin["missing_count"] == user["missing_count"]
    assert "MUST-NOT-LEAK" not in str(admin)
