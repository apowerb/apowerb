"""Tests for the Datadog logger tool (#136).

The request shape is pinned against the verified contract: POST to
https://http-intake.logs.<site>/api/v2/logs, header DD-API-KEY, JSON body with a
required ``message`` field, success = 202. httpx is mocked throughout — no
network, no real Datadog account.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from apowerb.tools_store.portfolio.datadog import (
    _SITE_HOSTS,
    _normalise_site,
    _normalise_tags,
    tool_send_datadog_log,
)


def _resp(status_code: int = 202, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    return resp


# ── Site resolution ────────────────────────────────────────────────────────
class TestSite:
    def test_known_region_codes(self):
        assert _normalise_site("us1") == "http-intake.logs.datadoghq.com"
        assert _normalise_site("eu") == "http-intake.logs.datadoghq.eu"
        assert _normalise_site("us3") == "http-intake.logs.us3.datadoghq.com"

    def test_case_insensitive(self):
        assert _normalise_site("US5") == _SITE_HOSTS["us5"]

    def test_full_domain_accepted(self):
        assert _normalise_site("datadoghq.eu") == "http-intake.logs.datadoghq.eu"

    def test_unknown_site_raises(self):
        with pytest.raises(ValueError, match="Unknown Datadog site"):
            _normalise_site("mars1")

    def test_loose_suffix_rejected(self):
        # "com" must not silently match a *.datadoghq.com host.
        with pytest.raises(ValueError, match="Unknown Datadog site"):
            _normalise_site("com")


# ── Tag normalisation ──────────────────────────────────────────────────────
class TestTags:
    def test_list_joined_with_comma(self):
        assert _normalise_tags(["env:prod", "team:bi"]) == "env:prod,team:bi"

    def test_string_passthrough(self):
        assert _normalise_tags("env:prod,team:bi") == "env:prod,team:bi"

    def test_none_is_none(self):
        assert _normalise_tags(None) is None

    def test_empty_list_is_none(self):
        assert _normalise_tags([]) is None

    def test_blank_entries_dropped(self):
        assert _normalise_tags(["env:prod", "  ", ""]) == "env:prod"

    def test_bad_type_raises(self):
        with pytest.raises(ValueError, match="ddtags"):
            _normalise_tags(42)


# ── Missing key: error and NO network ──────────────────────────────────────
class TestMissingKey:
    def test_no_key_no_network(self, monkeypatch):
        monkeypatch.delenv("DD_API_KEY", raising=False)
        with patch("httpx.post") as mock_post:
            result = tool_send_datadog_log("hello")
        assert result["status"] == "error"
        assert "DD_API_KEY" in result["error_message"]
        mock_post.assert_not_called()


# ── Validation ─────────────────────────────────────────────────────────────
class TestValidation:
    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch):
        monkeypatch.setenv("DD_API_KEY", "fake-key")

    def test_empty_message(self):
        with patch("httpx.post") as mock_post:
            result = tool_send_datadog_log("   ")
        assert result["status"] == "error"
        assert "message" in result["error_message"]
        mock_post.assert_not_called()

    def test_no_message_no_messages(self):
        with patch("httpx.post") as mock_post:
            result = tool_send_datadog_log()
        assert result["status"] == "error"
        mock_post.assert_not_called()

    def test_too_many_entries(self):
        with patch("httpx.post") as mock_post:
            result = tool_send_datadog_log(messages=["x"] * 1001)
        assert result["status"] == "error"
        assert "at most" in result["error_message"]
        mock_post.assert_not_called()

    def test_unknown_site(self):
        with patch("httpx.post") as mock_post:
            result = tool_send_datadog_log("hi", site="pluto")
        assert result["status"] == "error"
        assert "Unknown Datadog site" in result["error_message"]
        mock_post.assert_not_called()

    def test_bad_messages_type(self):
        with patch("httpx.post") as mock_post:
            result = tool_send_datadog_log(messages="not-a-list")
        assert result["status"] == "error"
        mock_post.assert_not_called()


# ── Happy path: request building + response ────────────────────────────────
class TestHappyPath:
    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch):
        monkeypatch.setenv("DD_API_KEY", "fake-key")
        monkeypatch.delenv("DD_SITE", raising=False)

    def test_single_log_success(self):
        with patch("httpx.post", return_value=_resp(202)) as mock_post:
            result = tool_send_datadog_log(
                "something happened",
                service="agent-runtime",
                ddtags=["env:prod"],
                status="info",
            )
        assert result["status"] == "success"
        assert result["entries_sent"] == 1
        assert result["http_status"] == 202
        assert result["site"] == "http-intake.logs.datadoghq.com"

        args, kwargs = mock_post.call_args
        assert args[0] == "https://http-intake.logs.datadoghq.com/api/v2/logs"
        assert kwargs["headers"]["DD-API-KEY"] == "fake-key"
        assert kwargs["headers"]["Content-Type"] == "application/json"
        # Single entry → single object, not an array.
        body = json.loads(kwargs["content"])
        assert isinstance(body, dict)
        assert body["message"] == "something happened"
        assert body["service"] == "agent-runtime"
        assert body["ddtags"] == "env:prod"
        assert body["ddsource"] == "apowerb"
        assert body["status"] == "info"

    def test_multiple_logs_send_array(self):
        with patch("httpx.post", return_value=_resp(202)) as mock_post:
            result = tool_send_datadog_log(messages=["a", "b", "c"])
        assert result["status"] == "success"
        assert result["entries_sent"] == 3
        body = json.loads(mock_post.call_args.kwargs["content"])
        assert isinstance(body, list)
        assert [e["message"] for e in body] == ["a", "b", "c"]

    def test_message_and_messages_combine(self):
        with patch("httpx.post", return_value=_resp(202)) as mock_post:
            result = tool_send_datadog_log("first", messages=["second"])
        assert result["entries_sent"] == 2
        body = json.loads(mock_post.call_args.kwargs["content"])
        assert [e["message"] for e in body] == ["first", "second"]

    def test_optional_fields_omitted(self):
        with patch("httpx.post", return_value=_resp(202)) as mock_post:
            tool_send_datadog_log("bare")
        body = json.loads(mock_post.call_args.kwargs["content"])
        assert body == {"message": "bare", "ddsource": "apowerb"}

    def test_site_override(self):
        with patch("httpx.post", return_value=_resp(202)) as mock_post:
            tool_send_datadog_log("hi", site="eu")
        assert mock_post.call_args.args[0].startswith(
            "https://http-intake.logs.datadoghq.eu/"
        )

    def test_dd_site_env_default(self, monkeypatch):
        # With no explicit site, DD_SITE is read at call time and drives the host.
        monkeypatch.setenv("DD_SITE", "us5")
        with patch("httpx.post", return_value=_resp(202)) as mock_post:
            tool_send_datadog_log("hi")
        assert (
            mock_post.call_args.args[0]
            == "https://http-intake.logs.us5.datadoghq.com/api/v2/logs"
        )

    def test_defaults_to_us1_without_site(self, monkeypatch):
        monkeypatch.delenv("DD_SITE", raising=False)
        with patch("httpx.post", return_value=_resp(202)) as mock_post:
            tool_send_datadog_log("hi")
        assert mock_post.call_args.args[0].startswith(
            "https://http-intake.logs.datadoghq.com/"
        )


# ── Error responses ────────────────────────────────────────────────────────
class TestErrors:
    @pytest.fixture(autouse=True)
    def _key(self, monkeypatch):
        monkeypatch.setenv("DD_API_KEY", "fake-key")

    def test_auth_failure_401(self):
        with patch("httpx.post", return_value=_resp(401, '{"errors":["bad key"]}')):
            result = tool_send_datadog_log("hi")
        assert result["status"] == "error"
        assert result["http_status"] == 401
        assert "Authentication failed" in result["error_message"]

    def test_forbidden_403(self):
        with patch("httpx.post", return_value=_resp(403, "forbidden")):
            result = tool_send_datadog_log("hi")
        assert result["http_status"] == 403
        assert "Authentication failed" in result["error_message"]

    def test_rejected_400(self):
        with patch("httpx.post", return_value=_resp(400, "bad request")):
            result = tool_send_datadog_log("hi")
        assert result["status"] == "error"
        assert result["http_status"] == 400
        assert "bad request" in result["error_message"]

    def test_network_error(self):
        import httpx

        with patch("httpx.post", side_effect=httpx.ConnectError("boom")):
            result = tool_send_datadog_log("hi")
        assert result["status"] == "error"
        assert "failed" in result["error_message"].lower()
