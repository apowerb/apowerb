"""Unit tests for integrations.jev_client — the HTTP client for Jev decisions.

Covers: transport selection (OpenRouter / TypeSafe), the request shape, the
error mapping, and that the API key never appears in an error message.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
import requests

from apowerb.integrations import jev_client
from apowerb.integrations.jev_client import (
    JevAPIError,
    JevClient,
    JevNotConfigured,
    JevUnavailable,
)

_KEY = "sk-or-v1-fake-key-for-tests"
_QUESTIONS = {
    "urgent": {
        "type": "noul",
        "instructions": "Is it urgent?",
        "criteria": {"true": "yes", "false": "no"},
    }
}


def _response(status: int, body=None, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    if body is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = body
    return resp


def test_missing_key_is_not_configured():
    with pytest.raises(JevNotConfigured):
        JevClient(api_key="", transport="openrouter", model="", timeout_s=5)


def test_unknown_transport_is_not_configured():
    with pytest.raises(JevNotConfigured):
        JevClient(api_key=_KEY, transport="carrier-pigeon", model="", timeout_s=5)


@pytest.mark.parametrize(
    "transport,url,model",
    [
        (
            "openrouter",
            "https://openrouter.ai/api/alpha/decisions",
            "typesafe/jev-1.13",
        ),
        ("typesafe", "https://api.typesafe.ai/v1/systemone", "jev-1.13.0"),
    ],
)
def test_request_shape_per_transport(transport, url, model):
    client = JevClient(api_key=_KEY, transport=transport, model="", timeout_s=5)
    payload = {"answers": {"urgent": {"noul": 0.9}}, "usage": {"cost": 0.0001}}
    with patch.object(
        jev_client.requests, "post", return_value=_response(200, payload)
    ) as post:
        result = client.decide({"item": "server down"}, _QUESTIONS)

    assert result == payload
    args, kwargs = post.call_args
    assert args[0] == url
    assert kwargs["json"] == {
        "model": model,
        "state": {"item": "server down"},
        "questions": _QUESTIONS,
    }
    assert kwargs["headers"]["Authorization"] == f"Bearer {_KEY}"
    assert kwargs["timeout"] == 5


def test_explicit_model_overrides_transport_default():
    client = JevClient(
        api_key=_KEY, transport="openrouter", model="typesafe/jev-2", timeout_s=5
    )
    with patch.object(
        jev_client.requests, "post", return_value=_response(200, {"answers": {}})
    ) as post:
        client.decide({}, _QUESTIONS)
    assert post.call_args.kwargs["json"]["model"] == "typesafe/jev-2"


@pytest.mark.parametrize("status", [401, 402, 403, 429])
def test_business_errors_raise_api_error_without_key(status):
    client = JevClient(api_key=_KEY, transport="openrouter", model="", timeout_s=5)
    with patch.object(
        jev_client.requests,
        "post",
        return_value=_response(status, {"error": {"message": f"echo {_KEY}"}}),
    ):
        with pytest.raises(JevAPIError) as exc_info:
            client.decide({}, _QUESTIONS)
    assert exc_info.value.status_code == status
    assert _KEY not in str(exc_info.value)
    assert _KEY not in exc_info.value.user_message


def test_server_error_is_unavailable():
    client = JevClient(api_key=_KEY, transport="openrouter", model="", timeout_s=5)
    with patch.object(
        jev_client.requests, "post", return_value=_response(503, None, "oops")
    ):
        with pytest.raises(JevUnavailable):
            client.decide({}, _QUESTIONS)


def test_network_error_is_unavailable_without_key():
    client = JevClient(api_key=_KEY, transport="openrouter", model="", timeout_s=5)
    with patch.object(
        jev_client.requests,
        "post",
        side_effect=requests.ConnectionError(f"boom Authorization: Bearer {_KEY}"),
    ):
        with pytest.raises(JevUnavailable) as exc_info:
            client.decide({}, _QUESTIONS)
    assert _KEY not in str(exc_info.value)


def test_unparsable_body_is_unavailable():
    client = JevClient(api_key=_KEY, transport="openrouter", model="", timeout_s=5)
    with patch.object(
        jev_client.requests, "post", return_value=_response(200, None, "<html>")
    ):
        with pytest.raises(JevUnavailable):
            client.decide({}, _QUESTIONS)


def test_answers_missing_is_unavailable():
    client = JevClient(api_key=_KEY, transport="openrouter", model="", timeout_s=5)
    with patch.object(
        jev_client.requests, "post", return_value=_response(200, {"usage": {}})
    ):
        with pytest.raises(JevUnavailable):
            client.decide({}, _QUESTIONS)


def test_settings_are_read_when_arguments_are_omitted():
    settings = MagicMock(
        jev_api_key=_KEY, jev_transport="typesafe", jev_model="", jev_timeout_s=12
    )
    with patch.object(jev_client, "get_settings", return_value=settings):
        client = JevClient()
    assert client.url == "https://api.typesafe.ai/v1/systemone"
    assert client.model == "jev-1.13.0"
    assert client.timeout_s == 12


@pytest.mark.parametrize("timeout", [0, -1])
def test_non_positive_timeout_is_refused_not_replaced(timeout):
    with pytest.raises(JevNotConfigured):
        JevClient(api_key=_KEY, transport="openrouter", model="", timeout_s=timeout)
