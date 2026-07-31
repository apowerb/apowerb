"""Unit tests for the th2etl orchestrator client. Loaded by file path so the
heavy apowerb package is not imported; only `requests` is needed (mocked)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_MOD_PATH = Path(__file__).resolve().parents[1] / "src" / "apowerb" / "scheduler" / "th2etl_client.py"
_spec = importlib.util.spec_from_file_location("th2etl_client", _MOD_PATH)
th2etl_client = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(th2etl_client)

Th2etlAPIClient = th2etl_client.Th2etlAPIClient
interval_to_cron = th2etl_client.interval_to_cron


class _Resp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise th2etl_client.requests.HTTPError(f"status {self.status_code}")

    def json(self):
        return self._payload


class _FakeRequests:
    """Records calls and returns queued/derived responses."""

    HTTPError = Exception
    RequestException = Exception

    def __init__(self):
        self.calls = []
        self.responses = {}  # (method, url) -> _Resp

    def _handle(self, method, url, **kw):
        self.calls.append({"method": method, "url": url, "json": kw.get("json")})
        return self.responses.get((method, url), _Resp(200, {}))

    def get(self, url, **kw):
        return self._handle("GET", url, **kw)

    def post(self, url, **kw):
        return self._handle("POST", url, **kw)

    def put(self, url, **kw):
        return self._handle("PUT", url, **kw)


@pytest.fixture
def fake(monkeypatch):
    f = _FakeRequests()
    monkeypatch.setattr(th2etl_client, "requests", f)
    return f


@pytest.fixture
def client():
    return Th2etlAPIClient("http://th2etl:8009")


# --- interval mapping ---

def test_interval_to_cron_shortcuts():
    assert interval_to_cron("@hourly") == "0 * * * *"
    assert interval_to_cron("@daily") == "0 0 * * *"
    assert interval_to_cron("*/5 * * * *") == "*/5 * * * *"


def test_interval_to_cron_rejects_unsupported():
    with pytest.raises(ValueError):
        interval_to_cron("@once")
    with pytest.raises(ValueError):
        interval_to_cron("not a cron")


# --- block lifecycle adapter (Mage's agent_exe block has no th2etl equivalent) ---

def test_block_exists_always_true_without_http(fake, client):
    # Reports the seeded server-side execution bloc as present, so the
    # Mage-shaped orchestrator never tries to inject a code block.
    assert client.block_exists("agents", "agent_exe") is True
    assert fake.calls == []


def test_create_block_is_noop_without_http(fake, client):
    out = client.create_block("agents", "agent_exe", "print('x')", "data_loader")
    assert out["status"] == "managed_by_th2etl"
    assert fake.calls == []


def test_create_api_trigger_backs_onto_inactive_schedule_trigger(fake, client):
    # A Mage API trigger is on-demand only: the backing th2etl scheduler must
    # NOT fire on cron, so it is deactivated right after creation.
    out = client.create_api_trigger("agents", "42", {"agent_id": "42"})
    assert out == {"id": "42", "name": "42", "token": None}
    sched = next(c for c in fake.calls if c["url"].endswith("/schedulers/"))
    assert sched["json"]["name"] == "42"
    assert sched["json"]["variables"] == {"agent_id": "42"}
    # created active, then immediately toggled inactive
    deactivate = next(c for c in fake.calls if c["method"] == "PUT" and c["url"].endswith("/schedulers/42"))
    assert deactivate["json"] == {"active": False}


# --- create schedule (trigger + scheduler with variables) ---

def test_create_schedule_trigger_posts_trigger_and_scheduler(fake, client):
    out = client.create_schedule_trigger(
        pipeline_uuid="agents",
        trigger_name="42",
        schedule_interval="@hourly",
        runtime_variables={"agent_id": "42", "jwt_token": "tok"},
    )
    assert out == {"id": "42", "name": "42"}
    posts = [c for c in fake.calls if c["method"] == "POST"]
    trig = next(c for c in posts if c["url"].endswith("/triggers/"))
    sched = next(c for c in posts if c["url"].endswith("/schedulers/"))
    assert trig["json"] == {"name": "42_trigger", "pipeline_name": "agents", "cron_expression": "0 * * * *"}
    assert sched["json"]["name"] == "42"
    assert sched["json"]["variables"] == {"agent_id": "42", "jwt_token": "tok"}
    assert sched["json"]["active"] is True


def test_create_schedule_with_future_start_is_inactive(fake, client):
    client.create_schedule_trigger("agents", "42", "@hourly", start_time="2099-01-01T00:00:00Z")
    sched = next(c for c in fake.calls if c["url"].endswith("/schedulers/"))
    assert sched["json"]["active"] is False


# --- variable rotation ---

def test_update_schedule_variables_calls_variables_endpoint(fake, client):
    client.update_schedule_variables("42", {"jwt_token": "new"})
    call = fake.calls[-1]
    assert call["method"] == "PUT"
    assert call["url"] == "http://th2etl:8009/schedulers/42/variables"
    assert call["json"] == {"variables": {"jwt_token": "new"}}


# --- ad-hoc run ---

def test_trigger_pipeline_runs_scheduler_and_maps_run_id(fake, client):
    fake.responses[("POST", "http://th2etl:8009/schedulers/42/run")] = _Resp(
        202, {"run_id": 7, "pipeline_name": "agents", "status": "pending"}
    )
    out = client.trigger_pipeline("42", trigger_token="ignored", run_variables={"jwt_token": "t"})
    call = fake.calls[-1]
    assert call["url"] == "http://th2etl:8009/schedulers/42/run"
    assert call["json"] == {"variables": {"jwt_token": "t"}}
    assert out["id"] == 7 and out["run_id"] == 7 and out["status"] == "pending"


# --- listing ---

def test_get_pipeline_schedules_filters_and_uses_name_as_id(fake, client):
    fake.responses[("GET", "http://th2etl:8009/schedulers/")] = _Resp(200, [
        {"name": "42", "pipeline_name": "agents", "active": True},
        {"name": "other", "pipeline_name": "etl", "active": True},
    ])
    out = client.get_pipeline_schedules("agents")
    assert out == [{"id": "42", "name": "42", "status": "active"}]


# --- update status ---

def test_update_schedule_status_toggles_active(fake, client):
    client.update_schedule("42", status="inactive")
    call = fake.calls[-1]
    assert call["url"] == "http://th2etl:8009/schedulers/42"
    assert call["json"] == {"active": False}


# --- run inspection (dashboard) ---

def test_get_pipeline_runs_hits_scheduler_runs(fake, client):
    fake.responses[("GET", "http://th2etl:8009/schedulers/42/runs")] = _Resp(
        200, [{"id": 1, "status": "pending"}, {"id": 2, "status": "success"}]
    )
    runs = client.get_pipeline_runs("42")
    # statuses are translated to the Mage/front vocabulary
    assert [r["status"] for r in runs] == ["initial", "completed"]
    assert [r["id"] for r in runs] == [1, 2]


def test_get_pipeline_run_hits_top_level_runs(fake, client):
    fake.responses[("GET", "http://th2etl:8009/runs/7")] = _Resp(
        200, {"id": 7, "status": "success", "finished_at": "2026-07-02T08:00:00+00:00", "started_at": "2026-07-02T07:59:00+00:00"}
    )
    run = client.get_pipeline_run(7)
    assert run["id"] == 7
    assert run["status"] == "completed"  # success -> completed
    assert run["completed_at"] == "2026-07-02T08:00:00+00:00"  # finished_at alias
    assert run["execution_date"] == "2026-07-02T07:59:00+00:00"  # started_at alias
    assert fake.calls[-1]["url"] == "http://th2etl:8009/runs/7"


def test_cancel_pipeline_run_posts_cancel(fake, client):
    fake.responses[("POST", "http://th2etl:8009/runs/7/cancel")] = _Resp(200, {"id": 7, "status": "cancelled"})
    assert client.cancel_pipeline_run(7)["status"] == "cancelled"
    call = fake.calls[-1]
    assert call["method"] == "POST" and call["url"] == "http://th2etl:8009/runs/7/cancel"


def test_get_pipeline_run_logs_hits_logs_endpoint(fake, client):
    fake.responses[("GET", "http://th2etl:8009/runs/7/logs")] = _Resp(200, [
        {"id": 1, "run_id": 7, "ts": "2026-07-02T13:00:00+00:00", "level": "INFO",
         "logger_name": "th2etl.pipelines.runner", "message": "Run 7 started"},
    ])
    logs = client.get_pipeline_run_logs(7)
    assert fake.calls[-1]["url"] == "http://th2etl:8009/runs/7/logs"
    assert logs[0]["message"] == "Run 7 started"


def test_get_pipeline_run_logs_returns_empty_on_error(fake, client):
    # a failing upstream must degrade to [] (dashboard stays up), not raise
    fake.responses[("GET", "http://th2etl:8009/runs/9/logs")] = _Resp(500, {})
    assert client.get_pipeline_run_logs(9) == []


def test_status_mapping_covers_all_th2etl_statuses():
    # every th2etl RunStatus must map to a status the front's STATUS_CONFIG knows,
    # otherwise a run silently renders as "Initial".
    to_mage = th2etl_client._TH2ETL_TO_MAGE_STATUS
    assert to_mage == {
        "pending": "initial",
        "running": "running",
        "success": "completed",
        "failed": "failed",
        "cancelled": "cancelled",
    }
    # unknown status passes through unchanged (surfaces instead of masking as Initial)
    assert th2etl_client._to_mage_run({"id": 1, "status": "weird"})["status"] == "weird"
    # status absent or explicitly None -> None, no crash (and no unmapped-status noise)
    assert th2etl_client._to_mage_run({"id": 2})["status"] is None
    assert th2etl_client._to_mage_run({"id": 3, "status": None})["status"] is None
    # non-dict input is returned untouched
    assert th2etl_client._to_mage_run(None) is None
