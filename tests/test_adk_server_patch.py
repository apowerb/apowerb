"""The ADK server monkeypatch must land on the class ADK actually instantiates.

This is the regression guard for a failure that is entirely silent. Two things
here are wired by patching an ADK class at import time: the hot-reload endpoint
(which reads `app.state.adk_web_server` / `adk_agent_loader`) and the LLM call
cap (which replaces `RunConfig` in the server module). If the patch targets a
class ADK no longer instantiates, nothing raises — the patch applies cleanly to
a class that is simply never used, and `run_config_patch` still logs that it
succeeded.

Measured on 2026-08-06 while moving to ADK 2.6.2, with the patch still aimed at
the deprecated `AdkWebServer`: `app.state.adk_web_server` was None (the
hot-reload endpoint answers 503 forever) and `max_llm_calls` came back as 500
instead of 25 — a twentyfold cap increase on every agent run, paid per token,
with a log line claiming the cap was applied.

`get_fast_api_app(web=False)` — the call in main.py — builds an `ApiServer`.
`AdkWebServer` is a deprecated empty subclass of `DevServer`, which is the
`web=True` branch. These tests fail the moment that stops being true.
"""
from __future__ import annotations

import google.adk.cli.api_server as api_server
import pytest

import apowerb.main as main_module


@pytest.fixture(scope="module")
def app():
    return main_module.app


def test_the_server_instance_was_captured(app):
    """None here means the capture never fired."""
    assert getattr(app.state, "adk_web_server", None) is not None


def test_the_captured_instance_is_the_class_adk_runs(app):
    """Not merely non-None: the right class.

    A capture that fired on a subclass ADK doesn't build would still store
    something, and the hot-reload endpoint would drive a dead object.
    """
    assert isinstance(app.state.adk_web_server, api_server.ApiServer)


def test_the_agent_loader_was_captured(app):
    assert getattr(app.state, "adk_agent_loader", None) is not None


def test_the_captured_server_exposes_what_hot_reload_needs(app):
    srv = app.state.adk_web_server
    assert hasattr(srv, "agent_loader")
    assert hasattr(srv, "runner_dict")


def test_run_config_is_capped_in_the_module_adk_runs():
    """The cap lives in whichever module builds RunConfig for /run_sse."""
    assert api_server.RunConfig.__name__ == "_CappedRunConfig"


def test_the_cap_is_the_configured_value_not_the_adk_default():
    """500 is ADK's default. Seeing it means the cap silently stopped applying."""
    from apowerb.core.agent_helpers.run_config_patch import get_llm_max_calls

    assert api_server.RunConfig().max_llm_calls == get_llm_max_calls()
    assert api_server.RunConfig().max_llm_calls != 500


def test_get_runner_async_is_patched_for_the_webhook_path():
    """/run calls runner.run_async() without a run_config, so the cap has to be
    injected on the runner itself."""
    assert api_server.ApiServer.get_runner_async.__name__ == "_patched_get_runner_async"
