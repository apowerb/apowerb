"""ADK's telemetry path must be importable, because deployments enable it.

ADK reads OTEL_EXPORTER_OTLP_ENDPOINT at startup and, when it is set, imports
`opentelemetry.exporter.otlp.proto.http` to build a span exporter. In ADK 2.x
that package moved into the `gcp` / `all` extras, so a dependency set that looks
complete in every test run fails at boot on any host that configures OTEL.

That is exactly how the dev VM went down on 2026-08-06: `uv sync` succeeded,
the service started, and died on
`ModuleNotFoundError: No module named 'opentelemetry.exporter'`. Nothing in the
suite caught it, because the suite never sets the variable.

These tests are cheap and they close that gap: they assert the module is
installed at all, and they walk ADK's own code path with the variable set.
"""
from __future__ import annotations

import importlib

import pytest


def test_the_otlp_http_exporter_is_installed():
    """The single import ADK performs. Missing it costs a boot, not a test."""
    mod = importlib.import_module(
        "opentelemetry.exporter.otlp.proto.http.trace_exporter"
    )
    assert hasattr(mod, "OTLPSpanExporter")


def test_the_log_exporter_is_installed_too():
    """ADK reaches for the log exporter on the same path."""
    mod = importlib.import_module(
        "opentelemetry.exporter.otlp.proto.http._log_exporter"
    )
    assert mod is not None


def test_adk_can_build_its_exporters_when_otel_is_configured(monkeypatch):
    """Walk ADK's own branch rather than trusting that our import matches it.

    A future ADK release could reach for a different exporter module; asserting
    on our own import would keep passing while boot broke again.
    """
    setup = pytest.importorskip("google.adk.telemetry.setup")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")

    getter = getattr(setup, "_get_otel_span_exporter", None)
    if getter is None:
        pytest.skip("ADK no longer exposes _get_otel_span_exporter")

    exporter = getter()
    assert exporter is not None
