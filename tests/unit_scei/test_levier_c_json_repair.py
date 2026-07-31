"""Levier C — central JSON repair in build_validating_state_writer.

When a SCEI sub-agent emits prose or a narrated tool-call instead of the
final JSON payload, the writer used to write an __error__ sentinel that
poisons the downstream pipeline. Levier C adds ONE litellm repair attempt
on the failure path before the sentinel.

litellm.acompletion is mocked (AsyncMock) via the symbol imported into
callbacks. Tests run with asyncio_mode = auto (pytest-asyncio), so async
test functions are awaited directly.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apowerb.core.agent_helpers.callbacks import (
    build_validating_state_writer,
)
from th2customers.scei.schemas import ARIntakePayload


STATE_KEY = "ar_intake"
REPAIR_MODEL = "ovhcloud/Qwen2.5-Coder-32B-Instruct"


def _valid_intake_dict() -> dict:
    return {"status": "skip", "raison": "not an AR"}


def _llm_response(text: str):
    """Shape a minimal litellm-like response: resp.choices[0].message.content."""
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


def _ctx(state: dict) -> MagicMock:
    ctx = MagicMock()
    ctx.state = state
    return ctx


@pytest.mark.asyncio
async def test_repair_recovers_from_prose():
    """Prose + repair_model + litellm returns valid JSON -> validated, no sentinel."""
    state = {STATE_KEY: "I analyzed the email, it is an AR."}
    cb = build_validating_state_writer(
        ARIntakePayload, STATE_KEY, repair_model=REPAIR_MODEL
    )
    mock_ac = AsyncMock(return_value=_llm_response(json.dumps(_valid_intake_dict())))
    with patch(
        "apowerb.core.agent_helpers.callbacks.litellm.acompletion", mock_ac
    ):
        await cb(_ctx(state))

    written = json.loads(state[STATE_KEY])
    assert "__error__" not in written, f"sentinel written despite repair: {written}"
    assert written["status"] == "skip"
    assert mock_ac.await_count == 1


@pytest.mark.asyncio
async def test_repair_recovers_from_narrated_tool_call():
    """JSON that is a narrated tool-call (not the schema) -> repair -> validated."""
    tool_call = {"tool_name": "tool_run_sql", "tool_args": {"query": "SELECT 1"}}
    state = {STATE_KEY: json.dumps(tool_call)}
    cb = build_validating_state_writer(
        ARIntakePayload, STATE_KEY, repair_model=REPAIR_MODEL
    )
    mock_ac = AsyncMock(return_value=_llm_response(json.dumps(_valid_intake_dict())))
    with patch(
        "apowerb.core.agent_helpers.callbacks.litellm.acompletion", mock_ac
    ):
        await cb(_ctx(state))

    written = json.loads(state[STATE_KEY])
    assert "__error__" not in written, f"sentinel written despite repair: {written}"
    assert written["status"] == "skip"
    assert mock_ac.await_count == 1


@pytest.mark.asyncio
async def test_repair_fails_still_prose_writes_sentinel():
    """Repair returns prose again -> sentinel preserved (current behaviour)."""
    state = {STATE_KEY: "still not json at all"}
    cb = build_validating_state_writer(
        ARIntakePayload, STATE_KEY, repair_model=REPAIR_MODEL
    )
    mock_ac = AsyncMock(return_value=_llm_response("sorry, here is more prose"))
    with patch(
        "apowerb.core.agent_helpers.callbacks.litellm.acompletion", mock_ac
    ):
        await cb(_ctx(state))

    written = json.loads(state[STATE_KEY])
    assert "__error__" in written, f"expected sentinel, got: {written}"
    assert mock_ac.await_count == 1


@pytest.mark.asyncio
async def test_no_repair_model_writes_sentinel_without_calling_litellm():
    """repair_model=None + invalid output -> sentinel direct, litellm NOT called."""
    state = {STATE_KEY: "I analyzed the email, it is an AR."}
    cb = build_validating_state_writer(ARIntakePayload, STATE_KEY)
    mock_ac = AsyncMock(return_value=_llm_response(json.dumps(_valid_intake_dict())))
    with patch(
        "apowerb.core.agent_helpers.callbacks.litellm.acompletion", mock_ac
    ):
        await cb(_ctx(state))

    written = json.loads(state[STATE_KEY])
    assert "__error__" in written, f"expected sentinel, got: {written}"
    assert mock_ac.await_count == 0, "litellm must NOT be called when repair_model is None"


@pytest.mark.asyncio
async def test_happy_path_no_repair_invoked():
    """Already-valid JSON -> validated payload, repair NOT invoked."""
    state = {STATE_KEY: json.dumps(_valid_intake_dict())}
    cb = build_validating_state_writer(
        ARIntakePayload, STATE_KEY, repair_model=REPAIR_MODEL
    )
    mock_ac = AsyncMock(return_value=_llm_response("should never be used"))
    with patch(
        "apowerb.core.agent_helpers.callbacks.litellm.acompletion", mock_ac
    ):
        await cb(_ctx(state))

    written = json.loads(state[STATE_KEY])
    assert "__error__" not in written
    assert written["status"] == "skip"
    assert mock_ac.await_count == 0, "happy path must not invoke repair"


@pytest.mark.asyncio
async def test_repair_exception_falls_back_to_sentinel():
    """litellm raises -> no crash, sentinel preserved."""
    state = {STATE_KEY: "prose only"}
    cb = build_validating_state_writer(
        ARIntakePayload, STATE_KEY, repair_model=REPAIR_MODEL
    )
    mock_ac = AsyncMock(side_effect=TimeoutError("boom"))
    with patch(
        "apowerb.core.agent_helpers.callbacks.litellm.acompletion", mock_ac
    ):
        await cb(_ctx(state))

    written = json.loads(state[STATE_KEY])
    assert "__error__" in written, f"expected sentinel after exception, got: {written}"


# ---------------------------------------------------------------------------
# TÂCHE 1 — explicit auth propagation (no reliance on global env)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repair_passes_api_key_explicitly_without_api_base():
    """repair_api_key set, no api_base -> acompletion called with the raw
    provider model name AND api_key passed explicitly (not via env)."""
    state = {STATE_KEY: "prose, please repair"}
    cb = build_validating_state_writer(
        ARIntakePayload,
        STATE_KEY,
        repair_model=REPAIR_MODEL,
        repair_api_key="sk-secret-key",
    )
    mock_ac = AsyncMock(return_value=_llm_response(json.dumps(_valid_intake_dict())))
    with patch(
        "apowerb.core.agent_helpers.callbacks.litellm.acompletion", mock_ac
    ):
        await cb(_ctx(state))

    assert mock_ac.await_count == 1
    kwargs = mock_ac.await_args.kwargs
    assert kwargs["api_key"] == "sk-secret-key"
    assert kwargs["model"] == REPAIR_MODEL
    assert "api_base" not in kwargs or kwargs["api_base"] is None
    assert kwargs.get("timeout") is not None


@pytest.mark.asyncio
async def test_repair_uses_openai_prefix_and_api_base_when_base_set():
    """repair_api_base set -> openai/ provider prefix forced, api_base and
    api_key both passed explicitly (mirrors build_litellm_model)."""
    state = {STATE_KEY: "prose, please repair"}
    cb = build_validating_state_writer(
        ARIntakePayload,
        STATE_KEY,
        repair_model="ovhcloud/Meta-Llama-3_3-70B-Instruct",
        repair_api_key="sk-secret-key",
        repair_api_base="https://endpoint.example.net/api/openai_compat/v1",
    )
    mock_ac = AsyncMock(return_value=_llm_response(json.dumps(_valid_intake_dict())))
    with patch(
        "apowerb.core.agent_helpers.callbacks.litellm.acompletion", mock_ac
    ):
        await cb(_ctx(state))

    assert mock_ac.await_count == 1
    kwargs = mock_ac.await_args.kwargs
    assert kwargs["model"] == "openai/Meta-Llama-3_3-70B-Instruct"
    assert kwargs["api_base"] == "https://endpoint.example.net/api/openai_compat/v1"
    assert kwargs["api_key"] == "sk-secret-key"
    assert kwargs.get("timeout") is not None


# ---------------------------------------------------------------------------
# TÂCHE 2 — extra coverage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dict_shaped_tool_call_triggers_repair():
    """(a) state[key] is a DICT tool-call (not a string): dict branch ->
    ValidationError -> repair triggered -> valid payload, no sentinel."""
    state = {STATE_KEY: {"tool_name": "tool_run_sql", "tool_args": {}}}
    cb = build_validating_state_writer(
        ARIntakePayload, STATE_KEY, repair_model=REPAIR_MODEL
    )
    mock_ac = AsyncMock(return_value=_llm_response(json.dumps(_valid_intake_dict())))
    with patch(
        "apowerb.core.agent_helpers.callbacks.litellm.acompletion", mock_ac
    ):
        await cb(_ctx(state))

    written = json.loads(state[STATE_KEY])
    assert "__error__" not in written, f"sentinel written despite repair: {written}"
    assert written["status"] == "skip"
    assert mock_ac.await_count == 1


@pytest.mark.asyncio
async def test_repair_returns_tool_call_again_writes_sentinel():
    """(b) repair output is STILL a tool-call JSON -> _looks_like_tool_call
    guard rejects it -> sentinel."""
    state = {STATE_KEY: "prose"}
    cb = build_validating_state_writer(
        ARIntakePayload, STATE_KEY, repair_model=REPAIR_MODEL
    )
    again = {"tool_name": "tool_run_sql", "tool_args": {"q": "x"}}
    mock_ac = AsyncMock(return_value=_llm_response(json.dumps(again)))
    with patch(
        "apowerb.core.agent_helpers.callbacks.litellm.acompletion", mock_ac
    ):
        await cb(_ctx(state))

    written = json.loads(state[STATE_KEY])
    assert "__error__" in written, f"expected sentinel, got: {written}"
    assert mock_ac.await_count == 1


@pytest.mark.asyncio
async def test_repair_returns_off_schema_json_writes_sentinel():
    """(d) repair output is valid JSON but off-schema -> ValidationError ->
    sentinel."""
    state = {STATE_KEY: "prose"}
    cb = build_validating_state_writer(
        ARIntakePayload, STATE_KEY, repair_model=REPAIR_MODEL
    )
    mock_ac = AsyncMock(return_value=_llm_response(json.dumps({"foo": "bar"})))
    with patch(
        "apowerb.core.agent_helpers.callbacks.litellm.acompletion", mock_ac
    ):
        await cb(_ctx(state))

    written = json.loads(state[STATE_KEY])
    assert "__error__" in written, f"expected sentinel, got: {written}"
    assert mock_ac.await_count == 1
