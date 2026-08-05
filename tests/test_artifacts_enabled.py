"""The Artifacts toggle must actually hand the agent its saving tool.

The agent editor exposes a "Code Artifacts — Agent saves code as downloadable
artifacts" switch, stored as ``artifacts_enabled``. That column was written,
read back, cloned and seeded — but nothing ever turned it into a tool. Every
agent with the switch on behaved exactly like one with it off.

Measured on the dev host on 2026-08-05: 0 of 184 agents carried
``artifacts.tool_save_code_artifact``, and a live demo failed because the tool
had to be added by hand. The switch was decoration.

``artifacts_enabled`` reaches this code as a bool or as the strings "true" /
"false" depending on the path (DB column is VARCHAR, the API schema is bool),
so both shapes have to be honoured.
"""
from __future__ import annotations

import pytest

from apowerb.core.agent_helpers.agent_utils import _should_inject_artifact_tool


@pytest.mark.parametrize("value", [True, "true", "True", "TRUE"])
def test_switch_on_gives_the_tool(value):
    assert _should_inject_artifact_tool({"artifacts_enabled": value}) is True


@pytest.mark.parametrize("value", [False, "false", "False", None, "", "null"])
def test_switch_off_withholds_the_tool(value):
    assert _should_inject_artifact_tool({"artifacts_enabled": value}) is False


def test_absent_key_withholds_the_tool():
    assert _should_inject_artifact_tool({}) is False


def test_structured_output_agents_never_get_it():
    """Same reasoning as the chat action-card tools.

    A pipeline agent with an output schema must emit its JSON, not a tool call.
    Handing it a saving tool is how the SCEI intake was broken in 2026-05-22.
    """
    assert (
        _should_inject_artifact_tool(
            {"artifacts_enabled": True, "output_schema_name": "InvoiceSchema"}
        )
        is False
    )


def test_the_tool_it_injects_is_the_catalogue_one():
    """Guards against the injected function drifting from the catalogue entry.

    The tool was invisible to agents until 2026-08-04 because its name lacked
    the ``tool_`` prefix the catalogue requires; injecting a differently-named
    function here would resurrect that class of bug silently.
    """
    from apowerb.tools_store.portfolio.artifacts import tool_save_code_artifact

    assert tool_save_code_artifact.__name__ == "tool_save_code_artifact"
