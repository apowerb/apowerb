"""Appel d un outil depuis un nœud de graphe (workflow_runtime.call_tool).

Le 21/09, un nœud Tool sans arguments déclarés recevait tout le payload du
trigger en kwargs : ``get_weather(**{"message": ...})`` levait un TypeError.
L entrée amont implicite ne garde que les paramètres que l outil accepte ;
des arguments déclarés par l utilisateur restent passés tels quels.
"""

import asyncio

import pytest

from apowerb.core.workflow_graph import UpstreamArgs
from apowerb.core.workflow_runtime import call_tool


def get_weather(city: str = "Paris") -> str:
    return f"weather:{city}"


def takes_kwargs(**kwargs) -> dict:
    return kwargs


def test_implicit_upstream_input_keeps_only_accepted_parameters():
    out = asyncio.run(call_tool(get_weather, UpstreamArgs(message="hi", city="Metz")))
    assert out == "weather:Metz"


def test_implicit_upstream_input_without_matching_keys_uses_defaults():
    assert asyncio.run(call_tool(get_weather, UpstreamArgs(message="hi"))) == "weather:Paris"


def test_var_keyword_tool_receives_the_whole_upstream_input():
    out = asyncio.run(call_tool(takes_kwargs, UpstreamArgs(message="hi")))
    assert out == {"message": "hi"}


def test_declared_args_are_passed_verbatim():
    with pytest.raises(TypeError):
        asyncio.run(call_tool(get_weather, {"message": "hi"}))
