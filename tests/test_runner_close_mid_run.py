"""Does rebuilding a stale runner cut a run that is still using it?

When the fingerprint changes, the sync drops the cached agent and ADK closes
the old runner (``Runner.close`` closes its toolsets). A run started before the
edit still holds that runner. A real stdio MCP server with a slow tool and a
scripted model stand in for the agent; the edit lands while the tool runs.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi import HTTPException
from google.adk.agents import LlmAgent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.genai import types
from mcp import StdioServerParameters

import apowerb.core.agent_helpers as agent_helpers
from tests.test_agent_cache_across_workers import APP_NAME, cluster, synced  # noqa: F401

TOOL_SECONDS = 3


class _ScriptedLlm(BaseLlm):
    """Calls ``slow`` once, then answers."""

    model: str = "scripted"

    async def generate_content_async(self, llm_request, stream=False):
        answered = any(
            part.function_response
            for content in llm_request.contents
            for part in (content.parts or [])
        )
        if answered:
            part = types.Part(text="final")
        else:
            part = types.Part(
                function_call=types.FunctionCall(name="slow", args={"seconds": TOOL_SECONDS})
            )
        yield LlmResponse(content=types.Content(role="model", parts=[part]))


def _mcp_server(tmp_path: Path) -> tuple[Path, Path]:
    script, marker = tmp_path / "slow_server.py", tmp_path / "tool_started"
    script.write_text(
        textwrap.dedent(
            f"""
            import asyncio, pathlib
            from mcp.server.fastmcp import FastMCP
            mcp = FastMCP("slow")

            @mcp.tool()
            async def slow(seconds: float) -> str:
                journal = pathlib.Path({str(marker)!r})
                with journal.open("a") as f:
                    f.write("start\\n")
                await asyncio.sleep(seconds)
                with journal.open("a") as f:
                    f.write("end\\n")
                return "done"

            mcp.run()
            """
        )
    )
    return script, marker


def _mcp_agent_builder(script: Path):
    def to_agent(agent_name: str) -> LlmAgent:
        toolset = McpToolset(
            connection_params=StdioConnectionParams(
                server_params=StdioServerParameters(command=sys.executable, args=[str(script)]),
                timeout=30,
            )
        )
        return LlmAgent(name=agent_name, model=_ScriptedLlm(), tools=[toolset])

    return to_agent


def _python_agent_builder(marker: Path):
    async def slow(seconds: float) -> str:
        marker.write_text("started")
        await asyncio.sleep(seconds)
        return "done"

    def to_agent(agent_name: str) -> LlmAgent:
        return LlmAgent(name=agent_name, model=_ScriptedLlm(), tools=[slow])

    return to_agent


async def _run(runner) -> dict:
    session = await runner.session_service.create_session(app_name=APP_NAME, user_id="u")
    outcome: dict = {"responses": [], "texts": [], "error": None}
    try:
        async for event in runner.run_async(
            user_id="u",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text="go")]),
        ):
            outcome["responses"] += [r.response for r in event.get_function_responses()]
            if event.content:
                outcome["texts"] += [p.text for p in (event.content.parts or []) if p.text]
            if event.error_message:
                # ADK reports a failed node as an event, not an exception.
                outcome["error"] = event.error_message
    except BaseException as exc:  # noqa: BLE001 -- the point is to see what escapes
        outcome["error"] = f"{type(exc).__name__}: {exc}"
    return outcome


async def _run_with_edit_midway(synced, marker: Path, edit: bool) -> dict:  # noqa: F811
    runner = await synced.a.get_runner_async(APP_NAME)
    run = asyncio.create_task(_run(runner))
    for _ in range(200):
        if marker.exists():
            break
        await asyncio.sleep(0.05)
    assert marker.exists(), "the tool never started"

    if edit:
        # An edit landed elsewhere; the next request on this worker rebuilds.
        synced.db.write("gemini-2.5-pro", version=2)
        fresh = await synced.a.get_runner_async(APP_NAME)
        assert fresh is not runner, "the stale runner was not replaced"

    outcome = await asyncio.wait_for(run, timeout=60)
    if not edit:
        await runner.close()
    print(f"\n[edit={edit}] {outcome}")
    return outcome


async def test_mcp_run_without_edit_completes(synced, tmp_path, monkeypatch):  # noqa: F811
    script, marker = _mcp_server(tmp_path)
    monkeypatch.setattr(agent_helpers, "to_agent", _mcp_agent_builder(script))
    outcome = await _run_with_edit_midway(synced, marker, edit=False)
    assert marker.read_text().split() == ["start", "end"]
    assert outcome["error"] is None
    assert "done" in str(outcome["responses"])
    assert outcome["texts"] == ["final"]


async def test_mcp_run_survives_a_rebuild_midway(synced, tmp_path, monkeypatch):  # noqa: F811
    script, marker = _mcp_server(tmp_path)
    monkeypatch.setattr(agent_helpers, "to_agent", _mcp_agent_builder(script))
    outcome = await _run_with_edit_midway(synced, marker, edit=True)
    journal = marker.read_text().split()
    print(f"[mcp journal] {journal}")
    assert outcome["error"] is None
    assert "done" in str(outcome["responses"])
    assert outcome["texts"] == ["final"]
    # The model asked for the tool once: it must have run once, to the end.
    assert journal == ["start", "end"]


async def test_python_tool_run_survives_a_rebuild_midway(synced, tmp_path, monkeypatch):  # noqa: F811
    marker = tmp_path / "tool_started"
    monkeypatch.setattr(agent_helpers, "to_agent", _python_agent_builder(marker))
    outcome = await _run_with_edit_midway(synced, marker, edit=True)
    assert outcome["error"] is None
    assert "done" in str(outcome["responses"])
    assert outcome["texts"] == ["final"]


# --- the replaced runner is closed, once, when its last run ends -------------


class _FailingLlm(_ScriptedLlm):
    """Calls ``slow`` once, then fails instead of answering."""

    async def generate_content_async(self, llm_request, stream=False):
        async for response in super().generate_content_async(llm_request, stream):
            if response.content.parts[0].text:
                raise RuntimeError("model failed after the tool")
            yield response


def _failing_agent_builder(marker: Path):
    base = _python_agent_builder(marker)

    def to_agent(agent_name: str) -> LlmAgent:
        agent = base(agent_name)
        agent.model = _FailingLlm()
        return agent

    return to_agent


@pytest.fixture
def closed(monkeypatch) -> list:
    """Every runner ``Runner.close`` is called on, in order."""
    seen: list = []
    original = Runner.close

    async def spy(self):
        seen.append(self)
        await original(self)

    monkeypatch.setattr(Runner, "close", spy)
    return seen


async def _eventually(condition, seconds: float = 5) -> bool:
    for _ in range(int(seconds / 0.05)):
        if condition():
            return True
        await asyncio.sleep(0.05)
    return condition()


async def _start_run(synced, marker: Path):  # noqa: F811
    runner = await synced.a.get_runner_async(APP_NAME)
    run = asyncio.create_task(_run(runner))
    assert await _eventually(marker.exists), "the tool never started"
    return runner, run


async def _replace(synced, runner) -> None:  # noqa: F811
    synced.db.write("gemini-2.5-pro", version=2)
    assert await synced.a.get_runner_async(APP_NAME) is not runner


async def test_an_idle_replaced_runner_is_closed_at_once(synced, closed):  # noqa: F811
    runner = await synced.a.get_runner_async(APP_NAME)

    await _replace(synced, runner)

    assert await _eventually(lambda: closed == [runner])


async def test_a_replaced_mcp_runner_is_closed_once_its_run_ends(
    synced, closed, tmp_path, monkeypatch  # noqa: F811
):
    script, marker = _mcp_server(tmp_path)
    monkeypatch.setattr(agent_helpers, "to_agent", _mcp_agent_builder(script))
    runner, run = await _start_run(synced, marker)

    await _replace(synced, runner)
    await asyncio.sleep(0.5)
    assert closed == [], "closed while its run was still using it"

    outcome = await asyncio.wait_for(run, timeout=60)
    assert outcome["error"] is None
    assert marker.read_text().split() == ["start", "end"]
    assert closed == [runner]


async def test_a_replaced_runner_is_closed_when_its_run_fails(
    synced, closed, tmp_path, monkeypatch  # noqa: F811
):
    marker = tmp_path / "tool_started"
    monkeypatch.setattr(agent_helpers, "to_agent", _failing_agent_builder(marker))
    runner, run = await _start_run(synced, marker)

    await _replace(synced, runner)
    assert closed == []

    outcome = await asyncio.wait_for(run, timeout=60)
    assert "model failed after the tool" in outcome["error"]
    assert closed == [runner]


async def test_a_replaced_runner_is_closed_when_its_run_is_cancelled(
    synced, closed, tmp_path, monkeypatch  # noqa: F811
):
    """A client disconnect cancels the task that iterates the run."""
    marker = tmp_path / "tool_started"
    monkeypatch.setattr(agent_helpers, "to_agent", _python_agent_builder(marker))
    runner, run = await _start_run(synced, marker)

    await _replace(synced, runner)
    assert closed == []

    run.cancel()
    outcome = await asyncio.wait_for(run, timeout=60)
    assert outcome["error"].startswith("CancelledError")
    assert await _eventually(lambda: closed == [runner])


async def test_a_deleted_agent_answers_404_but_lets_its_mcp_run_finish(
    synced, closed, tmp_path, monkeypatch  # noqa: F811
):
    script, marker = _mcp_server(tmp_path)
    monkeypatch.setattr(agent_helpers, "to_agent", _mcp_agent_builder(script))
    runner, run = await _start_run(synced, marker)

    synced.db.delete()
    with pytest.raises(HTTPException) as exc:
        await synced.a.get_runner_async(APP_NAME)
    assert exc.value.status_code == 404
    await asyncio.sleep(0.5)
    assert closed == [], "closed while its run was still using it"

    outcome = await asyncio.wait_for(run, timeout=60)
    assert outcome["error"] is None
    assert marker.read_text().split() == ["start", "end"]
    assert closed == [runner]
