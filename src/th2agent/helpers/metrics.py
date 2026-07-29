"""Prometheus metrics facade (B19).

Wraps ``prometheus_client`` behind a small API so the rest of the
codebase only imports this module. When ``prometheus_client`` is not
installed, every helper becomes a no-op (``record_request`` does
nothing, ``make_metrics_asgi_app`` returns a minimal ASGI callable
that responds with a clear message). Production deployments MUST
install ``prometheus-client`` — dev setups can skip it.

Metric cardinality is kept low on purpose:

- ``path`` is normalized through :func:`normalize_path` so arbitrary
  IDs in URL params (``/agents/{id}``) collapse to a template. The
  set of exposed templates is capped at ``_MAX_PATH_CARDINALITY``;
  anything beyond that bucket falls into ``"/other"``.
- No ``user_id`` label on request metrics — that's the job of logs.
"""

from __future__ import annotations

import re
from typing import Optional

try:
    from prometheus_client import (  # type: ignore
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        make_asgi_app,
        generate_latest,
        CONTENT_TYPE_LATEST,
    )

    _PROMETHEUS_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised in environments without the lib
    _PROMETHEUS_AVAILABLE = False
    CollectorRegistry = None  # type: ignore
    Counter = Gauge = Histogram = None  # type: ignore
    make_asgi_app = None  # type: ignore
    generate_latest = None  # type: ignore
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4; charset=utf-8"


# ---------------------------------------------------------------------------
# Registry + metrics (module-level singletons, rebuildable for tests)
# ---------------------------------------------------------------------------

_registry: Optional["CollectorRegistry"] = None
_metrics: dict = {}
_MAX_PATH_CARDINALITY = 50
_seen_paths: set[str] = set()


def _build_metrics() -> None:
    """(Re)create the shared registry + metric collectors."""
    global _registry, _metrics, _seen_paths
    if not _PROMETHEUS_AVAILABLE:
        _registry = None
        _metrics = {}
        _seen_paths = set()
        return

    _registry = CollectorRegistry()
    _seen_paths = set()
    _metrics = {
        "requests_total": Counter(
            "th2agent_requests_total",
            "Total HTTP requests processed",
            ["method", "path", "status"],
            registry=_registry,
        ),
        "request_duration_seconds": Histogram(
            "th2agent_request_duration_seconds",
            "HTTP request duration in seconds",
            ["method", "path"],
            registry=_registry,
        ),
        "agent_runs_total": Counter(
            "th2agent_agent_runs_total",
            "Total agent runs executed",
            ["agent_id", "status"],
            registry=_registry,
        ),
        "llm_tokens_total": Counter(
            "th2agent_llm_tokens_total",
            "Total LLM tokens consumed",
            ["model", "type"],
            registry=_registry,
        ),
        "active_sessions": Gauge(
            "th2agent_active_sessions",
            "Number of active ADK sessions",
            registry=_registry,
        ),
    }


_build_metrics()


def reset_metrics_for_tests() -> None:
    """Rebuild the registry + metrics (unit-test helper)."""
    _build_metrics()


def is_enabled() -> bool:
    return _PROMETHEUS_AVAILABLE


# ---------------------------------------------------------------------------
# Path normalization — kept deterministic and small
# ---------------------------------------------------------------------------

_HEX_RE = re.compile(r"^[0-9a-fA-F]{8,}$")
_NUM_RE = re.compile(r"^\d+$")
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def normalize_path(path: str) -> str:
    """Collapse variable URL segments to a stable template.

    ``/api/agents/xyz-123`` → ``/api/agents/{id}``
    ``/api/adk/sessions/a/b/c``     → ``/api/adk/sessions/{id}/{id}/{id}``

    The set of distinct normalized paths is capped at
    ``_MAX_PATH_CARDINALITY``. Paths beyond that fall into ``/other``.
    """
    segments = path.split("?", 1)[0].rstrip("/").split("/")
    normalized = []
    for seg in segments:
        if not seg:
            normalized.append("")
            continue
        if _NUM_RE.match(seg) or _UUID_RE.match(seg) or _HEX_RE.match(seg):
            normalized.append("{id}")
        else:
            normalized.append(seg)
    result = "/".join(normalized) or "/"

    if result in _seen_paths:
        return result
    if len(_seen_paths) >= _MAX_PATH_CARDINALITY:
        return "/other"
    _seen_paths.add(result)
    return result


# ---------------------------------------------------------------------------
# Recording helpers
# ---------------------------------------------------------------------------


def record_request(method: str, path: str, status: int, duration_s: float) -> None:
    if not _PROMETHEUS_AVAILABLE or not _metrics:
        return
    normalized = normalize_path(path)
    _metrics["requests_total"].labels(
        method=method, path=normalized, status=str(status)
    ).inc()
    _metrics["request_duration_seconds"].labels(
        method=method, path=normalized
    ).observe(duration_s)


def record_agent_run(agent_id: str, status: str) -> None:
    if not _PROMETHEUS_AVAILABLE or not _metrics:
        return
    _metrics["agent_runs_total"].labels(
        agent_id=agent_id, status=status
    ).inc()


def record_llm_tokens(model: str, token_type: str, count: int) -> None:
    if not _PROMETHEUS_AVAILABLE or not _metrics or count <= 0:
        return
    _metrics["llm_tokens_total"].labels(
        model=model, type=token_type
    ).inc(count)


def set_active_sessions(count: int) -> None:
    if not _PROMETHEUS_AVAILABLE or not _metrics:
        return
    _metrics["active_sessions"].set(count)


# ---------------------------------------------------------------------------
# ASGI endpoint
# ---------------------------------------------------------------------------


def make_metrics_asgi_app():
    """Return an ASGI app exposing the current registry."""
    if _PROMETHEUS_AVAILABLE and _registry is not None:
        return make_asgi_app(registry=_registry)

    async def _unavailable(scope, receive, send):  # pragma: no cover - trivial
        body = b"prometheus_client is not installed"
        await send(
            {
                "type": "http.response.start",
                "status": 501,
                "headers": [(b"content-type", b"text/plain")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return _unavailable


__all__ = [
    "is_enabled",
    "normalize_path",
    "record_request",
    "record_agent_run",
    "record_llm_tokens",
    "set_active_sessions",
    "make_metrics_asgi_app",
    "reset_metrics_for_tests",
]
