"""Logging configuration (B19).

Two formats are supported:

- ``json``  : machine-readable, one JSON object per log record with
  a stable schema ``{timestamp, level, logger, message, module, line,
  ...extras}``. Use in staging/production so log pipelines can index
  fields reliably.
- ``text``  : colourless human-friendly format for local dev.

The legacy ``setup_logging(module_name)`` helper is preserved for
backward-compatibility with existing call-sites (~80 imports across
the codebase); it now delegates to the new structured logger but only
re-configures root once per process.

Extras propagation: any field passed via ``logger.info("msg", extra={...})``
is merged into the JSON record, as are contextvars like the request id.

Two things make a container log stream readable when it is the only
window onto a deployment:

- **Run separation.** ``docker logs`` concatenates restarts, so an error
  from the run before a redeploy reads exactly like a fresh one. Every
  line carries a ``run_id``, and every start opens with a banner.
  ``TH2_LOG_RUN_ID`` prefixes that id with a release or deployment id
  when you set one; the random suffix is what keeps two starts of the
  same deployment apart.
- **Local time.** Servers run on UTC and people do not. ``TH2_LOG_TZ``
  (falling back to ``TZ``) decides the zone the text format prints, and
  the offset is always shown so a line is never ambiguous. The JSON
  ``timestamp`` stays UTC -- pipelines index on it -- and the local
  reading is added alongside as ``timestamp_local``.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone, tzinfo
from logging import Logger, getLogger
from typing import Any, Optional, TextIO
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ---------------------------------------------------------------------------
# JSON formatter
# ---------------------------------------------------------------------------

# Standard ``LogRecord`` attributes we don't want to duplicate in the JSON
# body. Any attribute attached via ``extra=`` that isn't in this set is
# forwarded verbatim.
_RESERVED_LOGRECORD_KEYS = {
    "name",
    "msg",
    "args",
    "levelname",
    "levelno",
    "pathname",
    "filename",
    "module",
    "exc_info",
    "exc_text",
    "stack_info",
    "lineno",
    "funcName",
    "created",
    "msecs",
    "relativeCreated",
    "thread",
    "threadName",
    "processName",
    "process",
    "message",
    "asctime",
    "taskName",
}


# ---------------------------------------------------------------------------
# Run identity and display zone
# ---------------------------------------------------------------------------

_RUN_ID: Optional[str] = None


def current_run_id() -> str:
    """The id every line of this run carries. One per process.

    ``TH2_LOG_RUN_ID`` is a *prefix*, not the whole id: set it to a release
    or deployment id and the stream stays searchable months later, while a
    random suffix still separates two starts of the same deployment. A
    fixed id would leave a plain restart indistinguishable -- measured on
    04/09/2026, and that is the defect this whole field exists to fix.
    """
    global _RUN_ID
    if _RUN_ID is None:
        suffix = uuid.uuid4().hex[:8]
        prefix = os.getenv("TH2_LOG_RUN_ID", "").strip()
        _RUN_ID = f"{prefix}.{suffix[:6]}" if prefix else suffix
    return _RUN_ID


def display_timezone() -> tzinfo:
    """Zone the human-readable timestamp is printed in.

    ``TH2_LOG_TZ`` wins, then the conventional ``TZ``; UTC when neither is
    set. An unknown zone falls back to UTC rather than raising: a logger
    that dies on a typo takes the application down with it, and the
    offset printed on every line makes the fallback visible.
    """
    for var in ("TH2_LOG_TZ", "TZ"):
        name = os.getenv(var, "").strip()
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            continue
    return timezone.utc


def _local_isoformat(created: float, tz: tzinfo) -> str:
    """Wall-clock time in ``tz``, seconds resolution, offset always shown."""
    return datetime.fromtimestamp(created, tz=tz).isoformat(timespec="seconds")


def _current_request_id() -> Optional[str]:
    """Lazy import to avoid circular dependency on the middleware."""
    try:
        from apowerb.helpers.request_id_middleware import get_request_id

        return get_request_id()
    except Exception:  # pragma: no cover - defensive
        return None


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record.

    ``timestamp`` stays UTC whatever the display zone is: it is the field
    log pipelines sort and index on, and moving it would silently reorder
    history. ``timestamp_local`` carries the reading a human wants.
    """

    def __init__(self, tz: Optional[tzinfo] = None, run_id: Optional[str] = None):
        super().__init__()
        self._tz = tz or timezone.utc
        self._run_id = run_id or current_run_id()

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "timestamp_local": _local_isoformat(record.created, self._tz),
            "run_id": self._run_id,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "line": record.lineno,
        }

        request_id = _current_request_id()
        if request_id:
            payload["request_id"] = request_id

        # Forward any extras the caller attached to the record.
        for key, value in record.__dict__.items():
            if key in _RESERVED_LOGRECORD_KEYS or key.startswith("_"):
                continue
            if key in payload:
                continue
            payload[key] = _serialize(value)

        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str, ensure_ascii=False)


def _serialize(value: Any) -> Any:
    """Best-effort JSON-friendly coercion for extras."""
    if isinstance(value, (str, int, float, bool, type(None))):
        return value
    if isinstance(value, (list, tuple)):
        return [_serialize(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _serialize(v) for k, v in value.items()}
    return str(value)


class LocalTimeFormatter(logging.Formatter):
    """Human-readable lines, stamped in the display zone and tagged by run.

    ``logging.Formatter`` renders ``%(asctime)s`` through ``time.localtime``,
    which is the process's zone -- UTC on every server here. Formatting the
    timestamp ourselves is what lets the zone be a setting rather than a
    property of the host.
    """

    _TEMPLATE = "%s | run %s | %-8s | %s:%d | %s"

    def __init__(self, tz: Optional[tzinfo] = None, run_id: Optional[str] = None):
        super().__init__()
        self._tz = tz or timezone.utc
        self._run_id = run_id or current_run_id()

    def format(self, record: logging.LogRecord) -> str:
        line = self._TEMPLATE % (
            _local_isoformat(record.created, self._tz),
            self._run_id,
            record.levelname,
            record.name,
            record.lineno,
            record.getMessage(),
        )
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


# ---------------------------------------------------------------------------
# Public configuration entry points
# ---------------------------------------------------------------------------

_CONFIGURED = False

_BANNER_LOGGER = "apowerb.run"
_BANNERED_RUN: Optional[str] = None


def _emit_run_banner(root: Logger, *, run_id: str, tz: tzinfo, fmt: str) -> None:
    """Open the stream with a line that says which run starts here.

    Emitted through the logger itself, so it obeys the format in force: a
    JSON deployment gets a parseable record, not a decorative separator
    that breaks the pipeline on the very first line.

    Once per run, not once per call: startup reconfigures root more than
    once in practice (measured -- two banners in a single boot), and a
    separator that repeats inside one run separates nothing.
    """
    global _BANNERED_RUN
    if _BANNERED_RUN == run_id:
        return
    _BANNERED_RUN = run_id

    started = _local_isoformat(datetime.now(tz=timezone.utc).timestamp(), tz)
    getLogger(_BANNER_LOGGER).log(
        logging.INFO,
        "run started - run_id=%s at %s (zone %s, format %s)",
        run_id,
        started,
        getattr(tz, "key", "UTC"),
        fmt,
        extra={"event": "run_started", "log_format": fmt},
    )


def configure_structured_logging(
    *,
    level: int = logging.INFO,
    fmt: Optional[str] = None,
    stream: Optional[TextIO] = None,
) -> None:
    """Configure the root logger once.

    ``fmt`` is ``"json"`` or ``"text"``. When ``None``, the format is
    picked from the environment: ``TH2_LOG_FORMAT`` (wins) → else
    ``WORKING_MODE`` (``production``/``staging`` → json, everything
    else → text).

    Calling twice replaces the handler — safe for test re-entry, safe
    for accidental double-init.
    """
    global _CONFIGURED

    if fmt is None:
        env_fmt = os.getenv("TH2_LOG_FORMAT", "").strip().lower()
        if env_fmt in {"json", "text"}:
            fmt = env_fmt
        else:
            mode = os.getenv("WORKING_MODE", "development").strip().lower()
            fmt = "json" if mode in {"production", "prod", "staging"} else "text"

    root = logging.getLogger()
    # Drop existing handlers so we don't double-emit.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    tz = display_timezone()
    run_id = current_run_id()

    handler = logging.StreamHandler(stream=stream or sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter(tz=tz, run_id=run_id))
    else:
        handler.setFormatter(LocalTimeFormatter(tz=tz, run_id=run_id))
    root.addHandler(handler)
    root.setLevel(level)
    _CONFIGURED = True

    _emit_run_banner(root, run_id=run_id, tz=tz, fmt=fmt)


def setup_logging(
    module_name: str,
    level: int = logging.INFO,
    module_levels: Optional[dict] = None,
) -> Logger:
    """Backward-compatible shim.

    Configures the root logger on first call (respecting env vars), then
    simply returns ``logging.getLogger(module_name)``. Existing call-sites
    like ``_logger = setup_logging(__name__)`` keep working unchanged.
    """
    if not _CONFIGURED:
        configure_structured_logging(level=level)
    if module_levels:
        for module, module_level in module_levels.items():
            logging.getLogger(module).setLevel(module_level)
    return getLogger(module_name)


__all__ = [
    "JsonFormatter",
    "LocalTimeFormatter",
    "configure_structured_logging",
    "current_run_id",
    "display_timezone",
    "setup_logging",
]
