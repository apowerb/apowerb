"""Bridge application logs to the collector, when one is configured.

ADK builds a span exporter on its own as soon as
``OTEL_EXPORTER_OTLP_ENDPOINT`` is set, and exports its GenAI spans through
it — tool calls, model calls, durations. Standard Python ``logging`` records
are **not** part of that: FastAPI's lines, this application's lines, every
``setup_logging(__name__)`` call site stay local to the container.

Measured on 04/09/2026 with the full stack running and that variable set:
th2pulse held ``{"count": 0}`` on ``/logs`` after six minutes and several
served requests, while a synthetic OTLP record pushed to the same collector
arrived fine. The pipe was open; nothing was writing to it.

``th2pulse.init_observability`` closes exactly that gap. This module is the
thin, failure-tolerant seam between it and the application:

* **Optional dependency.** th2pulse is an extra (``apowerb[otel]``). Absent,
  this is a no-op with one informative line — a deployment that never wanted
  telemetry must not fail to boot over an import.
* **Opt-in.** No endpoint configured, nothing happens, no cost.
* **Never fatal.** Observability that takes the application down is worse
  than no observability. Every path here swallows its exception and says so.
"""

from __future__ import annotations

import os

from apowerb.configs.th2logger import current_run_id, setup_logging

_logger = setup_logging(__name__)

_ENDPOINT_VARS = ("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "OTEL_EXPORTER_OTLP_ENDPOINT")

_bridged = False


def _configured_endpoint() -> str | None:
    """The endpoint th2pulse would resolve, read the same way it reads it."""
    for var in _ENDPOINT_VARS:
        value = os.getenv(var, "").strip()
        if value:
            return value
    return None


def bridge_logs_to_collector() -> bool:
    """Send this process's log records to the configured OTLP collector.

    Returns ``True`` when the bridge is active, ``False`` when it is
    deliberately off (no endpoint), unavailable (extra not installed) or
    failed. Never raises.

    Call it *after* ADK's own startup: th2pulse reuses the logger provider
    ADK installed rather than fighting it, and that provider does not exist
    until the server has started.
    """
    global _bridged

    if _bridged:
        return True

    endpoint = _configured_endpoint()
    if not endpoint:
        _logger.info("observability: no OTLP endpoint set, application logs stay local")
        return False

    try:
        from th2pulse import init_observability
    except ImportError:
        # Not an error: the extra is optional and most deployments do not
        # install it. Name the remedy rather than the missing symbol.
        _logger.warning(
            "observability: %s is set but th2pulse is not installed, so "
            "application logs are NOT exported (ADK spans still are). "
            "Install the 'otel' extra to bridge them.",
            endpoint,
        )
        return False

    try:
        active = init_observability(os.getenv("OTEL_SERVICE_NAME", "apowerb"))
    except Exception as exc:  # noqa: BLE001 - booting matters more than telemetry
        # th2pulse documents that it never raises, and today it does not. That
        # is its promise, not ours: this module promises the application boots
        # regardless, so it enforces it here rather than inheriting it.
        _logger.warning("observability: bridge init failed, continuing: %s", exc)
        return False
    if active:
        _bridged = True
        _logger.info(
            "observability: application logs bridged to %s (run %s)",
            endpoint,
            current_run_id(),
        )
    else:
        # init_observability already warned; do not claim success.
        _logger.warning("observability: th2pulse declined to start the bridge")
    return active


def flush_observability(timeout_millis: int = 5000) -> None:
    """Push what is buffered before the process goes away. Never raises.

    Records are batched, so a shutdown without this loses the last window —
    which is precisely the window that explains why a container stopped.
    """
    if not _bridged:
        return
    try:
        from th2pulse import force_flush

        force_flush(timeout_millis)
    except Exception as exc:  # noqa: BLE001 - shutdown must not fail
        _logger.warning("observability: flush on shutdown failed: %s", exc)


__all__ = ["bridge_logs_to_collector", "flush_observability"]
