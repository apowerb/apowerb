"""Tool calls streamed by google-adk >= 2.12 through LiteLlm, in SSE mode.

ADK 2.12 sends each tool call as partial events (``partial: true``,
``willContinue``, ``partialArgs``, no ``args``) before the final event with
the complete call. An OpenAI-family model (Azure) streams the arguments in
many deltas, so one call becomes many events. Only the final event is a
call: each tool must run once, the run must record it once, and the
history sent back to the model must not grow.

The model is scripted (no network): the stream reproduces the Azure chunk
shapes (an empty prompt-filter chunk, index 0, arguments split in 7-char
deltas, a final ``finish_reason="tool_calls"`` chunk).
"""

import json

from google.adk.agents import LlmAgent
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from litellm.types.utils import ModelResponseStream

from apowerb.core import run_main

PLAN = [
    ("tool_text_to_sql", {"question": "total des conversations"}),
    ("tool_create_chart", {"query": "select count(*) from conversations", "chart_type": "bar"}),
    ("embed_chart", {"chart_id": "c-1"}),
    "Il y a 42 conversations.",
]


def _chunk(delta=None, finish_reason=None, choices=True):
    if not choices:
        return ModelResponseStream(id="x", choices=[], model="")
    choice = {"index": 0, "delta": delta or {}}
    if finish_reason:
        choice["finish_reason"] = finish_reason
    return ModelResponseStream(id="x", model="gpt-4.1", choices=[choice])


def _azure_stream(turn, n):
    yield _chunk(choices=False)
    if isinstance(turn, str):
        yield _chunk({"content": turn})
        yield _chunk(finish_reason="stop")
        return
    name, args = turn
    raw = json.dumps(args)
    yield _chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": f"call_{n}", "type": "function", "function": {"name": name, "arguments": ""}}]})
    for i in range(0, len(raw), 7):
        yield _chunk({"tool_calls": [{"index": 0, "function": {"arguments": raw[i : i + 7]}}]})
    yield _chunk(finish_reason="tool_calls")


class _ScriptedAzure:
    def __init__(self):
        self.prompts = []

    async def acompletion(self, **kwargs):
        n = len(self.prompts)
        self.prompts.append(kwargs.get("messages"))

        async def gen():
            for c in _azure_stream(PLAN[min(n, len(PLAN) - 1)], n):
                yield c

        return gen()


async def _run():
    executed = []

    def tool_text_to_sql(question: str) -> dict:
        executed.append("tool_text_to_sql")
        return {"data": [{"count": 42}]}

    def tool_create_chart(query: str, chart_type: str) -> dict:
        executed.append("tool_create_chart")
        return {"chart_id": "c-1"}

    def embed_chart(chart_id: str) -> dict:
        executed.append("embed_chart")
        return {"status": "displayed"}

    model = LiteLlm(model="azure/gpt-4.1")
    client = _ScriptedAzure()
    model.llm_client = client
    agent = LlmAgent(name="da", model=model, instruction="x", tools=[tool_text_to_sql, tool_create_chart, embed_chart])
    sessions = InMemorySessionService()
    await sessions.create_session(app_name="a", user_id="u", session_id="s")
    runner = Runner(app_name="a", agent=agent, session_service=sessions)
    sse = []
    async for event in runner.run_async(
        user_id="u",
        session_id="s",
        new_message=types.Content(role="user", parts=[types.Part(text="total des conversations")]),
        run_config=RunConfig(streaming_mode=StreamingMode.SSE),
    ):
        # What /run_sse writes on the wire.
        sse.append(f"data: {event.model_dump_json(exclude_none=True, by_alias=True)}\n\n")
    return executed, client, sse


def _calls(sse, *, partial):
    names = []
    for block in sse:
        event = json.loads(block[len("data: ") :])
        if bool(event.get("partial")) is partial:
            names += run_main.executed_tools([event])
    return names


async def test_streamed_calls_run_each_tool_once():
    executed, client, sse = await _run()

    assert executed == ["tool_text_to_sql", "tool_create_chart", "embed_chart"]
    assert len(client.prompts) == 4
    # The final events carry each call exactly once ...
    assert _calls(sse, partial=False) == executed
    # ... and ADK also streams them as partial events, which clients must skip
    # (apowerb-ui processSSEBlock): several per call with an Azure stream.
    assert _calls(sse, partial=True).count("embed_chart") > 1


async def test_run_records_each_tool_once(monkeypatch):
    _, _, sse = await _run()
    settled = {}
    monkeypatch.setattr(run_main, "settle_run", lambda run_id, status, error, tools: settled.update(tools=tools, status=status))

    async def stream():
        for block in sse:
            yield block.encode()

    async for _ in run_main.track_agent_stream("run-1", stream()):
        pass

    assert settled == {"tools": ["tool_text_to_sql", "tool_create_chart", "embed_chart"], "status": run_main.STATUS_SUCCESS}
