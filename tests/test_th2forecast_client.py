"""Unit tests for the th2forecast client. Loaded by file path so the heavy
apowerb package is not imported; only `requests` is needed (mocked). See
tests/test_th2etl_client.py for the sibling pattern this follows.

Every client under test is built with base_url/token/timeout_s all supplied
explicitly, so get_settings() (and the env/DB parsing behind it) is never
invoked -- the client only falls back to it when an argument is left unset."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MOD_PATH = (
    Path(__file__).resolve().parents[1]
    / "src" / "apowerb" / "integrations" / "th2forecast_client.py"
)
_spec = importlib.util.spec_from_file_location("th2forecast_client", _MOD_PATH)
th2forecast_client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(th2forecast_client)

Th2forecastClient = th2forecast_client.Th2forecastClient
Th2forecastAPIError = th2forecast_client.Th2forecastAPIError
Th2forecastUnavailable = th2forecast_client.Th2forecastUnavailable
Th2forecastTimeout = th2forecast_client.Th2forecastTimeout
Th2forecastNotConfigured = th2forecast_client.Th2forecastNotConfigured


class _Resp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code = status
        self._payload = payload
        self.text = text if payload is None else str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class _FakeRequests:
    """Records calls; returns/raises queued items in order."""

    class exceptions:
        class RequestException(Exception):
            pass

        class Timeout(RequestException):
            pass

    def __init__(self):
        self.calls = []
        self._queue = []

    def queue(self, *items):
        self._queue.extend(items)
        return self

    def _next(self, method, url, **kw):
        self.calls.append(
            {"method": method, "url": url, "json": kw.get("json"), "headers": kw.get("headers")}
        )
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def post(self, url, **kw):
        return self._next("POST", url, **kw)

    def get(self, url, **kw):
        return self._next("GET", url, **kw)


@pytest.fixture
def fake(monkeypatch):
    f = _FakeRequests()
    monkeypatch.setattr(th2forecast_client, "requests", f)
    return f


@pytest.fixture
def client(fake):
    return Th2forecastClient(base_url="http://th2forecast:18000", token="test-token", timeout_s=5)


_REQUEST_BODY = {
    "data": [{"date": "2024-01-01", "sales": 10}],
    "date_var": "date",
    "target_var": "sales",
    "horizon": 3,
    "models": ["prophet"],
}


def test_forecast_succeeds_via_jobs(client, fake):
    fake.queue(
        _Resp(202, {"job_id": "j1", "status": "queued"}),
        _Resp(200, {"job_id": "j1", "status": "running", "result": None, "error": None}),
        _Resp(200, {"job_id": "j1", "status": "succeeded", "result": {"status": "success", "series": []}, "error": None}),
    )
    result = client.forecast(_REQUEST_BODY)
    assert result == {"status": "success", "series": []}
    assert [c["method"] for c in fake.calls] == ["POST", "GET", "GET"]
    assert fake.calls[0]["url"].endswith("/v1/jobs")
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer test-token"


def test_forecast_falls_back_to_sync_on_404(client, fake):
    fake.queue(
        _Resp(404, {}),
        _Resp(200, {"status": "success", "series": []}),
    )
    result = client.forecast(_REQUEST_BODY)
    assert result == {"status": "success", "series": []}
    assert fake.calls[1]["url"].endswith("/v1/forecast")


def test_forecast_relays_400_error_body(client, fake):
    error_body = {"status": "error", "errors": [{"field": "date_var", "message": "colonne absente"}]}
    fake.queue(_Resp(400, error_body))
    with pytest.raises(Th2forecastAPIError) as exc_info:
        client.forecast(_REQUEST_BODY)
    assert exc_info.value.status_code == 400
    assert exc_info.value.body == error_body


def test_forecast_relays_401_and_413(client, fake):
    fake.queue(_Resp(401, {"status": "error", "errors": [{"field": None, "message": "unauthorized"}]}))
    with pytest.raises(Th2forecastAPIError) as exc_info:
        client.forecast(_REQUEST_BODY)
    assert exc_info.value.status_code == 401

    fake.queue(_Resp(413, {"status": "error", "errors": [{"field": None, "message": "too many rows"}]}))
    with pytest.raises(Th2forecastAPIError) as exc_info2:
        client.forecast(_REQUEST_BODY)
    assert exc_info2.value.status_code == 413


def test_job_failure_is_relayed_as_api_error(client, fake):
    fake.queue(
        _Resp(202, {"job_id": "j1", "status": "queued"}),
        _Resp(200, {
            "job_id": "j1", "status": "failed", "result": None,
            "error": {"field": "target_var", "message": "colonne inconnue"},
        }),
    )
    with pytest.raises(Th2forecastAPIError) as exc_info:
        client.forecast(_REQUEST_BODY)
    assert exc_info.value.status_code == 400
    assert exc_info.value.body["errors"][0]["message"] == "colonne inconnue"


def test_forecast_times_out_when_job_never_finishes(client, fake, monkeypatch):
    # timeout_s=5 on the `client` fixture; the clock jumps 50s per call so
    # the deadline (set on the first call) is blown past on the second,
    # without a real sleep.
    clock = {"n": 0}

    def _fake_monotonic():
        clock["n"] += 50
        return clock["n"]

    monkeypatch.setattr(th2forecast_client.time, "monotonic", _fake_monotonic)
    monkeypatch.setattr(th2forecast_client.time, "sleep", lambda _s: None)

    fake.queue(
        _Resp(202, {"job_id": "j1", "status": "queued"}),
        _Resp(200, {"job_id": "j1", "status": "running", "result": None, "error": None}),
    )
    with pytest.raises(Th2forecastTimeout):
        client.forecast(_REQUEST_BODY)


def test_forecast_unreachable_raises_unavailable_without_leaking_token(client, fake):
    fake.queue(th2forecast_client.requests.exceptions.RequestException("connection refused"))
    with pytest.raises(Th2forecastUnavailable) as exc_info:
        client.forecast(_REQUEST_BODY)
    assert "test-token" not in str(exc_info.value)


def test_unparsable_body_raises_unavailable_without_leaking_token(client, fake):
    fake.queue(_Resp(200, payload=None, text="not json"))
    with pytest.raises(Th2forecastUnavailable) as exc_info:
        client._forecast_sync(_REQUEST_BODY)
    assert "test-token" not in str(exc_info.value)


def test_client_raises_not_configured_without_url():
    with pytest.raises(Th2forecastNotConfigured):
        Th2forecastClient(base_url="", token="", timeout_s=5)


# --- Engine: R (apowerb/th2forecast, the default) or Python (th2forecast-py) ---
#
# The R engine ignores unknown fields without a word (a request with
# `hierarchy` answers 200, unreconciled) and rejects unknown models with a 400.
# The client must therefore refuse what R cannot do BEFORE calling it.

_PYTHON_SERIES = {"status": "success", "series": [{"group": None, "forecast": [], "calibration": None, "demand": {}}]}
_R_SERIES = {"status": "success", "series": [{"group": None, "forecast": [], "model": "ets"}]}


def _client(engine):
    return Th2forecastClient(base_url="http://th2forecast:18000", token="t", timeout_s=5, engine=engine)


@pytest.mark.parametrize("field, value", [
    ("hierarchy", ["region", "store"]),
    ("reconciliation", "mint"),
    ("events", [{"name": "promo", "ranges": []}]),
    ("scenarios", [{"name": "Sans promo", "without": ["promo"]}]),
])
def test_r_engine_refuses_python_only_fields_before_calling(fake, field, value):
    with pytest.raises(Th2forecastAPIError) as exc_info:
        _client("r").forecast({**_REQUEST_BODY, field: value})
    assert exc_info.value.status_code == 400
    assert [e["field"] for e in exc_info.value.body["errors"]] == [field]
    assert fake.calls == [], "the engine must not be called: it would ignore the field and answer 200"


def test_r_engine_refuses_python_only_models(fake):
    with pytest.raises(Th2forecastAPIError) as exc_info:
        _client("r").forecast({**_REQUEST_BODY, "models": ["ets", "croston", "tsb"]})
    assert exc_info.value.status_code == 400
    error = exc_info.value.body["errors"][0]
    assert error["field"] == "models"
    assert "croston" in error["message"] and "tsb" in error["message"]
    assert fake.calls == []


@pytest.mark.parametrize("empty", [None, [], ""])
def test_r_engine_treats_empty_fields_as_absent(fake, empty):
    # The agent tool dumps its schema with exclude_none=False (keys set to None),
    # and an empty list asks for nothing either.
    fake.queue(_Resp(404, {}), _Resp(200, _R_SERIES))
    body = {**_REQUEST_BODY, "hierarchy": empty, "events": empty, "scenarios": empty, "reconciliation": empty}
    assert _client("r").forecast(body) == _R_SERIES


def test_r_engine_drops_feedback(fake):
    fake.queue(_Resp(404, {}), _Resp(200, _R_SERIES))
    body = {**_REQUEST_BODY, "feedback": [{"date": "2024-02-01", "actual": 12}]}
    _client("r").forecast(body)
    assert all("feedback" not in c["json"] for c in fake.calls)
    assert "feedback" in body, "the caller's payload must not be mutated"


def test_python_engine_relays_everything(fake):
    fake.queue(_Resp(404, {}), _Resp(200, _PYTHON_SERIES))
    body = {**_REQUEST_BODY, "hierarchy": ["store"], "models": ["croston"], "feedback": [{"x": 1}]}
    assert _client("python").forecast(body) == _PYTHON_SERIES
    assert fake.calls[0]["json"] == body


def test_engine_defaults_to_r_when_not_given(fake):
    with pytest.raises(Th2forecastAPIError):
        Th2forecastClient(base_url="http://th2forecast:18000", token="t", timeout_s=5).forecast(
            {**_REQUEST_BODY, "hierarchy": ["store"]}
        )


def test_unknown_engine_is_refused():
    with pytest.raises(ValueError):
        _client("julia")


@pytest.mark.parametrize("engine, answer", [("r", _PYTHON_SERIES), ("python", _R_SERIES)])
def test_engine_mismatch_is_logged(fake, caplog, engine, answer):
    fake.queue(_Resp(404, {}), _Resp(200, answer))
    with caplog.at_level("WARNING"):
        assert _client(engine).forecast(_REQUEST_BODY) == answer
    assert "TH2FORECAST_ENGINE" in caplog.text


@pytest.mark.parametrize("engine, answer", [("r", _R_SERIES), ("python", _PYTHON_SERIES)])
def test_matching_engine_logs_nothing(fake, caplog, engine, answer):
    fake.queue(_Resp(404, {}), _Resp(200, answer))
    with caplog.at_level("WARNING"):
        _client(engine).forecast(_REQUEST_BODY)
    assert "TH2FORECAST_ENGINE" not in caplog.text
