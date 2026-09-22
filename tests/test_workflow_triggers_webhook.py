"""``POST /api/hooks/workflows/{token}`` — endpoint public du contrat triggers.

``apowerb.core.workflow_triggers.launch_triggered_run`` est remplacé par un
double qui enregistre ses appels : ce fichier vérifie le ROUTEUR (jeton,
taille, débit, HMAC, forme du payload), pas l'exécution du graphe — déjà
couverte par ``test_workflow_defs_api.py`` / ``test_workflow_graph.py``, que
``launch_triggered_run`` réutilise sans les dupliquer.
"""

import hashlib
import hmac as hmac_mod
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from apowerb.core import workflow_main as wm
from apowerb.core import workflow_triggers as wt
from apowerb.helpers.encryptor import decrypt_value
from apowerb.routers import hooks as hooks_module

ALICE = "alice@acme.fr"


def _webhook_graph(hmac=False):
    return {
        "version": 1,
        "nodes": [
            {
                "id": "start",
                "type": "trigger",
                "config": {"kind": "webhook", "hmac": hmac},
            }
        ],
        "edges": [],
    }


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
        return "run-fake-1", None


@pytest.fixture()
def recorder(monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(hooks_module.wt, "launch_triggered_run", rec)
    return rec


@pytest.fixture(autouse=True)
def _reset_rate_limit():
    hooks_module._calls.clear()
    yield
    hooks_module._calls.clear()


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

    def _make(hmac=False, publish=True):
        wf = wm.create_workflow(
            owner_id=ALICE, name="W", graph=_webhook_graph(hmac=hmac)
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
        secret = (
            decrypt_value(d["hmac_secret_encrypted"])
            if d.get("hmac_secret_encrypted")
            else None
        )
        return wf, token, secret

    return client, _make


def test_object_body_is_passed_through_as_payload(app_and_trigger, recorder):
    client, make = app_and_trigger
    wf, token, _ = make()

    r = client.post(f"/api/hooks/workflows/{token}", json={"a": 1, "b": "x"})

    assert r.status_code == 202, r.text
    assert r.json() == {"run_id": "run-fake-1"}
    assert recorder.calls[0]["payload"] == {"a": 1, "b": "x"}
    assert recorder.calls[0]["kind"] == "webhook"
    assert recorder.calls[0]["workflow_id"] == wf["workflow_id"]
    assert recorder.calls[0]["owner_id"] == ALICE


def test_non_object_body_is_wrapped_in_body_key(app_and_trigger, recorder):
    client, make = app_and_trigger
    _, token, _ = make()

    r = client.post(f"/api/hooks/workflows/{token}", json=[1, 2, 3])

    assert r.status_code == 202
    assert recorder.calls[0]["payload"] == {"body": [1, 2, 3]}


def test_unknown_token_is_opaque_404(app_and_trigger, recorder):
    client, _make = app_and_trigger
    r = client.post("/api/hooks/workflows/not-a-real-token", json={})
    assert r.status_code == 404
    assert recorder.calls == []


def test_unpublished_workflow_is_opaque_404(app_and_trigger, recorder):
    client, make = app_and_trigger
    _, token, _ = make(publish=False)

    r = client.post(f"/api/hooks/workflows/{token}", json={})

    assert r.status_code == 404
    assert recorder.calls == []


def test_hmac_missing_header_is_refused(app_and_trigger, recorder):
    client, make = app_and_trigger
    _, token, secret = make(hmac=True)

    r = client.post(f"/api/hooks/workflows/{token}", json={"x": 1})

    assert r.status_code == 401
    assert recorder.calls == []


def test_hmac_wrong_signature_is_refused(app_and_trigger, recorder):
    client, make = app_and_trigger
    _, token, secret = make(hmac=True)

    r = client.post(
        f"/api/hooks/workflows/{token}",
        content=json.dumps({"x": 1}),
        headers={
            "X-Apowerb-Signature": "sha256=" + "0" * 64,
            "Content-Type": "application/json",
        },
    )

    assert r.status_code == 401
    assert recorder.calls == []


def test_hmac_correct_signature_is_accepted(app_and_trigger, recorder):
    client, make = app_and_trigger
    _, token, secret = make(hmac=True)

    body = json.dumps({"x": 1}).encode()
    sig = hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()

    r = client.post(
        f"/api/hooks/workflows/{token}",
        content=body,
        headers={
            "X-Apowerb-Signature": f"sha256={sig}",
            "Content-Type": "application/json",
        },
    )

    assert r.status_code == 202, r.text
    assert recorder.calls[0]["payload"] == {"x": 1}


def test_body_over_the_size_limit_is_413(app_and_trigger, recorder):
    client, make = app_and_trigger
    _, token, _ = make()

    too_big = json.dumps({"blob": "x" * (hooks_module.MAX_BODY_BYTES + 100)})

    r = client.post(
        f"/api/hooks/workflows/{token}",
        content=too_big,
        headers={"Content-Type": "application/json"},
    )

    assert r.status_code == 413
    assert recorder.calls == []


def test_rate_limit_60_per_minute_then_429(app_and_trigger, recorder):
    client, make = app_and_trigger
    _, token, _ = make()

    for _ in range(hooks_module.RATE_LIMIT_PER_MINUTE):
        r = client.post(f"/api/hooks/workflows/{token}", json={})
        assert r.status_code == 202

    r = client.post(f"/api/hooks/workflows/{token}", json={})
    assert r.status_code == 429
    assert len(recorder.calls) == hooks_module.RATE_LIMIT_PER_MINUTE


def test_no_plaintext_token_ever_stored_in_the_trigger_row(app_and_trigger):
    _, make = app_and_trigger
    _, token, _ = make()

    t = wt.workflow_trigger_store.trigger_table
    with wt.workflow_trigger_store.engine.begin() as conn:
        row = conn.execute(t.select()).fetchone()
    d = dict(row._mapping)

    assert d["token_hash"] == hashlib.sha256(token.encode()).hexdigest()
    assert d["token_hash"] != token
    assert d["token_encrypted"] != token  # chiffré, pas la valeur brute
    assert (
        decrypt_value(d["token_encrypted"]) == token
    )  # mais réversible pour GET .../triggers


# --- Revue 22/09 : les signatures invalides n'épuisent pas le débit légitime --


def _signed(secret, body):
    return "sha256=" + hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_bad_signatures_do_not_consume_the_legitimate_rate_limit(
    app_and_trigger, recorder
):
    client, make = app_and_trigger
    _, token, secret = make(hmac=True)
    body = json.dumps({"x": 1}).encode()
    bad = {"X-Apowerb-Signature": "sha256=" + "0" * 64}

    codes = {
        client.post(
            f"/api/hooks/workflows/{token}", content=body, headers=bad
        ).status_code
        for _ in range(hooks_module.RATE_LIMIT_PER_MINUTE + 5)
    }
    assert codes <= {401, 429}

    r = client.post(
        f"/api/hooks/workflows/{token}",
        content=body,
        headers={
            "X-Apowerb-Signature": _signed(secret, body),
            "Content-Type": "application/json",
        },
    )
    assert r.status_code == 202
    assert len(recorder.calls) == 1


def test_valid_signed_calls_are_still_rate_limited(app_and_trigger, recorder):
    client, make = app_and_trigger
    _, token, secret = make(hmac=True)
    body = json.dumps({"x": 1}).encode()
    headers = {"X-Apowerb-Signature": _signed(secret, body)}

    for _ in range(hooks_module.RATE_LIMIT_PER_MINUTE):
        r = client.post(f"/api/hooks/workflows/{token}", content=body, headers=headers)
        assert r.status_code == 202
    r = client.post(f"/api/hooks/workflows/{token}", content=body, headers=headers)
    assert r.status_code == 429
