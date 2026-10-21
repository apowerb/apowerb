"""Un nœud outil appelle un outil MCP sans agent, au nom du propriétaire.

``McpTool.run_async`` d'ADK exige un ``ToolContext`` complet (donc une
invocation d'agent) : un nœud ``tool`` ne pouvait pas viser un serveur MCP.
Référence : ``tool_config{id}:nom_de_l_outil``, où ``tool_config{id}`` est une
configuration ``mcp_server`` DU PROPRIÉTAIRE du workflow. Le runtime ouvre la
session MCP lui-même, appelle l'outil, puis la ferme.

Les tests parlent à un vrai serveur MCP (FastMCP, HTTP local) : la session,
la liste des outils, les en-têtes et les erreurs passent par le protocole.
stdio n'est jamais accepté dans un workflow (RCE corrigée en #260).
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time

import pytest
import uvicorn
from mcp.server.fastmcp import Context, FastMCP

from apowerb.core import workflow_mcp
from apowerb.core import workflow_runtime as rt
from apowerb.core.workflow_graph import GraphError, UpstreamArgs, tool_reported_error
from apowerb.tools_store import tools_helpers

ALICE = "alice@exemple.test"
BOB = "bob@exemple.test"

calls: list[str] = []


def _fake_server() -> FastMCP:
    mcp = FastMCP("factice", stateless_http=True, json_response=True)

    @mcp.tool()
    def echo(text: str, repeat: int = 1) -> dict:
        """Renvoie le texte reçu."""
        calls.append("echo")
        return {"text": text * repeat}

    @mcp.tool()
    def whoami(ctx: Context) -> dict:
        """En-tête d'authentification reçu par le serveur."""
        calls.append("whoami")
        request = ctx.request_context.request
        return {
            "api_key": request.headers.get("x-api-key"),
            "tenant": request.query_params.get("tenant"),
        }

    @mcp.tool()
    def boom() -> str:
        """Échoue toujours."""
        calls.append("boom")
        raise ValueError("stock indisponible")

    @mcp.tool()
    def zero():
        """Un scalaire JSON en texte."""
        return "0"

    @mcp.tool()
    def plain():
        """Texte brut, sans résultat structuré."""
        return "bonjour"

    return mcp


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve(app) -> tuple[uvicorn.Server, str]:
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if time.monotonic() > deadline:
            raise RuntimeError("serveur MCP factice non démarré")
        time.sleep(0.05)
    server.thread = thread
    return server, f"http://127.0.0.1:{port}"


def _stop(server: uvicorn.Server) -> None:
    server.should_exit = True
    server.thread.join(timeout=10)


@pytest.fixture(scope="module")
def http_url():
    server, base = _serve(_fake_server().streamable_http_app())
    yield f"{base}/mcp"
    _stop(server)


@pytest.fixture(scope="module")
def sse_url():
    server, base = _serve(_fake_server().sse_app())
    yield f"{base}/sse"
    _stop(server)


@pytest.fixture(autouse=True)
def _reset_calls():
    calls.clear()


def _config(owner: str, params: dict, category: str = "mcp_server") -> dict:
    return {
        "tool_config_id": "7",
        "owner_id": owner,
        "tool_category": category,
        "tool_name": f"mcp_{params.get('transport', 'http')}",
        "tool_config_params": params,
    }


@pytest.fixture
def configs(monkeypatch):
    """``tool_config7`` → la configuration posée par le test (filtre
    propriétaire/system identique à ``fetch_tool_configs``)."""
    store: dict[str, dict] = {}

    def fetch(tool_config_id, owner_id, items_to_select="*"):
        cfg = store.get(str(tool_config_id).replace("tool_config", ""))
        if not cfg or cfg["owner_id"] not in (owner_id, "system"):
            return {"status": 404, "message": "not found"}
        return cfg

    monkeypatch.setattr(tools_helpers, "fetch_tool_configs", fetch)
    return store


def _run(owner: str, ref: str, args):
    _agent, run_tool, _rag, _notify = rt.bindings_for(owner, None)
    return asyncio.run(run_tool(ref, args))


def test_tool_node_calls_an_mcp_tool_without_an_agent(configs, http_url):
    configs["7"] = _config(ALICE, {"transport": "http", "url": http_url})

    result = _run(ALICE, "tool_config7:echo", {"text": "salut", "repeat": 2})

    assert result == {"text": "salutsalut"}
    assert calls == ["echo"]


def test_mcp_call_uses_the_owner_config_headers_and_params(configs, http_url):
    configs["7"] = _config(
        ALICE,
        {
            "transport": "http",
            "url": http_url,
            "header_X-Api-Key": "cle-alice",
            "param_tenant": "alice",
        },
    )

    assert _run(ALICE, "tool_config7:whoami", {}) == {
        "api_key": "cle-alice",
        "tenant": "alice",
    }


def test_sse_transport_is_supported(configs, sse_url):
    configs["7"] = _config(ALICE, {"transport": "sse", "url": sse_url})

    assert _run(ALICE, "tool_config7:echo", {"text": "sse"}) == {"text": "sse"}


@pytest.mark.parametrize("owner", [BOB, "system"])
def test_another_owner_mcp_config_is_not_found(configs, http_url, owner):
    configs["7"] = _config(owner, {"transport": "http", "url": http_url})

    with pytest.raises(GraphError) as err:
        _run(ALICE, "tool_config7:echo", {"text": "x"})

    assert err.value.code == "tool_not_found"
    assert calls == []


def test_stdio_is_refused_even_when_the_deployment_enables_it(configs, monkeypatch):
    monkeypatch.setenv("MCP_STDIO_ENABLED", "1")
    monkeypatch.setattr(
        workflow_mcp,
        "_open",
        lambda *a, **k: pytest.fail("aucune session ne doit être ouverte"),
    )
    configs["7"] = _config(
        ALICE, {"transport": "stdio", "command": "touch", "args": "/tmp/pwned"}
    )

    with pytest.raises(GraphError) as err:
        _run(ALICE, "tool_config7:anything", {})

    assert err.value.code == "mcp_transport_refused"


def test_toolbox_db_config_is_reported_unsupported(configs):
    configs["7"] = _config(
        ALICE,
        {"transport": "http", "mcp_type": "toolbox-db", "url": "http://127.0.0.1:1"},
    )

    with pytest.raises(GraphError) as err:
        _run(ALICE, "tool_config7:list-tables", {})

    assert err.value.code == "mcp_transport_refused"


def test_mcp_tool_error_becomes_a_reported_error(configs, http_url):
    configs["7"] = _config(ALICE, {"transport": "http", "url": http_url})

    result = _run(ALICE, "tool_config7:boom", {})

    assert result["status"] == "error"
    assert "stock indisponible" in tool_reported_error(result)


def test_text_only_result_is_returned_as_text(configs, http_url):
    configs["7"] = _config(ALICE, {"transport": "http", "url": http_url})

    assert _run(ALICE, "tool_config7:plain", {}) == {"text": "bonjour"}


def test_unknown_mcp_tool_name_is_not_found(configs, http_url):
    configs["7"] = _config(ALICE, {"transport": "http", "url": http_url})

    with pytest.raises(GraphError) as err:
        _run(ALICE, "tool_config7:nope", {})

    assert err.value.code == "tool_not_found"
    assert "echo" in err.value.params["options"]


def test_server_with_several_tools_needs_a_tool_name(configs, http_url):
    configs["7"] = _config(ALICE, {"transport": "http", "url": http_url})

    with pytest.raises(GraphError) as err:
        _run(ALICE, "tool_config7", {})

    assert err.value.code == "tool_ambiguous"


def test_upstream_args_are_filtered_to_the_tool_schema(configs, http_url):
    configs["7"] = _config(ALICE, {"transport": "http", "url": http_url})

    result = _run(
        ALICE, "tool_config7:echo", UpstreamArgs({"text": "ok", "subject": "bruit"})
    )

    assert result == {"text": "ok"}


def test_missing_required_argument_is_refused_before_the_call(configs, http_url):
    configs["7"] = _config(ALICE, {"transport": "http", "url": http_url})

    with pytest.raises(GraphError) as err:
        _run(ALICE, "tool_config7:echo", {"repeat": 2})

    assert err.value.code == "tool_arguments"
    assert calls == []


def test_unreachable_server_does_not_leak_the_url(configs):
    configs["7"] = _config(
        ALICE,
        {
            "transport": "http",
            "url": f"http://127.0.0.1:{_free_port()}/mcp",
            "param_token": "secret-dans-l-url",
        },
    )

    with pytest.raises(GraphError) as err:
        _run(ALICE, "tool_config7:echo", {"text": "x"})

    assert err.value.code == "mcp_unreachable"
    assert "secret-dans-l-url" not in str(err.value)
    assert "secret-dans-l-url" not in repr(err.value.params)
    assert "secret-dans-l-url" not in repr(rt.resolve_tool("tool_config7:echo", ALICE))


def test_schema_of_an_mcp_tool_comes_from_its_input_schema(configs, http_url):
    configs["7"] = _config(ALICE, {"transport": "http", "url": http_url})
    tool = rt.resolve_tool("tool_config7:echo", ALICE)

    schema = asyncio.run(workflow_mcp.arg_schema(tool))

    assert schema["needs_agent_context"] is False
    assert schema["description"] == "Renvoie le texte reçu."
    by_name = {p["name"]: p for p in schema["params"]}
    assert by_name["text"]["required"] is True
    assert by_name["repeat"] == {
        "name": "repeat",
        "type": "integer",
        "required": False,
        "default": 1,
        "description": None,
        "enum": None,
    }


@pytest.fixture
def schema_client():
    from unittest.mock import MagicMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from apowerb.auth.dependencies import get_current_user
    from apowerb.routers import workflows as workflows_module

    app = FastAPI()
    app.include_router(workflows_module.router, prefix="/api")
    user = MagicMock(email=ALICE, user_id=1, role="USER")

    async def _user_override():
        return user

    app.dependency_overrides[get_current_user] = _user_override
    return TestClient(app)


def test_schema_route_reads_an_mcp_tool(configs, http_url, schema_client):
    configs["7"] = _config(ALICE, {"transport": "http", "url": http_url})

    resp = schema_client.get(
        "/api/workflows/tools/schema", params={"tool": "tool_config7:echo"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["tool"] == "tool_config7:echo"
    assert [p["name"] for p in resp.json()["params"]] == ["text", "repeat"]


def test_schema_route_502_when_the_mcp_server_is_down(configs, schema_client):
    configs["7"] = _config(
        ALICE, {"transport": "http", "url": f"http://127.0.0.1:{_free_port()}/mcp"}
    )

    resp = schema_client.get(
        "/api/workflows/tools/schema", params={"tool": "tool_config7:echo"}
    )

    assert resp.status_code == 502
    assert resp.json()["detail"]["code"] == "mcp_unreachable"


def test_inactive_mcp_config_is_not_found(configs, http_url):
    configs["7"] = {
        **_config(ALICE, {"transport": "http", "url": http_url}),
        "tool_config_type": "inactive",
    }

    with pytest.raises(GraphError) as err:
        _run(ALICE, "tool_config7:echo", {"text": "x"})

    assert err.value.code == "tool_not_found"
    assert calls == []


def test_json_scalar_text_stays_text(configs, http_url):
    configs["7"] = _config(ALICE, {"transport": "http", "url": http_url})

    assert _run(ALICE, "tool_config7:zero", {}) == {"text": "0"}


def test_json_rpc_error_on_call_is_a_reported_error_not_unreachable(
    configs, monkeypatch
):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    from mcp.shared.exceptions import McpError
    from mcp.types import ErrorData

    class _Session:
        async def list_tools(self):
            tool = SimpleNamespace(name="echo", inputSchema={}, description="")
            return SimpleNamespace(tools=[tool])

        async def call_tool(self, name, arguments, read_timeout_seconds):
            assert read_timeout_seconds.total_seconds() > 0
            raise McpError(ErrorData(code=-32602, message="paramètre invalide"))

    @asynccontextmanager
    async def _fake_open(tool):
        yield _Session()

    monkeypatch.setattr(workflow_mcp, "_open", _fake_open)
    configs["7"] = _config(ALICE, {"transport": "http", "url": "http://x.test/mcp"})

    result = _run(ALICE, "tool_config7:echo", {})

    assert tool_reported_error(result) == "paramètre invalide"


def test_schema_route_422_for_a_refused_transport(configs, schema_client):
    configs["7"] = _config(ALICE, {"transport": "stdio", "command": "echo"})

    resp = schema_client.get(
        "/api/workflows/tools/schema", params={"tool": "tool_config7:x"}
    )

    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "mcp_transport_refused"
