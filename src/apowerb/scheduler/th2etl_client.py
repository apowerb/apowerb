"""th2etl-backed orchestrator client — a drop-in alternative to MageAPIClient.

It implements the same method surface the schedulers/run-agent code calls, but
targets th2etl's HTTP API instead of MageAI. Selected via the ``ORCHESTRATOR``
setting (``mage`` | ``th2etl``); defaults to ``mage`` so nothing changes until
the flag is flipped.

Mapping conventions (one th2etl scheduler per agent, named by ``agent_id``):
  - schedule identifier  -> th2etl scheduler **name** (== agent_id).
    Mage uses integer ids; callers treat the id opaquely (read it from a
    listing, pass it back), so we return the name as the "id".
  - per-trigger variables -> th2etl scheduler ``variables`` (JSONB), injected
    into the run context on every cron fire.
  - ad-hoc trigger        -> ``POST /schedulers/{name}/run``.
  - variable update       -> ``PUT  /schedulers/{name}/variables``.

This module deliberately has NO th2agent imports so it can be unit-tested in
isolation with ``requests`` mocked.
"""
from __future__ import annotations

import logging
from typing import Any

import requests

# Bound by name rather than read off the ``requests`` module global, which the
# test doubles in this package replace. See ``orchestrator_is_unreachable``.
from requests.exceptions import ConnectionError as _TransportRefused
from requests.exceptions import Timeout as _TransportTimedOut

from apowerb.configs.settings import get_settings

logger = logging.getLogger(__name__)

# Mage "@interval" shortcuts -> 5-field cron expressions used by th2etl triggers.
_INTERVAL_TO_CRON = {
    "@hourly": "0 * * * *",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@weekly": "0 0 * * 0",
    "@monthly": "0 0 1 * *",
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
}


def interval_to_cron(schedule_interval: str) -> str:
    """Translate a Mage schedule_interval into a th2etl cron expression."""
    if not schedule_interval:
        raise ValueError("schedule_interval is required")
    key = schedule_interval.strip().lower()
    if key in _INTERVAL_TO_CRON:
        return _INTERVAL_TO_CRON[key]
    if key.startswith("@"):
        # @once / @reboot and friends have no recurring cron equivalent.
        raise ValueError(f"Unsupported schedule_interval {schedule_interval!r} for th2etl")
    parts = schedule_interval.split()
    if len(parts) == 5:
        return schedule_interval
    raise ValueError(f"Invalid schedule_interval {schedule_interval!r}: expected '@hourly'-style or a 5-field cron")


# th2etl RunStatus -> the Mage/front status vocabulary the dashboard expects.
# The SchedulerManager UI's STATUS_CONFIG only knows
# initial/running/completed/failed/cancelled and silently renders any *unmapped*
# status as "Initial" — so a th2etl run in "pending" or (worse) "success" would
# show as Initial without this translation. Keep this table in sync with th2etl's
# RunStatus enum.
_TH2ETL_TO_MAGE_STATUS = {
    "pending": "initial",
    "running": "running",
    "success": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}


def _to_mage_run(run: dict[str, Any]) -> dict[str, Any]:
    """Reshape a th2etl run into the Mage-run shape the dashboard reads:
    translate the status vocabulary and expose ``completed_at``/``execution_date``
    aliases (th2etl stores ``finished_at``/``started_at``). Unknown statuses are
    passed through unchanged so they surface instead of masquerading as Initial."""
    if not isinstance(run, dict):
        return run
    mapped = dict(run)
    raw_status = run.get("status")
    if raw_status is not None and raw_status not in _TH2ETL_TO_MAGE_STATUS:
        logger.warning(
            "th2etl run %s has unmapped status %r — it will render as-is in the "
            "dashboard; add it to _TH2ETL_TO_MAGE_STATUS.", run.get("id"), raw_status
        )
    mapped["status"] = _TH2ETL_TO_MAGE_STATUS.get(raw_status, raw_status)
    mapped["completed_at"] = run.get("finished_at")
    mapped["execution_date"] = run.get("started_at") or run.get("created_at")
    return mapped


class OrchestratorUnavailable(RuntimeError):
    """The orchestrator could not be reached at all.

    Raised rather than returning an empty list, because "unreachable" and "no
    pipelines" are not the same answer and only the caller can decide what to
    do with the difference. Returning ``[]`` made a dead orchestrator read as
    an empty dashboard: on the dev VM the service stopped on 2026-07-11 and
    nobody could see it until 2026-08-03, because the page it broke kept
    rendering perfectly.

    Kept in this module on purpose: it has no apowerb imports, so both clients
    and the tests that load it by file path can use it.
    """


# A gateway answering for an orchestrator it could not reach.
_GATEWAY_CANNOT_REACH_IT = frozenset({502, 503, 504})


def orchestrator_is_unreachable(exc: BaseException) -> bool:
    """Did the call fail to reach the orchestrator at all?

    An HTTP error response means the orchestrator answered, and an answer is
    not an outage: a 404 for a run that does not exist is a fact about that
    run. Reporting it as ``OrchestratorUnavailable`` would move the lie rather
    than remove it -- the caller would show "the scheduler is down" over a
    scheduler that just told it something true.

    So a transport failure counts: refused, timed out, DNS -- the two
    ``requests`` families that mean the request never got an answer.

    And so does a **gateway** saying it could not reach the orchestrator for
    us. 502, 503 and 504 are exactly that answer: the proxy is up, the thing
    behind it is not. Reading them as "it answered, degrade quietly" put the
    original bug straight back on every route sitting behind an nginx, a
    Traefik or an ALB -- which is the ordinary deployment, not an edge case.
    Every other status carries a fact about the request, not about reachability.

    The classes are imported by name rather than read off the module global:
    the test doubles in this package replace ``th2etl_client.requests`` with a
    fake that collapses the hierarchy to bare ``Exception``, under which an
    answered 500 and a refused connection become indistinguishable. Asking
    "did the response come back empty-handed" would then read one as the
    other -- which is exactly how the first version of this helper turned an
    upstream 500 into "the orchestrator is unreachable".

    Lives beside ``OrchestratorUnavailable`` and not in a helpers module for
    the same reason it does: both clients need it, and this module imports
    nothing from apowerb.
    """
    if isinstance(exc, (_TransportRefused, _TransportTimedOut)):
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status in _GATEWAY_CANNOT_REACH_IT


class Th2etlAPIClient:
    """HTTP client for th2etl, exposing the MageAPIClient method surface."""

    def __init__(self, base_url: str, timeout: int = 15, api_key: str | None = None) -> None:
        if not base_url:
            raise ValueError("Th2etlAPIClient requires a base_url")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        # Every request goes through this session so the bearer token cannot be
        # forgotten on a call added later -- the failure it would cause is a 401
        # swallowed by the caller, i.e. scheduling that stops without a trace.
        self._http = requests.Session()
        key = api_key if api_key is not None else get_settings().th2etl_api_key
        if key:
            self._http.headers["Authorization"] = f"Bearer {key}"
        else:
            logger.warning(
                "th2etl: no TH2ETL_API_KEY configured — the orchestrator will "
                "answer 401 on every business route."
            )

    # --- low-level helpers -------------------------------------------------
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _trigger_name(self, agent_id: str) -> str:
        return f"{agent_id}_trigger"

    # --- pipeline lifecycle (orchestrator init) ----------------------------
    def pipeline_exists(self, pipeline_uuid: str) -> bool:
        try:
            resp = self._http.get(self._url(f"/pipelines/{pipeline_uuid}"), timeout=self.timeout)
            return resp.status_code == 200
        except requests.RequestException as e:
            logger.error("th2etl pipeline_exists failed: %s", e)
            return False

    def create_pipeline(self, pipeline_uuid: str) -> dict[str, Any] | None:
        # In th2etl the agent pipelines are provisioned by the seed
        # (th2etl.seeds.default_pipelines), not created ad-hoc here. We only
        # confirm presence; creation would also require the blocs to exist.
        if self.pipeline_exists(pipeline_uuid):
            return {"name": pipeline_uuid}
        logger.error(
            "th2etl pipeline %r is missing — run the th2etl seed to provision it.", pipeline_uuid
        )
        return None

    def get_all_pipelines(self) -> list:
        try:
            resp = self._http.get(self._url("/pipelines/"), timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.error("th2etl get_all_pipelines failed: %s", e)
            if orchestrator_is_unreachable(e):
                raise OrchestratorUnavailable(
                    f"th2etl is unreachable at {self.base_url}: {e}"
                ) from e
            # It answered, and not with a pipeline list: a rejected key, a
            # broken th2etl. Still never `[]` -- an empty dashboard would be the
            # same lie this method exists to stop. But do not call it
            # unreachable either: that sends whoever reads it to check the
            # network instead of the answer they actually got.
            raise OrchestratorUnavailable(
                f"th2etl answered but could not list pipelines at {self.base_url}: {e}"
            ) from e

    # --- block lifecycle (orchestrator init) -------------------------------
    # In Mage the orchestrator injects an ``agent_exe`` code block that POSTs
    # to /api/adk/run_from_jwt. th2etl owns that logic server-side: the agent
    # execution blocs (``run_adk_from_jwt``) are provisioned by the th2etl
    # seed alongside their pipeline. So the adapter reports the block as
    # present and makes creation a no-op — injecting a Mage code block into
    # th2etl would be wrong. Without these, _initialize_pipeline() would hit
    # AttributeError the first time an agent is scheduled under ORCHESTRATOR=th2etl.
    def block_exists(self, pipeline_uuid: str, block_uuid: str) -> bool:
        logger.debug(
            "th2etl block_exists(%r/%r) -> True (execution bloc is managed by the "
            "th2etl seed; run failures here mean the seed did not provision it).",
            pipeline_uuid,
            block_uuid,
        )
        return True

    def create_block(
        self, pipeline_uuid: str, block_uuid: str, block_content: str, block_type: str
    ) -> dict[str, Any]:
        logger.warning(
            "th2etl create_block(%r/%r) is a no-op — execution blocs are seeded "
            "server-side; ignoring the Mage code block.",
            pipeline_uuid,
            block_uuid,
        )
        return {"uuid": block_uuid, "type": block_type, "status": "managed_by_th2etl"}

    def create_api_trigger(
        self, pipeline_uuid: str, trigger_name: str, runtime_variables: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Deprecated Mage API-trigger path. A Mage API trigger fires only
        on-demand, so we back it with an **inactive** th2etl scheduler (never
        auto-fires on cron) that can still be run ad-hoc, and return a
        token-less id."""
        result = self.create_schedule_trigger(
            pipeline_uuid=pipeline_uuid,
            trigger_name=trigger_name,
            schedule_interval="@daily",
            runtime_variables=runtime_variables,
        )
        if not result:
            return None
        # create_schedule_trigger makes the scheduler active by default; an API
        # trigger must not fire on a schedule, so deactivate it immediately.
        self.update_schedule(result["id"], status="inactive")
        return {"id": result["id"], "name": trigger_name, "token": None}

    # --- schedules ---------------------------------------------------------
    def get_pipeline_schedules(self, pipeline_uuid: str) -> list:
        """List schedulers for a pipeline as Mage-shaped schedule dicts.

        Each entry exposes ``id`` and ``name`` both set to the scheduler name
        (callers filter by ``name == agent_id`` and pass ``id`` back)."""
        try:
            resp = self._http.get(self._url("/schedulers/"), timeout=self.timeout)
            resp.raise_for_status()
            schedulers = resp.json()
        except requests.RequestException as e:
            if orchestrator_is_unreachable(e):
                raise OrchestratorUnavailable(
                    f"th2etl is unreachable at {self.base_url}: {e}"
                ) from e
            logger.error("th2etl get_pipeline_schedules failed: %s", e)
            return []
        out = []
        for s in schedulers:
            if s.get("pipeline_name") != pipeline_uuid:
                continue
            out.append({"id": s["name"], "name": s["name"], "status": "active" if s.get("active", True) else "inactive"})
        return out

    def create_schedule_trigger(
        self,
        pipeline_uuid: str,
        trigger_name: str,
        schedule_interval: str,
        runtime_variables: dict[str, Any] | None = None,
        start_time: str | None = None,
    ) -> dict[str, Any] | None:
        """Create a th2etl trigger (cron) + scheduler (carrying the runtime
        variables). ``trigger_name`` is the agent_id; the scheduler is named
        after it. Created inactive when ``start_time`` is in the future."""
        agent_id = trigger_name
        cron = interval_to_cron(schedule_interval)
        active = start_time is None  # future start -> create disabled, activate later
        try:
            self._http.post(
                self._url("/triggers/"),
                json={"name": self._trigger_name(agent_id), "pipeline_name": pipeline_uuid, "cron_expression": cron},
                timeout=self.timeout,
            ).raise_for_status()
            resp = self._http.post(
                self._url("/schedulers/"),
                json={
                    "name": agent_id,
                    "pipeline_name": pipeline_uuid,
                    "trigger_name": self._trigger_name(agent_id),
                    "variables": runtime_variables or {},
                    "active": active,
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.RequestException as e:
            logger.error("th2etl create_schedule_trigger failed for %r: %s", agent_id, e)
            return None
        return {"id": agent_id, "name": agent_id}

    def update_schedule(
        self,
        schedule_id: str,
        schedule_interval: str | None = None,
        start_time: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """Update a scheduler's active status and/or its trigger's cron."""
        agent_id = schedule_id
        if status is not None:
            self._http.put(
                self._url(f"/schedulers/{agent_id}"),
                json={"active": status == "active"},
                timeout=self.timeout,
            ).raise_for_status()
        if schedule_interval is not None:
            cron = interval_to_cron(schedule_interval)
            self._http.put(
                self._url(f"/triggers/{self._trigger_name(agent_id)}"),
                json={"cron_expression": cron},
                timeout=self.timeout,
            ).raise_for_status()
        return {"id": agent_id}

    def update_schedule_variables(self, schedule_id: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Replace a scheduler's runtime variables (token rotation)."""
        resp = self._http.put(
            self._url(f"/schedulers/{schedule_id}/variables"),
            json={"variables": variables},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        return {"id": schedule_id}

    # --- ad-hoc runs -------------------------------------------------------
    def trigger_pipeline_run_for_schedule(
        self, schedule_id: str, run_variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Trigger the scheduler's pipeline now, merging the scheduler's stored
        variables with ``run_variables``."""
        resp = self._http.post(
            self._url(f"/schedulers/{schedule_id}/run"),
            json={"variables": run_variables or {}},
            timeout=self.timeout,
        )
        resp.raise_for_status()
        data = resp.json()
        # expose both shapes so callers reading "id" or "run_id"/"status" work
        return {"id": data.get("run_id"), "run_id": data.get("run_id"), "status": data.get("status")}

    def trigger_pipeline(
        self, schedule_id: str, trigger_token: str | None = None, run_variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Ad-hoc trigger (Mage's API-token variant). th2etl has no trigger
        token; the call maps to the same run-now endpoint."""
        return self.trigger_pipeline_run_for_schedule(schedule_id, run_variables)

    # --- run inspection (dashboard) ----------------------------------------
    def get_pipeline_runs(self, schedule_id: str) -> list:
        """Runs for a scheduler (== agent_id), most recent first."""
        try:
            resp = self._http.get(self._url(f"/schedulers/{schedule_id}/runs"), timeout=self.timeout)
            resp.raise_for_status()
            return [_to_mage_run(r) for r in resp.json()]
        except requests.RequestException as e:
            if orchestrator_is_unreachable(e):
                raise OrchestratorUnavailable(
                    f"th2etl is unreachable at {self.base_url}: {e}"
                ) from e
            logger.error("th2etl get_pipeline_runs failed for %r: %s", schedule_id, e)
            return []

    def get_pipeline_run(self, run_id: int) -> dict[str, Any] | None:
        try:
            resp = self._http.get(self._url(f"/runs/{run_id}"), timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as e:
            if orchestrator_is_unreachable(e):
                raise OrchestratorUnavailable(
                    f"th2etl is unreachable at {self.base_url}: {e}"
                ) from e
            # Answered, and not with a run. `None` is what the route turns into
            # 404; re-raising sent the raw `HTTPError` out instead, so a run
            # that simply does not exist came back as a 500 with a trace. The
            # same lookup under Mage has always returned `None`.
            logger.error("th2etl %s failed for %r: %s", "get_pipeline_run", run_id, e)
            return None
        return _to_mage_run(resp.json())

    def get_pipeline_run_logs(self, run_id: int) -> list[dict[str, Any]]:
        """Structured, step-by-step log for a run (th2etl-native
        ``GET /runs/{id}/logs``). Each entry is ``{id, run_id, ts, level,
        logger_name, message}`` in chronological order. Returns ``[]`` on error
        so the dashboard degrades gracefully rather than 500-ing."""
        try:
            resp = self._http.get(self._url(f"/runs/{run_id}/logs"), timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            if orchestrator_is_unreachable(e):
                raise OrchestratorUnavailable(
                    f"th2etl is unreachable at {self.base_url}: {e}"
                ) from e
            logger.error("th2etl get_pipeline_run_logs failed for %r: %s", run_id, e)
            return []

    def cancel_pipeline_run(self, run_id: int) -> dict[str, Any] | None:
        try:
            resp = self._http.post(self._url(f"/runs/{run_id}/cancel"), timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as e:
            if orchestrator_is_unreachable(e):
                raise OrchestratorUnavailable(
                    f"th2etl is unreachable at {self.base_url}: {e}"
                ) from e
            # Answered, and not with a run. `None` is what the route turns into
            # 404; re-raising sent the raw `HTTPError` out instead, so a run
            # that simply does not exist came back as a 500 with a trace. The
            # same lookup under Mage has always returned `None`.
            logger.error("th2etl %s failed for %r: %s", "cancel_pipeline_run", run_id, e)
            return None
        return _to_mage_run(resp.json())
