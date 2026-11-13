"""Contract of the Langfuse export: opt-in, optional, and never fatal.

The core is public and deployed where no Langfuse is reachable. Without its
three variables, this must do nothing at all — not even import the SDK.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import sys
import types

import pytest
from google.adk.runners import Runner

from apowerb.configs import langfuse_tracing

_VARS = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST")


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    for var in _VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(langfuse_tracing, "_client", None)
    langfuse_tracing._open_call_llm_spans.clear()
    # enable_langfuse_tracing wraps Runner.run_async: undo it after each test.
    monkeypatch.setattr(Runner, "run_async", Runner.run_async)
    monkeypatch.setattr(langfuse_tracing, "_run_async_wrapped", False)
    yield


def _configure(monkeypatch):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test-secret")
    monkeypatch.setenv("LANGFUSE_HOST", "http://langfuse.test:3000")


_propagated: contextvars.ContextVar = contextvars.ContextVar("propagated", default=None)


def _fake_sdk(monkeypatch, *, init_error=None, propagate_error=None):
    """Stand in for the optional extra and record how it was built."""
    built = {}

    @contextlib.contextmanager
    def propagate_attributes(**kwargs):
        if propagate_error is not None:
            raise propagate_error
        token = _propagated.set(kwargs)
        try:
            yield
        finally:
            _propagated.reset(token)

    class Langfuse:
        def __init__(self, **kwargs):
            if init_error is not None:
                raise init_error
            built.update(kwargs)
            self.shut_down = False

        def shutdown(self):
            self.shut_down = True

    sdk = types.ModuleType("langfuse")
    sdk.Langfuse = Langfuse
    sdk.propagate_attributes = propagate_attributes
    span_filter = types.ModuleType("langfuse.span_filter")
    span_filter.is_default_export_span = lambda span: bool(
        getattr(span, "genai", False)
    )
    monkeypatch.setitem(sys.modules, "langfuse", sdk)
    monkeypatch.setitem(sys.modules, "langfuse.span_filter", span_filter)
    return built


def _span(scope_name, *, genai=False, name="span", span_id=1, parent_id=None):
    return types.SimpleNamespace(
        instrumentation_scope=types.SimpleNamespace(name=scope_name),
        genai=genai,
        name=name,
        context=types.SimpleNamespace(span_id=span_id),
        parent=None if parent_id is None else types.SimpleNamespace(span_id=parent_id),
    )


class TestOptIn:
    def test_no_configuration_means_no_export_and_no_import(self, monkeypatch):
        # A poisoned module proves the SDK is not even imported.
        monkeypatch.setitem(sys.modules, "langfuse", None)
        assert langfuse_tracing.enable_langfuse_tracing() is False

    def test_blank_values_count_as_absent(self, monkeypatch):
        for var in _VARS:
            monkeypatch.setenv(var, "  ")
        monkeypatch.setitem(sys.modules, "langfuse", None)
        assert langfuse_tracing.enable_langfuse_tracing() is False

    def test_incomplete_configuration_names_what_is_missing(self, monkeypatch, caplog):
        monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
        monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test-secret")
        _fake_sdk(monkeypatch)
        with caplog.at_level("WARNING"):
            assert langfuse_tracing.enable_langfuse_tracing() is False
        assert "LANGFUSE_HOST" in caplog.text
        assert "sk-lf-test-secret" not in caplog.text

    def test_full_configuration_builds_the_client(self, monkeypatch):
        _configure(monkeypatch)
        built = _fake_sdk(monkeypatch)
        assert langfuse_tracing.enable_langfuse_tracing() is True
        assert built["base_url"] == "http://langfuse.test:3000"
        assert built["public_key"] == "pk-lf-test"
        assert built["secret_key"] == "sk-lf-test-secret"

    def test_a_second_call_does_not_build_a_second_client(self, monkeypatch):
        _configure(monkeypatch)
        _fake_sdk(monkeypatch)
        assert langfuse_tracing.enable_langfuse_tracing() is True
        first = langfuse_tracing._client
        assert langfuse_tracing.enable_langfuse_tracing() is True
        assert langfuse_tracing._client is first


class TestOptionalAndNeverFatal:
    def test_a_missing_extra_is_not_a_boot_failure(self, monkeypatch, caplog):
        _configure(monkeypatch)
        monkeypatch.setitem(sys.modules, "langfuse", None)
        with caplog.at_level("WARNING"):
            assert langfuse_tracing.enable_langfuse_tracing() is False
        assert "'langfuse' extra" in caplog.text

    def test_a_failing_client_is_not_a_boot_failure(self, monkeypatch, caplog):
        _configure(monkeypatch)
        _fake_sdk(monkeypatch, init_error=RuntimeError("boom"))
        with caplog.at_level("WARNING"):
            assert langfuse_tracing.enable_langfuse_tracing() is False
        assert langfuse_tracing._client is None
        assert "sk-lf-test-secret" not in caplog.text

    def test_flush_without_client_is_a_no_op(self):
        langfuse_tracing.flush_langfuse_tracing()

    def test_flush_shuts_the_client_down_once(self, monkeypatch):
        _configure(monkeypatch)
        _fake_sdk(monkeypatch)
        langfuse_tracing.enable_langfuse_tracing()
        client = langfuse_tracing._client
        langfuse_tracing.flush_langfuse_tracing()
        assert client.shut_down is True
        assert langfuse_tracing._client is None

    def test_a_failing_flush_does_not_raise(self, monkeypatch):
        class Broken:
            def shutdown(self):
                raise RuntimeError("network down")

        monkeypatch.setattr(langfuse_tracing, "_client", Broken())
        langfuse_tracing.flush_langfuse_tracing()
        assert langfuse_tracing._client is None


class TestExportFilter:
    def test_every_adk_span_is_exported(self, monkeypatch):
        _configure(monkeypatch)
        built = _fake_sdk(monkeypatch)
        langfuse_tracing.enable_langfuse_tracing()
        should_export = built["should_export_span"]
        # The root invocation span carries no gen_ai attribute: dropping it
        # would orphan the agent, model and tool spans below it.
        assert should_export(_span("gcp.vertex.agent")) is True

    def test_the_default_selection_still_applies_elsewhere(self, monkeypatch):
        _configure(monkeypatch)
        built = _fake_sdk(monkeypatch)
        langfuse_tracing.enable_langfuse_tracing()
        should_export = built["should_export_span"]
        assert should_export(_span("sqlalchemy")) is False
        assert should_export(_span("litellm", genai=True)) is True


class TestNoDoubleCountedModelCalls:
    """Measured 16/10/2026: each Gemini call reached Langfuse as two
    generations (``call_llm`` and its ``generate_content`` child) with the
    same usage, doubling the cost of every call."""

    def _filter(self, monkeypatch):
        _configure(monkeypatch)
        built = _fake_sdk(monkeypatch)
        langfuse_tracing.enable_langfuse_tracing()
        return built["should_export_span"]

    def test_generate_content_under_call_llm_is_dropped(self, monkeypatch):
        should_export = self._filter(monkeypatch)
        tracker = langfuse_tracing._CallLlmTracker()
        call_llm = _span("gcp.vertex.agent", name="call_llm", span_id=10)
        child = _span(
            "gcp.vertex.agent", name="generate_content gemini-2.5-flash",
            span_id=11, parent_id=10,
        )
        tracker.on_start(call_llm)
        assert should_export(child) is False
        tracker.on_end(call_llm)
        assert should_export(call_llm) is True
        assert langfuse_tracing._open_call_llm_spans == set()

    def test_generate_content_without_call_llm_is_kept(self, monkeypatch):
        # ADK plans to remove call_llm; the generation must not vanish then.
        should_export = self._filter(monkeypatch)
        child = _span(
            "gcp.vertex.agent", name="generate_content gemini-2.5-flash",
            span_id=21, parent_id=20,
        )
        assert should_export(child) is True

    def test_other_scopes_are_not_tracked(self):
        tracker = langfuse_tracing._CallLlmTracker()
        tracker.on_start(_span("litellm", name="call_llm", span_id=30))
        assert langfuse_tracing._open_call_llm_spans == set()


class TestTrackerOnARealProvider:
    """A fake span cannot catch a processor the SDK refuses: the first
    tracker lacked SpanProcessor's hooks and crashed every agent run."""

    def test_tracker_follows_real_adk_spans(self):
        from opentelemetry.sdk.trace import TracerProvider

        provider = TracerProvider()
        provider.add_span_processor(langfuse_tracing._CallLlmTracker())
        tracer = provider.get_tracer("gcp.vertex.agent")
        with tracer.start_as_current_span("call_llm") as call_llm:
            call_llm_id = call_llm.get_span_context().span_id
            assert call_llm_id in langfuse_tracing._open_call_llm_spans
            with tracer.start_as_current_span("generate_content gemini-2.5-flash"):
                pass
        assert langfuse_tracing._open_call_llm_spans == set()


async def _fake_run_async(self, *, user_id, session_id, new_message=None):
    """A runner's run, reporting what was propagated when each event is made.

    ADK opens its root span on the first iteration: what matters is the
    context seen inside the generator, not around the call.
    """
    for index in range(2):
        yield (index, _propagated.get())


def _collect(run_async, **kwargs):
    async def drain():
        return [event async for event in run_async(object(), **kwargs)]

    return asyncio.run(drain())


class TestRunIdentity:
    """Measured 16/10/2026: a POST /run reached Langfuse with no user id at
    all, and a session id on the agent span only, not on the trace root."""

    def test_runs_are_untouched_while_langfuse_is_off(self):
        original = Runner.run_async
        assert langfuse_tracing.enable_langfuse_tracing() is False
        assert Runner.run_async is original

    def test_enabling_wraps_every_runner_once(self, monkeypatch):
        original = Runner.run_async
        _configure(monkeypatch)
        _fake_sdk(monkeypatch)
        assert langfuse_tracing.enable_langfuse_tracing() is True
        wrapped = Runner.run_async
        assert wrapped.__wrapped__ is original
        langfuse_tracing.flush_langfuse_tracing()
        assert langfuse_tracing.enable_langfuse_tracing() is True
        assert Runner.run_async is wrapped

    def test_user_and_session_reach_the_spans_of_the_run(self, monkeypatch):
        _configure(monkeypatch)
        _fake_sdk(monkeypatch)
        langfuse_tracing.enable_langfuse_tracing()
        run_async = langfuse_tracing._wrap_run_async(_fake_run_async)
        events = _collect(run_async, user_id="u-1", session_id="s-1")
        expected = {"user_id": "u-1", "session_id": "s-1"}
        assert events == [(0, expected), (1, expected)]
        assert _propagated.get() is None

    def test_nothing_is_propagated_once_langfuse_is_stopped(self, monkeypatch):
        _configure(monkeypatch)
        _fake_sdk(monkeypatch)
        langfuse_tracing.enable_langfuse_tracing()
        langfuse_tracing.flush_langfuse_tracing()
        run_async = langfuse_tracing._wrap_run_async(_fake_run_async)
        assert _collect(run_async, user_id="u-1", session_id="s-1") == [
            (0, None),
            (1, None),
        ]

    def test_a_failing_propagation_does_not_stop_the_run(self, monkeypatch, caplog):
        _configure(monkeypatch)
        _fake_sdk(monkeypatch, propagate_error=ValueError("bad id"))
        langfuse_tracing.enable_langfuse_tracing()
        run_async = langfuse_tracing._wrap_run_async(_fake_run_async)
        with caplog.at_level("WARNING"):
            events = _collect(run_async, user_id="u-1", session_id="s-1")
        assert events == [(0, None), (1, None)]
        assert "bad id" in caplog.text

    def test_stopping_early_closes_the_runner_generator(self, monkeypatch):
        _configure(monkeypatch)
        _fake_sdk(monkeypatch)
        langfuse_tracing.enable_langfuse_tracing()
        closed = []

        async def run(self, **kwargs):
            try:
                yield 0
                yield 1
            finally:
                closed.append(True)

        async def first_only():
            events = langfuse_tracing._wrap_run_async(run)(
                object(), user_id="u", session_id="s"
            )
            first = await anext(events)
            await events.aclose()
            return first

        assert asyncio.run(first_only()) == 0
        assert closed == [True]
