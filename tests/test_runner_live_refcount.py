"""A rebuild must not close a runner out from under a ``run_live`` stream.

#256 deferred closing a replaced runner until its in-flight ``run_async`` runs
end, but only ``run_async`` was counted. The ``/run_live`` WebSocket reaches the
same runner through ``get_runner_async`` and calls ``runner.run_live`` on it, so
a rebuild mid-stream still tore down its toolsets (and any live MCP session).

These drive the bookkeeping in ``_instrument_runner`` / ``_retire_runner``
directly with a fake runner whose ``run_async`` / ``run_live`` are async
generators gated by an event — no WebSocket, no real ADK Runner needed. The
first test is RED without wrapping ``run_live`` (the runner closes mid-stream).
"""

from __future__ import annotations

import asyncio

import pytest

from apowerb.core import agent_runtime_sync as sync


class _FakeRunner:
    """Async-gen ``run_async`` / ``run_live`` gated by an event, plus close()."""

    def __init__(self) -> None:
        self.closed = 0

    async def run_async(self, gate: asyncio.Event):
        yield "a-start"
        await gate.wait()
        yield "a-end"

    async def run_live(self, gate: asyncio.Event):
        yield "l-start"
        await gate.wait()
        yield "l-end"

    async def close(self) -> None:
        self.closed += 1


async def _exhaust(agen) -> None:
    with pytest.raises(StopAsyncIteration):
        await agen.__anext__()


async def _eventually(condition, seconds: float = 2.0) -> bool:
    for _ in range(int(seconds / 0.02)):
        if condition():
            return True
        await asyncio.sleep(0.02)
    return condition()


async def test_retire_during_run_live_defers_close():
    runner = _FakeRunner()
    sync._instrument_runner(runner)

    gate = asyncio.Event()
    live = runner.run_live(gate)
    assert await live.__anext__() == "l-start"  # the live run is now in flight

    sync._retire_runner(runner)
    await asyncio.sleep(0)
    assert runner.closed == 0, "closed while a live stream was still running"

    gate.set()
    assert await live.__anext__() == "l-end"
    await _exhaust(live)  # the finally runs: last run ended -> close once
    assert runner.closed == 1


async def test_run_async_and_run_live_share_one_counter():
    runner = _FakeRunner()
    sync._instrument_runner(runner)

    ga, gl = asyncio.Event(), asyncio.Event()
    http = runner.run_async(ga)
    live = runner.run_live(gl)
    assert await http.__anext__() == "a-start"
    assert await live.__anext__() == "l-start"  # _ACTIVE == 2

    sync._retire_runner(runner)

    gl.set()
    assert await live.__anext__() == "l-end"
    await _exhaust(live)
    assert runner.closed == 0, "closed while the http run was still active"

    ga.set()
    assert await http.__anext__() == "a-end"
    await _exhaust(http)
    assert runner.closed == 1


async def test_idle_retire_closes_after_grace_and_wrap_is_idempotent(monkeypatch):
    monkeypatch.setattr(sync, "_RUNNER_CLOSE_GRACE", 0.02)
    runner = _FakeRunner()
    sync._instrument_runner(runner)
    sync._instrument_runner(runner)  # idempotent: no second wrapper

    sync._retire_runner(runner)
    await asyncio.sleep(0)
    assert runner.closed == 0, "idle retire must defer, not close in the handout window"
    assert await _eventually(lambda: runner.closed == 1)  # swept after the grace


async def test_a_run_starting_in_the_handout_window_is_not_closed(monkeypatch):
    # Gap B: get_runner_async hands out the runner, but _ACTIVE stays 0 until the
    # run's first iteration. A retire in that window must not close it.
    monkeypatch.setattr(sync, "_RUNNER_CLOSE_GRACE", 0.1)
    runner = _FakeRunner()
    sync._instrument_runner(runner)

    sync._retire_runner(runner)  # retired while idle (the vulnerable window)
    await asyncio.sleep(0)
    assert runner.closed == 0, "RED without the grace: closed before the run started"

    gate = asyncio.Event()
    run = runner.run_async(gate)
    assert await run.__anext__() == "a-start"  # the run starts inside the grace

    await asyncio.sleep(0.2)  # let the grace timer fire while the run is active
    assert runner.closed == 0, "the grace timer closed a runner with a live run"

    gate.set()
    assert await run.__anext__() == "a-end"
    await _exhaust(run)
    assert runner.closed == 1  # the run's finally closes it, exactly once


async def test_grace_timer_and_run_finally_close_exactly_once(monkeypatch):
    # The run ends before the grace timer fires: the finally closes it, and the
    # later timer must find it already claimed and do nothing.
    monkeypatch.setattr(sync, "_RUNNER_CLOSE_GRACE", 0.3)
    runner = _FakeRunner()
    sync._instrument_runner(runner)
    sync._retire_runner(runner)

    gate = asyncio.Event()
    run = runner.run_async(gate)
    await run.__anext__()
    gate.set()
    await run.__anext__()
    await _exhaust(run)
    assert runner.closed == 1

    await asyncio.sleep(0.4)  # the grace timer now fires on an already-closed runner
    assert runner.closed == 1, "grace timer double-closed after the finally"
