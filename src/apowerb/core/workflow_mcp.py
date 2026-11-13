"""Appel d'un outil MCP par un nœud ``tool`` de workflow, sans agent.

``McpTool.run_async`` d'ADK exige un ``ToolContext`` complet, donc une
invocation d'agent : un nœud outil ne pouvait pas viser un serveur MCP. Ce
module ouvre la session MCP lui-même avec le client du SDK ``mcp`` (mêmes
transports que les agents), liste les outils, appelle celui demandé, puis
ferme la session. Pas ``MCPSessionManager`` d'ADK : à chaque session, il
sonde ``google.auth.default()`` pour un éventuel mTLS Google (~3 s sans
identifiants GCP), inutile pour un serveur tiers.

Référence : ``tool_config{id}[:outil]``, où ``tool_config{id}`` est une
configuration ``mcp_server`` (``POST /mcp_configs``) appartenant AU
PROPRIÉTAIRE du workflow — ni ``system``, ni celle d'un autre compte. Les
en-têtes (``header_*``) et paramètres d'URL (``param_*``) de cette
configuration sont ceux du propriétaire : l'appel se fait en son nom.

Transports : ``http`` (Streamable HTTP) et ``sse``. ``stdio`` est toujours
refusé ici, même si ``MCP_STDIO_ENABLED`` l'autorise pour les agents : un
workflow tourne sans session utilisateur (déclencheurs, planification), et
lancer une commande locale depuis un graphe est la RCE corrigée en #260.
``toolbox-db`` (MCP Toolbox, qui n'est pas du MCP standard) n'est pas pris en
charge non plus : le nœud ``db_write`` couvre l'écriture en base.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import timedelta
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Awaitable, Callable, Optional
from urllib.parse import urlencode

import httpx
from mcp import ClientSession
from mcp.shared.exceptions import McpError
from mcp.client.sse import sse_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from apowerb.core.workflow_graph import GraphError, UpstreamArgs

logger = logging.getLogger(__name__)

_CONFIG_REF = re.compile(r"^tool_config\d+$")
_CONNECT_TIMEOUT_S = 10.0
_READ_TIMEOUT_S = 120.0
# Délai maximal d'un appel d'outil, même si le flux reste vivant (pings).
_CALL_TIMEOUT_S = 300.0
_JSON_TYPES = {"string", "integer", "number", "boolean", "array", "object"}


@dataclass(frozen=True)
class McpNodeTool:
    """Un outil MCP résolu pour un propriétaire (aucune connexion ouverte)."""

    ref: str
    tool_name: str
    transport: str
    # Secrets du propriétaire (paramètres déchiffrés, en-têtes) : hors repr.
    url: str = field(repr=False)
    headers: dict = field(repr=False)


def resolve(tool_ref: str, owner_email: str) -> Optional[McpNodeTool]:
    """L'outil MCP visé par ``tool_ref``, ou ``None`` si la référence n'est
    pas une configuration ``mcp_server`` (le runtime continue alors avec les
    outils Python).

    La configuration d'un autre compte (ou ``system``) est « introuvable »,
    pas « interdite » : on ne confirme pas son existence.
    """
    ref, _, wanted = tool_ref.partition(":")
    if not _CONFIG_REF.match(ref):
        return None
    from apowerb.tools_store.tools_helpers import fetch_tool_configs

    config = fetch_tool_configs(ref, owner_id=owner_email) or {}
    if config.get("tool_category") != "mcp_server":
        return None
    if (
        config.get("owner_id") != owner_email
        or config.get("tool_config_type", "active") != "active"
    ):
        raise GraphError(
            f"outil introuvable : {tool_ref}",
            code="tool_not_found",
            params={"tool": tool_ref},
        )
    params = config.get("tool_config_params") or {}
    transport = str(params.get("transport") or "http").lower()
    if transport not in ("http", "sse") or params.get("mcp_type") == "toolbox-db":
        kind = "toolbox-db" if params.get("mcp_type") == "toolbox-db" else transport
        raise GraphError(
            f"{ref} : le transport MCP {kind} n'est pas utilisable dans un "
            "workflow (http ou sse seulement)",
            code="mcp_transport_refused",
            params={"tool": tool_ref, "transport": kind},
        )
    url = str(params.get("url") or "").strip()
    if not url:
        raise GraphError(
            f"{ref} : aucune URL de serveur MCP",
            code="mcp_unreachable",
            params={"tool": tool_ref},
        )
    query = {k[len("param_") :]: v for k, v in params.items() if k.startswith("param_")}
    if query:
        url = f"{url}{'&' if '?' in url else '?'}{urlencode(query)}"
    headers = {
        k[len("header_") :]: str(v)
        for k, v in params.items()
        if k.startswith("header_")
    }
    return McpNodeTool(
        ref=ref, tool_name=wanted, transport=transport, url=url, headers=headers
    )


@asynccontextmanager
async def _open(tool: McpNodeTool) -> AsyncIterator[ClientSession]:
    headers = tool.headers or None
    if tool.transport == "sse":
        transport = sse_client(
            tool.url,
            headers=headers,
            timeout=_CONNECT_TIMEOUT_S,
            sse_read_timeout=_READ_TIMEOUT_S,
        )
        async with transport as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session
        return
    client = create_mcp_http_client(
        headers=headers,
        timeout=httpx.Timeout(_CONNECT_TIMEOUT_S, read=_READ_TIMEOUT_S),
    )
    async with (
        client,
        streamable_http_client(tool.url, http_client=client) as (
            read,
            write,
            _session_id,
        ),
    ):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def _with_session(
    tool: McpNodeTool, work: Callable[[ClientSession, list], Awaitable[Any]]
) -> Any:
    """``work(session, outils)`` dans une session ouverte puis fermée.

    Une ``GraphError`` levée par ``work`` est relancée telle quelle, hors des
    groupes de tâches du transport (qui l'envelopperaient) ; toute autre
    erreur devient ``mcp_unreachable``, sans l'URL (paramètres déchiffrés en
    query) ni les en-têtes : seulement le type d'exception, comme mcp_loader.
    """
    outcome: Optional[tuple[bool, Any]] = None
    try:
        async with _open(tool) as session:
            tools = (await session.list_tools()).tools
            try:
                outcome = (True, await work(session, tools))
            except GraphError as exc:
                outcome = (False, exc)
    except Exception as exc:  # noqa: BLE001 - transport tiers, détails journalisés
        if outcome is None:
            logger.warning(
                "[workflow mcp] %s : session en échec (%s)",
                tool.ref,
                type(exc).__name__,
            )
            raise GraphError(
                f"{tool.ref} : serveur MCP injoignable ou en erreur",
                code="mcp_unreachable",
                params={"tool": tool.ref},
            ) from None
        # Résultat déjà obtenu : une erreur de fermeture ne l'annule pas.
        logger.debug("[workflow mcp] fermeture : %s", type(exc).__name__)
    ok, value = outcome
    if not ok:
        raise value
    return value


def _pick(tool: McpNodeTool, tools: list) -> Any:
    options = ", ".join(t.name for t in tools)
    if tool.tool_name:
        for t in tools:
            if t.name == tool.tool_name:
                return t
        raise GraphError(
            f"outil introuvable : {tool.ref}:{tool.tool_name} (disponibles : {options})",
            code="tool_not_found",
            params={"tool": f"{tool.ref}:{tool.tool_name}", "options": options},
        )
    if len(tools) == 1:
        return tools[0]
    raise GraphError(
        f"{tool.ref} expose plusieurs outils ({options}) : précise ref:outil",
        code="tool_ambiguous",
        params={"tool": tool.ref, "options": options},
    )


def _checked_args(name: str, schema: dict, args: dict) -> dict:
    """Mêmes règles que ``call_tool`` pour une fonction Python : les sorties
    amont (``UpstreamArgs``) sont filtrées sur les propriétés déclarées ; un
    argument obligatoire manquant est refusé avant tout appel."""
    props = schema.get("properties") or {}
    if (
        isinstance(args, UpstreamArgs)
        and schema.get("additionalProperties") is not True
    ):
        args = {k: v for k, v in args.items() if k in props}
    missing = [r for r in schema.get("required") or [] if r not in args]
    if missing:
        problem = f"argument(s) obligatoire(s) manquant(s) : {', '.join(missing)}"
        raise GraphError(
            f"{name} : arguments refusés ({problem})",
            code="tool_arguments",
            params={"tool": name, "problem": problem},
        )
    return dict(args)


def _texts(result: Any) -> list[str]:
    return [c.text for c in result.content or [] if getattr(c, "type", None) == "text"]


def _as_output(result: Any) -> Any:
    """Le résultat MCP sous la forme attendue par un nœud outil.

    Une erreur MCP (``isError``) devient ``{"status": "error", ...}`` : la
    convention des outils du portfolio, que ``tool_reported_error`` (et donc
    ``on_error``) traite déjà.
    """
    texts = _texts(result)
    if result.isError:
        return {
            "status": "error",
            "error_message": "\n".join(texts) or "erreur MCP sans message",
        }
    if result.structuredContent is not None:
        return result.structuredContent
    if len(texts) == 1:
        try:
            parsed = json.loads(texts[0])
        except ValueError:
            parsed = None
        # Un objet ou une liste JSON ; un scalaire (« null », « 0 ») reste du texte.
        return parsed if isinstance(parsed, (dict, list)) else {"text": texts[0]}
    return {"content": [c.model_dump(mode="json") for c in result.content or []]}


async def call(tool: McpNodeTool, args: dict) -> Any:
    """Appelle l'outil MCP au nom du propriétaire (session ouverte puis fermée)."""

    async def work(session: ClientSession, tools: list) -> Any:
        target = _pick(tool, tools)
        arguments = _checked_args(target.name, target.inputSchema or {}, args or {})
        try:
            result = await session.call_tool(
                target.name,
                arguments=arguments,
                read_timeout_seconds=timedelta(seconds=_CALL_TIMEOUT_S),
            )
        except McpError as exc:
            # Réponse JSON-RPC d'erreur (arguments refusés, délai dépassé...) :
            # le serveur a répondu, ce n'est pas une panne de transport.
            message = str(exc.error.message or "erreur MCP sans message")
            return {"status": "error", "error_message": message[:500]}
        return _as_output(result)

    return await _with_session(tool, work)


def _param(name: str, spec: dict, required: bool) -> dict:
    spec = dict(spec)
    for option in spec.pop("anyOf", None) or []:
        if option.get("type") not in (None, "null"):
            spec = {**option, **spec}
            break
    kind = spec.get("type")
    return {
        "name": name,
        "type": kind if kind in _JSON_TYPES else "string",
        "required": required,
        "default": None if required else spec.get("default"),
        "description": spec.get("description"),
        "enum": spec.get("enum"),
    }


async def arg_schema(tool: McpNodeTool) -> dict:
    """Même forme que ``workflow_runtime.tool_arg_schema``, lue dans
    l'``inputSchema`` annoncé par le serveur MCP."""

    async def work(_session: ClientSession, tools: list) -> Any:
        return _pick(tool, tools)

    target = await _with_session(tool, work)
    schema = target.inputSchema or {}
    required = set(schema.get("required") or [])
    return {
        "description": target.description,
        "params": [
            _param(name, spec or {}, name in required)
            for name, spec in (schema.get("properties") or {}).items()
        ],
        "accepts_kwargs": schema.get("additionalProperties") is True,
        "needs_agent_context": False,
    }
