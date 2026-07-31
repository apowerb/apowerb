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
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from logging import Logger, getLogger
from typing import Any, Optional, TextIO

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


def _current_request_id() -> Optional[str]:
    """Lazy import to avoid circular dependency on the middleware."""
    try:
        from apowerb.helpers.request_id_middleware import get_request_id

        return get_request_id()
    except Exception:  # pragma: no cover - defensive
        return None


class JsonFormatter(logging.Formatter):
    """Emit one JSON object per log record."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
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


# ---------------------------------------------------------------------------
# Public configuration entry points
# ---------------------------------------------------------------------------

_CONFIGURED = False


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

    handler = logging.StreamHandler(stream=stream or sys.stdout)
    if fmt == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter(
                "%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
        )
    root.addHandler(handler)
    root.setLevel(level)
    _CONFIGURED = True


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
    "configure_structured_logging",
    "setup_logging",
]
