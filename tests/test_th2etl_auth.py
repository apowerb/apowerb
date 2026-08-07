"""Every call to th2etl must carry its bearer token.

th2etl requires `Authorization: Bearer <API_KEY>` on its business routes and
compares it in constant time. The client used to call `requests.get(...)`
directly, with no headers at all: the orchestrator answers 401, the client
swallows the request error, and agent scheduling stops without a single line
saying so.

The token lives on a `requests.Session` rather than on each call, so a method
added later cannot forget it -- which is the failure mode that would be
invisible.
"""

from __future__ import annotations

import pytest

from apowerb.scheduler.th2etl_client import Th2etlAPIClient


def test_the_session_carries_the_bearer_token():
    client = Th2etlAPIClient("http://etl.test:8009", api_key="s3cr3t")
    assert client._http.headers["Authorization"] == "Bearer s3cr3t"


def test_no_key_is_loud_rather_than_silent(caplog, monkeypatch):
    """A missing key yields 401s the caller swallows; the log is the only clue."""
    import logging

    from apowerb.configs import settings as settings_module

    monkeypatch.setattr(
        settings_module.get_settings(), "th2etl_api_key", None, raising=False
    )
    with caplog.at_level(logging.WARNING, logger="apowerb.scheduler.th2etl_client"):
        client = Th2etlAPIClient("http://etl.test:8009", api_key="")

    assert "Authorization" not in client._http.headers
    assert "TH2ETL_API_KEY" in caplog.text


def test_no_call_site_bypasses_the_session():
    """A bare `requests.<verb>(` would go out unauthenticated."""
    import inspect
    import re

    from apowerb.scheduler import th2etl_client

    source = inspect.getsource(th2etl_client)
    stray = re.findall(r"\brequests\.(get|post|put|delete|patch)\(", source)
    assert not stray, f"appels non authentifies: {stray}"


@pytest.mark.parametrize(
    "method, args",
    [
        ("pipeline_exists", ("agents",)),
        ("get_all_pipelines", ()),
        ("get_pipeline_schedules", ("agents",)),
    ],
)
def test_read_calls_go_through_the_authenticated_session(monkeypatch, method, args):
    client = Th2etlAPIClient("http://etl.test:8009", api_key="s3cr3t")
    seen: dict[str, object] = {}

    class _Resp:
        status_code = 200
        ok = True

        @staticmethod
        def json():
            return []

        @staticmethod
        def raise_for_status():
            return None

    def _get(url, *a, **kw):
        seen["url"] = url
        seen["auth"] = client._http.headers.get("Authorization")
        return _Resp()

    monkeypatch.setattr(client._http, "get", _get)
    getattr(client, method)(*args)

    assert seen.get("auth") == "Bearer s3cr3t", f"{method} est parti sans jeton"
