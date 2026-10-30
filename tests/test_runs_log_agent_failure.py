"""A failing agent started from POST /agents/{id}/run is logged with its traceback (#365)."""

import asyncio
import logging

from apowerb.routers import runs


class _FailingAgent:
    def run(self):
        raise RuntimeError("boom")


def test_agent_failure_is_logged_with_traceback(monkeypatch, caplog):
    monkeypatch.setattr(runs, "get_agent", lambda store, agent_id: _FailingAgent())
    monkeypatch.setattr(runs, "running_agents", {})

    with caplog.at_level(logging.ERROR, logger=runs.__name__):
        asyncio.run(
            runs.run_adk_agent("agent-365", agent_store=None, _current_user=None)
        )
        runs.running_agents["agent-365"].join(timeout=5)

    records = [r for r in caplog.records if r.name == runs.__name__]
    assert len(records) == 1
    assert "agent-365" in records[0].getMessage()
    assert records[0].exc_info is not None
    assert records[0].exc_info[0] is RuntimeError


def test_no_print_left_in_runs_router():
    source = open(runs.__file__, encoding="utf-8").read()
    assert "print(" not in source
