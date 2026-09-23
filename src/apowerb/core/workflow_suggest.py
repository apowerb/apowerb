"""Nœud suivant proposé par un modèle, pour l'éditeur de workflows (roadmap#85).

L'éditeur propose déjà, par règles, le TYPE du nœud qui suit. Le modèle
ajoute ce que les règles ne peuvent pas deviner : la configuration tirée du
contexte (noms de branches, agent à utiliser, champs à extraire, consignes).

Trois garde-fous, parce que la sortie d'un modèle est une donnée, pas un ordre :

- ce qui part au modèle est une DESCRIPTION du graphe, pas le graphe : types,
  identifiants, libellés, noms de routes et de champs. Jamais les valeurs du
  ``sample_payload``, ni les gabarits, URL, en-têtes, destinataires ;
- chaque proposition repasse les règles du validateur (``validate_node``),
  ne peut citer qu'un agent du demandeur et ne référence que des nœuds en
  amont ; sinon elle est écartée, jamais « réparée » ;
- l'appel passe par ``apply_run_guards`` (plafond de jetons du modèle
  mutualisé) et sa consommation est consignée dans ``llm_usage``.

La branche à câbler n'est pas demandée au modèle : c'est la première route
déclarée sans arête, calculée ici comme dans l'éditeur.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from fastapi import HTTPException, status
from pydantic import ValidationError

from apowerb.core.workflow_graph import (
    GraphError,
    Node,
    WorkflowGraph,
    _outer_refs,
    validate_node,
)

logger = logging.getLogger(__name__)

# Ce que le modèle peut proposer. Exclus : trigger (un seul par workflow),
# approval (pas encore exécutable), tool/http (le modèle inventerait un outil
# ou une URL), loop/try/subworkflow/merge (des structures, pas une étape).
SUGGESTIBLE_TYPES = (
    "agent",
    "classifier",
    "router",
    "condition",
    "convert",
    "extract",
    "rag",
    "set",
    "notification",
    "output",
)
_NEEDS_AGENT = {"agent", "classifier", "extract", "rag"}
MAX_SUGGESTIONS = 2
USAGE_AGENT_NAME = "workflow_suggest"
_MAX_CONFIG_CHARS = 4000
_MAX_REASON_CHARS = 200
_MAX_LABEL_CHARS = 80
_MAX_AGENTS = 50

_SYSTEM = (
    "You help build an automation workflow, one node at a time. Given the "
    "workflow description, its nodes and the user's agents, propose at most "
    f"{MAX_SUGGESTIONS} nodes to add right after the selected node. Allowed "
    f"types: {', '.join(SUGGESTIBLE_TYPES)}. Each node must be complete and "
    "runnable: agent, classifier, extract and rag need an agent_id taken from "
    "the agents list; a classifier needs at least two routes; templates "
    "reference an upstream node output as {{node_id}} or {{node_id.field}}. "
    "Never invent URLs, e-mail addresses or agents. Answer with JSON only: "
    '{"suggestions": [{"type": "...", "label": "...", "config": {...}, '
    '"reason": "one short sentence"}]}. Write label and reason in the '
    "language of the workflow name."
)


class SuggestUnavailable(Exception):
    """Le modèle n'a pas pu répondre utilement ; l'éditeur garde ses règles."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# --- Branche à câbler --------------------------------------------------------


def ordered_routes(node: Node) -> Optional[list[str]]:
    """Routes déclarées, dans l'ordre de l'éditeur ; ``None`` hors routeur."""
    cfg = node.config
    if node.type == "router":
        routes = [r.get("route") for r in cfg.get("rules") or [] if isinstance(r, dict)]
        routes.append(cfg.get("default_route"))
    elif node.type == "classifier":
        routes = [r.get("route") for r in cfg.get("routes") or [] if isinstance(r, dict)]
    elif node.type == "condition":
        routes = ["true", "false"]
    elif node.type == "try":
        routes = ["ok", "error"]
    else:
        return None
    return list(dict.fromkeys(r for r in routes if isinstance(r, str) and r))


def target_route(graph: WorkflowGraph, source: Node) -> tuple[bool, Optional[str]]:
    """(y a-t-il une place à remplir, route à câbler).

    Un nœud simple déjà relié, ou un routeur dont toutes les routes ont une
    arête, n'a plus de place : pas de proposition plutôt qu'une mauvaise.
    """
    outgoing = [e for e in graph.edges if e.source == source.id]
    routes = ordered_routes(source)
    if routes is None:
        return (source.type != "output" and not outgoing), None
    wired = {e.route for e in outgoing}
    free = next((r for r in routes if r not in wired), None)
    return free is not None, free


# --- Ce qui part au modèle ---------------------------------------------------


def _node_facts(node: Node) -> dict:
    cfg = node.config
    facts: dict[str, Any] = {"id": node.id, "type": node.type}
    if node.label:
        facts["label"] = node.label[:_MAX_LABEL_CHARS]
    if node.type == "trigger":
        facts["kind"] = cfg.get("kind")
        sample = cfg.get("sample_payload")
        if isinstance(sample, dict):
            facts["payload_fields"] = sorted(str(k) for k in sample)[:30]
    if node.type in _NEEDS_AGENT and cfg.get("agent_id"):
        facts["agent_id"] = cfg.get("agent_id")
    routes = ordered_routes(node)
    if routes is not None:
        facts["routes"] = routes
    if node.type == "extract":
        facts["fields"] = [f.get("name") for f in cfg.get("fields") or [] if isinstance(f, dict)]
    if node.type == "set":
        facts["fields"] = [f.get("key") for f in cfg.get("fields") or [] if isinstance(f, dict)]
    if node.type == "convert":
        facts["to"] = cfg.get("to")
    if node.type == "notification":
        facts["channel"] = cfg.get("channel")
    if node.type == "http":
        facts["method"] = cfg.get("method")
    if node.type == "tool":
        facts["tool"] = cfg.get("tool")
    return facts


def describe_graph(
    graph: WorkflowGraph,
    *,
    selected: str,
    route: Optional[str],
    name: Optional[str],
    description: Optional[str],
    agents: list[dict],
) -> dict:
    """La vue du graphe envoyée au modèle : structure et intentions, pas de valeurs."""
    return {
        "workflow": {"name": (name or "")[:200], "description": (description or "")[:1000]},
        "nodes": [_node_facts(n) for n in graph.nodes],
        "edges": [
            {"source": e.source, "target": e.target, **({"route": e.route} if e.route else {})}
            for e in graph.edges
        ],
        "selected_node": selected,
        "branch_to_fill": route,
        "agents": agents,
    }


# --- Ce qui revient du modèle ------------------------------------------------


def _upstream(graph: WorkflowGraph, node_id: str) -> set[str]:
    """``node_id`` et tous ses ancêtres : ce qu'un nœud accroché là peut lire."""
    parents: dict[str, set[str]] = {}
    for e in graph.edges:
        parents.setdefault(e.target, set()).add(e.source)
    seen, todo = {node_id}, [node_id]
    while todo:
        for p in parents.get(todo.pop(), ()):
            if p not in seen:
                seen.add(p)
                todo.append(p)
    return seen


def _free_id(node_type: str, taken: set[str]) -> str:
    """Même schéma que l'éditeur : ``agent1``, ``agent2``…"""
    k = 1
    while f"{node_type}{k}" in taken:
        k += 1
    return f"{node_type}{k}"


def check_proposal(
    raw: Any,
    *,
    graph: WorkflowGraph,
    source: Node,
    owned_agents: set[str],
    taken_ids: set[str],
) -> Optional[dict]:
    """La proposition, si elle passe toutes les règles ; ``None`` sinon."""
    if not isinstance(raw, dict) or raw.get("type") not in SUGGESTIBLE_TYPES:
        return None
    config = raw.get("config") if isinstance(raw.get("config"), dict) else {}
    if len(json.dumps(config, ensure_ascii=False)) > _MAX_CONFIG_CHARS:
        return None
    label = raw.get("label") if isinstance(raw.get("label"), str) else None
    try:
        node = Node(
            id=_free_id(raw["type"], taken_ids),
            type=raw["type"],
            label=(label or "")[:_MAX_LABEL_CHARS] or None,
            config=config,
        )
        validate_node(node)
    except (ValidationError, GraphError) as exc:
        logger.info("[SUGGEST] proposition %s écartée : %s", raw.get("type"), exc)
        return None
    if node.type in _NEEDS_AGENT and str(config.get("agent_id")) not in owned_agents:
        logger.info("[SUGGEST] proposition %s écartée : agent non possédé", node.type)
        return None
    if not _outer_refs(node) <= _upstream(graph, source.id):
        logger.info("[SUGGEST] proposition %s écartée : référence hors amont", node.type)
        return None
    reason = raw.get("reason") if isinstance(raw.get("reason"), str) else ""
    return {
        "type": node.type,
        "label": node.label,
        "config": node.config,
        "reason": reason.strip()[:_MAX_REASON_CHARS],
    }


def parse_suggestions(
    content: Optional[str],
    *,
    graph: WorkflowGraph,
    source: Node,
    owned_agents: set[str],
) -> list[dict]:
    try:
        data = json.loads(content or "")
    except (TypeError, ValueError) as exc:
        raise SuggestUnavailable("bad_output") from exc
    items = data.get("suggestions") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise SuggestUnavailable("bad_output")
    taken = {n.id for n in graph.nodes}
    kept: list[dict] = []
    for raw in items:
        proposal = check_proposal(
            raw, graph=graph, source=source, owned_agents=owned_agents, taken_ids=taken
        )
        if proposal is None or any(p["type"] == proposal["type"] for p in kept):
            continue
        kept.append(proposal)
        if len(kept) == MAX_SUGGESTIONS:
            break
    return kept


# --- Appel ------------------------------------------------------------------


def suggest_enabled() -> bool:
    """Activée par l'opérateur ET servie par le modèle mutualisé."""
    from apowerb.configs.settings import get_settings
    from apowerb.core.agent_helpers.default_llm import default_llm_available

    return bool(getattr(get_settings(), "workflow_suggest_enabled", False)) and default_llm_available()


def _owner_agents(owner_id: str) -> list[dict]:
    from apowerb.core.agent_main import fetch_agents

    return [
        {
            "agent_id": f"agent{a['agent_id']}",
            "name": a.get("agent_name") or "",
            "description": (a.get("agent_description") or "")[:300],
        }
        for a in fetch_agents(owner_id)[:_MAX_AGENTS]
    ]


def _completion_kwargs(messages: list[dict]) -> dict:
    from apowerb.configs.settings import get_settings

    settings = get_settings()
    model = (settings.workflow_suggest_model or settings.default_llm_model).strip()
    kwargs: dict = {
        "model": model,
        "messages": messages,
        "api_key": settings.default_llm_api_key.strip(),
        "temperature": 0.2,
        "max_tokens": 800,
        "timeout": settings.workflow_suggest_timeout_s,
        "num_retries": 0,
        "drop_params": True,
        "response_format": {"type": "json_object"},
    }
    api_base = (settings.default_llm_api_base or "").strip()
    if api_base:
        # Même règle que build_litellm_model : un endpoint OpenAI-compatible
        # impose le préfixe openai/ (Azure AI Foundry garde le sien).
        if not model.startswith(("openai/", "azure_ai/")):
            kwargs["model"] = "openai/" + model.split("/", 1)[-1]
        kwargs["api_base"] = api_base
    elif model.startswith("gemini/"):
        kwargs["reasoning_effort"] = "disable"
    return kwargs


async def _record_usage(owner_id: str, model: str, usage: Any) -> None:
    from apowerb.core.agent_helpers.usage_recorder import _persist_usage_row

    def count(name: str) -> int:
        return int(getattr(usage, name, 0) or 0)

    await _persist_usage_row(
        agent_id=0,
        agent_name=USAGE_AGENT_NAME,
        owner_id=owner_id,
        invocation_source=USAGE_AGENT_NAME,
        model=model,
        input_tokens=count("prompt_tokens"),
        output_tokens=count("completion_tokens"),
        total_tokens=count("total_tokens"),
        billed_to_thaink2=True,
    )


async def suggest_next(
    graph: WorkflowGraph,
    node_id: str,
    *,
    owner_id: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
) -> dict:
    """Jusqu'à deux nœuds proposés après ``node_id``, déjà vérifiés.

    Lève ``HTTPException`` 404 si la fonction est éteinte, 422 si le nœud
    n'existe pas, 402 si le plafond est atteint (``apply_run_guards``), 503
    si le modèle ne répond pas utilement.
    """
    if not suggest_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "SUGGEST_DISABLED"})
    source = next((n for n in graph.nodes if n.id == node_id), None)
    if source is None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"nœud inconnu : {node_id}"
        )
    open_slot, route = target_route(graph, source)
    if not open_slot:
        return {"route": None, "suggestions": []}

    from apowerb.core.run_gate import apply_run_guards, resolve_owner_plan

    await apply_run_guards(
        agent_name=USAGE_AGENT_NAME,
        owner_id=owner_id,
        plan=await resolve_owner_plan(owner_id),
    )

    agents = _owner_agents(owner_id)
    view = describe_graph(
        graph, selected=node_id, route=route, name=name, description=description, agents=agents
    )
    kwargs = _completion_kwargs(
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": json.dumps(view, ensure_ascii=False)},
        ]
    )

    import litellm

    try:
        response = await litellm.acompletion(**kwargs)
    except Exception as exc:  # noqa: BLE001 - délai, fournisseur, réseau
        reason = "timeout" if isinstance(exc, litellm.Timeout) else "error"
        logger.warning("[SUGGEST] modèle indisponible (%s) : %s", reason, exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"code": "SUGGEST_UNAVAILABLE", "reason": reason},
        ) from exc
    await _record_usage(owner_id, kwargs["model"], getattr(response, "usage", None))

    owned = {a["agent_id"] for a in agents} | {a["agent_id"][len("agent"):] for a in agents}
    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        content = None
    try:
        suggestions = parse_suggestions(
            content,
            graph=graph,
            source=source,
            owned_agents=owned,
        )
    except SuggestUnavailable as exc:
        logger.warning("[SUGGEST] réponse du modèle inutilisable")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"code": "SUGGEST_UNAVAILABLE", "reason": exc.reason},
        ) from exc
    return {"route": route, "suggestions": suggestions}
