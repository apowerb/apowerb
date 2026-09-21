"""Nœuds http et notification (LOT 3, 21/09).

``http`` fait une requête sortante protégée SSRF (réutilise la garde de
``routers/rag/validators``), avec réponse bornée à 1 Mo et redirections
revalidées à chaque saut. ``notification`` envoie un email par destinataire
ou prévient le propriétaire du workflow en app, sous une limite de débit en
mémoire du process.

Aucun appel réseau réel : le transport httpx est remplacé par
``httpx.MockTransport`` et la résolution DNS de la garde SSRF par un faux
résolveur.
"""

from __future__ import annotations

import asyncio
import json
import socket
from unittest.mock import AsyncMock

import httpx
import pytest

from apowerb.core import workflow_graph as wg
from apowerb.core import workflow_runtime as rt
from apowerb.routers.rag import validators as rag_validators

T = {"id": "t", "type": "trigger"}


# --- Infrastructure de test : aucun réseau réel ------------------------------


@pytest.fixture(autouse=True)
def no_real_dns(monkeypatch):
    """La garde SSRF résout les noms de domaine avant toute requête : on
    intercepte ``socket.getaddrinfo`` pour qu'aucun test ne dépende d'un vrai
    DNS. Par défaut, un nom résout en IP publique ; un test peut déclarer une
    résolution privée via le dict retourné."""
    mapping: dict[str, str] = {}

    def _fake_getaddrinfo(host, *_a, **_kw):
        ip = mapping.get(host, "93.184.216.34")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(rag_validators.socket, "getaddrinfo", _fake_getaddrinfo)
    return mapping


def _patch_transport(monkeypatch, handler):
    """Route tout ``httpx.AsyncClient`` créé par le nœud http vers ``handler``."""

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


def _http_graph(cfg):
    return [T, {"id": "h", "type": "http", "config": cfg}], [
        {"source": "t", "target": "h"}
    ]


def _node_error(events, node_id="h"):
    return next(
        e for e in events if e["event"] == "node_error" and e["node_id"] == node_id
    )


def _refused(monkeypatch, url, *, never_call=True):
    """Exécute un nœud http sur ``url`` et renvoie l'événement d'erreur final."""

    def handler(request):  # pragma: no cover - ne doit jamais être atteint
        if never_call:
            raise AssertionError(f"requête réseau non attendue : {request.url}")
        return httpx.Response(200, content=b"{}")

    _patch_transport(monkeypatch, handler)
    nodes, edges = _http_graph({"method": "GET", "url": url})
    events = _run(nodes, edges)
    return events[-1]


# --- SSRF : refusé avant toute requête ---------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/meta",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/x",
        "http://10.1.2.3/internal",
    ],
)
def test_ip_literal_targets_are_refused_before_any_request(monkeypatch, url):
    final = _refused(monkeypatch, url)
    assert final["event"] == "error"
    assert final["code"] == "http_url_refused"
    assert final["params"]["node"] == "h"
    assert final["params"]["reason"]


def test_domain_resolving_to_a_private_ip_is_refused(monkeypatch, no_real_dns):
    no_real_dns["evil.example.com"] = "10.9.9.9"
    final = _refused(monkeypatch, "http://evil.example.com/x")
    assert final["code"] == "http_url_refused"


def test_redirect_to_a_private_ip_is_revalidated_and_refused(monkeypatch, no_real_dns):
    no_real_dns["public.example.com"] = "93.184.216.34"

    def handler(request):
        if request.url.host == "public.example.com":
            return httpx.Response(302, headers={"location": "http://127.0.0.1/secret"})
        raise AssertionError(
            f"la redirection n'aurait pas dû être suivie : {request.url}"
        )

    _patch_transport(monkeypatch, handler)
    nodes, edges = _http_graph(
        {"method": "GET", "url": "https://public.example.com/start"}
    )
    events = _run(nodes, edges)
    assert events[-1]["event"] == "error"
    assert events[-1]["code"] == "http_url_refused"


# --- Réponses : succès, non-bloquant, bornes ---------------------------------


def test_200_json_response_is_parsed(monkeypatch):
    def handler(request):
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=json.dumps({"ok": True, "n": 3}).encode(),
        )

    _patch_transport(monkeypatch, handler)
    nodes, edges = _http_graph({"method": "GET", "url": "https://api.example.com/ok"})
    events = _run(nodes, edges)
    assert events[-1]["event"] == "done"
    out = events[-1]["output"]
    assert out["status"] == 200
    assert out["body"] == {"ok": True, "n": 3}


def test_404_response_completes_the_run_successfully(monkeypatch):
    def handler(request):
        return httpx.Response(404, content=b"not found")

    _patch_transport(monkeypatch, handler)
    nodes, edges = _http_graph(
        {"method": "GET", "url": "https://api.example.com/missing"}
    )
    events = _run(nodes, edges)
    assert events[-1]["event"] == "done"
    assert events[-1]["output"]["status"] == 404
    assert events[-1]["output"]["body"] == "not found"


def test_timeout_is_reported_with_its_own_code(monkeypatch):
    def handler(request):
        raise httpx.ReadTimeout("boom", request=request)

    _patch_transport(monkeypatch, handler)
    nodes, edges = _http_graph(
        {"method": "GET", "url": "https://api.example.com/slow", "timeout_s": 2}
    )
    events = _run(nodes, edges)
    err = _node_error(events)
    assert err["code"] == "http_timeout"
    assert err["params"]["seconds"] == "2"


def test_response_over_1mb_is_rejected_streamed(monkeypatch):
    big = b"x" * (wg.MAX_HTTP_RESPONSE_BYTES + 100_000)

    def handler(request):
        return httpx.Response(200, content=big)

    _patch_transport(monkeypatch, handler)
    nodes, edges = _http_graph({"method": "GET", "url": "https://api.example.com/huge"})
    events = _run(nodes, edges)
    err = _node_error(events)
    assert err["code"] == "http_response_too_large"
    assert err["params"]["max"] == str(wg.MAX_HTTP_RESPONSE_BYTES)


def test_network_error_never_shows_the_raw_exception(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("connection refused to 10.0.0.7:9999 leaking topology")

    _patch_transport(monkeypatch, handler)
    nodes, edges = _http_graph({"method": "GET", "url": "https://api.example.com/down"})
    events = _run(nodes, edges)
    err = _node_error(events)
    assert err["code"] == "http_failed"
    assert "10.0.0.7" not in json.dumps(events)
    assert "leaking topology" not in json.dumps(events)


# --- Secrets : jamais dans les événements, la sortie ou les erreurs ----------


def test_no_header_value_or_secret_leaks_into_events(monkeypatch):
    seen_headers = {}

    def handler(request):
        seen_headers.update(request.headers)
        return httpx.Response(200, content=b'{"ack":true}')

    _patch_transport(monkeypatch, handler)
    nodes, edges = _http_graph(
        {
            "method": "POST",
            "url": "https://api.example.com/notify",
            "headers": [{"key": "X-Custom-Token", "value": "{{t.secret}}"}],
            "body": {"note": "{{t.secret}}"},
        }
    )
    events = _run(nodes, edges, payload={"secret": "s3cr3t-token-value"})
    # La requête sortante a bien reçu le secret (c'est le but du nœud)...
    assert seen_headers["x-custom-token"] == "s3cr3t-token-value"
    # ...mais ni le démarrage ni la sortie du nœud http (ses en-têtes/corps de
    # REQUÊTE ne sont jamais émis, contrairement à la réponse) ne le répètent.
    # Le trigger, lui, réémet légitimement le payload complet qu'on lui a
    # donné (comportement générique, pas spécifique au nœud http) : on ne
    # l'inclut pas dans ce contrôle.
    http_events = [e for e in events if e.get("node_id") in (None, "h")]
    assert "s3cr3t-token-value" not in json.dumps(http_events)
    assert events[-1]["event"] == "done"


# --- Validation : refusée avant toute exécution ------------------------------


@pytest.mark.parametrize(
    "header_key",
    [
        "Authorization",
        "authorization",
        "AUTHORIZATION",
        "Proxy-Authorization",
        "Cookie",
        "X-Api-Key",
    ],
)
def test_forbidden_auth_headers_are_refused_at_validation(header_key):
    graph = wg.WorkflowGraph.model_validate(
        {
            "version": 1,
            "nodes": [
                T,
                {
                    "id": "h",
                    "type": "http",
                    "config": {
                        "method": "GET",
                        "url": "https://api.example.com/x",
                        "headers": [{"key": header_key, "value": "secret"}],
                    },
                },
            ],
            "edges": [{"source": "t", "target": "h"}],
        }
    )
    with pytest.raises(wg.GraphError, match="clair"):
        wg.validate_graph(graph)


def test_http_auth_field_is_not_yet_supported():
    graph = wg.WorkflowGraph.model_validate(
        {
            "version": 1,
            "nodes": [
                T,
                {
                    "id": "h",
                    "type": "http",
                    "config": {
                        "method": "GET",
                        "url": "https://api.example.com/x",
                        "auth": {"integration_id": "int1"},
                    },
                },
            ],
            "edges": [{"source": "t", "target": "h"}],
        }
    )
    with pytest.raises(wg.GraphError, match="pas encore disponible"):
        wg.validate_graph(graph)


def test_unknown_http_method_is_refused():
    graph = wg.WorkflowGraph.model_validate(
        {
            "version": 1,
            "nodes": [
                T,
                {
                    "id": "h",
                    "type": "http",
                    "config": {"method": "TRACE", "url": "https://x.example.com"},
                },
            ],
            "edges": [{"source": "t", "target": "h"}],
        }
    )
    with pytest.raises(wg.GraphError, match="méthode"):
        wg.validate_graph(graph)


def test_missing_url_is_refused():
    graph = wg.WorkflowGraph.model_validate(
        {
            "version": 1,
            "nodes": [T, {"id": "h", "type": "http", "config": {"method": "GET"}}],
            "edges": [{"source": "t", "target": "h"}],
        }
    )
    with pytest.raises(wg.GraphError, match="url"):
        wg.validate_graph(graph)


def test_timeout_out_of_range_is_refused():
    graph = wg.WorkflowGraph.model_validate(
        {
            "version": 1,
            "nodes": [
                T,
                {
                    "id": "h",
                    "type": "http",
                    "config": {
                        "method": "GET",
                        "url": "https://x.example.com",
                        "timeout_s": 60,
                    },
                },
            ],
            "edges": [{"source": "t", "target": "h"}],
        }
    )
    with pytest.raises(wg.GraphError, match="timeout_s"):
        wg.validate_graph(graph)


# =============================================================================
# Notification
# =============================================================================


def test_notification_node_renders_templates_and_reports_sent_count():
    calls = []

    async def run_notify(node_id, channel, to, subject, body):
        calls.append((node_id, channel, to, subject, body))
        return len(to)

    events = _run(
        [
            T,
            {
                "id": "n",
                "type": "notification",
                "config": {
                    "channel": "email",
                    "to": ["{{t.email}}"],
                    "subject": "Bonjour {{t.name}}",
                    "body": "Corps fixe",
                },
            },
        ],
        [{"source": "t", "target": "n"}],
        payload={"email": "a@b.com", "name": "Alice"},
        run_notify=run_notify,
    )
    assert events[-1] == {"event": "done", "output": {"channel": "email", "sent": 1}}
    assert calls == [("n", "email", ["a@b.com"], "Bonjour Alice", "Corps fixe")]


@pytest.mark.parametrize(
    "cfg,match",
    [
        ({"channel": "sms", "to": [], "subject": "s", "body": "b"}, "canal"),
        ({"channel": "email", "to": [], "subject": "s", "body": "b"}, "destinataires"),
        (
            {
                "channel": "email",
                "to": [f"u{i}@x.fr" for i in range(11)],
                "subject": "s",
                "body": "b",
            },
            "destinataires",
        ),
        (
            {"channel": "email", "to": ["a@b.com"], "subject": "", "body": "b"},
            "subject",
        ),
        ({"channel": "app", "to": ["a@b.com"], "subject": "s", "body": "b"}, "app"),
    ],
)
def test_notification_validation_rules(cfg, match):
    graph = wg.WorkflowGraph.model_validate(
        {
            "version": 1,
            "nodes": [T, {"id": "n", "type": "notification", "config": cfg}],
            "edges": [{"source": "t", "target": "n"}],
        }
    )
    with pytest.raises(wg.GraphError, match=match):
        wg.validate_graph(graph)


# --- workflow_runtime._notify_for : email, app, débit, destinataire ---------


def test_email_channel_sends_to_each_recipient_and_counts_exactly(monkeypatch):
    sent = []

    async def fake_send_email(*, to, subject, body):
        sent.append((to, subject, body))

    monkeypatch.setattr("apowerb.helpers.email_sender.send_email", fake_send_email)
    run_notify = rt._notify_for("owner-email-1@x.fr")
    count = asyncio.run(
        run_notify("n1", "email", ["a@b.com", "c@d.com"], "Sujet", "Corps")
    )
    assert count == 2
    assert sent == [("a@b.com", "Sujet", "Corps"), ("c@d.com", "Sujet", "Corps")]


def test_app_channel_notifies_only_the_owner(monkeypatch):
    calls = []

    async def fake_notify(owner_email, title, message):
        calls.append((owner_email, title, message))

    monkeypatch.setattr(rt, "_notify_owner_in_app", fake_notify)
    run_notify = rt._notify_for("owner-email-2@x.fr")
    count = asyncio.run(run_notify("n1", "app", [], "Titre", "Message"))
    assert count == 1
    assert calls == [("owner-email-2@x.fr", "Titre", "Message")]


def test_rate_limit_refuses_the_whole_call_past_30_per_hour(monkeypatch):
    monkeypatch.setattr("apowerb.helpers.email_sender.send_email", AsyncMock())
    run_notify = rt._notify_for("owner-email-ratelimit@x.fr")
    ten = [f"u{i}@example.com" for i in range(10)]
    for _ in range(3):
        assert asyncio.run(run_notify("n1", "email", ten, "s", "b")) == 10
    with pytest.raises(wg.GraphError) as info:
        asyncio.run(run_notify("n1", "email", ["one-more@example.com"], "s", "b"))
    assert info.value.code == "notification_rate_limited"
    assert info.value.params["limit"] == "30"


def test_bad_recipient_after_render_is_refused_with_its_value(monkeypatch):
    monkeypatch.setattr("apowerb.helpers.email_sender.send_email", AsyncMock())
    run_notify = rt._notify_for("owner-email-badrecipient@x.fr")
    with pytest.raises(wg.GraphError) as info:
        asyncio.run(run_notify("n1", "email", ["not-an-email"], "s", "b"))
    assert info.value.code == "notification_bad_recipient"
    assert info.value.params["recipient"] == "not-an-email"
