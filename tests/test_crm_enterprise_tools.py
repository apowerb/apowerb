"""Tests for the Salesforce and Dynamics 365 tools.

Request shapes are pinned against the contracts verified 2026-10-06 (see each
module docstring). httpx is mocked throughout — no network, no real org.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from apowerb.tools_store.portfolio import dynamics365, salesforce

GUID = "00000000-0000-0000-0000-0000000000ab"


def _resp(payload: dict | None = None, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload or {}
    resp.content = b"{}" if payload is not None else b""
    resp.raise_for_status.return_value = None
    resp.text = ""
    return resp


def _http_error(
    status_code: int, url: str = "https://example.test"
) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", url)
    response = httpx.Response(status_code, text="nope", request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


# ═══════════════════════════ Salesforce ═══════════════════════════
class TestSalesforce:
    @pytest.fixture(autouse=True)
    def _cfg(self, monkeypatch):
        monkeypatch.setenv("SALESFORCE_DOMAIN", "acme")
        monkeypatch.setenv("SALESFORCE_CLIENT_ID", "cid")
        monkeypatch.setenv("SALESFORCE_CLIENT_SECRET", "sec")
        monkeypatch.delenv("SALESFORCE_API_VERSION", raising=False)
        salesforce._token_cache.clear()

    def _token(self, instance="https://acme.my.salesforce.com"):
        return _resp(
            {"access_token": "tok", "instance_url": instance, "token_type": "Bearer"}
        )

    def test_token_request_and_query(self):
        page = {
            "totalSize": 1,
            "done": True,
            "records": [
                {"attributes": {"type": "Account"}, "Id": "001", "Name": "Acme"}
            ],
        }
        with (
            patch("httpx.post", return_value=self._token()) as post,
            patch("httpx.request", return_value=_resp(page)) as req,
        ):
            result = salesforce.tool_salesforce_query("SELECT Id, Name FROM Account")
        assert result["records"] == [{"Id": "001", "Name": "Acme"}]
        assert (
            post.call_args.args[0]
            == "https://acme.my.salesforce.com/services/oauth2/token"
        )
        assert post.call_args.kwargs["data"] == {
            "grant_type": "client_credentials",
            "client_id": "cid",
            "client_secret": "sec",
        }
        assert req.call_args.args == (
            "GET",
            "https://acme.my.salesforce.com/services/data/v62.0/query",
        )
        assert req.call_args.kwargs["params"] == {"q": "SELECT Id, Name FROM Account"}
        assert req.call_args.kwargs["headers"]["Authorization"] == "Bearer tok"

    def test_token_is_cached(self):
        with (
            patch("httpx.post", return_value=self._token()) as post,
            patch("httpx.request", return_value=_resp({"done": True, "records": []})),
        ):
            salesforce.tool_salesforce_query("SELECT Id FROM Account")
            salesforce.tool_salesforce_query("SELECT Id FROM Contact")
        assert post.call_count == 1

    def test_401_refreshes_token_once(self):
        expired = _resp({}, status_code=401)
        ok = _resp({"done": True, "records": [], "totalSize": 0})
        with (
            patch("httpx.post", return_value=self._token()) as post,
            patch("httpx.request", side_effect=[expired, ok]),
        ):
            result = salesforce.tool_salesforce_query("SELECT Id FROM Account")
        assert result["status"] == "success"
        assert post.call_count == 2

    def test_pagination_follows_next_records_url(self):
        first = {
            "totalSize": 3,
            "done": False,
            "nextRecordsUrl": "/services/data/v62.0/query/01g-2000",
            "records": [{"Id": "1"}, {"Id": "2"}],
        }
        second = {"totalSize": 3, "done": True, "records": [{"Id": "3"}]}
        with (
            patch("httpx.post", return_value=self._token()),
            patch("httpx.request", side_effect=[_resp(first), _resp(second)]) as req,
        ):
            result = salesforce.tool_salesforce_query("SELECT Id FROM Lead")
        assert [r["Id"] for r in result["records"]] == ["1", "2", "3"]
        assert result["truncated"] is False
        assert req.call_args.args[1] == (
            "https://acme.my.salesforce.com/services/data/v62.0/query/01g-2000"
        )

    def test_foreign_instance_url_refused(self):
        with (
            patch("httpx.post", return_value=self._token("https://evil.example.com")),
            patch("httpx.request") as req,
        ):
            result = salesforce.tool_salesforce_query("SELECT Id FROM Account")
        assert result["status"] == "error"
        req.assert_not_called()

    def test_domain_must_be_salesforce(self, monkeypatch):
        monkeypatch.setenv("SALESFORCE_DOMAIN", "https://evil.example.com")
        with patch("httpx.post") as post:
            result = salesforce.tool_salesforce_query("SELECT Id FROM Account")
        assert "SALESFORCE_DOMAIN" in result["error_message"]
        post.assert_not_called()

    def test_create_and_update(self):
        with (
            patch("httpx.post", return_value=self._token()),
            patch(
                "httpx.request",
                side_effect=[_resp({"id": "00Q1", "success": True}), _resp(None, 204)],
            ) as req,
        ):
            created = salesforce.tool_salesforce_create_record(
                "Lead", {"LastName": "Doe"}
            )
            updated = salesforce.tool_salesforce_update_record(
                "Lead", "00Q000000000001AAA", {"Status": "Working"}
            )
        assert created == {"status": "success", "id": "00Q1"}
        assert updated["status"] == "success"
        create_call, update_call = req.call_args_list
        assert create_call.args[1].endswith("/services/data/v62.0/sobjects/Lead")
        assert create_call.kwargs["json"] == {"LastName": "Doe"}
        assert update_call.args[0] == "PATCH"
        assert update_call.args[1].endswith("/sobjects/Lead/00Q000000000001AAA")

    @pytest.mark.parametrize(
        "call",
        [
            lambda: salesforce.tool_salesforce_create_record("Lead/../x", {"a": 1}),
            lambda: salesforce.tool_salesforce_create_record("Lead", {}),
            lambda: salesforce.tool_salesforce_update_record("Lead", "../1", {"a": 1}),
            lambda: salesforce.tool_salesforce_query("  "),
        ],
    )
    def test_invalid_input_makes_no_request(self, call):
        with patch("httpx.post") as post, patch("httpx.request") as req:
            result = call()
        assert result["status"] == "error"
        post.assert_not_called()
        req.assert_not_called()

    def test_bad_credentials_mapped(self):
        err = _http_error(400, "https://acme.my.salesforce.com/services/oauth2/token")
        with patch("httpx.post", side_effect=err):
            result = salesforce.tool_salesforce_query("SELECT Id FROM Account")
        assert result["http_status"] == 400
        assert "SALESFORCE_CLIENT_ID" in result["error_message"]


# ═══════════════════════════ Dynamics 365 ═══════════════════════════
class TestDynamics365:
    @pytest.fixture(autouse=True)
    def _cfg(self, monkeypatch):
        monkeypatch.setenv("DYNAMICS365_URL", "https://contoso.crm4.dynamics.com/")
        monkeypatch.setenv("DYNAMICS365_TENANT_ID", "tenant-1")
        monkeypatch.setenv("DYNAMICS365_CLIENT_ID", "app")
        monkeypatch.setenv("DYNAMICS365_CLIENT_SECRET", "sec")
        dynamics365._token_cache.clear()

    def _token(self):
        return _resp({"access_token": "eyJ", "expires_in": 3599})

    def test_token_and_query_shape(self):
        page = {"value": [{"@odata.etag": "W/1", "accountid": GUID, "name": "Acme"}]}
        with (
            patch("httpx.post", return_value=self._token()) as post,
            patch("httpx.request", return_value=_resp(page)) as req,
        ):
            result = dynamics365.tool_dynamics365_query(
                "accounts", select="name", filter="statecode eq 0", max_rows=5
            )
        assert result["rows"] == [{"accountid": GUID, "name": "Acme"}]
        assert result["truncated"] is False
        assert post.call_args.args[0] == (
            "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/token"
        )
        assert (
            post.call_args.kwargs["data"]["scope"]
            == "https://contoso.crm4.dynamics.com/.default"
        )
        assert req.call_args.args == (
            "GET",
            "https://contoso.crm4.dynamics.com/api/data/v9.2/accounts",
        )
        assert req.call_args.kwargs["params"] == {
            "$top": 6,
            "$select": "name",
            "$filter": "statecode eq 0",
        }
        headers = req.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer eyJ"
        assert headers["OData-Version"] == "4.0"

    def test_extra_row_means_truncated(self):
        page = {"value": [{"name": "a"}, {"name": "b"}, {"name": "c"}]}
        with (
            patch("httpx.post", return_value=self._token()),
            patch("httpx.request", return_value=_resp(page)),
        ):
            result = dynamics365.tool_dynamics365_query("accounts", max_rows=2)
        assert result["count"] == 2
        assert result["truncated"] is True

    def test_create_and_update_headers(self):
        with (
            patch("httpx.post", return_value=self._token()) as post,
            patch(
                "httpx.request",
                side_effect=[
                    _resp({"leadid": GUID, "subject": "Demo"}),
                    _resp(None, 204),
                ],
            ) as req,
        ):
            created = dynamics365.tool_dynamics365_create_record(
                "leads", {"subject": "Demo"}
            )
            updated = dynamics365.tool_dynamics365_update_record(
                "leads", GUID, {"subject": "Demo 2"}
            )
        assert created["record"]["leadid"] == GUID
        assert updated == {"status": "success", "id": GUID}
        assert post.call_count == 1  # token reused
        create_call, update_call = req.call_args_list
        assert create_call.kwargs["headers"]["Prefer"] == "return=representation"
        assert update_call.args == (
            "PATCH",
            f"https://contoso.crm4.dynamics.com/api/data/v9.2/leads({GUID})",
        )
        assert update_call.kwargs["headers"]["If-Match"] == "*"

    def test_list_tables(self):
        page = {
            "value": [
                {
                    "LogicalName": "account",
                    "EntitySetName": "accounts",
                    "IsCustomEntity": False,
                },
                {"LogicalName": "x", "EntitySetName": None, "IsCustomEntity": False},
            ]
        }
        with (
            patch("httpx.post", return_value=self._token()),
            patch("httpx.request", return_value=_resp(page)) as req,
        ):
            result = dynamics365.tool_dynamics365_list_tables()
        assert result["tables"] == [
            {"logical_name": "account", "entity_set": "accounts", "custom": False}
        ]
        assert req.call_args.args[1].endswith("/api/data/v9.2/EntityDefinitions")

    @pytest.mark.parametrize(
        "call",
        [
            lambda: dynamics365.tool_dynamics365_query("accounts?$x=1"),
            lambda: dynamics365.tool_dynamics365_update_record(
                "leads", "1 or 1", {"a": 1}
            ),
            lambda: dynamics365.tool_dynamics365_create_record("leads", {}),
        ],
    )
    def test_invalid_input_makes_no_request(self, call):
        with patch("httpx.post") as post, patch("httpx.request") as req:
            result = call()
        assert result["status"] == "error"
        post.assert_not_called()
        req.assert_not_called()

    def test_url_must_be_dynamics(self, monkeypatch):
        monkeypatch.setenv("DYNAMICS365_URL", "https://evil.example.com")
        with patch("httpx.post") as post:
            result = dynamics365.tool_dynamics365_query("accounts")
        assert "DYNAMICS365_URL" in result["error_message"]
        post.assert_not_called()

    def test_403_mapped(self):
        with (
            patch("httpx.post", return_value=self._token()),
            patch("httpx.request", side_effect=_http_error(403)),
        ):
            result = dynamics365.tool_dynamics365_query("accounts")
        assert result["http_status"] == 403
        assert "application user" in result["error_message"]


def test_tools_discovered():
    from apowerb.tools_store.tool_manager import ToolsStore

    store = ToolsStore()
    assert "salesforce.tool_salesforce_query" in store.get_tools_in_category(
        "salesforce"
    )
    assert "dynamics365.tool_dynamics365_query" in store.get_tools_in_category(
        "dynamics365"
    )
    keys = {
        p["key"]
        for p in store.get_tool_expected_params("dynamics365.tool_dynamics365_query")
    }
    assert {
        "DYNAMICS365_URL",
        "DYNAMICS365_TENANT_ID",
        "DYNAMICS365_CLIENT_SECRET",
    } <= keys


def test_auth_hint_matches_token_host_not_substring():
    # A Dataverse error whose URL merely *contains* the login host must not be
    # reported as an authentication failure (CodeQL py/incomplete-url-substring).
    url = "https://contoso.crm4.dynamics.com/api/data/v9.2/accounts?x=login.microsoftonline.com"
    result = dynamics365._error_result(_http_error(400, url))
    assert "Authentication failed" not in result["error_message"]
    login = "https://login.microsoftonline.com/tenant-1/oauth2/v2.0/token"
    assert (
        "Authentication failed"
        in dynamics365._error_result(_http_error(400, login))["error_message"]
    )
    sf = "https://acme.my.salesforce.com/services/data/v62.0/query?q=oauth2/token"
    assert (
        "SALESFORCE_CLIENT_ID"
        not in salesforce._error_result(_http_error(400, sf))["error_message"]
    )
