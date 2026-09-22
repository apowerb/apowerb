"""API de gestion : GET /api/workflows/{wid}/triggers, POST .../rotate.

Propriétaire seulement (404 pour un autre utilisateur, jamais 403) ; un kind
pas encore branché répond ``active:false, reason:"not_available"`` ; le
secret HMAC n'est montré qu'à la génération (``rotate``), jamais par ``GET``.
"""

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.pool import StaticPool

from apowerb.core import workflow_main as wm
from apowerb.core import workflow_triggers as wt

ALICE, BOB = "alice@acme.fr", "bob@other.fr"


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


def _schedule_graph():
    return {
        "version": 1,
        "nodes": [
            {
                "id": "start",
                "type": "trigger",
                "config": {
                    "kind": "schedule",
                    "cron": "0 9 * * *",
                    "timezone": "Europe/Paris",
                },
            }
        ],
        "edges": [],
    }


def _agent_tool_graph():
    # Kind T2 : validé en forme, mais pas encore exécuté -> "not_available".
    return {
        "version": 1,
        "nodes": [
            {
                "id": "start",
                "type": "trigger",
                "config": {
                    "kind": "agent_tool",
                    "tool_name": "lookup_order",
                    "description": "x",
                    "input_schema": [],
                },
            }
        ],
        "edges": [],
    }


def _user(email):
    u = MagicMock()
    u.email, u.user_id, u.role = email, 1, "USER"
    return u


@pytest.fixture()
def client(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection, _record):  # pragma: no cover
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS public")

    monkeypatch.setattr(wm.workflow_store, "engine", engine)
    wm.workflow_store.metadata.create_all(engine)
    monkeypatch.setattr(wt.workflow_trigger_store, "engine", engine)
    wt.workflow_trigger_store.metadata.create_all(engine)

    from apowerb.auth.dependencies import get_current_user
    from apowerb.routers import workflow_defs, workflow_triggers as triggers_router

    who = {"email": ALICE}
    app = FastAPI()
    app.include_router(workflow_defs.router, prefix="/api")
    app.include_router(triggers_router.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: _user(who["email"])
    c = TestClient(app)
    c.who = who
    return c


def _create(client, graph):
    r = client.post("/api/workflows/defs", json={"name": "W", "graph": graph})
    assert r.status_code == 201, r.text
    return r.json()


def _publish(client, wid, version):
    r = client.put(
        f"/api/workflows/defs/{wid}",
        json={"expected_version": version, "status": "published"},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_get_triggers_unpublished_webhook_is_inactive_with_reason(client):
    wf = _create(client, _webhook_graph())
    r = client.get(f"/api/workflows/{wf['workflow_id']}/triggers")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "webhook"
    assert body["active"] is False
    assert body["reason"] == "unpublished"
    assert body["webhook_url"] is not None  # copiable avant publication
    assert body["hmac_enabled"] is False


def test_get_triggers_published_webhook_is_active(client):
    wf = _create(client, _webhook_graph())
    _publish(client, wf["workflow_id"], wf["version"])

    r = client.get(f"/api/workflows/{wf['workflow_id']}/triggers")
    body = r.json()
    assert body["active"] is True
    assert body["reason"] is None
    assert "/api/hooks/workflows/" in body["webhook_url"]


def test_get_triggers_not_yet_branched_kind_reports_not_available(client):
    wf = _create(client, _agent_tool_graph())
    _publish(client, wf["workflow_id"], wf["version"])

    r = client.get(f"/api/workflows/{wf['workflow_id']}/triggers")
    body = r.json()
    assert body["kind"] == "agent_tool"
    assert body["active"] is False
    assert body["reason"] == "not_available"


def test_get_triggers_schedule_exposes_next_run_at(client):
    wf = _create(client, _schedule_graph())
    _publish(client, wf["workflow_id"], wf["version"])

    r = client.get(f"/api/workflows/{wf['workflow_id']}/triggers")
    body = r.json()
    assert body["kind"] == "schedule"
    assert body["active"] is True
    assert body["next_run_at"] is not None
    assert body["webhook_url"] is None


def test_other_owner_gets_404_not_403(client):
    wf = _create(client, _webhook_graph())
    client.who["email"] = BOB

    r = client.get(f"/api/workflows/{wf['workflow_id']}/triggers")
    assert r.status_code == 404
    r2 = client.post(f"/api/workflows/{wf['workflow_id']}/triggers/rotate")
    assert r2.status_code == 404


def test_unknown_workflow_is_404(client):
    r = client.get("/api/workflows/does-not-exist/triggers")
    assert r.status_code == 404


def test_rotate_invalidates_the_old_token(client):
    wf = _create(client, _webhook_graph())
    before = client.get(f"/api/workflows/{wf['workflow_id']}/triggers").json()
    old_token = before["webhook_url"].rsplit("/", 1)[1]

    r = client.post(f"/api/workflows/{wf['workflow_id']}/triggers/rotate")
    assert r.status_code == 200, r.text
    rotated = r.json()
    new_token = rotated["webhook_url"].rsplit("/", 1)[1]
    assert new_token != old_token

    # L'ancien jeton ne correspond plus à rien en base.
    assert wt.find_active_webhook_trigger(old_token) is None


def test_rotate_shows_the_hmac_secret_only_once(client):
    wf = _create(client, _webhook_graph(hmac=True))

    rotated = client.post(f"/api/workflows/{wf['workflow_id']}/triggers/rotate").json()
    assert rotated["hmac_secret"]  # montré à la régénération

    # ... mais GET ne l'expose jamais, ni avant ni après un rotate.
    status_body = client.get(f"/api/workflows/{wf['workflow_id']}/triggers").json()
    assert "hmac_secret" not in status_body


def test_rotate_on_a_kind_without_a_token_is_refused(client):
    wf = _create(client, _schedule_graph())
    r = client.post(f"/api/workflows/{wf['workflow_id']}/triggers/rotate")
    assert r.status_code == 400
