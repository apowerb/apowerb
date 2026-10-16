"""Tool declarations lose what the model does not need on every call.

Measured on the catalogue (tiktoken o200k): the pydantic ``title`` fields
cost 5,678 tokens and the ``Returns:`` sections 5,788, over 172 tools; both
are re-sent on every model call of a turn.
"""
from __future__ import annotations

import json
from typing import Literal

from google.adk.models import LlmRequest
from google.adk.tools import FunctionTool

from apowerb.core.tool_declarations import slim_tool_declarations


def tool_sample(source: str, mode: Literal["fast", "full"] = "fast", limit: int = 10) -> dict:
    """Do something with a source.

    Args:
        source: Where to read from.
        mode: How hard to try.
        limit: Max rows.

    Returns:
        dict: status, rows (list of dicts), and many other details the
        model can read from the result itself.

    Raises:
        Nothing, errors come back in the dict.
    """
    return {}


def _declaration(req: LlmRequest):
    return req.config.tools[0].function_declarations[0]


def _request() -> LlmRequest:
    req = LlmRequest()
    req.append_tools([FunctionTool(tool_sample)])
    return req


def test_titles_and_returns_are_removed_the_rest_is_kept():
    req = _request()
    before = _declaration(req).model_dump(exclude_none=True, mode="json")
    assert "title" in json.dumps(before["parameters_json_schema"])

    slim_tool_declarations(req)

    decl = _declaration(req)
    schema = decl.parameters_json_schema
    assert "title" not in json.dumps(schema)
    assert schema["required"] == ["source"]
    assert schema["properties"]["mode"]["enum"] == ["fast", "full"]
    assert schema["properties"]["limit"]["default"] == 10
    assert "Args:" in decl.description and "limit: Max rows." in decl.description
    assert "Returns:" not in decl.description and "many other details" not in decl.description
    assert "Raises:" in decl.description
    assert len(json.dumps(decl.model_dump(exclude_none=True, mode="json"))) < len(json.dumps(before))


def test_property_named_title_survives():
    def tool_titled(title: str) -> dict:
        """Create a thing.

        Args:
            title: Its title.
        """
        return {}

    req = LlmRequest()
    req.append_tools([FunctionTool(tool_titled)])
    slim_tool_declarations(req)

    schema = _declaration(req).parameters_json_schema
    assert "title" in schema["properties"]
    assert schema["required"] == ["title"]


def test_idempotent_and_safe_without_tools():
    req = _request()
    slim_tool_declarations(req)
    once = _declaration(req).model_dump(exclude_none=True, mode="json")
    slim_tool_declarations(req)
    assert _declaration(req).model_dump(exclude_none=True, mode="json") == once
    slim_tool_declarations(LlmRequest())


def test_litellm_still_converts_the_slimmed_declaration():
    from google.adk.models.lite_llm import _function_declaration_to_tool_param

    req = _request()
    slim_tool_declarations(req)
    param = _function_declaration_to_tool_param(_declaration(req))

    assert param["function"]["name"] == "tool_sample"
    assert param["function"]["parameters"]["required"] == ["source"]
