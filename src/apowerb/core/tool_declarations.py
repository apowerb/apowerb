"""Trim the tool declarations sent to the model on every call.

ADK builds each declaration from the function: the docstring becomes the
description and pydantic the JSON schema. Two parts of it tell the model
nothing it can use, and are re-sent on every model call of a turn (measured
on the catalogue with tiktoken o200k, over 172 tools):

- the schema ``title`` of every parameter (``"source" -> "Source"``):
  5,678 tokens;
- the ``Returns:`` section of the docstring, which the result itself
  shows when the tool runs: 5,788 tokens.

The docstrings stay as they are in the code; only the request is trimmed.
"""
from __future__ import annotations

import re

_SECTION = re.compile(r"^\s*([A-Z][A-Za-z ]*):\s*$")
_RETURNS = re.compile(r"^\s*Returns?:\s*$")


def _drop_titles(node) -> None:
    if isinstance(node, dict):
        # A "title" holding a string is schema metadata; under "properties"
        # a "title" holds a schema, i.e. a parameter called title.
        if isinstance(node.get("title"), str):
            del node["title"]
        for value in node.values():
            _drop_titles(value)
    elif isinstance(node, list):
        for item in node:
            _drop_titles(item)


def _drop_returns_section(description: str) -> str:
    lines = description.split("\n")
    kept: list[str] = []
    skipping = False
    for line in lines:
        if _RETURNS.match(line):
            skipping = True
            continue
        if skipping and _SECTION.match(line):
            skipping = False
        if not skipping:
            kept.append(line)
    return "\n".join(kept).rstrip()


def slim_tool_declarations(llm_request) -> None:
    """Remove schema titles and ``Returns:`` sections, in place."""
    config = getattr(llm_request, "config", None)
    for tool in getattr(config, "tools", None) or []:
        for decl in getattr(tool, "function_declarations", None) or []:
            if decl.parameters_json_schema:
                _drop_titles(decl.parameters_json_schema)
            if decl.description:
                decl.description = _drop_returns_section(decl.description)
