"""Branchement de production des nœuds d'un graphe de workflow.

``workflow_graph`` ne sait pas exécuter un agent, un outil ni une recherche
RAG : il les reçoit (``run_agent``, ``run_tool``, ``run_rag``). Ce module les
fournit pour un propriétaire donné, avec les mêmes règles que le reste du
produit :

* un nœud agent ne peut viser qu'un agent **du même propriétaire** (même
  filtre que ``get_agent``) ; il s'exécute par ``/run`` sous son jeton ;
* un nœud outil passe par ``load_agent_tools_functions``, qui filtre déjà
  les ``tool_config{id}`` par propriétaire. Référence : ``categorie.outil``,
  ou ``tool_config{id}:nom_de_fonction`` quand la configuration en expose
  plusieurs ;
* un nœud rag vise le même agent (même contrôle d'appartenance) et
  interroge les bases de connaissances qui lui sont rattachées, lues comme
  ``GET /rag/knowledge/{agent_id}`` (``read_knowledge_map``), via
  ``tool_search_knowledge`` (appel bloquant, exécuté dans un thread).
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Optional

from apowerb.core.workflow_engine import access_token_factory, run_agent_message
from apowerb.core.workflow_graph import GraphError, UpstreamArgs


def _agent_number(agent_id: str) -> int:
    raw = str(agent_id)
    numeric = raw[len("agent") :] if raw.startswith("agent") else raw
    if not numeric.isdigit():
        raise GraphError(f"identifiant d'agent invalide : {raw!r}")
    return int(numeric)


def check_agent_owner(agent_id: str, owner_email: str) -> str:
    """Nom de dossier ADK de l'agent s'il appartient à ``owner_email``."""
    from apowerb.core.agent_helpers import get_agent_details

    number = _agent_number(agent_id)
    details = get_agent_details(agent_id=number) or {}
    if details.get("owner_id") != owner_email:
        # Introuvable plutôt qu'interdit : on ne confirme pas l'existence d'un
        # agent d'autrui.
        raise GraphError(
            f"agent introuvable : agent{number}",
            code="agent_not_found",
            params={"agent": f"agent{number}"},
        )
    return f"agent{number}"


def resolve_tool(tool_ref: str, owner_email: str):
    """La fonction Python d'un outil, résolue pour ``owner_email``."""
    from apowerb.tools_store.tools_helpers import load_agent_tools_functions

    ref, _, wanted = tool_ref.partition(":")
    names, funcs = load_agent_tools_functions(tools=[ref], owner_id=owner_email)
    if wanted:
        pairs = [
            (n, f)
            for n, f in zip(names, funcs)
            if n.split(".")[-1] == wanted or n == wanted
        ]
    else:
        pairs = list(zip(names, funcs))
    if not pairs:
        raise GraphError(
            f"outil introuvable : {tool_ref}",
            code="tool_not_found",
            params={"tool": tool_ref},
        )
    if len(pairs) > 1:
        options = ", ".join(n for n, _ in pairs)
        raise GraphError(
            f"{tool_ref} expose plusieurs fonctions ({options}) : précise ref:fonction",
            code="tool_ambiguous",
            params={"tool": tool_ref, "options": options},
        )
    return pairs[0][1]


def _knowledge_sources(folder: str) -> list[dict]:
    """Sources RAG indexées (statut ``complete``) de l'agent ``folder``.

    Même lecture que ``GET /rag/knowledge/{agent_id}``
    (``apowerb.routers.rag.status``), sans notion de session : un nœud de
    workflow n'a pas de ``session_id`` d'upload.
    """
    from apowerb.core.knowledge_map import read_knowledge_map

    kmap = read_knowledge_map(folder)
    return [
        s
        for s in kmap.get("sources", [])
        if s.get("status") == "complete" and s.get("knowledge_id")
    ]


def _search_rag(folder: str, agent_id: str, query: str, top_k: int) -> dict:
    """Interroge jusqu'à ``top_k`` bases de connaissances de l'agent (bloquant).

    ``tool_search_knowledge`` est une recherche conversationnelle (une
    question, une réponse), pas un moteur de passages notés : chaque base
    interrogée avec succès fournit un seul passage, sa réponse complète, sans
    score (le service n'en renvoie pas).
    """
    from apowerb.tools_store.portfolio.rag import tool_search_knowledge

    sources = _knowledge_sources(folder)
    if not sources:
        raise GraphError(
            f"agent {agent_id} : aucune base de connaissances disponible",
            code="rag_no_knowledge",
            params={"agent": agent_id},
        )
    passages = []
    for source in sources[:top_k]:
        kid = str(source["knowledge_id"])
        try:
            result = tool_search_knowledge(kid, query)
        except Exception:  # noqa: BLE001 - le service RAG est hors de notre contrôle
            result = {"status": "error"}
        if result.get("status") == "success":
            passages.append(
                {
                    "text": str(result.get("answer") or ""),
                    "source": source.get("name") or kid,
                    "score": None,
                }
            )
    if not passages:
        raise GraphError(
            "le service RAG a échoué pour toutes les bases interrogées",
            code="rag_failed",
            params={},
        )
    return {"query": query, "passages": passages}


async def call_tool(func, args: dict) -> Any:
    name = getattr(func, "__name__", str(func))
    signature = inspect.signature(func)
    params = signature.parameters
    if (
        "tool_context" in params
        and params["tool_context"].default is inspect.Parameter.empty
    ):
        raise GraphError(
            f"{name} dépend du contexte d'un agent : utilise-le dans un nœud agent",
            code="tool_needs_agent_context",
            params={"tool": name},
        )
    if isinstance(args, UpstreamArgs) and not any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    ):
        args = {k: v for k, v in args.items() if k in params}
    try:
        # Lier avant d'appeler : un TypeError levé *dans* l'outil n'est pas
        # un problème d'arguments et ne doit pas se déguiser en l'un d'eux.
        signature.bind(**args)
    except TypeError as exc:
        raise GraphError(
            f"{name} : arguments refusés ({exc})",
            code="tool_arguments",
            params={"tool": name, "problem": str(exc)},
        ) from None
    if inspect.iscoroutinefunction(func):
        return await func(**args)
    # Les outils du portfolio sont synchrones et souvent bloquants (HTTP, SQL).
    return await asyncio.to_thread(func, **args)


def bindings_for(owner_email: str, plan: Optional[str]):
    """(run_agent, run_tool, run_rag) pour exécuter un graphe au nom de ``owner_email``."""
    token_factory = access_token_factory(owner_email)

    async def run_agent(agent_id: str, message: str) -> Any:
        folder = check_agent_owner(agent_id, owner_email)
        return await run_agent_message(
            folder,
            message,
            owner_email=owner_email,
            plan=plan,
            token_factory=token_factory,
        )

    async def run_tool(tool_ref: str, args: dict) -> Any:
        return await call_tool(resolve_tool(tool_ref, owner_email), args or {})

    async def run_rag(agent_id: str, query: str, top_k: int) -> dict:
        folder = check_agent_owner(agent_id, owner_email)
        return await asyncio.to_thread(_search_rag, folder, agent_id, query, top_k)

    return run_agent, run_tool, run_rag
