"""Tests guarding that the artifact-saving tool actually reaches the catalogue.

``save_code_artifact`` shipped inside ``portfolio/artifacts/`` but was invisible
to agents, for two independent reasons:

* ``artifacts`` is the only tool category that is a *package* rather than a
  flat module, and its ``__init__.py`` was empty — so importing the category
  exposed no function at all;
* the catalogue only collects functions whose name starts with ``tool_``
  (see ``ToolsStore.get_tools_in_category``), and the function lacked the
  prefix.

Consequence, verified in production on 2026-08-04: ``artifacts_store`` held 0
files and no agent could be granted the tool, so the Artifacts screen had
nothing to display and never could.
"""
from __future__ import annotations

import inspect

from apowerb.tools_store.tool_manager import get_tools_store
from apowerb.tools_store.tools_helpers import load_agent_tools_functions


def test_artifacts_category_is_discovered():
    store = get_tools_store()
    assert "artifacts" in store.get_categories()


def test_artifacts_category_exposes_the_save_tool():
    store = get_tools_store()
    assert "artifacts.tool_save_code_artifact" in store.get_tools_in_category(
        "artifacts"
    )


def test_save_tool_is_loadable_by_its_catalogue_name():
    # The helper returns a (names, callables) pair.
    names, functions = load_agent_tools_functions(
        ["artifacts.tool_save_code_artifact"], owner_id="owner-under-test"
    )
    assert names == ["artifacts.tool_save_code_artifact"]
    assert len(functions) == 1
    assert functions[0].__name__ == "tool_save_code_artifact"


def test_tool_context_parameter_carries_no_annotation():
    """ADK matches ``tool_context`` by NAME; any annotation risks a NameError.

    Same guard as tests/test_tool_context_canonical.py — this module is the
    pattern that test cites as canonical, so it must keep honouring it.
    """
    from apowerb.tools_store.portfolio.artifacts import tool_save_code_artifact

    sig = inspect.signature(tool_save_code_artifact)
    param = sig.parameters["tool_context"]
    assert param.annotation is inspect.Parameter.empty
