"""``form`` (T2) — kind 3/5 : formulaire public/authentifié qui lance un run.

Réutilise le jeton haché/chiffré du contrat webhook (même colonnes, même
404 opaque). Deux couches testées séparément :

* ``core.workflow_form_triggers`` — recherche du trigger par jeton et
  validation SERVEUR des valeurs soumises (pur, sans HTTP).
* ``routers.hooks`` — les deux routes publiques, ``launch_triggered_run``
  remplacé par un double (exécution de graphe hors périmètre ici).
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from apowerb.core import workflow_form_triggers as wft
from apowerb.core import workflow_main as wm
from apowerb.core import workflow_triggers as wt
from apowerb.helpers.encryptor import decrypt_value
from apowerb.routers import hooks as hooks_module

ALICE = "alice@acme.fr"


def _form_graph(access="public", fields=None):
    return {
        "version": 1,
        "nodes": [
            {
                "id": "start",
                "type": "trigger",
                "config": {
                    "kind": "form",
                    "title": "Contact",
                    "description": "Formulaire de contact",
                    "access": access,
                    "fields": fields
                    if fields is not None
                    else [
                        {
                            "name": "email",
                            "label": "Email",
                            "type": "text",
                            "required": True,
                        },
                        {
                            "name": "age",
                            "label": "Âge",
                            "type": "number",
                            "required": False,
                        },
                        {
                            "name": "newsletter",
                            "label": "Newsletter",
                            "type": "boolean",
                            "required": False,
                        },
                        {
                            "name": "plan",
                            "label": "Plan",
                            "type": "select",
                            "required": True,
                            "options": ["free", "pro"],
                        },
                    ],
                },
            }
        ],
        "edges": [],
    }


_FIELDS = _form_graph()["nodes"][0]["config"]["fields"]


# --- validate_form_values (pur) ---------------------------------------------


def test_valid_submission_returns_cleaned_values():
    ok, error, cleaned = wft.validate_form_values(
        _FIELDS, {"email": "a@b.fr", "age": 30, "newsletter": True, "plan": "pro"}
    )
    assert ok is True
    assert error is None
    assert cleaned == {"email": "a@b.fr", "age": 30, "newsletter": True, "plan": "pro"}


def test_missing_required_field_is_rejected():
    ok, error, _ = wft.validate_form_values(_FIELDS, {"plan": "pro"})
    assert ok is False
    assert "email" in error


def test_missing_optional_field_is_accepted():
    ok, _error, cleaned = wft.validate_form_values(
        _FIELDS, {"email": "a@b.fr", "plan": "free"}
    )
    assert ok is True
    assert "age" not in cleaned


def test_wrong_number_type_is_rejected():
    ok, error, _ = wft.validate_form_values(
        _FIELDS, {"email": "a@b.fr", "plan": "free", "age": "not-a-number"}
    )
    assert ok is False
    assert "age" in error


def test_select_value_outside_options_is_rejected():
    ok, error, _ = wft.validate_form_values(
        _FIELDS, {"email": "a@b.fr", "plan": "enterprise"}
    )
    assert ok is False
    assert "plan" in error


def test_unknown_extra_keys_are_dropped_not_rejected():
    ok, _error, cleaned = wft.validate_form_values(
        _FIELDS, {"email": "a@b.fr", "plan": "free", "__proto__": "x"}
    )
    assert ok is True
    assert "__proto__" not in cleaned


def test_wrong_boolean_type_is_rejected():
    ok, error, _ = wft.validate_form_values(
        _FIELDS, {"email": "a@b.fr", "plan": "free", "newsletter": "yes"}
    )
    assert ok is False
    assert "newsletter" in error


# --- HTTP : GET/POST /api/hooks/forms/{token} -------------------------------


def _sqlite_engine():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection, _record):  # pragma: no cover
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS public")

    return engine


class _Recorder:
    def __init__(self):
        self.calls = []

    async def __call__(self, *, workflow_id, owner_id, kind, detail, payload):
        self.calls.append(
            dict(
                workflow_id=workflow_id,
                owner_id=owner_id,
                kind=kind,
                detail=detail,
                payload=payload,
            )
        )
        return "run-form-1", None


@pytest.fixture()
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(hooks_module.wt, "launch_triggered_run", rec)
    return rec


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    hooks_module._form_calls.clear()
    yield
    hooks_module._form_calls.clear()


@pytest.fixture()
def app_and_trigger(monkeypatch):
    engine = _sqlite_engine()
    monkeypatch.setattr(wm.workflow_store, "engine", engine)
    wm.workflow_store.metadata.create_all(engine)
    monkeypatch.setattr(wt.workflow_trigger_store, "engine", engine)
    wt.workflow_trigger_store.metadata.create_all(engine)

    app = FastAPI()
    app.include_router(hooks_module.router, prefix="/api")
    client = TestClient(app)

    def _make(access="public", publish=True):
        wf = wm.create_workflow(
            owner_id=ALICE, name="F", graph=_form_graph(access=access)
        )
        if publish:
            wm.update_workflow(
                wf["workflow_id"],
                owner_id=ALICE,
                expected_version=wf["version"],
                status="published",
            )
        t = wt.workflow_trigger_store.trigger_table
        with wt.workflow_trigger_store.engine.begin() as conn:
            row = conn.execute(
                t.select().where(t.c.workflow_id == wf["workflow_id"])
            ).fetchone()
        d = dict(row._mapping)
        token = decrypt_value(d["token_encrypted"])
        return wf, token

    return client, _make


def test_get_form_definition_is_public_and_hides_internals(app_and_trigger):
    client, make = app_and_trigger
    _, token = make()

    r = client.get(f"/api/hooks/forms/{token}")

    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "Contact"
    assert body["access"] == "public"
    assert len(body["fields"]) == 4
    assert "owner_id" not in body
    assert "workflow_id" not in body


def test_get_form_unknown_token_is_opaque_404(app_and_trigger):
    client, _make = app_and_trigger
    r = client.get("/api/hooks/forms/not-a-real-token")
    assert r.status_code == 404


def test_get_form_unpublished_is_opaque_404(app_and_trigger):
    client, make = app_and_trigger
    _, token = make(publish=False)
    r = client.get(f"/api/hooks/forms/{token}")
    assert r.status_code == 404


def test_post_valid_submission_starts_a_run(app_and_trigger, recorder):
    client, make = app_and_trigger
    wf, token = make()

    r = client.post(
        f"/api/hooks/forms/{token}",
        json={"email": "a@b.fr", "plan": "pro"},
    )

    assert r.status_code == 202, r.text
    assert r.json() == {"run_id": "run-form-1"}
    assert recorder.calls[0]["kind"] == "form"
    assert recorder.calls[0]["payload"] == {"email": "a@b.fr", "plan": "pro"}
    assert recorder.calls[0]["workflow_id"] == wf["workflow_id"]


def test_post_invalid_submission_is_rejected_before_launching(
    app_and_trigger, recorder
):
    client, make = app_and_trigger
    _, token = make()

    r = client.post(f"/api/hooks/forms/{token}", json={"plan": "enterprise"})

    assert r.status_code == 422
    assert recorder.calls == []


def test_post_unknown_token_is_opaque_404(app_and_trigger, recorder):
    client, _make = app_and_trigger
    r = client.post("/api/hooks/forms/not-a-real-token", json={})
    assert r.status_code == 404
    assert recorder.calls == []


def test_post_authenticated_access_requires_login(app_and_trigger, recorder):
    client, make = app_and_trigger
    _, token = make(access="authenticated")

    r = client.post(
        f"/api/hooks/forms/{token}", json={"email": "a@b.fr", "plan": "pro"}
    )

    assert r.status_code == 401
    assert recorder.calls == []


def test_post_public_access_rate_limited_at_10_per_minute(app_and_trigger, recorder):
    client, make = app_and_trigger
    _, token = make(access="public")

    for _ in range(10):
        r = client.post(
            f"/api/hooks/forms/{token}", json={"email": "a@b.fr", "plan": "pro"}
        )
        assert r.status_code == 202

    r = client.post(
        f"/api/hooks/forms/{token}", json={"email": "a@b.fr", "plan": "pro"}
    )
    assert r.status_code == 429
    assert len(recorder.calls) == 10
