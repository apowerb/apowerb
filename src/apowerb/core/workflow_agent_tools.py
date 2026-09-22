"""``agent_tool`` (T2) — un workflow publié, choisi comme outil d'agent.

Un workflow dont le nœud trigger de tête porte ``kind:"agent_tool"`` devient,
une fois publié, un outil ``workflow:<tool_name>`` sélectionnable par les
agents du MÊME propriétaire — ``tools_store.tools_helpers.
load_agent_tools_functions`` appelle ``resolve_workflow_agent_tools`` pour
tout nom d'outil qu'il ne reconnaît pas dans son catalogue portfolio/overlay.

Deux responsabilités séparées :

* ``resolve_workflow_agent_tools`` — construit les callables Python exposés
  à google-adk (nom, signature, docstring), sans jamais exécuter de run.
* L'exécution elle-même est déléguée à ``workflow_triggers.call_agent_tool``
  (run synchrone, borné à 120 s, garde anti-récursion via
  ``run.trigger.detail.chain``) — pas dupliquée ici.

La signature Python de chaque outil est construite via ``inspect.Signature``
(jamais ``exec``/``eval`` sur du texte fourni par l'utilisateur — un nom de
champ, une description, sont des données, pas du code) : un nom de champ qui
n'est pas un identifiant Python valide fait échouer SEULEMENT la construction
de CET outil (``inspect.Parameter`` lève ``ValueError``), jamais le
chargement des autres outils de l'agent.
"""

from __future__ import annotations

import inspect
import json
from contextvars import ContextVar
from logging import getLogger
from typing import Any, Callable, Optional

from apowerb.core import workflow_triggers as wt

logger = getLogger(__name__)

_SCHEMA_TYPE_TO_PYTHON: dict[str, type] = {
    "string": str,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}

# ContextVar plutôt qu'une variable de process : deux invocations d'agent
# servies simultanément par le même worker ne doivent pas se partager la
# chaîne anti-boucle. Même mécanisme que ``core.invocation_context`` pour
# l'invoker courant (propagation prouvée à travers les appels d'outil ADK
# in-process par ``resolve_integration_user``).
_current_chain: ContextVar[list] = ContextVar("workflow_tool_trigger_chain", default=[])


def current_trigger_chain() -> list[str]:
    """La chaîne de déclenchement de l'invocation en cours (vide par défaut)."""
    return list(_current_chain.get())


def _build_signature(schema: list[dict]) -> Optional[inspect.Signature]:
    params: list[inspect.Parameter] = []
    for field in schema:
        py_type = _SCHEMA_TYPE_TO_PYTHON.get(field.get("type"), str)
        required = field.get("required", True)
        try:
            params.append(
                inspect.Parameter(
                    field["name"],
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    default=inspect.Parameter.empty if required else None,
                    annotation=py_type,
                )
            )
        except (ValueError, TypeError):
            return None
    return inspect.Signature(params)


def _build_docstring(description: str, sig: inspect.Signature) -> str:
    doc = (description or "").strip() or "Déclenche un workflow."
    if not sig.parameters:
        return doc
    lines = [doc, "", "Args:"]
    for p in sig.parameters.values():
        type_name = getattr(p.annotation, "__name__", "str")
        lines.append(f"    {p.name} ({type_name}): ")
    return "\n".join(lines)


def build_workflow_tool_function(trigger_row: dict) -> Optional[Callable[..., Any]]:
    """Un callable ``workflow:<tool_name>`` pour CETTE ligne de trigger, ou
    ``None`` si son ``input_schema`` ne peut pas devenir une signature Python
    valide (journalisé, jamais levé — un outil mal formé ne doit pas casser
    le chargement des autres outils de l'agent)."""
    cfg = json.loads(trigger_row["config"] or "{}")
    tool_name = cfg.get("tool_name")
    schema = cfg.get("input_schema") or []
    sig = _build_signature(schema)
    if sig is None:
        logger.warning(
            "[workflow_agent_tools] input_schema invalide pour tool_name=%r "
            "(workflow_id=%s) — outil ignoré",
            tool_name,
            trigger_row.get("workflow_id"),
        )
        return None

    workflow_id = trigger_row["workflow_id"]
    owner_id = trigger_row["owner_id"]

    async def _tool(**kwargs: Any) -> dict:
        return await wt.call_agent_tool(
            workflow_id=workflow_id,
            owner_id=owner_id,
            tool_name=tool_name,
            arguments=kwargs,
            prior_chain=current_trigger_chain(),
        )

    _tool.__name__ = f"workflow:{tool_name}"
    _tool.__signature__ = sig
    _tool.__doc__ = _build_docstring(cfg.get("description", ""), sig)
    return _tool


def resolve_single_workflow_tool(
    tool_ref: str, *, owner_id: str
) -> Optional[Callable[..., Any]]:
    """Le callable pour UN ``workflow:<tool_name>`` précis, filtré par
    ``owner_id``, ou ``None`` s'il n'existe pas / n'est pas publié / n'est
    pas au propriétaire. Utilisé par ``tools_helpers.
    load_agent_tools_functions`` pour un nom d'outil référencé par un agent.
    """
    tool_name = tool_ref.removeprefix("workflow:")
    for row in wt.list_active_triggers("agent_tool", owner_id=owner_id):
        cfg = json.loads(row["config"] or "{}")
        if cfg.get("tool_name") == tool_name:
            return build_workflow_tool_function(row)
    return None


def resolve_workflow_agent_tools(owner_id: str) -> tuple[list[str], list[Callable]]:
    """``(noms, callables)`` des workflows-outils publiés de ``owner_id``.

    Même forme de retour que ``tools_helpers.load_agent_tools_functions``,
    pour que ``workflow:<tool_name>`` s'y glisse sans changer son contrat.
    """
    names: list[str] = []
    funcs: list[Callable] = []
    for row in wt.list_active_triggers("agent_tool", owner_id=owner_id):
        fn = build_workflow_tool_function(row)
        if fn is None:
            continue
        names.append(fn.__name__)
        funcs.append(fn)
    return names, funcs
