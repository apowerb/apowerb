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
from requests.exceptions import InvalidJSONError as _AnsweredUnreadably
from requests.exceptions import RequestException as _RequestFailed
from requests.exceptions import TooManyRedirects as _RedirectLoop

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

    ``unreachable`` says which of the two failures happened: nobody answered,
    or the orchestrator answered and its answer was not the one asked for.
    Both mean the caller cannot be told what it asked, which is why both leave
    as 503; only the log level and the sentence differ.
    """

    def __init__(self, message: str, *, unreachable: bool = True, status: int | None = None):
        super().__init__(message)
        self.unreachable = unreachable
        self.status = status


# A gateway answering for an orchestrator it could not reach.
_GATEWAY_CANNOT_REACH_IT = frozenset({502, 503, 504})


def _a_list_or_nothing(parsed, what: str, base_url: str):
    """A listing that is not a list is not an empty listing.

    th2etl answers these endpoints with a bare JSON array, so anything else --
    an error object, a paginated envelope, `null` -- is an answer we cannot
    read. Iterating it walked a dict's keys, matched nothing, and produced the
    empty list this whole change set exists to stop.
    """
    if not isinstance(parsed, list):
        raise OrchestratorUnavailable(
            f"th2etl answered at {base_url} with something other than a list of "
            f"{what}",
            unreachable=False,
        )
    return parsed


def _an_object_or_nothing(parsed, what: str, base_url: str):
    """The mirror of ``_a_list_or_nothing``, for answers that are one resource
    rather than a listing. Without it a body that came back as an array was
    handed on untouched -- `_to_mage_run` passes non-dicts straight through --
    and the route served nonsense as a run."""
    if not isinstance(parsed, dict):
        raise OrchestratorUnavailable(
            f"th2etl answered at {base_url} with something other than {what}",
            unreachable=False,
        )
    return parsed


def ask_orchestrator(
    send,
    *,
    name: str,
    base_url: str,
    doing: str,
    expect: str | None = None,
    body: bool = True,
    enveloped: bool = True,
):
    """Make one call to an orchestrator and return the body it promised.

    The single place where a failed call is classified. Before this existed,
    every method decided for itself and they did not agree: `return []`,
    `return None`, `return False`, a `.get(key, [])` default that produced an
    empty answer with **no exception at all**, and parsing left outside the
    guarded block. Three rounds of review found a new one each round, each time
    after the previous had been declared fixed -- the last one reaching
    `get_all_pipelines`, the method that had been considered safe since the
    outage that started all this. A guard per method could not converge because
    there was no one place to guard.

    A caller that wants to degrade now says so in two visible lines. Silence is
    no longer something a method can arrange by accident.

    Raises ``OrchestratorUnavailable`` for every failure, and sets
    ``unreachable`` to say which kind:

      * no answer came back at all -- refused, DNS, timed out, body cut off,
        redirect loop, broken encoding;
      * a gateway answered 502/503/504 for an orchestrator it could not reach;
      * the orchestrator answered an HTTP error (``unreachable=False``);
      * it answered 200 with a body that would not parse, that carries an
        ``error`` envelope, or that lacks the ``expect`` key it promised
        (``unreachable=False``).

    That last case is the one no ``except`` could ever have caught: the body
    arrived, `.get(key, [])` handed back an empty list, and an orchestrator
    saying "not found" rendered as a screen saying "you have none".
    """
    try:
        response = send()
    except Exception as exc:
        if orchestrator_is_unreachable(exc):
            raise OrchestratorUnavailable(
                f"{name} is unreachable at {base_url}: {exc}"
            ) from exc
        raise OrchestratorUnavailable(
            f"{name} answered but could not {doing} at {base_url}: {exc}",
            unreachable=False,
        ) from exc

    status = getattr(response, "status_code", None)
    if status in _GATEWAY_CANNOT_REACH_IT:
        raise OrchestratorUnavailable(
            f"{name} is unreachable at {base_url}: the gateway answered {status}",
            status=status,
        )
    try:
        response.raise_for_status()
    except Exception as exc:
        if orchestrator_is_unreachable(exc):
            raise OrchestratorUnavailable(
                f"{name} is unreachable at {base_url}: {exc}"
            ) from exc
        raise OrchestratorUnavailable(
            f"{name} answered {status} and could not {doing} at {base_url}",
            unreachable=False,
            status=status,
        ) from exc

    if not body:
        # A write whose reply carries nothing to read. Parsing it anyway would
        # turn a perfectly good 204 into "answered with a body that would not
        # parse".
        return None
    try:
        parsed = response.json()
    except Exception as exc:
        raise OrchestratorUnavailable(
            f"{name} answered {status} at {base_url} with a body that would not parse",
            unreachable=False,
            status=status,
        ) from exc

    # `error` at the top of an *enveloped* answer is the orchestrator refusing.
    # Where the body IS the resource it is a field of that resource: a th2etl
    # run that failed carries the exception that killed it, by design, and
    # reading that as a refusal made `get_pipeline_run` answer `None` for a run
    # that plainly existed -- "pipeline run not found", a fresh lie about a run.
    #
    # Which of the two it is belongs to the API, not to whether this particular
    # call names a key. Keying it on `expect` was the next version of the same
    # mistake: it let four Mage writes that unwrap nothing accept
    # `{"error": "duplicate pipeline uuid"}` as a success.
    #
    # And the value, not the key: `{"error": null}` beside a good payload is
    # not an error.
    if enveloped and isinstance(parsed, dict) and parsed.get("error"):
        raise OrchestratorUnavailable(
            f"{name} answered {status} at {base_url} with an error: "
            f"{parsed['error']}",
            unreachable=False,
            status=status,
        )
    if expect is not None:
        if not isinstance(parsed, dict) or expect not in parsed:
            raise OrchestratorUnavailable(
                f"{name} answered {status} at {base_url} without the {expect!r} it "
                f"promised",
                unreachable=False,
                status=status,
            )
        return parsed[expect]
    return parsed


def degrade_unless_unreachable(exc: OrchestratorUnavailable, fallback, logger, doing: str):
    """A caller's deliberate, visible decision to answer something else.

    An orchestrator that answered "no such run" has told us a fact, and the
    route turns ``None`` into a 404. One that never answered has told us
    nothing, and the caller must not invent an answer on its behalf.
    """
    if exc.unreachable:
        raise exc
    logger.error("could not %s: %s", doing, exc)
    return fallback


def orchestrator_is_unreachable(exc: BaseException) -> bool:
    """Did the call fail to reach the orchestrator at all?

    An HTTP error response means the orchestrator answered, and an answer is
    not an outage: a 404 for a run that does not exist is a fact about that
    run. Reporting it as ``OrchestratorUnavailable`` would move the lie rather
    than remove it -- the caller would show "the scheduler is down" over a
    scheduler that just told it something true.

    So the question is whether an answer came back at all, and ``requests``
    already records it: a failure that carries no ``response`` never got one.
    That covers more than a refused connection -- a DNS miss, a read timeout,
    a body cut off mid-flight, a redirect loop, a broken content encoding. An
    earlier version named ``ConnectionError`` and ``Timeout`` explicitly and
    let the other three through, which is the same bug in a narrower disguise.

    One failure carries no response and is still an answer: a body that came
    back whole and would not parse. The orchestrator replied; we could not
    read it. That is a fact about the payload, not about reachability.

    A **gateway** saying it could not reach the orchestrator counts too. 502,
    503 and 504 are exactly that answer: the proxy is up, the thing behind it
    is not. Reading them as "it answered, degrade quietly" put the original
    bug straight back on every route sitting behind an nginx, a Traefik or an
    ALB -- the ordinary deployment, not an edge case. Every other status
    carries a fact about the request rather than about reachability.

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
    if isinstance(exc, _AnsweredUnreadably):
        return False
    if isinstance(exc, _RedirectLoop):
        # `requests` attaches the last redirect to this one, so asking whether a
        # response came back does not catch it -- and a loop of redirects never
        # reached the orchestrator either. A test that built this exception by
        # hand, without the response `requests` always attaches, passed for an
        # input that cannot occur and hid exactly this.
        return True
    if isinstance(exc, _RequestFailed) and getattr(exc, "response", None) is None:
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

    def _ask(self, send, *, doing: str, expect: str | None = None, body: bool = True):
        """Every call this client makes goes through here. See
        ``ask_orchestrator``.

        ``enveloped=False``: th2etl answers with the resource itself, so a
        top-level ``error`` belongs to that resource -- a failed run carries the
        exception that killed it.
        """
        return ask_orchestrator(
            send,
            name="th2etl",
            base_url=self.base_url,
            doing=doing,
            expect=expect,
            body=body,
            enveloped=False,
        )

    def _trigger_name(self, agent_id: str) -> str:
        return f"{agent_id}_trigger"

    # --- pipeline lifecycle (orchestrator init) ----------------------------
    def pipeline_exists(self, pipeline_uuid: str) -> bool:
        """Check if a pipeline exists."""
        try:
            self._ask(
                lambda: self._http.get(
                    self._url(f"/pipelines/{pipeline_uuid}"), timeout=self.timeout
                ),
                doing="check whether a pipeline exists",
            )
        except OrchestratorUnavailable as e:
            # "There is no such pipeline" is the question being asked, and a 404
            # is a real answer to it. "I could not reach anyone" is not, and
            # answering `False` to that is how `POST /pipelines/agents/triggers`
            # kept returning a bare 500 while it was believed fixed.
            return degrade_unless_unreachable(e, False, logger, "check a pipeline")
        return True

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
        """Every pipeline th2etl knows about.

        Raises rather than degrading: "unreachable" and "there are none" are
        different answers, and only the caller can act on the difference.
        """
        pipelines = self._ask(
            lambda: self._http.get(self._url("/pipelines/"), timeout=self.timeout),
            doing="list pipelines",
        )
        return _a_list_or_nothing(pipelines, "pipelines", self.base_url)

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
        schedulers = self._ask(
            lambda: self._http.get(self._url("/schedulers/"), timeout=self.timeout),
            doing="list schedules",
        )
        # Iterating a dict here walked its keys, matched nothing, and returned
        # an empty list: an answer we never received, in the shape of one we did.
        _a_list_or_nothing(schedulers, "schedulers", self.base_url)
        out = []
        for s in schedulers:
            if not isinstance(s, dict) or s.get("pipeline_name") != pipeline_uuid:
                continue
            name = s.get("name")
            if name is None:
                # Reading `s["name"]` raised a bare `KeyError` from outside every
                # guard, and four routes answered 500 with no body at all.
                raise OrchestratorUnavailable(
                    f"th2etl answered at {self.base_url} with a scheduler carrying "
                    f"no name",
                    unreachable=False,
                )
            out.append(
                {
                    "id": name,
                    "name": name,
                    "status": "active" if s.get("active", True) else "inactive",
                }
            )
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
            self._ask(
                lambda: self._http.post(
                    self._url("/triggers/"),
                    json={
                        "name": self._trigger_name(agent_id),
                        "pipeline_name": pipeline_uuid,
                        "cron_expression": cron,
                    },
                    timeout=self.timeout,
                ),
                doing="create a trigger",
                body=False,
            )
            self._ask(
                lambda: self._http.post(
                    self._url("/schedulers/"),
                    json={
                        "name": agent_id,
                        "pipeline_name": pipeline_uuid,
                        "trigger_name": self._trigger_name(agent_id),
                        "variables": runtime_variables or {},
                        "active": active,
                    },
                    timeout=self.timeout,
                ),
                doing="create a scheduler",
                body=False,
            )
        except OrchestratorUnavailable as e:
            return degrade_unless_unreachable(
                e, None, logger, "create a schedule trigger"
            )
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
            self._ask(
                lambda: self._http.put(
                    self._url(f"/schedulers/{agent_id}"),
                    json={"active": status == "active"},
                    timeout=self.timeout,
                ),
                doing="update a schedule",
                body=False,
            )
        if schedule_interval is not None:
            cron = interval_to_cron(schedule_interval)
            self._ask(
                lambda: self._http.put(
                    self._url(f"/triggers/{self._trigger_name(agent_id)}"),
                    json={"cron_expression": cron},
                    timeout=self.timeout,
                ),
                doing="update a trigger",
                body=False,
            )
        return {"id": agent_id}

    def update_schedule_variables(self, schedule_id: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Replace a scheduler's runtime variables (token rotation)."""
        self._ask(
            lambda: self._http.put(
                self._url(f"/schedulers/{schedule_id}/variables"),
                json={"variables": variables},
                timeout=self.timeout,
            ),
            doing="update a schedule's variables",
            body=False,
        )
        return {"id": schedule_id}

    def trigger_pipeline_run_for_schedule(
        self, schedule_id: str, run_variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Trigger the scheduler's pipeline now, merging the scheduler's stored
        variables with ``run_variables``."""
        data = self._ask(
            lambda: self._http.post(
                self._url(f"/schedulers/{schedule_id}/run"),
                json={"variables": run_variables or {}},
                timeout=self.timeout,
            ),
            doing="trigger a run",
        )
        # expose both shapes so callers reading "id" or "run_id"/"status" work
        return {
            "id": data.get("run_id"),
            "run_id": data.get("run_id"),
            "status": data.get("status"),
        }

    def trigger_pipeline(
        self, schedule_id: str, trigger_token: str | None = None, run_variables: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Ad-hoc trigger (Mage's API-token variant). th2etl has no trigger
        token; the call maps to the same run-now endpoint."""
        return self.trigger_pipeline_run_for_schedule(schedule_id, run_variables)

    # --- run inspection (dashboard) ----------------------------------------
    def get_pipeline_runs(self, schedule_id: str) -> list:
        """Runs for a scheduler (== agent_id), most recent first."""
        runs = self._ask(
            lambda: self._http.get(
                self._url(f"/schedulers/{schedule_id}/runs"), timeout=self.timeout
            ),
            doing="list runs",
        )
        _a_list_or_nothing(runs, "runs", self.base_url)
        return [_to_mage_run(r) for r in runs]

    def get_pipeline_run(self, run_id: int) -> dict[str, Any] | None:
        try:
            run = self._ask(
                lambda: self._http.get(
                    self._url(f"/runs/{run_id}"), timeout=self.timeout
                ),
                doing="read a run",
            )
        except OrchestratorUnavailable as e:
            # Asking after one run has a legitimate negative answer, and `None`
            # is what the route turns into 404. Not reaching anyone does not.
            return degrade_unless_unreachable(e, None, logger, "read a run")
        return _to_mage_run(_an_object_or_nothing(run, "a run", self.base_url))

    def get_pipeline_run_logs(self, run_id: int) -> list[dict[str, Any]]:
        """Structured, step-by-step log for a run (th2etl-native
        ``GET /runs/{id}/logs``). Each entry is ``{id, run_id, ts, level,
        logger_name, message}`` in chronological order.

        Raises rather than degrading to ``[]``. This docstring used to promise
        the opposite -- "returns [] on error so the dashboard degrades
        gracefully" -- and the dashboard duly stayed up saying the run had
        produced no log, which is a claim about the run and was not true."""
        logs = self._ask(
            lambda: self._http.get(
                self._url(f"/runs/{run_id}/logs"), timeout=self.timeout
            ),
            doing="read a run log",
        )
        return _a_list_or_nothing(logs, "log entries", self.base_url)

    def cancel_pipeline_run(self, run_id: int) -> dict[str, Any] | None:
        try:
            run = self._ask(
                lambda: self._http.post(
                    self._url(f"/runs/{run_id}/cancel"), timeout=self.timeout
                ),
                doing="cancel a run",
            )
        except OrchestratorUnavailable as e:
            return degrade_unless_unreachable(e, None, logger, "cancel a run")
        return _to_mage_run(_an_object_or_nothing(run, "a run", self.base_url))
