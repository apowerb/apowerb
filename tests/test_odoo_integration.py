"""Unit tests for the Odoo integration service and tools.

Covers:
- JSON-RPC authenticate: success, wrong creds, transport/HTTP errors
- save_integration + get_credentials round-trip (encryption in the middle)
- Tool wrappers: search_read / read / create / write happy paths and errors
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from apowerb.integrations import odoo as odoo_svc
from apowerb.tools_store.portfolio import odoo as odoo_tools


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_httpx_post(resp_status=200, resp_json=None, raise_exc=None):
    """Patch httpx.AsyncClient used inside odoo_svc._jsonrpc."""
    def _factory():
        cls_patch = patch("apowerb.integrations.odoo.httpx.AsyncClient")
        mock_cls = cls_patch.start()
        client = AsyncMock()
        if raise_exc is not None:
            client.post = AsyncMock(side_effect=raise_exc)
        else:
            resp = MagicMock(status_code=resp_status)
            resp.json.return_value = resp_json or {}
            resp.text = json.dumps(resp_json or {})
            client.post = AsyncMock(return_value=resp)
        mock_cls.return_value.__aenter__.return_value = client
        return cls_patch, client
    return _factory


def _db_with_integration(integration):
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = integration
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    return db


# ---------------------------------------------------------------------------
# authenticate()
# ---------------------------------------------------------------------------

class TestAuthenticate:
    @pytest.mark.asyncio
    async def test_success_returns_uid(self):
        cls_patch, _ = _mock_httpx_post(resp_json={"result": 42})()
        try:
            uid = await odoo_svc.authenticate(
                "https://acme.odoo.com", "acme", "admin@acme.com", "sk_key"
            )
            assert uid == 42
        finally:
            cls_patch.stop()

    @pytest.mark.asyncio
    async def test_wrong_creds_result_false_raises(self):
        # Odoo returns result=False for bad credentials (not an "error" key).
        cls_patch, _ = _mock_httpx_post(resp_json={"result": False})()
        try:
            with pytest.raises(odoo_svc.OdooConnectionError):
                await odoo_svc.authenticate("https://x.odoo.com", "db", "l", "k")
        finally:
            cls_patch.stop()

    @pytest.mark.asyncio
    async def test_odoo_error_key_raises(self):
        cls_patch, _ = _mock_httpx_post(
            resp_json={"error": {"data": {"message": "Database does not exist"}}}
        )()
        try:
            with pytest.raises(odoo_svc.OdooConnectionError, match="Database does not exist"):
                await odoo_svc.authenticate("https://x.odoo.com", "wrong_db", "l", "k")
        finally:
            cls_patch.stop()

    @pytest.mark.asyncio
    async def test_transport_error_raises(self):
        cls_patch, _ = _mock_httpx_post(raise_exc=httpx.ConnectError("host unreachable"))()
        try:
            with pytest.raises(odoo_svc.OdooConnectionError, match="Could not reach"):
                await odoo_svc.authenticate("https://x.odoo.com", "db", "l", "k")
        finally:
            cls_patch.stop()

    @pytest.mark.asyncio
    async def test_http_500_raises(self):
        cls_patch, _ = _mock_httpx_post(resp_status=500, resp_json={})()
        try:
            with pytest.raises(odoo_svc.OdooConnectionError, match="HTTP 500"):
                await odoo_svc.authenticate("https://x.odoo.com", "db", "l", "k")
        finally:
            cls_patch.stop()


# ---------------------------------------------------------------------------
# save_integration + get_credentials
# ---------------------------------------------------------------------------

class TestSaveAndGet:
    @pytest.mark.asyncio
    async def test_new_row_then_read_back_decrypted(self):
        db = _db_with_integration(None)  # no existing row

        # The Integration object db.add() receives will have a real instance;
        # we capture it to feed the subsequent get_credentials call.
        captured = {}

        def _capture(integration):
            captured["row"] = integration

        db.add = MagicMock(side_effect=_capture)

        await odoo_svc.save_integration(
            db=db, user_id=7,
            url="https://acme.odoo.com/",  # trailing slash to exercise normalization
            database="acme",
            login="admin@acme.com",
            api_key="sk_live_plain",
            uid=123,
            display_name="Admin",
        )

        row = captured["row"]
        # URL normalized, meta populated, api_key encrypted (not equal to plaintext)
        assert row.meta["url"] == "https://acme.odoo.com"
        assert row.meta["database"] == "acme"
        assert row.access_token != "sk_live_plain"
        assert row.provider_username == "admin@acme.com"
        assert row.provider_user_id == "123"

        # Now simulate get_credentials reading the same row
        db2 = _db_with_integration(row)
        creds = await odoo_svc.get_credentials(db2, user_id=7)
        assert creds == {
            "url":      "https://acme.odoo.com",
            "database": "acme",
            "login":    "admin@acme.com",
            "api_key":  "sk_live_plain",   # round-trip through encryption
            "uid":      123,
        }

    @pytest.mark.asyncio
    async def test_get_credentials_returns_none_when_absent(self):
        db = _db_with_integration(None)
        creds = await odoo_svc.get_credentials(db, user_id=99)
        assert creds is None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

class TestTools:
    def setup_method(self):
        # Clear creds cache and prime with a fake AGENT_OWNER → fake creds so
        # the tool functions don't hit the DB.
        odoo_tools.reset_odoo_creds_cache()
        odoo_tools._creds_cache["owner-1"] = {
            "url":      "https://acme.odoo.com",
            "database": "acme",
            "login":    "admin@acme.com",
            "api_key":  "sk_live_plain",
            "uid":      123,
        }

    def teardown_method(self):
        odoo_tools.reset_odoo_creds_cache()

    def _patch_post(self, resp_json=None, status_code=200, raise_exc=None):
        p = patch("apowerb.tools_store.portfolio.odoo.httpx.post")
        mock_post = p.start()
        if raise_exc is not None:
            mock_post.side_effect = raise_exc
        else:
            resp = MagicMock(status_code=status_code)
            resp.raise_for_status = MagicMock()
            resp.json.return_value = resp_json or {}
            mock_post.return_value = resp
        return p, mock_post

    def _with_owner(self, monkeypatch):
        monkeypatch.setenv("AGENT_OWNER", "owner-1")

    def test_search_records_happy_path(self, monkeypatch):
        self._with_owner(monkeypatch)
        p, mock_post = self._patch_post(resp_json={"result": [{"id": 1, "name": "ACME"}]})
        try:
            out = odoo_tools.tool_odoo_search_records(
                model="res.partner",
                domain=[("is_company", "=", True)],
                fields=["name"],
                limit=10,
            )
            assert out == {"success": True, "records": [{"id": 1, "name": "ACME"}]}

            # Inspect the JSON-RPC envelope that was sent
            call_json = mock_post.call_args.kwargs["json"]
            assert call_json["params"]["method"] == "execute_kw"
            model_call = call_json["params"]["args"]
            # args shape: [db, uid, key, model, method, args, kwargs]
            assert model_call[3] == "res.partner"
            assert model_call[4] == "search_read"
            assert model_call[5] == [[("is_company", "=", True)]]
            assert model_call[6] == {"limit": 10, "offset": 0, "fields": ["name"]}
        finally:
            p.stop()

    def test_search_limit_is_capped_at_500(self, monkeypatch):
        self._with_owner(monkeypatch)
        p, mock_post = self._patch_post(resp_json={"result": []})
        try:
            odoo_tools.tool_odoo_search_records("res.partner", limit=99999)
            kwargs = mock_post.call_args.kwargs["json"]["params"]["args"][6]
            assert kwargs["limit"] == 500
        finally:
            p.stop()

    def test_search_error_returns_error_dict(self, monkeypatch):
        self._with_owner(monkeypatch)
        p, _ = self._patch_post(resp_json={"error": {"data": {"message": "Access denied"}}})
        try:
            out = odoo_tools.tool_odoo_search_records("res.partner")
            assert out["success"] is False
            assert "Access denied" in out["error"]
        finally:
            p.stop()

    def test_create_record_returns_new_id(self, monkeypatch):
        self._with_owner(monkeypatch)
        p, mock_post = self._patch_post(resp_json={"result": 321})
        try:
            out = odoo_tools.tool_odoo_create_record(
                "res.partner", {"name": "New Co", "email": "hi@new.co"}
            )
            assert out == {"success": True, "id": 321}

            sent = mock_post.call_args.kwargs["json"]["params"]["args"]
            assert sent[3] == "res.partner"
            assert sent[4] == "create"
            assert sent[5] == [{"name": "New Co", "email": "hi@new.co"}]
        finally:
            p.stop()

    def test_update_record_true_when_write_ok(self, monkeypatch):
        self._with_owner(monkeypatch)
        p, mock_post = self._patch_post(resp_json={"result": True})
        try:
            out = odoo_tools.tool_odoo_update_record(
                "res.partner", [1, 2, 3], {"active": False}
            )
            assert out == {"success": True, "updated": True}
            sent = mock_post.call_args.kwargs["json"]["params"]["args"]
            assert sent[4] == "write"
            assert sent[5] == [[1, 2, 3], {"active": False}]
        finally:
            p.stop()

    def test_read_records_happy_path(self, monkeypatch):
        self._with_owner(monkeypatch)
        p, _ = self._patch_post(resp_json={"result": [{"id": 1, "name": "ACME"}]})
        try:
            out = odoo_tools.tool_odoo_read_records("res.partner", [1], fields=["name"])
            assert out["success"] is True
            assert out["records"] == [{"id": 1, "name": "ACME"}]
        finally:
            p.stop()

    def test_transport_error_returns_error_dict(self, monkeypatch):
        self._with_owner(monkeypatch)
        p, _ = self._patch_post(raise_exc=httpx.RequestError("boom"))
        try:
            out = odoo_tools.tool_odoo_search_records("res.partner")
            assert out["success"] is False
            assert "boom" in out["error"]
        finally:
            p.stop()
