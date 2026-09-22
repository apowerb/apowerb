"""Branchement de production des nœuds d'un graphe de workflow.

``workflow_graph`` ne sait pas exécuter un agent, un outil ni un sous-workflow :
il les reçoit (``run_agent``, ``run_tool``, ``run_subworkflow``). Ce module les
fournit pour un propriétaire donné, avec les mêmes règles que le reste du
produit :

* un nœud agent ne peut viser qu'un agent **du même propriétaire** (même
  filtre que ``get_agent``) ; il s'exécute par ``/run`` sous son jeton ;
* un nœud outil passe par ``load_agent_tools_functions``, qui filtre déjà
  les ``tool_config{id}`` par propriétaire. Référence : ``categorie.outil``,
  ou ``tool_config{id}:nom_de_fonction`` quand la configuration en expose
  plusieurs ;
* un nœud subworkflow ne peut viser qu'un workflow **du même propriétaire**
  (même filtre que ``workflow_main.get_workflow``, donc le même contrôle
  d'accès que pour lancer ce workflow directement via ``POST .../run``) —
  voir ``resolve_workflow_for``.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Optional

from apowerb.core.workflow_engine import access_token_factory, run_agent_message
from apowerb.core.workflow_graph import (
    GraphError,
    ResolveWorkflow,
    UpstreamArgs,
    WorkflowGraph,
)


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
    """(run_agent, run_tool) pour exécuter un graphe au nom de ``owner_email``."""
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

    return run_agent, run_tool


def resolve_workflow_for(owner_email: str) -> ResolveWorkflow:
    """``run_subworkflow`` de production : même filtre propriétaire que ``/run``.

    ``workflow_main.get_workflow`` ne renvoie rien pour un workflow d'autrui
    (même filtre que ``get_agent``, ``check_agent_owner``) : un identifiant
    inaccessible se comporte donc comme un identifiant inconnu — le graphe ne
    confirme jamais l'existence d'un workflow d'autrui. Bloquant (moteur de
    stockage synchrone) : déporté dans un thread pour ne pas geler la boucle
    d'événements pendant un run.
    """

    async def _resolve(workflow_id: str) -> Optional[WorkflowGraph]:
        from apowerb.core import workflow_main

        wf = await asyncio.to_thread(
            workflow_main.get_workflow, workflow_id, owner_id=owner_email
        )
        if wf is None:
            return None
        return WorkflowGraph.model_validate(wf["graph"])

    return _resolve
