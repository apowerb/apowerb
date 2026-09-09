"""Tests for apowerb.core.agent_helpers.usage_recorder.

No live DB: sessionmanager is monkeypatched with in-memory fakes.
"""
from __future__ import annotations

import asyncio

import pytest

# ``chain_after_model_callbacks`` stayed in the core: chaining two ADK
# callbacks is generic plumbing, not a sold feature.
from apowerb.core.agent_helpers.callback_chain import chain_after_model_callbacks
from apowerb.core.agent_helpers.usage_recorder import (
    _extract_usage,
    create_usage_recorder_callback,
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeSession:
    def __init__(self, session_id):
        self.id = session_id


class FakeCallbackContext:
    def __init__(self, session_id="sess-123"):
        self.session = FakeSession(session_id)


class FakeLlmResponse:
    def __init__(self, usage_metadata=None, partial=None):
        self.usage_metadata = usage_metadata
        self.partial = partial


class FakePydanticUsage:
    """Mimics google.genai.types.GenerateContentResponseUsageMetadata:
    snake_case attributes + a .model_dump() method."""

    def __init__(self, **kwargs):
        self._data = kwargs
        for k, v in kwargs.items():
            setattr(self, k, v)

    def model_dump(self):
        return dict(self._data)


class FakeAsyncSessionCM:
    """Mimics ``sessionmanager.session()``: an async context manager
    yielding an object with .add()/.commit()."""

    def __init__(self, store, raise_on_enter=None, raise_on_commit=None):
        self.store = store
        self.raise_on_enter = raise_on_enter
        self.raise_on_commit = raise_on_commit

    async def __aenter__(self):
        if self.raise_on_enter:
            raise self.raise_on_enter
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, obj):
        self.store.append(obj)

    async def commit(self):
        if self.raise_on_commit:
            raise self.raise_on_commit


class FakeSessionManager:
    def __init__(self, raise_on_enter=None, raise_on_commit=None):
        self.store = []
        self.raise_on_enter = raise_on_enter
        self.raise_on_commit = raise_on_commit

    def session(self):
        return FakeAsyncSessionCM(
            self.store,
            raise_on_enter=self.raise_on_enter,
            raise_on_commit=self.raise_on_commit,
        )


async def _run_and_wait_scheduled_tasks(coro):
    """Runs coro (the callback call) and waits for any write task it
    scheduled, by inspecting the real strong-reference registry
    (usage_recorder._pending_writes) rather than monkeypatching
    asyncio.create_task -- exercises the production task-retention path
    directly (see test_scheduled_write_task_is_retained_in_pending_writes_until_done
    below for the dedicated regression test)."""
    import apowerb.core.agent_helpers.usage_recorder as ur_module

    before = set(ur_module._pending_writes)
    await coro
    new_tasks = set(ur_module._pending_writes) - before
    if new_tasks:
        await asyncio.gather(*new_tasks, return_exceptions=True)
    return new_tasks


# ---------------------------------------------------------------------------
# _extract_usage — pure function
# ---------------------------------------------------------------------------


def test_extract_usage_from_dict_all_five_fields():
    result = _extract_usage(
        {
            "prompt_token_count": 100,
            "candidates_token_count": 50,
            "thoughts_token_count": 20,
            "cached_content_token_count": 10,
            "total_token_count": 170,
        }
    )
    assert result == {
        "input_tokens": 100,
        "output_tokens": 50,
        "thoughts_tokens": 20,
        "cached_tokens": 10,
        "total_tokens": 170,
    }


def test_extract_usage_from_pydantic_like_object():
    usage = FakePydanticUsage(
        prompt_token_count=5,
        candidates_token_count=7,
        thoughts_token_count=None,
        cached_content_token_count=None,
        total_token_count=12,
    )
    result = _extract_usage(usage)
    assert result == {
        "input_tokens": 5,
        "output_tokens": 7,
        "thoughts_tokens": 0,
        "cached_tokens": 0,
        "total_tokens": 12,
    }


def test_extract_usage_none_returns_none():
    assert _extract_usage(None) is None


def test_extract_usage_missing_keys_default_to_zero():
    result = _extract_usage({})
    assert result == {
        "input_tokens": 0,
        "output_tokens": 0,
        "thoughts_tokens": 0,
        "cached_tokens": 0,
        "total_tokens": 0,
    }


# ---------------------------------------------------------------------------
# create_usage_recorder_callback — orchestration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_usage_metadata_none_records_nothing_and_does_not_crash(monkeypatch):
    fake_mgr = FakeSessionManager()
    import apowerb.helpers.database as db_module

    monkeypatch.setattr(db_module, "sessionmanager", fake_mgr)

    callback = create_usage_recorder_callback(
        agent_id=1, agent_name="agent1", owner_id="owner@x.com", model_name="gpt-4"
    )
    ctx = FakeCallbackContext()
    resp = FakeLlmResponse(usage_metadata=None, partial=None)

    await _run_and_wait_scheduled_tasks(callback(callback_context=ctx, llm_response=resp)
    )

    assert fake_mgr.store == []


@pytest.mark.asyncio
async def test_partial_then_final_records_exactly_once(monkeypatch):
    fake_mgr = FakeSessionManager()
    import apowerb.helpers.database as db_module

    monkeypatch.setattr(db_module, "sessionmanager", fake_mgr)

    callback = create_usage_recorder_callback(
        agent_id=1, agent_name="agent1", owner_id="owner@x.com", model_name="gpt-4"
    )
    ctx = FakeCallbackContext()

    partial_resp = FakeLlmResponse(
        usage_metadata=None,
        partial=True,
    )
    final_resp = FakeLlmResponse(
        usage_metadata={
            "prompt_token_count": 10,
            "candidates_token_count": 5,
            "thoughts_token_count": 0,
            "cached_content_token_count": 0,
            "total_token_count": 15,
        },
        partial=False,
    )

    await _run_and_wait_scheduled_tasks(callback(callback_context=ctx, llm_response=partial_resp)
    )
    await _run_and_wait_scheduled_tasks(callback(callback_context=ctx, llm_response=final_resp)
    )

    assert len(fake_mgr.store) == 1
    row = fake_mgr.store[0]
    assert row.total_tokens == 15
    assert row.input_tokens == 10
    assert row.output_tokens == 5


@pytest.mark.asyncio
async def test_partial_true_with_usage_metadata_still_skipped(monkeypatch):
    """Even if a partial chunk carries usage_metadata (defensive case),
    it must not be recorded — only the non-partial final chunk counts."""
    fake_mgr = FakeSessionManager()
    import apowerb.helpers.database as db_module

    monkeypatch.setattr(db_module, "sessionmanager", fake_mgr)

    callback = create_usage_recorder_callback(
        agent_id=1, agent_name="agent1", owner_id="owner@x.com", model_name="gpt-4"
    )
    ctx = FakeCallbackContext()
    resp = FakeLlmResponse(
        usage_metadata={"prompt_token_count": 1, "candidates_token_count": 1},
        partial=True,
    )

    await _run_and_wait_scheduled_tasks(callback(callback_context=ctx, llm_response=resp)
    )

    assert fake_mgr.store == []


@pytest.mark.asyncio
async def test_full_usage_recorded_with_expected_row_fields(monkeypatch):
    fake_mgr = FakeSessionManager()
    import apowerb.helpers.database as db_module

    monkeypatch.setattr(db_module, "sessionmanager", fake_mgr)

    callback = create_usage_recorder_callback(
        agent_id=42, agent_name="agent42", owner_id="owner@x.com", model_name="gpt-4o"
    )
    ctx = FakeCallbackContext(session_id="sess-abc")
    resp = FakeLlmResponse(
        usage_metadata={
            "prompt_token_count": 100,
            "candidates_token_count": 50,
            "thoughts_token_count": 20,
            "cached_content_token_count": 10,
            "total_token_count": 180,
        },
        partial=None,
    )

    await _run_and_wait_scheduled_tasks(callback(callback_context=ctx, llm_response=resp)
    )

    assert len(fake_mgr.store) == 1
    row = fake_mgr.store[0]
    assert row.agent_id == 42
    assert row.agent_name == "agent42"
    assert row.owner_id == "owner@x.com"
    assert row.session_id == "sess-abc"
    assert row.model == "gpt-4o"
    assert row.input_tokens == 100
    assert row.output_tokens == 50
    assert row.thoughts_tokens == 20
    assert row.cached_tokens == 10
    assert row.total_tokens == 180


@pytest.mark.asyncio
async def test_db_failure_is_swallowed_and_logged(monkeypatch, caplog):
    fake_mgr = FakeSessionManager(raise_on_enter=RuntimeError("db is down"))
    import apowerb.helpers.database as db_module

    monkeypatch.setattr(db_module, "sessionmanager", fake_mgr)

    callback = create_usage_recorder_callback(
        agent_id=1, agent_name="agent1", owner_id="owner@x.com", model_name="gpt-4"
    )
    ctx = FakeCallbackContext()
    resp = FakeLlmResponse(
        usage_metadata={"prompt_token_count": 1, "candidates_token_count": 1},
        partial=None,
    )

    with caplog.at_level("WARNING"):
        await _run_and_wait_scheduled_tasks(callback(callback_context=ctx, llm_response=resp)
        )

    assert any("db is down" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_callback_never_raises_even_on_broken_callback_context(monkeypatch):
    fake_mgr = FakeSessionManager()
    import apowerb.helpers.database as db_module

    monkeypatch.setattr(db_module, "sessionmanager", fake_mgr)

    callback = create_usage_recorder_callback(
        agent_id=1, agent_name="agent1", owner_id=None, model_name="gpt-4"
    )

    class BrokenContext:
        @property
        def session(self):
            raise RuntimeError("no session bound")

    resp = FakeLlmResponse(
        usage_metadata={"prompt_token_count": 1, "candidates_token_count": 1},
        partial=None,
    )

    result = await callback(callback_context=BrokenContext(), llm_response=resp)
    assert result is None


@pytest.mark.asyncio
async def test_callback_always_returns_none():
    callback = create_usage_recorder_callback(
        agent_id=1, agent_name="agent1", owner_id="o", model_name="m"
    )
    ctx = FakeCallbackContext()
    resp = FakeLlmResponse(usage_metadata=None, partial=None)
    result = await callback(callback_context=ctx, llm_response=resp)
    assert result is None


# ---------------------------------------------------------------------------
# chain_after_model_callbacks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_with_no_existing_callback_returns_recorder_itself():
    calls = []

    async def recorder(*, callback_context, llm_response):
        calls.append("recorder")
        return None

    chained = chain_after_model_callbacks(recorder, None)
    result = await chained(callback_context=object(), llm_response=object())

    assert calls == ["recorder"]
    assert result is None


@pytest.mark.asyncio
async def test_chain_runs_recorder_first_then_existing_sync_and_returns_existing_value():
    calls = []
    sentinel = object()

    async def recorder(*, callback_context, llm_response):
        calls.append("recorder")
        return None

    def existing(*, callback_context, llm_response):
        calls.append("existing")
        return sentinel

    chained = chain_after_model_callbacks(recorder, existing)
    result = await chained(callback_context=object(), llm_response=object())

    assert calls == ["recorder", "existing"]
    assert result is sentinel


@pytest.mark.asyncio
async def test_chain_supports_async_existing_callback():
    calls = []
    sentinel = object()

    async def recorder(*, callback_context, llm_response):
        calls.append("recorder")
        return None

    async def existing(*, callback_context, llm_response):
        calls.append("existing")
        return sentinel

    chained = chain_after_model_callbacks(recorder, existing)
    result = await chained(callback_context=object(), llm_response=object())

    assert calls == ["recorder", "existing"]
    assert result is sentinel


@pytest.mark.asyncio
async def test_chain_returns_none_when_existing_returns_none():
    async def recorder(*, callback_context, llm_response):
        return None

    def existing(*, callback_context, llm_response):
        return None

    chained = chain_after_model_callbacks(recorder, existing)
    result = await chained(callback_context=object(), llm_response=object())

    assert result is None


# ---------------------------------------------------------------------------
# _pending_writes — strong reference retention (no asyncio.create_task patch)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scheduled_write_task_is_retained_in_pending_writes_until_done(monkeypatch):
    """asyncio.create_task() alone does not keep a strong reference to the
    task — CPython's docs warn it can be garbage-collected mid-flight,
    silently dropping the write. This does NOT monkeypatch
    asyncio.create_task: it inspects the real module-level registry that
    must hold the reference until the task completes."""
    import apowerb.core.agent_helpers.usage_recorder as ur_module

    fake_mgr = FakeSessionManager()
    import apowerb.helpers.database as db_module

    monkeypatch.setattr(db_module, "sessionmanager", fake_mgr)

    assert len(ur_module._pending_writes) == 0

    callback = create_usage_recorder_callback(
        agent_id=1, agent_name="agent1", owner_id="owner@x.com", model_name="gpt-4"
    )
    ctx = FakeCallbackContext()
    resp = FakeLlmResponse(
        usage_metadata={"prompt_token_count": 1, "candidates_token_count": 1},
        partial=None,
    )

    await callback(callback_context=ctx, llm_response=resp)

    assert len(ur_module._pending_writes) == 1
    task = next(iter(ur_module._pending_writes))

    await task

    assert len(ur_module._pending_writes) == 0
    assert len(fake_mgr.store) == 1


@pytest.mark.asyncio
async def test_cached_tokens_mirrored_onto_current_span(monkeypatch):
    """The callback mirrors cached_content_token_count onto the current OTel
    span (as gen_ai.usage.cached_input_tokens) so the observability pipeline
    can compute a cache-hit ratio. It must NOT re-set input_tokens (ADK
    already does, and the stats query sums it across spans)."""
    fake_mgr = FakeSessionManager()
    import apowerb.helpers.database as db_module

    monkeypatch.setattr(db_module, "sessionmanager", fake_mgr)

    recorded = {}

    class FakeSpan:
        def is_recording(self):
            return True

        def set_attribute(self, key, value):
            recorded[key] = value

    import opentelemetry.trace as otel_trace

    monkeypatch.setattr(otel_trace, "get_current_span", lambda: FakeSpan())

    callback = create_usage_recorder_callback(
        agent_id=1, agent_name="a", owner_id="o", model_name="gemini/gemini-2.5-flash"
    )
    ctx = FakeCallbackContext(session_id="sess-cache")
    resp = FakeLlmResponse(
        usage_metadata={
            "prompt_token_count": 12000,
            "candidates_token_count": 200,
            "cached_content_token_count": 9000,
            "total_token_count": 12200,
        },
        partial=None,
    )

    await _run_and_wait_scheduled_tasks(
        callback(callback_context=ctx, llm_response=resp)
    )

    assert recorded.get("gen_ai.usage.cached_input_tokens") == 9000
    assert "gen_ai.usage.input_tokens" not in recorded  # avoid double-count


@pytest.mark.asyncio
async def test_span_mirroring_never_breaks_on_missing_span(monkeypatch):
    """A non-recording (or absent) span must not stop the usage row."""
    fake_mgr = FakeSessionManager()
    import apowerb.helpers.database as db_module

    monkeypatch.setattr(db_module, "sessionmanager", fake_mgr)

    class NonRecordingSpan:
        def is_recording(self):
            return False

        def set_attribute(self, key, value):  # pragma: no cover
            raise AssertionError("must not be called on a non-recording span")

    import opentelemetry.trace as otel_trace

    monkeypatch.setattr(otel_trace, "get_current_span", lambda: NonRecordingSpan())

    callback = create_usage_recorder_callback(
        agent_id=1, agent_name="a", owner_id="o", model_name="m"
    )
    ctx = FakeCallbackContext(session_id="sess-x")
    resp = FakeLlmResponse(
        usage_metadata={"prompt_token_count": 5, "cached_content_token_count": 0},
        partial=None,
    )

    await _run_and_wait_scheduled_tasks(
        callback(callback_context=ctx, llm_response=resp)
    )

    assert len(fake_mgr.store) == 1  # the row is still written
