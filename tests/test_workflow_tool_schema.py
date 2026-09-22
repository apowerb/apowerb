"""Schéma des arguments d'un outil (roadmap: studio de workflows).

``tool_arg_schema`` introspecte une fonction d'outil pour que le studio
demande automatiquement les bons champs selon l'outil choisi dans un nœud
tool. Elle ne change rien à ``call_tool`` : mêmes exclusions (tool_context
requis, *args, **kwargs), même code d'erreur pour le contexte d'agent.

Deuxième bloc : la route ``GET /api/workflows/tools/schema``.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient
from google.adk.tools.tool_context import ToolContext

from apowerb.core.workflow_runtime import tool_arg_schema


# ---------------------------------------------------------------------------
# Fonctions factices exerçant chaque cas d'introspection
# ---------------------------------------------------------------------------


def simple_types(name: str, count: int, ratio: float, active: bool) -> dict:
    """Une ligne de résumé.

    Args:
        name: le nom
        count: le compte
        ratio: le ratio
        active: actif ou non
    """
    return {}


def with_optional(city: str, country: Optional[str] = None) -> str:
    """Prévisions pour une ville.

    Args:
        city: la ville visée
        country: le pays, optionnel
    """
    return city


def with_literal(mode: Literal["fast", "slow", "auto"] = "auto") -> str:
    """Choisit un mode.

    Args:
        mode: fast, slow ou auto
    """
    return mode


def with_defaults(limit: int = 100, tag: str = "") -> dict:
    """Liste paginée.

    Args:
        limit: nombre maximum d'éléments
        tag: filtre optionnel
    """
    return {}


def with_kwargs(city: str, **kwargs) -> dict:
    """Accepte des extras.

    Args:
        city: la ville
    """
    return {"city": city, **kwargs}


def with_lists_and_dicts(
    tags: List[str], meta: Dict[str, Any], anything: list, obj: dict
) -> dict:
    """Types composés.

    Args:
        tags: étiquettes
        meta: métadonnées
        anything: liste non paramétrée
        obj: dict non paramétré
    """
    return {}


def needs_agent_context(query: str, tool_context: ToolContext) -> str:
    """Cherche dans la mémoire de l'agent.

    Args:
        query: la requête
    """
    return query


def optional_agent_context(
    query: str, tool_context: Optional[ToolContext] = None
) -> str:
    """Fonctionne aussi sans agent (tool_context est optionnel ici)."""
    return query


def no_docstring(x: str, y: int = 3):
    return x


def no_args_section(city: str) -> str:
    """Une seule ligne, sans section Args."""
    return city


# ---------------------------------------------------------------------------
# tool_arg_schema — cas unitaires
# ---------------------------------------------------------------------------


def test_simple_types_are_mapped_to_json_types():
    schema = tool_arg_schema(simple_types)
    by_name = {p["name"]: p for p in schema["params"]}
    assert by_name["name"]["type"] == "string"
    assert by_name["count"]["type"] == "integer"
    assert by_name["ratio"]["type"] == "number"
    assert by_name["active"]["type"] == "boolean"
    assert all(p["required"] for p in schema["params"])
    assert schema["description"] == "Une ligne de résumé."
    assert schema["accepts_kwargs"] is False
    assert schema["needs_agent_context"] is False


def test_optional_unwraps_to_the_inner_type():
    schema = tool_arg_schema(with_optional)
    by_name = {p["name"]: p for p in schema["params"]}
    assert by_name["country"]["type"] == "string"
    assert by_name["country"]["required"] is False
    assert by_name["country"]["default"] is None
    assert by_name["city"]["required"] is True
    assert by_name["country"]["description"] == "le pays, optionnel"


def test_literal_becomes_an_enum():
    schema = tool_arg_schema(with_literal)
    (mode,) = schema["params"]
    assert mode["type"] == "string"
    assert mode["enum"] == ["fast", "slow", "auto"]
    assert mode["required"] is False
    assert mode["default"] == "auto"


def test_default_values_are_reported_and_not_required():
    schema = tool_arg_schema(with_defaults)
    by_name = {p["name"]: p for p in schema["params"]}
    assert by_name["limit"]["required"] is False
    assert by_name["limit"]["default"] == 100
    assert by_name["tag"]["default"] == ""


def test_var_keyword_is_excluded_but_flagged():
    schema = tool_arg_schema(with_kwargs)
    names = [p["name"] for p in schema["params"]]
    assert names == ["city"]
    assert schema["accepts_kwargs"] is True


def test_list_and_dict_annotations():
    schema = tool_arg_schema(with_lists_and_dicts)
    by_name = {p["name"]: p for p in schema["params"]}
    assert by_name["tags"]["type"] == "array"
    assert by_name["meta"]["type"] == "object"
    assert by_name["anything"]["type"] == "array"
    assert by_name["obj"]["type"] == "object"


def test_google_docstring_args_are_parsed():
    schema = tool_arg_schema(with_defaults)
    by_name = {p["name"]: p for p in schema["params"]}
    assert by_name["limit"]["description"] == "nombre maximum d'éléments"
    assert by_name["tag"]["description"] == "filtre optionnel"


def test_missing_docstring_yields_no_description():
    schema = tool_arg_schema(no_docstring)
    assert schema["description"] is None
    by_name = {p["name"]: p for p in schema["params"]}
    assert by_name["x"]["description"] is None
    assert by_name["y"]["default"] == 3


def test_docstring_without_args_section_still_gives_tool_description():
    schema = tool_arg_schema(no_args_section)
    assert schema["description"] == "Une seule ligne, sans section Args."
    (city,) = schema["params"]
    assert city["description"] is None


def test_tool_context_excluded_and_flags_agent_context_when_required():
    schema = tool_arg_schema(needs_agent_context)
    names = [p["name"] for p in schema["params"]]
    assert "tool_context" not in names
    assert names == ["query"]
    assert schema["needs_agent_context"] is True


def test_tool_context_with_default_does_not_need_agent_context():
    schema = tool_arg_schema(optional_agent_context)
    names = [p["name"] for p in schema["params"]]
    assert "tool_context" not in names
    assert schema["needs_agent_context"] is False


# ---------------------------------------------------------------------------
# Route GET /api/workflows/tools/schema
# ---------------------------------------------------------------------------

USER_EMAIL = "alice@example.com"


def _fake_user(email: str = USER_EMAIL):
    u = MagicMock()
    u.email = email
    u.user_id = 1
    u.role = "USER"
    return u


@pytest.fixture()
def client():
    from apowerb.auth.dependencies import get_current_user
    from apowerb.routers import workflows as workflows_module

    app = FastAPI()
    app.include_router(workflows_module.router, prefix="/api")

    async def _user_override():
        return _fake_user()

    app.dependency_overrides[get_current_user] = _user_override
    return TestClient(app)


def test_schema_route_200_on_a_real_portfolio_tool(client):
    resp = client.get(
        "/api/workflows/tools/schema",
        params={"tool": "marketing.tool_hubspot_get_sales_leads"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tool"] == "marketing.tool_hubspot_get_sales_leads"
    assert body["description"] == "Retrieve sales leads from HubSpot."
    by_name = {p["name"]: p for p in body["params"]}
    assert by_name["limit"] == {
        "name": "limit",
        "type": "integer",
        "required": False,
        "default": 100,
        "description": "Maximum number of leads to return (1-100, default 100)",
        "enum": None,
    }
    assert by_name["after_date"]["required"] is False
    assert by_name["after_date"]["type"] == "string"
    assert "api_key" in by_name
    assert body["accepts_kwargs"] is False
    assert body["needs_agent_context"] is False


def test_schema_route_404_on_unknown_tool(client):
    resp = client.get("/api/workflows/tools/schema", params={"tool": "does.not_exist"})
    assert resp.status_code == 404, resp.text
    body = resp.json()["detail"]
    assert body["code"] == "tool_not_found"
    assert body["params"] == {"tool": "does.not_exist"}


def test_schema_route_without_auth_returns_401():
    from apowerb.routers import workflows as workflows_module
    from apowerb.auth.dependencies import get_current_user

    app = FastAPI()
    app.include_router(workflows_module.router, prefix="/api")

    async def _deny():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )

    app.dependency_overrides[get_current_user] = _deny
    c = TestClient(app)
    resp = c.get(
        "/api/workflows/tools/schema",
        params={"tool": "marketing.tool_hubspot_get_sales_leads"},
    )
    assert resp.status_code == 401, resp.text
