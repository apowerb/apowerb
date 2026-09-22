"""Notification Teams — webhook entrant chiffré + envoi (LOT 3, suite, 21/09).

Complète ``tests/test_workflow_http_notification.py`` pour le canal
``teams`` :

* enregistrement / lecture / suppression du webhook (``routers/integrations.py``,
  ``Integration`` provider ``teams_webhook``, chiffré via
  ``apowerb.helpers.encryptor``) ;
* validation à l'enregistrement (https, liste blanche de suffixe EXACT, SSRF
  via ``routers/rag/validators._validate_url_not_internal``) ;
* nœud ``notification`` : ``validate_graph`` (``to`` vide, ``subject``
  requis) et ``workflow_runtime._notify_for`` (Adaptive Card POST, aucune
  redirection suivie, codes ``teams_not_configured`` / ``teams_failed``,
  débit partagé avec email et app).

Aucun réseau réel : ``httpx.MockTransport`` pour les POST, résolution DNS
simulée pour la garde SSRF réutilisée à l'enregistrement et à l'envoi.
"""

from __future__ import annotations

import asyncio
import json
import socket
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.core import workflow_graph as wg
from apowerb.core import workflow_runtime as rt
from apowerb.integrations import teams as teams_integration
from apowerb.routers.rag import validators as rag_validators

T = {"id": "t", "type": "trigger"}
VALID_URL = (
    "https://acme.webhook.office.com/webhookb2/guid%40tenant/IncomingWebhook/abc/def"
)


# --- Infrastructure de test : aucun réseau réel ------------------------------


@pytest.fixture(autouse=True)
def no_real_dns(monkeypatch):
    """Même garde SSRF que ``test_workflow_http_notification.py`` : la
    résolution DNS d'un hôte est simulée, par défaut en IP publique."""
    mapping: dict[str, str] = {}

    def _fake_getaddrinfo(host, *_a, **_kw):
        ip = mapping.get(host, "93.184.216.34")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(rag_validators.socket, "getaddrinfo", _fake_getaddrinfo)
    return mapping


def _patch_transport(monkeypatch, handler):
    """Route tout ``httpx.AsyncClient`` créé par le nœud teams vers ``handler``."""

    class _MockClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _MockClient)


def _run(nodes, edges, payload=None, run_notify=None):
    async def run_agent(agent_id, message):
        return f"out:{agent_id}"

    async def run_tool(tool, args):
        return {"tool": tool}

    async def go():
        out = []
        async for chunk in wg.run_graph(
            wg.WorkflowGraph.model_validate(
                {"version": 1, "nodes": nodes, "edges": edges}
            ),
            payload=payload,
            run_agent=run_agent,
            run_tool=run_tool,
            cancel_event=asyncio.Event(),
            run_notify=run_notify,
        ):
            out.append(json.loads(chunk[len("data: ") :]))
        return out

    return asyncio.run(go())


def _fake_url(url):
    async def f(owner_email):
        return url

    return f


# =============================================================================
# Validation du graphe : canal teams
# =============================================================================


@pytest.mark.parametrize(
    "cfg,match",
    [
        ({"channel": "teams", "to": ["a@b.com"], "subject": "s", "body": "b"}, "teams"),
        ({"channel": "teams", "to": [], "subject": "", "body": "b"}, "subject"),
    ],
)
def test_teams_notification_validation_rules(cfg, match):
    graph = wg.WorkflowGraph.model_validate(
        {
            "version": 1,
            "nodes": [T, {"id": "n", "type": "notification", "config": cfg}],
            "edges": [{"source": "t", "target": "n"}],
        }
    )
    with pytest.raises(wg.GraphError, match=match):
        wg.validate_graph(graph)


def test_teams_notification_config_is_valid_without_to():
    graph = wg.WorkflowGraph.model_validate(
        {
            "version": 1,
            "nodes": [
                T,
                {
                    "id": "n",
                    "type": "notification",
                    "config": {"channel": "teams", "subject": "s", "body": "b"},
                },
            ],
            "edges": [{"source": "t", "target": "n"}],
        }
    )
    wg.validate_graph(graph)  # ne lève pas


# =============================================================================
# workflow_runtime._notify_for : canal teams
# =============================================================================


def test_nominal_send_posts_the_exact_adaptive_card(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=b"1")

    monkeypatch.setattr(rt, "_teams_webhook_url", _fake_url(VALID_URL))
    _patch_transport(monkeypatch, handler)

    run_notify = rt._notify_for("owner-teams-1@x.fr")
    count = asyncio.run(run_notify("n1", "teams", [], "Titre", "Corps"))

    assert count == 1
    assert len(calls) == 1
    assert str(calls[0].url) == VALID_URL
    body = json.loads(calls[0].content)
    assert body == {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "type": "AdaptiveCard",
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": "Titre",
                            "weight": "Bolder",
                            "size": "Medium",
                            "wrap": True,
                        },
                        {"type": "TextBlock", "text": "Corps", "wrap": True},
                    ],
                },
            }
        ],
    }


def test_redirect_is_never_followed_and_counts_as_failure(monkeypatch):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            302, headers={"location": "https://attacker.example.com/steal"}
        )

    monkeypatch.setattr(rt, "_teams_webhook_url", _fake_url(VALID_URL))
    _patch_transport(monkeypatch, handler)

    run_notify = rt._notify_for("owner-teams-2@x.fr")
    with pytest.raises(wg.GraphError) as info:
        asyncio.run(run_notify("n1", "teams", [], "s", "b"))

    assert len(calls) == 1  # jamais suivie : une seule requête
    assert info.value.code == "teams_failed"
    assert info.value.params["status"] == 302


def test_500_response_is_reported_as_teams_failed_with_its_status(monkeypatch):
    monkeypatch.setattr(rt, "_teams_webhook_url", _fake_url(VALID_URL))
    _patch_transport(
        monkeypatch, lambda request: httpx.Response(500, content=b"server error")
    )

    run_notify = rt._notify_for("owner-teams-3@x.fr")
    with pytest.raises(wg.GraphError) as info:
        asyncio.run(run_notify("n1", "teams", [], "s", "b"))

    assert info.value.code == "teams_failed"
    assert info.value.params["status"] == 500


def test_timeout_is_reported_as_teams_failed_with_a_null_status(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("boom", request=request)

    monkeypatch.setattr(rt, "_teams_webhook_url", _fake_url(VALID_URL))
    _patch_transport(monkeypatch, handler)

    run_notify = rt._notify_for("owner-teams-4@x.fr")
    with pytest.raises(wg.GraphError) as info:
        asyncio.run(run_notify("n1", "teams", [], "s", "b"))

    assert info.value.code == "teams_failed"
    assert info.value.params["status"] is None


def test_not_configured_raises_its_own_code(monkeypatch):
    monkeypatch.setattr(rt, "_teams_webhook_url", _fake_url(None))

    run_notify = rt._notify_for("owner-teams-5@x.fr")
    with pytest.raises(wg.GraphError) as info:
        asyncio.run(run_notify("n1", "teams", [], "s", "b"))

    assert info.value.code == "teams_not_configured"
    assert info.value.params == {"node": "n1"}


def test_rate_limit_is_shared_with_email_and_app(monkeypatch):
    """30/h par PROPRIÉTAIRE, tous canaux confondus (même compteur que
    ``test_rate_limit_refuses_the_whole_call_past_30_per_hour``)."""
    monkeypatch.setattr("apowerb.helpers.email_sender.send_email", AsyncMock())
    owner = "owner-teams-ratelimit@x.fr"
    run_notify = rt._notify_for(owner)

    thirty = [f"u{i}@example.com" for i in range(30)]
    assert asyncio.run(run_notify("n1", "email", thirty, "s", "b")) == 30

    monkeypatch.setattr(rt, "_teams_webhook_url", _fake_url(VALID_URL))
    with pytest.raises(wg.GraphError) as info:
        asyncio.run(run_notify("n1", "teams", [], "s", "b"))
    assert info.value.code == "notification_rate_limited"


def test_no_secret_url_leaks_into_events_output_or_errors(monkeypatch):
    secret_url = (
        "https://acme.webhook.office.com/webhookb2/"
        "super-secret-guid/IncomingWebhook/xyz"
    )
    monkeypatch.setattr(rt, "_teams_webhook_url", _fake_url(secret_url))

    # Un seul patch de httpx.AsyncClient (indirection mutable) : re-patcher
    # deux fois dans le même test empilerait les MockTransport (le second
    # sous-classe le premier, qui réécrit "transport" par-dessus dans son
    # propre __init__) et ferait rejouer la première réponse au second appel.
    current = {"fn": lambda request: httpx.Response(200, content=b"1")}
    _patch_transport(monkeypatch, lambda request: current["fn"](request))

    run_notify = rt._notify_for("owner-teams-leak@x.fr")
    events = _run(
        [
            T,
            {
                "id": "n",
                "type": "notification",
                "config": {"channel": "teams", "subject": "Sujet", "body": "Corps"},
            },
        ],
        [{"source": "t", "target": "n"}],
        run_notify=run_notify,
    )
    assert events[-1] == {"event": "done", "output": {"channel": "teams", "sent": 1}}
    assert "webhookb2" not in json.dumps(events)
    assert "super-secret-guid" not in json.dumps(events)

    # Même contrôle côté échec (500) : ni l'URL ni son détail ne sortent.
    current["fn"] = lambda request: httpx.Response(500, content=b"x")
    events_err = _run(
        [
            T,
            {
                "id": "n",
                "type": "notification",
                "config": {"channel": "teams", "subject": "s", "body": "b"},
            },
        ],
        [{"source": "t", "target": "n"}],
        run_notify=run_notify,
    )
    assert events_err[-1]["event"] == "error"
    assert events_err[-1]["code"] == "teams_failed"
    assert "webhookb2" not in json.dumps(events_err)
    assert "super-secret-guid" not in json.dumps(events_err)


# =============================================================================
# Routes /api/integrations/teams-webhook
# =============================================================================


def _build_app(store: dict, user_id: int) -> FastAPI:
    """App minimale montant le routeur ``integrations``, DB et auth fausses."""
    from apowerb.auth.dependencies import get_current_user
    from apowerb.helpers.database import get_db
    from apowerb.routers import integrations as integrations_module

    class _FakeResult:
        def __init__(self, record):
            self._record = record

        def scalar_one_or_none(self):
            return self._record

    class _FakeSession:
        async def execute(self, stmt):
            params = stmt.compile().params
            uid = next((v for v in params.values() if isinstance(v, int)), None)
            provider = next((v for v in params.values() if isinstance(v, str)), None)
            return _FakeResult(store.get((uid, provider)))

        def add(self, obj):
            store[(obj.user_id, obj.provider)] = obj

        async def delete(self, obj):
            store.pop((obj.user_id, obj.provider), None)

        async def commit(self):
            return None

        async def refresh(self, obj):
            return None

    async def _get_db_override():
        yield _FakeSession()

    async def _current_user_override():
        u = MagicMock()
        u.user_id = user_id
        u.email = f"user{user_id}@x.fr"
        u.role = "USER"
        return u

    app = FastAPI()
    app.include_router(integrations_module.router, prefix="/api")
    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_current_user] = _current_user_override
    return app


@pytest.fixture()
def store():
    return {}


@pytest.fixture()
def client_a(store):
    return TestClient(_build_app(store, user_id=1))


@pytest.fixture()
def client_b(store):
    return TestClient(_build_app(store, user_id=2))


class TestRegistrationValidation:
    def test_http_scheme_is_refused(self, client_a):
        resp = client_a.put(
            "/api/integrations/teams-webhook",
            json={"url": "http://acme.webhook.office.com/x"},
        )
        assert resp.status_code == 422, resp.text

    def test_host_outside_the_allowlist_is_refused(self, client_a):
        resp = client_a.put(
            "/api/integrations/teams-webhook",
            json={"url": "https://example.com/x"},
        )
        assert resp.status_code == 422, resp.text

    def test_fake_suffix_is_refused(self, client_a):
        resp = client_a.put(
            "/api/integrations/teams-webhook",
            json={"url": "https://evil-webhook.office.com.attacker.com/x"},
        )
        assert resp.status_code == 422, resp.text

    def test_host_resolving_to_a_private_ip_is_refused(self, client_a, no_real_dns):
        no_real_dns["evil.webhook.office.com"] = "10.1.2.3"
        resp = client_a.put(
            "/api/integrations/teams-webhook",
            json={"url": "https://evil.webhook.office.com/x"},
        )
        assert resp.status_code == 422, resp.text

    def test_refused_registration_stores_nothing(self, client_a, store):
        client_a.put(
            "/api/integrations/teams-webhook",
            json={"url": "http://x.webhook.office.com"},
        )
        assert store == {}


class TestStorageIsEncrypted:
    def test_put_succeeds_and_the_stored_value_differs_from_the_url(
        self, client_a, store
    ):
        resp = client_a.put("/api/integrations/teams-webhook", json={"url": VALID_URL})
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"configured": True}

        row = store[(1, teams_integration.TEAMS_WEBHOOK_PROVIDER)]
        assert row.access_token != VALID_URL
        assert VALID_URL not in row.access_token

    def test_replacing_an_existing_webhook_updates_the_same_row(self, client_a, store):
        client_a.put("/api/integrations/teams-webhook", json={"url": VALID_URL})
        other_url = "https://other.webhook.office.com/webhookb2/xyz"
        client_a.put("/api/integrations/teams-webhook", json={"url": other_url})
        assert len(store) == 1
        row = store[(1, teams_integration.TEAMS_WEBHOOK_PROVIDER)]
        assert row.access_token != other_url


class TestGet:
    def test_get_without_any_configuration_is_false(self, client_a):
        resp = client_a.get("/api/integrations/teams-webhook")
        assert resp.status_code == 200
        assert resp.json() == {"configured": False}

    def test_get_never_returns_the_url(self, client_a):
        client_a.put("/api/integrations/teams-webhook", json={"url": VALID_URL})
        resp = client_a.get("/api/integrations/teams-webhook")
        assert resp.status_code == 200
        assert resp.json() == {"configured": True}
        assert VALID_URL not in resp.text
        assert "webhookb2" not in resp.text


class TestDelete:
    def test_delete_clears_the_configuration(self, client_a, store):
        client_a.put("/api/integrations/teams-webhook", json={"url": VALID_URL})
        resp = client_a.delete("/api/integrations/teams-webhook")
        assert resp.status_code == 200
        assert resp.json() == {"configured": False}
        assert (1, teams_integration.TEAMS_WEBHOOK_PROVIDER) not in store

    def test_delete_is_idempotent_when_nothing_is_configured(self, client_a):
        resp = client_a.delete("/api/integrations/teams-webhook")
        assert resp.status_code == 200
        assert resp.json() == {"configured": False}


class TestOwnership:
    def test_another_user_cannot_see_or_modify_the_webhook(
        self, client_a, client_b, store
    ):
        client_a.put("/api/integrations/teams-webhook", json={"url": VALID_URL})

        resp_get = client_b.get("/api/integrations/teams-webhook")
        assert resp_get.json() == {"configured": False}

        resp_delete = client_b.delete("/api/integrations/teams-webhook")
        assert resp_delete.json() == {"configured": False}

        # La ligne de A n'a pas bougé.
        assert (1, teams_integration.TEAMS_WEBHOOK_PROVIDER) in store
        resp_a_get = client_a.get("/api/integrations/teams-webhook")
        assert resp_a_get.json() == {"configured": True}
