"""Tests for the Pipedrive and HubSpot CRM tools (#147).

Request shapes are pinned against the contracts verified 2026-10-05:
- Pipedrive v2, per-account host, ``x-api-token`` header, ``{success, data}``.
- HubSpot CRM v3, ``api.hubapi.com``, ``Authorization: Bearer``, ``properties``.
httpx is mocked throughout — no network, no real CRM account.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from apowerb.tools_store.portfolio import hubspot, pipedrive


def _resp(status_code: int = 200, payload: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload or {}
    resp.raise_for_status.return_value = None
    resp.text = ""
    return resp


def _http_error(status_code: int, text: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test")
    response = httpx.Response(status_code, text=text, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


# ═══════════════════════════ Pipedrive ═══════════════════════════
class TestPipedriveConfig:
    def test_missing_token(self, monkeypatch):
        monkeypatch.delenv("PIPEDRIVE_API_TOKEN", raising=False)
        monkeypatch.setenv("PIPEDRIVE_COMPANY_DOMAIN", "acme")
        with patch("httpx.request") as req:
            result = pipedrive.tool_pipedrive_create_deal(title="X")
        assert result["status"] == "error"
        assert "PIPEDRIVE_API_TOKEN" in result["error_message"]
        req.assert_not_called()

    def test_missing_domain(self, monkeypatch):
        monkeypatch.setenv("PIPEDRIVE_API_TOKEN", "tok")
        monkeypatch.delenv("PIPEDRIVE_COMPANY_DOMAIN", raising=False)
        with patch("httpx.request") as req:
            result = pipedrive.tool_pipedrive_create_deal(title="X")
        assert result["status"] == "error"
        assert "PIPEDRIVE_COMPANY_DOMAIN" in result["error_message"]
        req.assert_not_called()

    def test_domain_from_full_url_is_reduced(self, monkeypatch):
        monkeypatch.setenv("PIPEDRIVE_API_TOKEN", "tok")
        monkeypatch.setenv("PIPEDRIVE_COMPANY_DOMAIN", "https://acme.pipedrive.com")
        with patch(
            "httpx.request",
            return_value=_resp(201, {"success": True, "data": {"id": 1}}),
        ) as req:
            pipedrive.tool_pipedrive_create_deal(title="X")
        url = req.call_args.args[1]
        assert url == "https://acme.pipedrive.com/api/v2/deals"


class TestPipedriveCreate:
    @pytest.fixture(autouse=True)
    def _cfg(self, monkeypatch):
        monkeypatch.setenv("PIPEDRIVE_API_TOKEN", "tok")
        monkeypatch.setenv("PIPEDRIVE_COMPANY_DOMAIN", "acme")

    def test_create_deal_request_shape(self):
        payload = {"success": True, "data": {"id": 42, "title": "Big deal"}}
        with patch("httpx.request", return_value=_resp(201, payload)) as req:
            result = pipedrive.tool_pipedrive_create_deal(
                title="Big deal", value=1000, currency="EUR"
            )
        assert result["status"] == "success"
        assert result["id"] == 42
        method, url = req.call_args.args[0], req.call_args.args[1]
        assert method == "POST"
        assert url == "https://acme.pipedrive.com/api/v2/deals"
        assert req.call_args.kwargs["headers"]["x-api-token"] == "tok"
        assert req.call_args.kwargs["json"] == {
            "title": "Big deal",
            "value": 1000,
            "currency": "EUR",
        }

    def test_create_deal_requires_title(self):
        with patch("httpx.request") as req:
            result = pipedrive.tool_pipedrive_create_deal(title="  ")
        assert result["status"] == "error"
        assert "title" in result["error_message"]
        req.assert_not_called()

    def test_create_person_email_shape(self):
        payload = {"success": True, "data": {"id": 7, "name": "Ada"}}
        with patch("httpx.request", return_value=_resp(200, payload)) as req:
            result = pipedrive.tool_pipedrive_create_person(
                name="Ada", email="ada@x.io"
            )
        assert result["status"] == "success"
        assert result["id"] == 7
        body = req.call_args.kwargs["json"]
        assert body["name"] == "Ada"
        assert body["emails"] == [
            {"value": "ada@x.io", "primary": True, "label": "work"}
        ]

    def test_create_person_requires_name(self):
        with patch("httpx.request") as req:
            result = pipedrive.tool_pipedrive_create_person(name=None)
        assert result["status"] == "error"
        req.assert_not_called()


class TestPipedriveSearch:
    @pytest.fixture(autouse=True)
    def _cfg(self, monkeypatch):
        monkeypatch.setenv("PIPEDRIVE_API_TOKEN", "tok")
        monkeypatch.setenv("PIPEDRIVE_COMPANY_DOMAIN", "acme")

    def test_search_deals(self):
        payload = {"success": True, "data": {"items": [{"id": 1}, {"id": 2}]}}
        with patch("httpx.request", return_value=_resp(200, payload)) as req:
            result = pipedrive.tool_pipedrive_search_deals(term="acme")
        assert result["status"] == "success"
        assert result["count"] == 2
        assert req.call_args.args[1] == "https://acme.pipedrive.com/api/v2/deals/search"
        assert req.call_args.kwargs["params"]["term"] == "acme"

    def test_search_term_too_short(self):
        with patch("httpx.request") as req:
            result = pipedrive.tool_pipedrive_search_deals(term="a")
        assert result["status"] == "error"
        req.assert_not_called()

    def test_auth_error_mapped(self):
        with patch("httpx.request", side_effect=_http_error(401, "no")):
            result = pipedrive.tool_pipedrive_create_deal(title="X")
        assert result["status"] == "error"
        assert result["http_status"] == 401
        assert "Authentication failed" in result["error_message"]

    def test_success_false_envelope_is_error(self):
        # 2xx body with success=false must not be reported as success.
        payload = {"success": False, "error": "title is invalid"}
        with patch("httpx.request", return_value=_resp(200, payload)):
            result = pipedrive.tool_pipedrive_create_deal(title="X")
        assert result["status"] == "error"
        assert "title is invalid" in result["error_message"]


# ═══════════════════════════ HubSpot ═══════════════════════════
class TestHubspotMissingToken:
    def test_no_token_no_network(self, monkeypatch):
        monkeypatch.delenv("HUBSPOT_ACCESS_TOKEN", raising=False)
        with patch("httpx.request") as req:
            result = hubspot.tool_hubspot_create_contact(email="a@b.c")
        assert result["status"] == "error"
        assert "HUBSPOT_ACCESS_TOKEN" in result["error_message"]
        req.assert_not_called()


class TestHubspotCreate:
    @pytest.fixture(autouse=True)
    def _token(self, monkeypatch):
        monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "pat-123")

    def test_create_contact_shape(self):
        payload = {"id": "501", "properties": {"email": "a@b.c"}}
        with patch("httpx.request", return_value=_resp(201, payload)) as req:
            result = hubspot.tool_hubspot_create_contact(email="a@b.c", firstname="Al")
        assert result["status"] == "success"
        assert result["id"] == "501"
        method, url = req.call_args.args[0], req.call_args.args[1]
        assert method == "POST"
        assert url == "https://api.hubapi.com/crm/v3/objects/contacts"
        assert req.call_args.kwargs["headers"]["Authorization"] == "Bearer pat-123"
        assert req.call_args.kwargs["json"] == {
            "properties": {"email": "a@b.c", "firstname": "Al"}
        }

    def test_create_contact_needs_a_property(self):
        with patch("httpx.request") as req:
            result = hubspot.tool_hubspot_create_contact()
        assert result["status"] == "error"
        req.assert_not_called()

    def test_create_deal_shape(self):
        payload = {"id": "900", "properties": {"dealname": "D"}}
        with patch("httpx.request", return_value=_resp(201, payload)) as req:
            result = hubspot.tool_hubspot_create_deal(
                dealname="D", amount=5000, pipeline="default"
            )
        assert result["status"] == "success"
        assert result["id"] == "900"
        assert req.call_args.args[1] == "https://api.hubapi.com/crm/v3/objects/deals"
        assert req.call_args.kwargs["json"] == {
            "properties": {"dealname": "D", "amount": 5000, "pipeline": "default"}
        }

    def test_create_deal_requires_dealname(self):
        with patch("httpx.request") as req:
            result = hubspot.tool_hubspot_create_deal(dealname="")
        assert result["status"] == "error"
        req.assert_not_called()


class TestHubspotSearch:
    @pytest.fixture(autouse=True)
    def _token(self, monkeypatch):
        monkeypatch.setenv("HUBSPOT_ACCESS_TOKEN", "pat-123")

    def test_search_by_email_filter(self):
        payload = {
            "total": 1,
            "results": [{"id": "1", "properties": {"email": "a@b.c"}}],
        }
        with patch("httpx.request", return_value=_resp(200, payload)) as req:
            result = hubspot.tool_hubspot_search_contacts(email="a@b.c")
        assert result["status"] == "success"
        assert result["count"] == 1
        assert result["total"] == 1
        url = req.call_args.args[1]
        assert url == "https://api.hubapi.com/crm/v3/objects/contacts/search"
        body = req.call_args.kwargs["json"]
        assert body["filterGroups"][0]["filters"][0] == {
            "propertyName": "email",
            "operator": "EQ",
            "value": "a@b.c",
        }

    def test_search_by_query(self):
        with patch(
            "httpx.request", return_value=_resp(200, {"total": 0, "results": []})
        ) as req:
            result = hubspot.tool_hubspot_search_contacts(query="acme")
        assert result["status"] == "success"
        assert result["count"] == 0
        assert req.call_args.kwargs["json"]["query"] == "acme"

    def test_search_needs_query_or_email(self):
        with patch("httpx.request") as req:
            result = hubspot.tool_hubspot_search_contacts()
        assert result["status"] == "error"
        req.assert_not_called()

    def test_limit_capped(self):
        with patch(
            "httpx.request", return_value=_resp(200, {"total": 0, "results": []})
        ) as req:
            hubspot.tool_hubspot_search_contacts(query="x", limit=9999)
        assert req.call_args.kwargs["json"]["limit"] == 200

    def test_auth_error_mapped(self):
        with patch("httpx.request", side_effect=_http_error(403, "forbidden")):
            result = hubspot.tool_hubspot_create_contact(email="a@b.c")
        assert result["http_status"] == 403
        assert "Authentication failed" in result["error_message"]
