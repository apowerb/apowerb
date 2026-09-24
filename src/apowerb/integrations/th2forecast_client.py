"""Client for the th2forecast service (lot A of the forecast-tool contract).

Submits a forecast request via th2forecast's job API — ``POST /v1/jobs`` then
polling ``GET /v1/jobs/{id}`` with progressive backoff — up to a global
``TH2FORECAST_TIMEOUT_S`` deadline. Falls back to the synchronous
``POST /v1/forecast`` when ``/v1/jobs`` answers 404 (older th2forecast
deployment without the job API).

th2forecast's own errors (400/401/413) are relayed with their original status
code and JSON body untouched — see ``~/tmp/th2fc/CONTRAT.md`` for the exact
shape. Network failures (unreachable, timeout, unparsable body) raise
``Th2forecastUnavailable``/``Th2forecastTimeout`` with a message that never
includes the bearer token, even on error.

This module deliberately has no other apowerb imports besides ``get_settings``,
so it can be unit-tested with ``requests`` mocked in isolation.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

from apowerb.configs.settings import get_settings

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT_S = 300
# th2forecast's own error responses (see contract): relayed as-is, never
# turned into a generic "unavailable".
_RELAYED_ERROR_STATUSES = frozenset({400, 401, 413})
# Progressive backoff between job polls: starts fast, caps at 10s so a long
# forecast does not hammer th2forecast every second.
_POLL_BACKOFF_START_S = 1.0
_POLL_BACKOFF_CAP_S = 10.0
_POLL_BACKOFF_FACTOR = 1.7
# Per-HTTP-call timeout (submit / poll / sync fallback). Independent from the
# global job deadline: a single hung TCP call must not silently eat the whole
# budget without ever giving the poll loop a chance to time out cleanly.
_HTTP_CALL_TIMEOUT_S = 30


class Th2forecastNotConfigured(RuntimeError):
    """``TH2FORECAST_URL`` is not set — the caller should answer 503."""


class Th2forecastAPIError(RuntimeError):
    """th2forecast answered with a business error (400/401/413/failed job).

    ``status_code`` and ``body`` are th2forecast's own — relay them to the
    apowerb caller untouched rather than re-wrapping them.
    """

    def __init__(self, status_code: int, body: Any):
        self.status_code = status_code
        self.body = body
        super().__init__(f"th2forecast answered {status_code}")


class Th2forecastUnavailable(RuntimeError):
    """th2forecast could not be reached, or answered something unreadable.

    The message is built without the token or the raw request body, so it is
    safe to log and to return to a caller.
    """


class Th2forecastTimeout(Th2forecastUnavailable):
    """The global TH2FORECAST_TIMEOUT_S deadline was reached while polling."""


def _headers(token: str | None) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _redact(url: str) -> str:
    """Drop query string / fragment so a token pasted into the URL by mistake
    never reaches a log line. th2forecast is bearer-header auth only, so this
    is a defensive floor, not the primary safeguard."""
    return url.split("?", 1)[0].split("#", 1)[0]


def _safe_body_preview(response: requests.Response) -> str:
    """A short, token-free preview of an unreadable response body, for the
    unavailable-error message. Never includes request headers."""
    try:
        text = response.text
    except Exception:  # noqa: BLE001 -- best-effort preview for an error message, never re-raised
        return "<body unavailable>"
    text = text.strip()
    if len(text) > 300:
        text = text[:300] + "…"
    return text or "<empty body>"


def _parse_json_or_raise(response: requests.Response, *, base_url: str) -> Any:
    try:
        return response.json()
    except ValueError as exc:
        raise Th2forecastUnavailable(
            f"th2forecast at {_redact(base_url)} answered {response.status_code} "
            f"with a body that would not parse as JSON: {_safe_body_preview(response)}"
        ) from exc


def _raise_for_relayed_error(response: requests.Response, *, base_url: str) -> None:
    """Raise Th2forecastAPIError for the error statuses the contract defines
    (400/401/413), relaying th2forecast's JSON body untouched. Any other
    non-2xx status is treated as "th2forecast is broken", not a business
    error, and raises Th2forecastUnavailable instead."""
    if response.status_code in _RELAYED_ERROR_STATUSES:
        raise Th2forecastAPIError(response.status_code, _parse_json_or_raise(response, base_url=base_url))
    if response.status_code >= 400:
        raise Th2forecastUnavailable(
            f"th2forecast at {_redact(base_url)} answered an unexpected "
            f"{response.status_code}: {_safe_body_preview(response)}"
        )


class Th2forecastClient:
    """HTTP client for th2forecast's job-based forecast API."""

    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout_s: float | None = None,
    ) -> None:
        # get_settings() is only called when an argument is left to the
        # server config, so a caller that supplies all three (as tests do)
        # never needs a real Settings() -- no env parsing, no DB.
        needs_settings = base_url is None or token is None or timeout_s is None
        settings = get_settings() if needs_settings else None

        self.base_url = (base_url if base_url is not None else settings.th2forecast_url).rstrip("/")
        self.token = token if token is not None else (settings.th2forecast_api_token or None)
        self.timeout_s = (
            timeout_s if timeout_s is not None else (settings.th2forecast_timeout_s or _DEFAULT_TIMEOUT_S)
        )
        if not self.base_url:
            raise Th2forecastNotConfigured("TH2FORECAST_URL is not configured")

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    def _post(self, path: str, payload: dict[str, Any]) -> requests.Response:
        try:
            return requests.post(
                self._url(path),
                json=payload,
                headers=_headers(self.token),
                timeout=_HTTP_CALL_TIMEOUT_S,
            )
        except requests.exceptions.Timeout as exc:
            raise Th2forecastUnavailable(
                f"th2forecast at {_redact(self.base_url)} did not answer {path} in time"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise Th2forecastUnavailable(
                f"th2forecast at {_redact(self.base_url)} is unreachable: {exc.__class__.__name__}"
            ) from exc

    def _get(self, path: str) -> requests.Response:
        try:
            return requests.get(
                self._url(path),
                headers=_headers(self.token),
                timeout=_HTTP_CALL_TIMEOUT_S,
            )
        except requests.exceptions.Timeout as exc:
            raise Th2forecastUnavailable(
                f"th2forecast at {_redact(self.base_url)} did not answer {path} in time"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise Th2forecastUnavailable(
                f"th2forecast at {_redact(self.base_url)} is unreachable: {exc.__class__.__name__}"
            ) from exc

    def forecast(self, request_body: dict[str, Any]) -> dict[str, Any]:
        """Run a forecast and return th2forecast's response body.

        Tries the job API first (``POST /v1/jobs`` + poll); falls back to the
        synchronous ``POST /v1/forecast`` if ``/v1/jobs`` is not found (404).
        """
        deadline = time.monotonic() + self.timeout_s

        submit = self._post("/v1/jobs", request_body)
        if submit.status_code == 404:
            return self._forecast_sync(request_body)

        _raise_for_relayed_error(submit, base_url=self.base_url)
        if submit.status_code != 202:
            raise Th2forecastUnavailable(
                f"th2forecast at {_redact(self.base_url)} answered an unexpected "
                f"{submit.status_code} to POST /v1/jobs: {_safe_body_preview(submit)}"
            )

        submitted = _parse_json_or_raise(submit, base_url=self.base_url)
        job_id = submitted.get("job_id") if isinstance(submitted, dict) else None
        if not job_id:
            raise Th2forecastUnavailable(
                f"th2forecast at {_redact(self.base_url)} accepted the job but did "
                f"not return a job_id"
            )

        return self._poll_job(job_id, deadline=deadline)

    def _forecast_sync(self, request_body: dict[str, Any]) -> dict[str, Any]:
        response = self._post("/v1/forecast", request_body)
        _raise_for_relayed_error(response, base_url=self.base_url)
        if response.status_code != 200:
            raise Th2forecastUnavailable(
                f"th2forecast at {_redact(self.base_url)} answered an unexpected "
                f"{response.status_code} to POST /v1/forecast: {_safe_body_preview(response)}"
            )
        return _parse_json_or_raise(response, base_url=self.base_url)

    def _poll_job(self, job_id: str, *, deadline: float) -> dict[str, Any]:
        backoff = _POLL_BACKOFF_START_S
        while True:
            response = self._get(f"/v1/jobs/{job_id}")
            _raise_for_relayed_error(response, base_url=self.base_url)
            if response.status_code != 200:
                raise Th2forecastUnavailable(
                    f"th2forecast at {_redact(self.base_url)} answered an unexpected "
                    f"{response.status_code} to GET /v1/jobs/{job_id}: "
                    f"{_safe_body_preview(response)}"
                )

            job = _parse_json_or_raise(response, base_url=self.base_url)
            status = job.get("status") if isinstance(job, dict) else None

            if status == "succeeded":
                result = job.get("result")
                if result is None:
                    raise Th2forecastUnavailable(
                        f"th2forecast job {job_id} succeeded without a result"
                    )
                return result

            if status == "failed":
                error = job.get("error")
                # Shape it like the contract's 400 body so callers have one
                # error format to handle, whether it came from a submit-time
                # 400 or an async job failure. `error` may already be a full
                # {"status", "errors"} envelope, a single {"field", "message"}
                # entry, a list of such entries, or anything else th2forecast
                # decided to put there.
                if isinstance(error, dict) and "errors" in error:
                    body = error
                elif isinstance(error, dict) and ("field" in error or "message" in error):
                    body = {"status": "error", "errors": [error]}
                elif isinstance(error, list):
                    body = {"status": "error", "errors": error}
                else:
                    message = str(error) if error else "Le job de prévision a échoué"
                    body = {"status": "error", "errors": [{"field": None, "message": message}]}
                raise Th2forecastAPIError(400, body)

            if status not in ("queued", "running"):
                logger.warning(
                    "th2forecast job %s has unexpected status %r; treating as still running",
                    job_id, status,
                )

            if time.monotonic() >= deadline:
                raise Th2forecastTimeout(
                    f"th2forecast job {job_id} did not finish within "
                    f"{self.timeout_s:.0f}s"
                )

            sleep_for = min(backoff, _POLL_BACKOFF_CAP_S, max(0.0, deadline - time.monotonic()))
            if sleep_for > 0:
                time.sleep(sleep_for)
            backoff = min(backoff * _POLL_BACKOFF_FACTOR, _POLL_BACKOFF_CAP_S)

            if time.monotonic() >= deadline:
                raise Th2forecastTimeout(
                    f"th2forecast job {job_id} did not finish within "
                    f"{self.timeout_s:.0f}s"
                )
