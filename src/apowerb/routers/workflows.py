"""Workflows router — B18.

Implements the two endpoints the `DiagramEditor` frontend calls when a user
runs a workflow with an attached file:

* ``POST /api/workflows/run-sse`` — multipart: ``canvas_agent_ids`` (JSON
  list), ``workflow_id`` (client-generated wid) and optional ``file``.
  Starts a background task that drives the workflow and streams SSE events
  back to the client.

* ``POST /api/workflows/{wid}/cancel`` — sets the cancellation event for the
  matching run. The streaming task yields a final ``cancelled`` event and
  exits.

* ``GET /api/workflows/runs`` and ``GET /api/workflows/runs/{run_id}`` — what
  ran, how it ended, and whether its input is still on disk.

* ``POST /api/workflows/runs/{run_id}/replay`` — re-runs one from the input it
  kept, as a new run that cites the original. A canvas run replays its
  canvas; an agent run (``schedule``, ``chat``) resends its message to the
  same agent, in a fresh session; a persisted workflow run replays the graph
  of the version it ran, and fails if that graph is gone.

* ``GET /api/workflows/runs/awaiting``, ``POST /runs/{run_id}/resume`` and
  ``POST /runs/{run_id}/abandon`` — a graph run stopped on a human decision
  (``approval`` node, status ``awaiting``): the decisions waiting for the
  caller (owner or assignee), the answer that resumes the **same** run (its
  stream replays the nodes already done, flagged ``replayed``), or closing
  it unanswered.

* ``POST /runs/{run_id}/retry-from-failure`` — « Reprendre ici » : a graph
  run that failed on a node is run again from that node, as a new run that
  cites the original. The nodes done before are replayed from the snapshot
  kept at the failure (``replayed``, never executed again), against the
  workflow's latest saved version as long as they did not change.

* ``GET /api/workflows/tools/schema?tool=<tool_ref>`` — the argument schema
  of one tool (types, required, defaults, per-arg description), so the
  studio's node inspector can build its form without hardcoding it per tool.

* ``POST /api/workflows/render-preview`` — a template rendered by the engine's
  own ``render`` with the outputs of the studio's last test run, so the
  inspector previews a node's input exactly as a run would; nothing executes.

Two pieces of state, deliberately: the **run record** is persisted in
``agent_runs`` (it must survive a restart, a crash and the end of the stream),
while the in-memory dict keeps only what cannot be serialised — the
cancellation event of a run live *in this process*:

    ``{"cancel_event": asyncio.Event, "task": asyncio.Task, "owner": email}``

So cancellation still only reaches a run served by this process, but the
history no longer disappears with it.

The default runner is a thin stub: workflow execution itself is still owned
by the frontend (see ``DiagramEditor.jsx``'s ``runWorkflow``). The SSE route
exists primarily so the browser can centrally kill an in-flight run through
a single wid. The runner is swappable via ``_workflow_runner`` so tests —
and a future full backend implementation — can drop-in their own logic.
"""

from __future__ import annotations

import asyncio
import json
import os
from logging import getLogger
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from nanoid import generate as nanoid_generate
from pydantic import BaseModel, Field

from apowerb.auth.dependencies import get_current_user
from apowerb.core import run_main
from apowerb.users import schemas as user_schemas


logger = getLogger(__name__)

router = APIRouter(prefix="/workflows", tags=["workflows"])


# ---------------------------------------------------------------------------
# In-memory run registry
# ---------------------------------------------------------------------------

# Keyed by workflow id (``wid``). Each value holds the cancellation event and
# the asyncio Task driving the run. Cleared on stream completion.
_runs: Dict[str, Dict[str, Any]] = {}


async def _default_workflow_runner(
    wid: str,
    cancel_event: asyncio.Event,
    canvas_agent_ids: List[str],
    file_bytes: Optional[bytes],
) -> AsyncGenerator[str, None]:
    """Fallback runner.

    Real workflow logic lives in the frontend's ``runWorkflow`` helper — the
    backend SSE endpoint exists mostly to have a cancellable wid. This
    default runner simply echoes the canvas order as ``agent_start`` events
    and terminates. Tests substitute a richer stub via ``_workflow_runner``.
    """
    yield f"data: {json.dumps({'event': 'started', 'wid': wid})}\n\n"
    for agent_id in canvas_agent_ids:
        if cancel_event.is_set():
            yield f"data: {json.dumps({'event': 'cancelled'})}\n\n"
            return
        yield (
            "data: "
            + json.dumps({"event": "agent_start", "agent_id": agent_id})
            + "\n\n"
        )
        await asyncio.sleep(0)
    yield f"data: {json.dumps({'event': 'done'})}\n\n"


async def _server_workflow_runner(
    wid: str,
    cancel_event: asyncio.Event,
    canvas_agent_ids: List[str],
    file_bytes: Optional[bytes],
) -> AsyncGenerator[str, None]:
    """Runner réel : exécute le canvas dans le cœur (roadmap#55, étape 1).

    Activé par ``APOWERB_WORKFLOW_ENGINE=server``. Tant que le front exécute
    lui-même le canvas, l'activer sans changer le front ferait tourner chaque
    agent deux fois : le défaut reste le bouchon.
    """
    from apowerb.core import workflow_engine
    from apowerb.core.run_gate import resolve_owner_plan

    owner = (_runs.get(wid) or {}).get("owner") or ""
    run_leaf = workflow_engine.make_http_leaf_runner(
        owner_email=owner,
        plan=await resolve_owner_plan(owner),
        token_factory=workflow_engine.access_token_factory(owner),
    )
    async for chunk in workflow_engine.run_canvas(
        [str(a) for a in canvas_agent_ids],
        details_of=workflow_engine.owner_scoped_specs(owner),
        run_leaf=run_leaf,
        cancel_event=cancel_event,
    ):
        yield chunk


async def _agent_replay_runner(
    run_id: str, owner: str, config: dict[str, Any]
) -> AsyncGenerator[str, None]:
    """Rejoue un run d'agent : le message conservé, au même agent.

    Session ADK neuve (celle d'origine peut porter un échange à moitié fait),
    gardes de run et identité du propriétaire, comme un run planifié. Les
    outils appelés sont consignés, succès ou échec : c'est ce qui permettra, à
    son tour, de dire si ce rejeu peut être rejoué sans refaire d'effet.
    """
    from apowerb.core import adk_runner, agent_main, run_gate, workflow_engine
    from apowerb.core.invocation_context import set_current_invoker

    tools: list[str] | None = []
    try:
        folder = agent_main.get_agent_folder_name(config["agent_name"])
        await run_gate.apply_run_guards(
            agent_name=folder,
            owner_id=owner,
            plan=await run_gate.resolve_owner_plan(owner),
        )
        set_current_invoker(owner)
        token = workflow_engine.access_token_factory(owner)()
        session_id = f"replay_{run_id}"
        await adk_runner.create_adk_agent_session(
            agent_name=folder, user_id=owner, session_id=session_id, data={}, token=token
        )
        try:
            response = await adk_runner.run_adk_agent(
                agent_name=folder,
                user_id=owner,
                session_id=session_id,
                new_message=config["new_message"],
                run_mode="run",
                token=token,
            )
        except Exception:
            tools = await run_main.executed_tools_in_session(
                folder, owner, session_id, token
            )
            raise
        tools = run_main.executed_tools(response)
        output = workflow_engine.extract_response_text(response)
    except Exception as exc:  # l'issue doit être consignée
        _record_tools(run_id, tools)
        logger.exception("[workflows] rejeu d'agent run_id=%s a echoue", run_id)
        yield (
            "data: "
            + json.dumps({"event": "error", "detail": f"{type(exc).__name__}: {exc}"})
            + "\n\n"
        )
        return
    _record_tools(run_id, tools)
    yield f"data: {json.dumps({'event': 'done', 'output': output})}\n\n"


def _is_graph_run(agent_ids: list, config: dict) -> bool:
    """Un run de workflow persisté (``/defs/{id}/run`` ou trigger), pas un canvas.

    Son entrée est le graphe à la version exécutée plus le payload ; il ne
    porte aucun agent de canvas.
    """
    return not agent_ids and "workflow_id" in config and "version" in config


def pause_saver(run_id: str) -> Callable[[dict], None]:
    """Le ``on_await`` de ``run_graph`` : la pause est écrite sous ce run."""
    return lambda snapshot: run_main.pause_run(run_id, snapshot)


def failure_saver(run_id: str) -> Callable[[dict], None]:
    """Le ``on_fail`` de ``run_graph`` : de quoi reprendre au nœud en échec."""
    return lambda snapshot: run_main.save_failure(run_id, snapshot)


async def _graph_replay_runner(
    owner: str,
    config: dict[str, Any],
    cancel_event: asyncio.Event,
    run_id: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Rejoue un run de workflow : le graphe de la version qu'il avait exécutée.

    Graphe introuvable (workflow supprimé, version non archivée) : le rejeu
    échoue explicitement. Relancer autre chose, ou rien, ferait passer pour
    réussi un run qui n'a pas reproduit l'original.
    """
    from apowerb.core import workflow_graph, workflow_main, workflow_runtime
    from apowerb.core.run_gate import resolve_owner_plan

    workflow_id = config["workflow_id"]
    try:
        raw = workflow_main.get_workflow_graph_at(
            workflow_id, config["version"], owner_id=owner
        )
        if raw is None:
            raise LookupError(
                f"graphe du workflow {workflow_id} en version {config['version']} "
                "introuvable : rejeu impossible"
            )
        graph = workflow_main.parse_graph(raw)
        run_agent, run_tool, run_rag, run_notify = workflow_runtime.bindings_for(
            owner, await resolve_owner_plan(owner)
        )
    except Exception as exc:  # noqa: BLE001 - l'issue doit être consignée
        yield (
            "data: "
            + json.dumps({"event": "error", "detail": f"{type(exc).__name__}: {exc}"})
            + "\n\n"
        )
        return
    async for chunk in workflow_graph.run_graph(
        graph,
        payload=config.get("payload"),
        run_agent=run_agent,
        run_tool=run_tool,
        run_rag=run_rag,
        run_subworkflow=workflow_runtime.resolve_workflow_for(owner),
        workflow_id=workflow_id,
        cancel_event=cancel_event,
        run_notify=run_notify,
        services=workflow_runtime.services_for(owner),
        # Un « Tester jusqu'ici » se rejoue tel quel, sans son aval.
        until=config.get("until"),
        on_await=pause_saver(run_id) if run_id else None,
        on_fail=failure_saver(run_id) if run_id else None,
        run_id=run_id,
    ):
        yield chunk


async def _retry_runner(
    owner: str, retry: dict[str, Any], cancel_event: asyncio.Event
) -> AsyncGenerator[str, None]:
    """« Reprendre ici » : le graphe de la dernière sauvegarde, l'amont rejoué."""
    from apowerb.core import workflow_graph, workflow_runtime
    from apowerb.core.run_gate import resolve_owner_plan

    run_id, state, config = retry["run_id"], retry["state"], retry["config"]
    try:
        run_agent, run_tool, run_rag, run_notify = workflow_runtime.bindings_for(
            owner, await resolve_owner_plan(owner)
        )
    except Exception as exc:  # noqa: BLE001 - l'issue doit être consignée
        yield (
            "data: "
            + json.dumps({"event": "error", "detail": f"{type(exc).__name__}: {exc}"})
            + "\n\n"
        )
        return
    # Dit au client que les nœuds ``replayed`` qui suivent étaient faits
    # avant l'échec (et non avant une décision).
    started = {"event": "retry_started", "replay_of": retry["replay_of"], "node_id": config["retry_from"]}
    yield "data: " + json.dumps(started) + "\n\n"
    async for chunk in workflow_graph.run_graph(
        retry["graph"],
        payload=state.get("payload"),
        run_agent=run_agent,
        run_tool=run_tool,
        run_rag=run_rag,
        run_subworkflow=workflow_runtime.resolve_workflow_for(owner),
        workflow_id=config["workflow_id"],
        cancel_event=cancel_event,
        run_notify=run_notify,
        services=workflow_runtime.services_for(owner),
        until=state.get("until"),
        # Sans ``answer`` : rien à décider, seulement l'amont à rejouer.
        resume={k: state.get(k) for k in ("outputs", "routes", "artifacts")},
        on_await=pause_saver(run_id),
        on_fail=failure_saver(run_id),
        run_id=run_id,
    ):
        yield chunk


# Ce que l'assigné d'une décision ne reçoit pas du run d'un autre : les
# données du propriétaire (sorties, entrées, appels d'outils, graphiques).
_OWNER_ONLY_FIELDS = (
    "output",
    "input",
    "input_truncated",
    "input_length",
    "tool_calls",
    "artifacts",
    "reported_error",
    # Texte brut d'une exception : il peut citer les données du run ; le code
    # et ses paramètres suffisent à l'expliquer.
    "detail",
)


def _for_assignee(chunk: str, user: str) -> str:
    try:
        event = json.loads(chunk[len("data: "):])
    except ValueError:
        return chunk
    for field in _OWNER_ONLY_FIELDS:
        event.pop(field, None)
    if event.get("event") == "awaiting_input" and (event.get("assignee") or "").lower() != user.lower():
        # Une décision suivante qui ne lui revient pas : il sait seulement
        # que le run attend.
        for field in ("question", "choices"):
            event.pop(field, None)
    return "data: " + json.dumps(event, ensure_ascii=False, default=str) + "\n\n"


async def _decision_resume_runner(
    run_id: str, claimed: dict[str, Any], cancel_event: asyncio.Event
) -> AsyncGenerator[str, None]:
    """Reprend un run arrêté sur une décision, sous l'identité du propriétaire.

    Le graphe est celui de l'instantané (la version qui tournait), pas la
    version courante du workflow : une édition entre-temps ne change pas un
    run déjà à moitié fait. ``claimed["decided"]`` passe à vrai quand le
    moteur prend la réponse (``on_answered``) : avant, un arrêt remet le run
    en attente (voir ``_streaming_run``). L'assigné reçoit le flux sans les données du
    propriétaire.
    """
    answer = claimed["answer"]
    redact = answer["answered_by"] != claimed["run"]["owner_id"]
    async for chunk in _decision_resume_stream(run_id, claimed, cancel_event):
        yield _for_assignee(chunk, answer["answered_by"]) if redact else chunk


def _decision_taken(run_id: str, claimed: dict[str, Any]) -> Callable[[str], None]:
    """``on_answered`` de la reprise : posé par le moteur lui-même, pas lu
    dans le flux SSE, et consigné en base avant tout nœud en aval.

    Si la ligne n'est plus celle de cette prise (remise en attente puis
    reprise ailleurs), la suite ne doit pas s'exécuter deux fois : le nœud
    échoue et ``_streaming_run`` laisse la ligne à l'autre reprise.
    """
    from apowerb.core.workflow_graph import GraphError

    def _taken(node_id: str) -> None:
        if not run_main.mark_decided(run_id, claimed.get("resumed_at")):
            claimed["lost"] = True
            raise GraphError(
                f"{node_id} : cette décision a été reprise ailleurs",
                code="decision_already_answered",
                params={"node": node_id},
            )
        claimed["decided"] = True

    return _taken


async def _decision_resume_stream(
    run_id: str, claimed: dict[str, Any], cancel_event: asyncio.Event
) -> AsyncGenerator[str, None]:
    from apowerb.core import workflow_graph, workflow_runtime
    from apowerb.core.run_gate import resolve_owner_plan

    owner, state = claimed["run"]["owner_id"], claimed["state"]
    try:
        graph = workflow_graph.WorkflowGraph.model_validate(state["graph"])
        run_agent, run_tool, run_rag, run_notify = workflow_runtime.bindings_for(
            owner, await resolve_owner_plan(owner)
        )
    except Exception as exc:  # noqa: BLE001 - l'issue doit être consignée
        yield (
            "data: "
            + json.dumps({"event": "error", "detail": f"{type(exc).__name__}: {exc}"})
            + "\n\n"
        )
        return
    async for chunk in workflow_graph.run_graph(
        graph,
        payload=state.get("payload"),
        run_agent=run_agent,
        run_tool=run_tool,
        run_rag=run_rag,
        run_subworkflow=workflow_runtime.resolve_workflow_for(owner),
        workflow_id=claimed["run"]["config"].get("workflow_id"),
        cancel_event=cancel_event,
        run_notify=run_notify,
        services=workflow_runtime.services_for(owner),
        on_await=pause_saver(run_id),
        on_fail=failure_saver(run_id),
        resume={**state, "answer": claimed["answer"]},
        run_id=run_id,
        on_answered=_decision_taken(run_id, claimed),
        until=state.get("until"),
    ):
        yield chunk


def _record_tools(run_id: str, tools: list[str] | None) -> None:
    try:
        run_main.record_tools_executed(run_id, tools)
    except Exception:  # la trace ne doit pas casser le flux
        logger.exception("[workflows] outils du run_id=%s non consignes", run_id)


def _select_runner():
    if os.environ.get("APOWERB_WORKFLOW_ENGINE", "").strip().lower() == "server":
        return _server_workflow_runner
    return _default_workflow_runner


# Swappable hook (tests override this).
_workflow_runner: Callable[
    [str, asyncio.Event, List[str], Optional[bytes]],
    AsyncGenerator[str, None],
] = _select_runner()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _terminal_event(chunk: Any) -> Optional[dict]:
    """L'événement ``error`` ou ``cancelled`` qu'un runner émet pour finir.

    Les moteurs du cœur (``run_canvas``, ``run_graph``) ne lèvent pas : ils
    concluent le flux par un de ces événements. Sans cette lecture, un run
    échoué était consigné ``success``.
    """
    text = chunk.decode() if isinstance(chunk, bytes) else chunk
    if not isinstance(text, str) or not text.startswith("data: "):
        return None
    try:
        payload = json.loads(text[len("data: "):])
    except ValueError:
        return None
    if isinstance(payload, dict) and payload.get("event") in (
        "error",
        "cancelled",
        "awaiting_input",
    ):
        return payload
    return None


def _done_output(chunk: Any) -> tuple[bool, Any]:
    """``(True, output)`` si ``chunk`` est l'événement terminal ``done``.

    Seul ``core.workflow_graph.run_graph`` émet ``done`` (le canvas legacy ne
    le fait pas) — c'est la sortie que ``workflow_triggers`` (T2,
    ``workflow_done``/``agent_tool``) doit pouvoir lire sans reparser le SSE
    ailleurs.
    """
    text = chunk.decode() if isinstance(chunk, bytes) else chunk
    if not isinstance(text, str) or not text.startswith("data: "):
        return False, None
    try:
        payload = json.loads(text[len("data: ") :])
    except ValueError:
        return False, None
    if isinstance(payload, dict) and payload.get("event") == "done":
        return True, payload.get("output")
    return False, None


def _streaming_run(
    run_id: str,
    agent_ids: List[str],
    file_bytes: Optional[bytes],
    owner: str,
    runner: Optional[Callable[[asyncio.Event], AsyncGenerator[str, None]]] = None,
    resume_guard: Optional[dict] = None,
) -> StreamingResponse:
    """Drive one run and stream it, recording how it ends.

    ``runner`` remplace l'exécution du canvas (les runs de graphe passent par
    ici pour hériter du suivi, de l'annulation et de la trace en base).

    Factorisé entre le démarrage et le rejeu : les deux exécutent la même
    chose, la seule différence étant d'où vient l'entrée. L'issue est écrite
    en base dans le ``finally``, donc un flux interrompu ne laisse pas une
    ligne éternellement ``running`` — c'était tout le défaut du registre en
    mémoire, qui oubliait le run à la fin du flux.
    """
    cancel_event = asyncio.Event()
    entry = _runs[run_id] = {
        "cancel_event": cancel_event,
        "owner": owner,
        "task": None,
    }

    async def _event_generator() -> AsyncGenerator[bytes, None]:
        outcome = run_main.STATUS_SUCCESS
        error_message: Optional[str] = None
        final_output: Any = None
        try:
            # ``run_id`` est émis d'emblée pour que le client puisse se
            # raccrocher — y compris après un rejeu, où il diffère du wid
            # qu'il avait envoyé.
            yield f"data: {json.dumps({'event': 'run_started', 'wid': run_id})}\n\n".encode()
            stream = (
                runner(cancel_event)
                if runner is not None
                else _workflow_runner(run_id, cancel_event, agent_ids, file_bytes)
            )
            async for chunk in stream:
                # L'issue est lue AVANT de céder le morceau : un client qui
                # part pendant ce ``yield`` ne doit pas la faire mentir.
                terminal = _terminal_event(chunk)
                if terminal is not None and terminal["event"] == "error":
                    outcome = run_main.STATUS_ERROR
                    error_message = str(terminal.get("detail") or "")
                elif terminal is not None and terminal["event"] == "awaiting_input":
                    # Déjà consigné par ``run_main.pause_run`` (on_await).
                    outcome = run_main.STATUS_AWAITING
                elif terminal is not None:
                    outcome = run_main.STATUS_CANCELLED
                else:
                    is_done, done_output = _done_output(chunk)
                    if is_done:
                        final_output = done_output
                yield chunk.encode() if isinstance(chunk, str) else chunk
                if cancel_event.is_set():
                    # Drain one more iteration if the runner hasn't noticed.
                    continue
            if cancel_event.is_set() and outcome != run_main.STATUS_AWAITING:
                outcome = run_main.STATUS_CANCELLED
        except asyncio.CancelledError:
            # Le client s'est deconnecte : signaler l'annulation aux travaux
            # que le graphe a pu lancer hors de l'arbre de la task de drive
            # (sous-workflows, attentes de retry) et qui lisent cancel_event.
            cancel_event.set()
            if outcome != run_main.STATUS_AWAITING:
                outcome = run_main.STATUS_CANCELLED
            yield f"data: {json.dumps({'event': 'cancelled'})}\n\n".encode()
            raise
        except Exception as exc:  # noqa: BLE001 - l'issue doit être consignée
            outcome = run_main.STATUS_ERROR
            error_message = f"{type(exc).__name__}: {exc}"
            logger.exception("[workflows] run_id=%s a echoue", run_id)
            yield (
                "data: "
                + json.dumps({"event": "error", "detail": error_message})
                + "\n\n"
            ).encode()
        finally:
            # Une reprise du même run a pu déjà se réinscrire sous ce run_id.
            if _runs.get(run_id) is entry:
                _runs.pop(run_id, None)
            # Pas de ``return`` dans ce ``finally`` : il avalerait le
            # ``CancelledError`` d'un client parti.
            settles = True
            # Une décision à annoncer (pause) ou une fin sans réponse.
            notice = False
            if resume_guard is not None and resume_guard.get("lost"):
                # Une autre reprise tient la ligne : ni fin, ni remise en attente.
                settles = False
            elif resume_guard is not None and not resume_guard.get("decided"):
                settles = False
                if outcome != run_main.STATUS_AWAITING and entry.get("stopped"):
                    # Arrêt demandé avant que la réponse soit prise : le run
                    # s'arrête comme un abandon, le flux et la ligne disent
                    # « annulé » tous les deux.
                    try:
                        notice = run_main.stop_unanswered_resume(
                            run_id, resume_guard.get("resumed_at")
                        )
                    except Exception:  # noqa: BLE001
                        logger.exception("[workflows] run_id=%s non clos", run_id)
                elif outcome != run_main.STATUS_AWAITING:
                    # La reprise n'a pas atteint la décision (client parti,
                    # panne au démarrage, extinction) : rien n'a été fait, le
                    # run attend toujours sa réponse.
                    try:
                        run_main.release_paused_run(run_id)
                    except Exception:  # noqa: BLE001
                        logger.exception(
                            "[workflows] run_id=%s non remis en attente", run_id
                        )
            elif outcome == run_main.STATUS_AWAITING:
                # Une pause n'est pas une fin : ni ``finished_at``, ni
                # notification ``workflow_done``. La reprise finira le run.
                settles = False
                notice = True
            if notice:
                # Aussitôt, sans attendre le passage du planificateur ; chaque
                # avis est réservé avant l'envoi, il ne part qu'une fois.
                try:
                    from apowerb.core import decision_notices

                    await decision_notices.deliver(run_id)
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[workflows] avis de décision non envoyé (run_id=%s)", run_id
                    )
            if settles:
                try:
                    run_main.finish_run(run_id, status=outcome, error_message=error_message)
                except Exception:  # noqa: BLE001 - la trace ne doit pas casser le flux
                    logger.exception(
                        "[workflows] issue du run_id=%s non consignee", run_id
                    )
                # T2 — un run de graphe (persisté, ``config.workflow_id`` connu)
                # peut avoir des workflows en écoute (``workflow_done``). Import
                # différé : ``workflow_triggers`` importe ce module pour
                # ``launch_triggered_run`` — un import de niveau module créerait
                # un cycle. Best-effort : un échec de notification ne doit
                # jamais retirer au client le flux qu'il vient de recevoir.
                try:
                    from apowerb.core import workflow_triggers as _wt

                    await _wt.notify_run_finished(
                        run_id=run_id,
                        owner_id=owner,
                        status=outcome,
                        output=final_output,
                        error_message=error_message,
                    )
                except Exception:  # noqa: BLE001
                    logger.exception(
                        "[workflows] notification workflow_done non envoyee (run_id=%s)",
                        run_id,
                    )

    return StreamingResponse(
        _detached(_event_generator(), entry) if resume_guard is not None else _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Workflow-Id": run_id,
        },
    )


# Reprises en cours, gardées en vie hors de la connexion qui les a lancées.
_detached_runs: set[asyncio.Task] = set()


def _detached(
    events: AsyncGenerator[bytes, None], entry: dict[str, Any]
) -> AsyncGenerator[bytes, None]:
    """Exécute ``events`` dans une tâche à part et en relaie le flux.

    Une reprise suit une réponse déjà donnée : un client qui part (onglet
    fermé, page quittée, une autre décision ouverte) ne doit pas annuler le
    run et perdre la décision. La connexion ne fait que relayer ; l'arrêt
    volontaire passe par ``POST /{wid}/cancel``, qui annule aussi la tâche
    (``entry["task"]``) pour interrompre le nœud en cours.
    """
    queue: asyncio.Queue = asyncio.Queue()
    entry["queue"] = queue
    relayed = True

    async def _pump() -> None:
        try:
            async for chunk in events:
                # Plus personne n'écoute : la suite tourne sans s'accumuler.
                if relayed:
                    queue.put_nowait(chunk)
        finally:
            queue.put_nowait(None)

    task = asyncio.get_running_loop().create_task(_pump())
    entry["task"] = task
    _detached_runs.add(task)
    task.add_done_callback(_detached_runs.discard)

    async def _relay() -> AsyncGenerator[bytes, None]:
        nonlocal relayed
        try:
            while (chunk := await queue.get()) is not None:
                yield chunk
        finally:
            relayed = False

    return _relay()


# Codes de GraphError dont le statut HTTP n'est pas 404.
_SCHEMA_ERROR_STATUS = {
    "tool_ambiguous": status.HTTP_409_CONFLICT,
    "mcp_transport_refused": status.HTTP_422_UNPROCESSABLE_ENTITY,
    "mcp_unreachable": status.HTTP_502_BAD_GATEWAY,
}


@router.get("/tools/schema")
async def get_tool_schema(
    tool: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Schéma des arguments d'un outil, pour qu'un nœud tool du studio
    demande automatiquement les bons champs selon l'outil choisi.

    ``tool`` est la même référence que ``config.tool`` d'un nœud tool
    (``categorie.outil`` ou ``tool_config{id}[:fonction]``), résolue avec
    ``resolve_tool`` pour l'utilisateur courant : les outils qu'il a
    configurés (MCP, base de données...) fonctionnent donc aussi, pas
    seulement ceux du portfolio.
    """
    from apowerb.core import workflow_mcp
    from apowerb.core.workflow_graph import GraphError
    from apowerb.core.workflow_runtime import resolve_tool, tool_arg_schema

    try:
        func = resolve_tool(tool, current_user.email)
        if isinstance(func, workflow_mcp.McpNodeTool):
            return {"tool": tool, **await workflow_mcp.arg_schema(func)}
    except GraphError as exc:
        # Même code, mêmes params que la résolution faite au run (resolve_tool) :
        # l'UI n'a qu'une seule forme d'erreur à traiter, qu'elle vienne
        # d'ici ou d'un run qui a échoué sur le même nœud.
        http_status = _SCHEMA_ERROR_STATUS.get(exc.code, status.HTTP_404_NOT_FOUND)
        raise HTTPException(
            status_code=http_status,
            detail={"code": exc.code, "params": exc.params},
        )
    return {"tool": tool, **tool_arg_schema(func)}


# Corps d'un aperçu : gabarit + sorties gardées par l'UI ; au-delà, refusé
# avant tout rendu (les sorties d'un run peuvent être volumineuses).
RENDER_PREVIEW_MAX_BYTES = 1_000_000
# Résultat rendu : un petit corps peut référencer mille fois la même grande
# sortie. Borne supérieure calculée avant le rendu (voir _rendered_size_bound).
RENDER_PREVIEW_MAX_RENDERED_BYTES = 1_000_000


class RenderPreviewBody(BaseModel):
    template: Any = None
    outputs: Dict[str, Any] = Field(default_factory=dict)
    # Nœud dont la configuration est rendue : nommé dans template_ref_invalid.
    node_id: Optional[str] = None


def _rendered_size_bound(template: Any, outputs: Dict[str, Any]) -> int:
    """Majorant de la taille rendue, sans rien rendre.

    Chaque ``{{ref...}}`` produit au plus la sortie entière de ``ref`` en JSON
    (un sous-chemin, ou une chaîne rendue telle quelle, est plus court) ; le
    texte autour reste. Gabarits imbriqués (dict, liste) compris.
    """
    from apowerb.core.workflow_graph import _TEMPLATE

    sizes: Dict[str, int] = {}

    def _size(ref: str) -> int:
        if ref not in sizes:
            sizes[ref] = (
                len(json.dumps(outputs[ref], ensure_ascii=False, default=str).encode())
                if ref in outputs
                else 0
            )
        return sizes[ref]

    def _walk(value: Any) -> int:
        if isinstance(value, dict):
            return sum(_walk(v) for v in value.values())
        if isinstance(value, list):
            return sum(_walk(v) for v in value)
        if isinstance(value, str):
            return len(value.encode()) + sum(
                _size(m.group(1)) for m in _TEMPLATE.finditer(value)
            )
        return len(json.dumps(value, default=str).encode())

    return _walk(template)


def _too_large(max_bytes: int) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail={"code": "render_preview_too_large", "params": {"max": str(max_bytes)}},
    )


@router.post("/render-preview")
async def render_preview(
    body: RenderPreviewBody,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Gabarit rendu avec les sorties du dernier run de test du Studio.

    Même ``render`` que le moteur, pour que l'aperçu ne diverge jamais d'un
    vrai run ; aucun nœud n'est exécuté. ``template_ref_invalid`` revient tel
    que le run le lèverait (422, ``detail={code, params, message}``).
    """
    from apowerb.core.workflow_graph import GraphError, render

    size = len(json.dumps(body.model_dump(), ensure_ascii=False, default=str).encode())
    if size > RENDER_PREVIEW_MAX_BYTES:
        raise _too_large(RENDER_PREVIEW_MAX_BYTES)
    if (
        _rendered_size_bound(body.template, body.outputs)
        > RENDER_PREVIEW_MAX_RENDERED_BYTES
    ):
        raise _too_large(RENDER_PREVIEW_MAX_RENDERED_BYTES)
    try:
        rendered = render(body.template, body.outputs, body.node_id)
    except GraphError as exc:
        if exc.code == "render_too_large":
            raise _too_large(RENDER_PREVIEW_MAX_RENDERED_BYTES)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"code": exc.code, "params": exc.params, "message": str(exc)},
        )
    return {"rendered": rendered}


@router.get("/runs")
async def list_runs(
    limit: int = 50,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Les runs de l'utilisateur, du plus récent au plus ancien.

    Chaque entrée porte son déclencheur, son statut, la cause de son échec le
    cas echeant, et ``input_available`` — l'entrée est-elle encore sur disque,
    c'est-à-dire ce run est-il rejouable.
    """
    return run_main.list_runs(current_user.email, limit=limit)


@router.get("/runs/awaiting")
async def list_awaiting_runs(
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Les runs arrêtés sur une décision humaine qui revient à l'appelant.

    Déclarée avant ``/runs/{run_id}`` : sinon ``awaiting`` serait lu comme
    un identifiant de run.
    """
    from apowerb.core import workflow_main

    items = run_main.list_awaiting(current_user.email)
    names: dict[tuple, Optional[str]] = {}
    for item in items:
        key = (item["workflow_id"], item["owner_id"])
        if key not in names:
            wf = (
                workflow_main.get_workflow(key[0], owner_id=key[1])
                if key[0]
                else None
            )
            names[key] = (wf or {}).get("name")
        item["workflow_name"] = names[key]
        item.pop("owner_id", None)
    return items


class DecisionAnswer(BaseModel):
    choice: Optional[str] = Field(None, max_length=200)
    text: Optional[str] = Field(None, max_length=10_000)


@router.post("/runs/{run_id}/resume")
async def resume_run(
    run_id: str,
    body: DecisionAnswer,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Répond à la décision d'un run en attente et le reprend (SSE).

    Même ``run_id`` : le flux rejoue les nœuds déjà faits (``replayed``,
    jamais réexécutés), puis continue. 404 si le run n'est pas à l'appelant
    (propriétaire ou assigné), 409 s'il n'attend plus, 422 si la réponse
    n'est pas permise par la décision.
    """
    claimed = run_main.claim_paused_run(
        run_id, current_user.email, body.choice, body.text
    )
    return _streaming_run(
        run_id=run_id,
        agent_ids=[],
        file_bytes=None,
        owner=claimed["run"]["owner_id"],
        runner=lambda cancel: _decision_resume_runner(run_id, claimed, cancel),
        resume_guard=claimed,
    )


@router.post("/runs/{run_id}/retry-from-failure")
async def retry_from_failure(
    run_id: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """« Reprendre ici » : relance un run en échec à partir du nœud fautif (SSE).

    Nouveau run (``replay_of``) sur la dernière sauvegarde du workflow : les
    nœuds faits avant l'échec sont rejoués (``replayed``) sans être exécutés,
    le nœud en échec et la suite s'exécutent vraiment. Réservé au
    propriétaire (404 sinon), une seule fois par échec (409) ; les codes de
    refus sont ceux de ``run_main.prepare_retry``.
    """
    retry = run_main.prepare_retry(run_id, current_user.email)
    owner = current_user.email
    return _streaming_run(
        run_id=retry["run_id"],
        agent_ids=[],
        file_bytes=None,
        owner=owner,
        runner=lambda cancel: _retry_runner(owner, retry, cancel),
    )


@router.post("/runs/{run_id}/abandon")
async def abandon_run(
    run_id: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Clôt sans réponse un run en attente d'une décision (``cancelled``)."""
    from apowerb.core import decision_notices

    run_main.abandon_paused_run(run_id, current_user.email)
    # Le propriétaire est prévenu et ``workflow_done`` voit la fin ; un échec
    # ici est repris par le planificateur, il ne change pas la réponse.
    try:
        await decision_notices.deliver(run_id)
    except Exception:  # noqa: BLE001
        logger.exception("[workflows] fin de décision non annoncée (run_id=%s)", run_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Un run précis, s'il appartient à l'appelant."""
    run = run_main.get_run(run_id, owner_id=current_user.email)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown run: {run_id}",
        )
    return run


@router.post("/runs/{run_id}/replay")
async def replay_run(
    run_id: str,
    force: bool = False,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Relance un run à partir de l'entrée qu'il avait conservée.

    Un run réussi n'est pas rejoué par accident : ses effets de bord ont déjà
    eu lieu, il faut ``force=true``. Un run encore en vol est refusé tout
    court. Le rejeu est un **nouveau** run qui cite l'original.
    """
    replay = run_main.prepare_replay(run_id, owner_id=current_user.email, force=force)
    if replay["trigger"] in run_main.AGENT_TRIGGERS:
        new_run_id, owner, config = replay["run_id"], current_user.email, replay["config"]
        return _streaming_run(
            run_id=new_run_id,
            agent_ids=[],
            file_bytes=None,
            owner=owner,
            runner=lambda _cancel: _agent_replay_runner(new_run_id, owner, config),
        )
    if _is_graph_run(replay["agent_ids"], replay["config"]):
        owner, config = current_user.email, replay["config"]
        new_run_id = replay["run_id"]
        return _streaming_run(
            run_id=new_run_id,
            agent_ids=[],
            file_bytes=None,
            owner=owner,
            runner=lambda cancel: _graph_replay_runner(
                owner, config, cancel, run_id=new_run_id
            ),
        )
    return _streaming_run(
        run_id=replay["run_id"],
        agent_ids=[str(a) for a in replay["agent_ids"]],
        file_bytes=replay["file_bytes"],
        owner=current_user.email,
    )


@router.post("/run-sse")
async def run_workflow_sse(
    canvas_agent_ids: str = Form(...),
    workflow_id: Optional[str] = Form(None),
    config_json: Optional[str] = Form(None),
    file: Optional[UploadFile] = File(None),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Start a workflow run and stream its events back as SSE."""
    # canvas_agent_ids is a JSON-encoded list. Accept either a bare list or a
    # dict shape (``{"agents": [...]}``) for forward-compat.
    try:
        decoded = json.loads(canvas_agent_ids)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"canvas_agent_ids must be JSON: {exc}",
        )
    if isinstance(decoded, dict):
        agent_ids = decoded.get("agents") or []
    else:
        agent_ids = decoded or []
    if not isinstance(agent_ids, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="canvas_agent_ids must be a JSON list",
        )

    # Optional config payload for future extensions (e.g. per-row overrides).
    _config: Dict[str, Any] = {}
    if config_json:
        try:
            _config = json.loads(config_json)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"config_json must be JSON: {exc}",
            )

    file_bytes: Optional[bytes] = None
    if file is not None:
        file_bytes = await file.read()

    wid = workflow_id or nanoid_generate(size=21)

    # Le run est consigné AVANT de démarrer, avec son entrée : c'est elle qui
    # rend le rejeu possible. ``start_run`` renvoie l'identifiant retenu, qui
    # peut différer du wid demandé s'il était déjà pris.
    try:
        run_id = run_main.start_run(
            trigger="workflow",
            owner_id=current_user.email,
            agent_ids=agent_ids,
            config=_config,
            file_bytes=file_bytes,
            file_name=file.filename if file is not None else None,
            run_id=wid,
        )
    except Exception:  # noqa: BLE001
        # La trace est précieuse, l'exécution l'est davantage : une base
        # indisponible doit dégrader le suivi, pas transformer une panne de
        # stockage en panne du produit. Le run part quand même, sous son wid,
        # et l'incident est journalisé au niveau qui réveille quelqu'un.
        logger.exception(
            "[workflows] run non consigne (wid=%s) — il demarre sans trace, "
            "donc sans rejeu possible",
            wid,
        )
        run_id = wid

    logger.info(
        "[workflows] run-sse started run_id=%s agents=%d owner=%s",
        run_id,
        len(agent_ids),
        current_user.email,
    )

    return _streaming_run(
        run_id=run_id,
        agent_ids=agent_ids,
        file_bytes=file_bytes,
        owner=current_user.email,
    )


@router.post("/{wid}/cancel")
async def cancel_workflow(
    wid: str,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Signal a live run to terminate."""
    entry = _runs.get(wid)
    if entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown workflow id: {wid}",
        )
    # Ownership: only the run's owner may cancel it.
    if entry.get("owner") and entry["owner"] != current_user.email:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not the owner of this workflow run",
        )

    # Un arrêt voulu, pas une coupure : une reprise arrêtée avant de prendre
    # sa réponse est close au lieu d'être remise en attente.
    entry["stopped"] = True
    entry["cancel_event"].set()
    # ``cancel_event`` n'est lu qu'entre deux nœuds. Une reprise détachée n'a
    # plus de client dont le départ annulerait sa tâche : on l'annule ici,
    # comme le faisait la déconnexion, pour interrompre le nœud en cours.
    task = entry.get("task")
    if task is not None and not task.done():
        task.cancel()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
