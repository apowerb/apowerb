"""Moteur de workflow côté serveur, bâti sur ``google.adk.workflow``.

Étape 1 de roadmap#55 : un seul moteur, à sémantique constante. Le canvas du
builder est aujourd'hui exécuté par le navigateur (``thaink2/apowerb-ui``,
``src/lib/workflowRunner.js``) : fermer l'onglet interrompt le run. Ce module
reproduit ce runner dans le cœur, en confiant l'ordonnancement au ``Workflow``
d'ADK (chaînage, fan-out, jointure) plutôt qu'à un ordonnanceur maison. Les
nœuds outil et routeur viendront sur ce même moteur.

Ce qui est reproduit de ``workflowRunner.js`` :

* le canvas est une liste ordonnée ; chaque nœud reçoit la sortie du précédent,
  le premier ne reçoit rien ;
* un agent feuille (base, router) reçoit ``JSON.stringify(entrée)`` ou, sans
  entrée, un message de repli construit sur sa description ; sa réponse est
  relue comme JSON quand elle en contient ;
* un composite ``sequential`` chaîne ses sous-agents, un ``parallel`` leur
  donne la même entrée et rend leurs sorties dans l'ordre des sous-agents ;
  sans sous-agents, un composite s'exécute comme une feuille.

Ce qui ne l'est pas, volontairement : ``loop``. Le runner JS et ``LoopAgent``
n'ont pas la même sémantique (condition de sortie évaluée par le JS contre
plafond dur côté chat) et le JS porte en outre des champs propres à un client
(``contacts``, ``sent_at``). Laquelle fait foi est une décision produit
(roadmap#55, D1) : un canvas qui contient une boucle est refusé avant qu'aucun
agent ne tourne, plutôt que d'en choisir une en silence.

L'exécution d'un agent feuille est injectée (``run_leaf``) : en production elle
passe par le même chemin que le builder (quotas, session ADK, ``/run``), voir
``make_http_leaf_runner``.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass, field
from logging import getLogger
from typing import Any, AsyncGenerator, Awaitable, Callable, Optional

from google.adk.runners import InMemoryRunner
from google.adk.workflow import FunctionNode, JoinNode, Workflow
from google.genai import types

logger = getLogger(__name__)

_FALLBACK_MESSAGE = "Execute your task now and perform the required action."
_COMPOSITES = {"sequential", "parallel", "loop"}


class WorkflowUserError(ValueError):
    """Erreur rédigée par nous, sûre à montrer à l'utilisateur telle quelle."""


class UnsupportedWorkflow(WorkflowUserError):
    """Le canvas contient une construction que le moteur serveur ne porte pas."""


class WorkflowCancelled(Exception):
    """Levée dans un nœud quand l'annulation a été demandée."""


@dataclass
class AgentSpec:
    agent_id: str  # identifiant de dossier ADK, « agent{id} »
    agent_type: str = "base"
    sub_agents: list[str] = field(default_factory=list)
    description: str = ""


DetailsFn = Callable[[str], AgentSpec]
TokenFactory = Callable[[], str]


_FIELDS_ATTR = "_apowerb_error_fields"


def error_fields(exc: BaseException) -> dict[str, str]:
    """What an error event tells the client: ``code``, ``detail``, maybe ``ref``.

    ``code`` is stable and translated by the interface; ``detail`` is an
    English fallback. A library or tool exception can carry a URL with a key,
    a host or a path: it is logged under a ``ref`` and only the ``ref`` goes
    out. Our own written errors (``WorkflowUserError``) and HTTP refusals
    (quota) are shown as they are.

    One failure is one reference: ADK wraps a node's exception again when the
    whole workflow fails, so the fields are stored on the exception and found
    again along its causes -- the node event and the run event share the
    ``ref``, and the exception is logged once.
    """
    from fastapi import HTTPException

    from apowerb.core.adk_runner import AdkRunError

    chain: list[BaseException] = []
    seen: Optional[BaseException] = exc
    while seen is not None and seen not in chain:
        chain.append(seen)
        seen = seen.__cause__ or seen.__context__

    for link in chain:
        cached = getattr(link, _FIELDS_ATTR, None)
        if cached is not None:
            fields = cached
            break
    else:
        fields = None
        for link in chain:
            if isinstance(link, WorkflowUserError):
                fields = {"code": "workflow_error", "detail": str(link)}
            elif isinstance(link, HTTPException) and isinstance(link.detail, str):
                fields = {"code": "http_error", "detail": link.detail}
            elif isinstance(link, AdkRunError) and link.code:
                fields = {"code": link.code, "detail": link.detail or str(link)}
                if link.ref:
                    fields["ref"] = link.ref
            if fields is not None:
                break
        if fields is None:
            ref = uuid.uuid4().hex[:8]
            logger.error(
                "[workflow] internal error ref=%s : %r", ref, exc, exc_info=exc
            )
            fields = {
                "code": "internal",
                "detail": f"Internal error during the run (ref. {ref}).",
                "ref": ref,
            }
    for link in chain:
        try:
            setattr(link, _FIELDS_ATTR, fields)
        except (AttributeError, TypeError):
            pass
    return dict(fields)


def client_error(exc: BaseException) -> str:
    """The message of :func:`error_fields`, for callers that only show text."""
    return error_fields(exc)["detail"]


LeafFn = Callable[[AgentSpec, Any], Awaitable[Any]]


# --- Transport entre agents : portage de workflowRunner.js -----------------


def leaf_message(node_input: Any, description: str = "") -> str:
    """Le message envoyé à un agent feuille (``runSingleAgent`` côté JS)."""
    if node_input is None or node_input == "":
        return f"Execute your task: {description}" if description else _FALLBACK_MESSAGE
    return json.dumps(node_input, ensure_ascii=False)


def try_parse_json(text: Any) -> Any:
    """Relit une réponse comme JSON si possible (``tryParseJSON`` côté JS)."""
    if not isinstance(text, str):
        return text
    stripped = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    stripped = stripped.replace("```", "").strip()
    match = re.search(r"[{\[]", stripped)
    if not match:
        return text
    for candidate in (stripped[match.start() :], stripped):
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return text


def _event_text(ev: Any) -> Optional[str]:
    if not isinstance(ev, dict):
        return None
    parts = (ev.get("content") or {}).get("parts") or ev.get("parts") or []
    if parts and isinstance(parts[0], dict) and parts[0].get("text"):
        return parts[0]["text"]
    return ev.get("text") or None


def extract_response_text(response: Any) -> str:
    """Le texte final d'une réponse ADK (``extractResponseText`` côté JS)."""
    if isinstance(response, str):
        return response
    if isinstance(response, list):
        for ev in reversed(response):
            text = _event_text(ev)
            if text:
                return text
    if isinstance(response, dict):
        if response.get("text"):
            return response["text"]
        text = _event_text(response)
        if text:
            return text
        content = response.get("content")
        if content is not None:
            return content if isinstance(content, str) else json.dumps(content)
    return json.dumps(response)


# --- Compilation canvas -> google.adk.workflow.Workflow --------------------


def _kind(spec: AgentSpec) -> str:
    agent_type = (spec.agent_type or "base").lower()
    if agent_type in _COMPOSITES and spec.sub_agents:
        return agent_type
    return "leaf"


class _Compiler:
    def __init__(self, details_of: DetailsFn, run_leaf: LeafFn, emit, cancel_event):
        self._details_of = details_of
        self._run_leaf = run_leaf
        self._emit = emit
        self._cancel = cancel_event
        self._counter = 0

    def _name(self, spec: AgentSpec) -> str:
        # Un même agent peut figurer plusieurs fois : le nom de nœud doit rester
        # unique dans le graphe, l'identifiant d'agent voyage dans l'événement.
        self._counter += 1
        return f"n{self._counter}_{re.sub(r'[^A-Za-z0-9_]', '_', spec.agent_id)}"

    def check(self, agent_id: str, trail: tuple[str, ...] = ()) -> None:
        """Refuse boucles et cycles AVANT toute exécution."""
        if agent_id in trail:
            raise UnsupportedWorkflow(
                f"cycle de sous-agents : {' -> '.join(trail + (agent_id,))}"
            )
        spec = self._details_of(agent_id)
        kind = _kind(spec)
        if kind == "loop":
            raise UnsupportedWorkflow(
                f"{agent_id} est un agent loop : pas encore exécuté côté serveur "
                "(sémantique de boucle à trancher, roadmap#55 D1)"
            )
        if kind != "leaf":
            for sub in spec.sub_agents:
                self.check(sub, trail + (agent_id,))

    def node(self, agent_id: str):
        spec = self._details_of(agent_id)
        kind = _kind(spec)
        if kind == "sequential":
            children = [self.node(sub) for sub in spec.sub_agents]
            inner = Workflow(name=self._name(spec), edges=[("START", *children)])
            return self._framed(spec, "sequential", inner)
        if kind == "parallel":
            children = [self.node(sub) for sub in spec.sub_agents]
            join = JoinNode(name=f"{self._name(spec)}_join")
            order = [child.name for child in children]

            async def _in_order(node_input: dict) -> list:
                return [node_input.get(name) for name in order]

            ordered = FunctionNode(func=_in_order, name=f"{join.name}_ordered")
            edges = [("START", child) for child in children]
            edges += [(child, join) for child in children]
            edges.append((join, ordered))
            inner = Workflow(name=self._name(spec), edges=edges)
            return self._framed(spec, "parallel", inner)
        return self._leaf(spec)

    def _guard(self) -> None:
        if self._cancel.is_set():
            raise WorkflowCancelled()

    def _leaf(self, spec: AgentSpec) -> FunctionNode:
        async def _run(node_input: Any) -> Any:
            self._guard()
            if isinstance(node_input, types.Content):
                node_input = None  # le premier nœud du canvas ne reçoit rien
            self._emit(
                {"event": "step_start", "agent_id": spec.agent_id, "kind": "base"}
            )
            try:
                result = await self._run_leaf(spec, node_input)
            except Exception as exc:
                self._emit(
                    {
                        "event": "step_error",
                        "agent_id": spec.agent_id,
                        **error_fields(exc),
                    }
                )
                raise
            self._emit(
                {"event": "step_complete", "agent_id": spec.agent_id, "output": result}
            )
            return result

        return FunctionNode(func=_run, name=self._name(spec))

    def _framed(self, spec: AgentSpec, kind: str, inner: Workflow) -> Workflow:
        """Encadre un composite par ses événements de début et de fin."""

        async def _start(node_input: Any) -> Any:
            self._guard()
            self._emit({"event": "step_start", "agent_id": spec.agent_id, "kind": kind})
            return None if isinstance(node_input, types.Content) else node_input

        async def _complete(node_input: Any) -> Any:
            self._emit(
                {
                    "event": "step_complete",
                    "agent_id": spec.agent_id,
                    "output": node_input,
                }
            )
            return node_input

        start = FunctionNode(func=_start, name=f"{inner.name}_start")
        complete = FunctionNode(func=_complete, name=f"{inner.name}_complete")
        return Workflow(
            name=f"{inner.name}_frame", edges=[("START", start, inner, complete)]
        )


def build_canvas_workflow(
    canvas_ids: list[str],
    *,
    details_of: DetailsFn,
    run_leaf: LeafFn,
    emit: Callable[[dict], None],
    cancel_event: asyncio.Event,
) -> Workflow:
    """Compile un canvas (liste ordonnée d'agents) en ``Workflow`` ADK."""
    if not canvas_ids:
        raise UnsupportedWorkflow("canvas vide")
    compiler = _Compiler(details_of, run_leaf, emit, cancel_event)
    for agent_id in canvas_ids:
        compiler.check(agent_id)
    nodes = [compiler.node(agent_id) for agent_id in canvas_ids]
    return Workflow(name="canvas", edges=[("START", *nodes)])


@dataclass
class _Finished:
    exc: Optional[BaseException]
    outputs: list


async def drive_workflow(
    workflow: Workflow, queue: asyncio.Queue, *, root_name: str
) -> AsyncGenerator[Any, None]:
    """Exécute ``workflow`` et relaie les événements que ses nœuds déposent.

    Produit les dictionnaires de ``queue`` au fil de l'eau, puis un
    ``_Finished`` portant l'exception éventuelle et les sorties émises par la
    racine (dans l'ordre).
    """
    outputs: list = []

    async def _drive() -> None:
        runner = InMemoryRunner(node=workflow, app_name="apowerb_workflow")
        session = await runner.session_service.create_session(
            app_name="apowerb_workflow", user_id="workflow"
        )
        async for ev in runner.run_async(
            user_id="workflow",
            session_id=session.id,
            new_message=types.Content(role="user", parts=[types.Part(text="")]),
        ):
            if ev.author == root_name and ev.output is not None:
                outputs.append(ev.output)

    task = asyncio.create_task(_drive())
    while True:
        getter = asyncio.create_task(queue.get())
        done, _ = await asyncio.wait(
            {task, getter}, return_when=asyncio.FIRST_COMPLETED
        )
        if getter in done:
            yield getter.result()
            continue
        getter.cancel()
        break
    while not queue.empty():
        yield queue.get_nowait()
    yield _Finished(exc=task.exception(), outputs=outputs)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n"


async def run_canvas(
    canvas_ids: list[str],
    *,
    details_of: DetailsFn,
    run_leaf: LeafFn,
    cancel_event: asyncio.Event,
) -> AsyncGenerator[str, None]:
    """Exécute un canvas et produit ses événements SSE.

    Événements : ``step_start`` / ``step_complete`` / ``step_error`` (avec
    ``agent_id``), puis un seul terminal parmi ``done`` (avec ``output``),
    ``cancelled`` et ``error`` (avec ``detail``).
    """
    queue: asyncio.Queue = asyncio.Queue()
    try:
        workflow = build_canvas_workflow(
            canvas_ids,
            details_of=details_of,
            run_leaf=run_leaf,
            emit=queue.put_nowait,
            cancel_event=cancel_event,
        )
    except (UnsupportedWorkflow, KeyError, ValueError) as exc:
        yield _sse({"event": "error", "detail": str(exc)})
        return

    exc: Optional[BaseException] = None
    final: list = []
    async for item in drive_workflow(workflow, queue, root_name="canvas"):
        if isinstance(item, _Finished):
            exc, final = item.exc, item.outputs
            break
        yield _sse(item)

    if cancel_event.is_set() or isinstance(exc, WorkflowCancelled):
        yield _sse({"event": "cancelled"})
    elif exc is not None:
        yield _sse({"event": "error", **error_fields(exc)})
    elif not final:
        yield _sse(
            {
                "event": "error",
                "code": "no_output",
                "detail": "The workflow finished without an output.",
            }
        )
    else:
        yield _sse({"event": "done", "output": final[-1]})


# --- Branchement production ------------------------------------------------


def owner_scoped_specs(owner_email: str) -> DetailsFn:
    """``details_of`` de production, limité aux agents de ``owner_email``.

    ``get_agent_details`` ne filtre pas par propriétaire : sans ce contrôle,
    un canvas pouvait viser — et exécuter — l'agent d'un autre client. Un
    agent d'autrui est « introuvable » et rien de sa définition ne remonte.
    """
    from apowerb.core.agent_helpers import get_agent_details
    from apowerb.core.agent_main import _parse_string_list

    def _details_of(agent_id: str) -> AgentSpec:
        raw = str(agent_id)
        numeric = raw[len("agent") :] if raw.startswith("agent") else raw
        if not numeric.isdigit():
            raise UnsupportedWorkflow(f"identifiant d'agent invalide : {raw!r}")
        details = get_agent_details(agent_id=int(numeric)) or {}
        if not details or details.get("owner_id") != owner_email:
            raise UnsupportedWorkflow(f"agent introuvable : agent{numeric}")
        return AgentSpec(
            agent_id=f"agent{numeric}",
            agent_type=details.get("agent_type") or "base",
            sub_agents=_parse_string_list(details.get("sub_agents")),
            description=details.get("agent_description") or "",
        )

    return _details_of


def access_token_factory(owner_email: str) -> TokenFactory:
    """Un jeton court neuf à chaque appel : un run long ne meurt pas d'expiration."""
    from datetime import timedelta

    from apowerb.helpers.security import create_access_token

    return lambda: create_access_token(
        data={"sub": owner_email, "type": "access"}, expires_delta=timedelta(minutes=15)
    )


async def run_agent_message(
    agent_id: str,
    message: str,
    *,
    owner_email: str,
    plan: Optional[str],
    token_factory: TokenFactory,
) -> Any:
    """Exécute un agent par le même chemin que le builder, message brut.

    ``workflowRunner.js`` appelle ``createSession`` puis ``runAgent`` : une
    session neuve par agent, les gardes de run (quotas), puis ``/run`` d'ADK
    sous le jeton de l'utilisateur — donc les mêmes contrôles d'accès.
    """
    import uuid

    from apowerb.core.adk_runner import create_adk_agent_session, run_adk_agent
    from apowerb.core.run_gate import apply_run_guards

    await apply_run_guards(agent_name=agent_id, owner_id=owner_email, plan=plan)
    token = token_factory()
    session_id = f"workflow_{agent_id}_{uuid.uuid4().hex[:12]}"
    await create_adk_agent_session(
        agent_name=agent_id,
        user_id=owner_email,
        session_id=session_id,
        data={},
        token=token,
    )
    response = await run_adk_agent(
        agent_name=agent_id,
        user_id=owner_email,
        session_id=session_id,
        new_message={"role": "user", "parts": [{"text": message}]},
        run_mode="run",
        token=token,
    )
    return try_parse_json(extract_response_text(response))


def make_http_leaf_runner(
    *, owner_email: str, plan: Optional[str], token_factory: TokenFactory
) -> LeafFn:
    """``run_leaf`` de production pour le canvas : voir ``run_agent_message``."""

    async def _run(spec: AgentSpec, node_input: Any) -> Any:
        return await run_agent_message(
            spec.agent_id,
            leaf_message(node_input, spec.description),
            owner_email=owner_email,
            plan=plan,
            token_factory=token_factory,
        )

    return _run
