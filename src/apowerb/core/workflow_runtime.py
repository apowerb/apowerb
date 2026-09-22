"""Branchement de production des nœuds d'un graphe de workflow.

``workflow_graph`` ne sait pas exécuter un agent, un outil, une recherche
RAG ni un sous-workflow : il les reçoit (``run_agent``, ``run_tool``,
``run_rag``, ``run_subworkflow``). Ce module les
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
* un nœud subworkflow ne peut viser qu'un workflow **du même propriétaire**
  (même filtre que ``workflow_main.get_workflow``, donc le même contrôle
  d'accès que pour lancer ce workflow directement via ``POST .../run``) —
  voir ``resolve_workflow_for``.
"""

from __future__ import annotations

import asyncio
import enum
import inspect
import re
import types
from typing import Any, Literal, Optional, Union, get_args, get_origin, get_type_hints

from apowerb.core.workflow_engine import access_token_factory, run_agent_message
from apowerb.core.workflow_graph import (
    GraphError,
    ResolveWorkflow,
    UpstreamArgs,
    WorkflowGraph,
)

# Paramètres injectés par le runtime, jamais demandés à l'utilisateur.
_INJECTED_PARAMS = frozenset({"tool_context"})

# ``Optional[X]``/``Union[X, None]`` et, depuis 3.10, ``X | None`` partagent
# la même lecture : un seul type utile une fois ``None`` écarté.
_UNION_ORIGINS = {Union}
if hasattr(types, "UnionType"):
    _UNION_ORIGINS.add(types.UnionType)


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


def _tool_context_required(params: Any) -> bool:
    """Vrai si ``tool_context`` est un paramètre sans défaut (donc obligatoire).

    Optionnel (``tool_context: ToolContext = None``), l'outil se passe très
    bien d'un agent ; obligatoire, il ne peut s'exécuter que dans un nœud
    agent. Partagé par ``call_tool`` (qui refuse l'appel) et
    ``tool_arg_schema`` (qui le signale au studio via ``needs_agent_context``)
    pour que les deux ne divergent jamais.
    """
    return (
        "tool_context" in params
        and params["tool_context"].default is inspect.Parameter.empty
    )


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
    if _tool_context_required(params):
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


_SECTION_HEADER = re.compile(
    r"^(args?|arguments|returns?|raises?|yields?|examples?|note|notes|attributes):\s*$",
    re.IGNORECASE,
)
_ARG_LINE = re.compile(r"^(\w+)\s*(?:\([^)]*\))?\s*:\s*(.*)$")


def _tool_description(doc: str) -> Optional[str]:
    """Premier paragraphe de la docstring, avant une éventuelle section Args."""
    if not doc:
        return None
    before_args = re.split(
        r"\n[ \t]*Args:[ \t]*\n", doc, maxsplit=1, flags=re.IGNORECASE
    )[0]
    paragraph = before_args.strip().split("\n\n", 1)[0].strip()
    return paragraph or None


def _parse_google_args(doc: str) -> dict[str, str]:
    """Description de chaque paramètre depuis la section ``Args:`` (style Google)."""
    lines = doc.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^\s*Args:\s*$", line, re.IGNORECASE):
            start = i + 1
            break
    if start is None:
        return {}

    result: dict[str, str] = {}
    current: Optional[str] = None
    item_indent: Optional[int] = None
    for line in lines[start:]:
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        stripped = line.strip()
        if indent == 0 and _SECTION_HEADER.match(stripped):
            break
        if item_indent is None:
            item_indent = indent
        match = _ARG_LINE.match(stripped) if indent <= item_indent else None
        if match:
            current = match.group(1)
            result[current] = match.group(2).strip()
        elif current is not None:
            result[current] = (result[current] + " " + stripped).strip()
    return result


def _json_type(annotation: Any) -> tuple[str, Optional[list]]:
    """Type JSON (« string », « integer »...) et énumération éventuelle.

    Couvre les cas demandés par le studio : types simples, ``Optional``/
    ``Union`` avec ``None``, ``list[...]``/``dict[...]``, ``Literal[...]``
    (→ enum) et les ``Enum``. Tout le reste (types custom, ``ToolContext``
    laissé par erreur, etc.) retombe sur « any » plutôt que d'échouer.
    """
    if annotation is inspect.Parameter.empty or annotation is None:
        return "any", None
    if annotation is type(None):
        return "any", None

    origin = get_origin(annotation)

    if origin in _UNION_ORIGINS:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if len(args) == 1:
            return _json_type(args[0])
        return "any", None

    if origin is Literal:
        values = list(get_args(annotation))
        if values and all(isinstance(v, bool) for v in values):
            base = "boolean"
        elif values and all(
            isinstance(v, int) and not isinstance(v, bool) for v in values
        ):
            base = "integer"
        else:
            base = "string"
        return base, values

    if inspect.isclass(annotation) and issubclass(annotation, enum.Enum):
        return "string", [member.value for member in annotation]

    if origin in (list, tuple, set) or annotation in (list, tuple, set):
        return "array", None
    if origin is dict or annotation is dict:
        return "object", None
    if annotation is bool:
        return "boolean", None
    if annotation is int:
        return "integer", None
    if annotation is float:
        return "number", None
    if annotation is str:
        return "string", None
    return "any", None


def _jsonable(value: Any) -> Any:
    """Une valeur par défaut sous une forme sérialisable en JSON."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return str(value)


def tool_arg_schema(func) -> dict:
    """Schéma des arguments d'un outil, pour que le studio de workflows
    demande automatiquement les bons champs selon l'outil choisi.

    Introspection pure — ne modifie ni n'appelle ``func`` — et cohérente
    avec ``call_tool`` : les mêmes paramètres injectés (``tool_context``) en
    sont exclus, et ``needs_agent_context`` reflète exactement la condition
    qui ferait échouer un appel réel avec ``tool_needs_agent_context``.

    Retourne ``{"description", "params", "accepts_kwargs",
    "needs_agent_context"}`` ; c'est à l'appelant (la route) d'ajouter
    ``"tool"``, qui n'est pas une propriété de la fonction elle-même.
    """
    doc = inspect.getdoc(func) or ""
    description = _tool_description(doc)
    arg_docs = _parse_google_args(doc)

    signature = inspect.signature(func)
    try:
        hints = get_type_hints(func)
    except Exception:
        # Annotation non résolvable (forward ref exotique, dépendance
        # absente...) : on retombe sur les annotations brutes plutôt que de
        # faire échouer tout le schéma pour un seul paramètre.
        hints = {}

    params: list[dict] = []
    accepts_kwargs = False
    needs_agent_context = _tool_context_required(signature.parameters)

    for name, param in signature.parameters.items():
        if param.kind is inspect.Parameter.VAR_KEYWORD:
            accepts_kwargs = True
            continue
        if param.kind is inspect.Parameter.VAR_POSITIONAL:
            continue
        if name in _INJECTED_PARAMS:
            continue

        annotation = hints.get(name, param.annotation)
        type_str, enum_values = _json_type(annotation)
        required = param.default is inspect.Parameter.empty
        params.append(
            {
                "name": name,
                "type": type_str,
                "required": required,
                "default": None if required else _jsonable(param.default),
                "description": arg_docs.get(name),
                "enum": enum_values,
            }
        )

    return {
        "description": description,
        "params": params,
        "accepts_kwargs": accepts_kwargs,
        "needs_agent_context": needs_agent_context,
    }


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
