"""Contract of the log bridge: opt-in, optional, and never fatal.

Defect measured on 04/09/2026: with OTEL_EXPORTER_OTLP_ENDPOINT set and the
whole stack up, th2pulse held ``{"count": 0}`` on /logs — ADK exported its
spans, application logs went nowhere, because nothing ever called
``th2pulse.init_observability``.

The rules below are what keeps closing that gap from becoming a new way to
fail at boot.
"""

from __future__ import annotations

import sys
import types

import pytest

from apowerb.configs import observability


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    for var in (
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
        "OTEL_SERVICE_NAME",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(observability, "_bridged", False, raising=False)
    yield


def _fake_th2pulse(monkeypatch, *, init=None, flush=None):
    """Stand in for the optional extra without installing it."""
    module = types.ModuleType("th2pulse")
    module.init_observability = init or (lambda *a, **k: True)
    module.force_flush = flush or (lambda *a, **k: None)
    monkeypatch.setitem(sys.modules, "th2pulse", module)
    return module


class TestOptIn:
    def test_no_endpoint_means_no_bridge(self):
        assert observability.bridge_logs_to_collector() is False

    def test_an_endpoint_is_enough_to_bridge(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
        _fake_th2pulse(monkeypatch)
        assert observability.bridge_logs_to_collector() is True

    def test_the_logs_specific_endpoint_also_counts(self, monkeypatch):
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "http://collector:4318/v1/logs"
        )
        _fake_th2pulse(monkeypatch)
        assert observability.bridge_logs_to_collector() is True

    def test_a_blank_endpoint_designates_nothing(self, monkeypatch):
        # A stray `OTEL_EXPORTER_OTLP_ENDPOINT=` in an env file must not be
        # read as "export to the empty string".
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "   ")
        _fake_th2pulse(monkeypatch)
        assert observability.bridge_logs_to_collector() is False


class TestOptionalDependency:
    def test_a_missing_extra_is_not_a_boot_failure(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
        monkeypatch.setitem(sys.modules, "th2pulse", None)  # forces ImportError
        assert observability.bridge_logs_to_collector() is False

    def test_a_missing_extra_says_so_out_loud(self, monkeypatch, caplog):
        # Silence here is the failure mode that cost a day: the endpoint is
        # set, the operator believes logs flow, and they do not.
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
        monkeypatch.setitem(sys.modules, "th2pulse", None)
        with caplog.at_level("WARNING"):
            observability.bridge_logs_to_collector()
        assert "th2pulse is not installed" in caplog.text


class TestNeverFatal:
    def test_an_exploding_init_does_not_propagate(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")

        def boom(*a, **k):
            raise RuntimeError("collector unreachable")

        _fake_th2pulse(monkeypatch, init=boom)
        with pytest.raises(RuntimeError):
            boom()  # the stand-in really does raise
        assert observability.bridge_logs_to_collector() is False

    def test_a_declined_init_is_not_reported_as_success(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
        _fake_th2pulse(monkeypatch, init=lambda *a, **k: False)
        assert observability.bridge_logs_to_collector() is False

    def test_flush_is_silent_when_nothing_was_bridged(self):
        observability.flush_observability()  # must not raise

    def test_an_exploding_flush_does_not_break_shutdown(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")

        def boom(*a, **k):
            raise RuntimeError("flush failed")

        _fake_th2pulse(monkeypatch, flush=boom)
        observability.bridge_logs_to_collector()
        observability.flush_observability()  # must not raise


class TestBehaviour:
    def test_the_service_name_reaches_th2pulse(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
        monkeypatch.setenv("OTEL_SERVICE_NAME", "apowerb-dev")
        seen = {}
        _fake_th2pulse(
            monkeypatch, init=lambda name, **k: seen.setdefault("name", name) or True
        )
        observability.bridge_logs_to_collector()
        assert seen["name"] == "apowerb-dev"

    def test_bridging_twice_initialises_once(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
        calls = []
        _fake_th2pulse(monkeypatch, init=lambda *a, **k: calls.append(1) or True)
        observability.bridge_logs_to_collector()
        observability.bridge_logs_to_collector()
        assert len(calls) == 1

    def test_flush_reaches_th2pulse_once_bridged(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
        flushed = []
        _fake_th2pulse(monkeypatch, flush=lambda ms: flushed.append(ms))
        observability.bridge_logs_to_collector()
        observability.flush_observability(1234)
        assert flushed == [1234]
