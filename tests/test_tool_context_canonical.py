"""Regression tests for the tool_context signature pattern in scei_mail.

The 2026-05-18 cutover failed 3 times. The 3rd fail was a NameError on
ToolContext raised by ADK at tool instantiation: ``get_type_hints`` on
the function signature tried to resolve the forward-ref ``"ToolContext
| None"`` but the import was gated by ``TYPE_CHECKING`` and absent at
runtime.

The fix: drop the annotation entirely. ADK matches ``tool_context`` by
parameter NAME, not by type (cf adk/.../function_tool.py). The
canonical project pattern (cf save_code_artifact.py) declares
``tool_context`` without any annotation.

These tests guard the regression:
* the module imports cleanly (no ToolContext NameError)
* the parameter exists with default ``None``
* the parameter has NO annotation (any annotation re-introduces the
  get_type_hints risk)
* ``typing.get_type_hints`` on the function does NOT raise
"""

from __future__ import annotations

import inspect
import typing


def test_module_imports_without_toolcontext_nameerror():
    # If the previous TYPE_CHECKING + forward-ref pattern is reintroduced,
    # this import (which mirrors ADK's tool registration at boot) will
    # raise NameError.
    from th2customers.scei.tools import scei_mail

    assert callable(scei_mail.tool_send_scei_mail)


def test_tool_context_param_default_is_none():
    from th2customers.scei.tools.scei_mail import tool_send_scei_mail

    sig = inspect.signature(tool_send_scei_mail)
    assert "tool_context" in sig.parameters
    assert sig.parameters["tool_context"].default is None


def test_tool_context_has_no_annotation():
    """ADK identifies the param by name, not by type. Any annotation
    (string or class) drags ``get_type_hints`` into play and re-opens
    the door to the 2026-05-18 NameError."""
    from th2customers.scei.tools.scei_mail import tool_send_scei_mail

    sig = inspect.signature(tool_send_scei_mail)
    annotation = sig.parameters["tool_context"].annotation
    assert annotation is inspect.Parameter.empty, (
        f"tool_context must have NO annotation (got {annotation!r}). "
        "ADK matches by parameter name. Adding an annotation re-introduces "
        "the get_type_hints forward-ref resolution trap that broke the "
        "2026-05-18 cutover."
    )


def test_get_type_hints_does_not_raise():
    """This is exactly what ADK does at tool boot (function_tool.py)."""
    from th2customers.scei.tools.scei_mail import tool_send_scei_mail

    # Must not raise NameError, AttributeError, anything.
    hints = typing.get_type_hints(tool_send_scei_mail)
    # tool_context must NOT appear in hints (because we removed annotation)
    assert "tool_context" not in hints, (
        f"tool_context should have no annotation; get_type_hints "
        f"returned {hints}"
    )
