"""Workflows persistés : CRUD, validation, historique, exécution serveur.

Complète ``routers/workflows.py`` (runs, annulation, rejeu), dont il réutilise
le flux SSE : un run de graphe est consigné dans ``agent_runs`` et annulable
par ``POST /api/workflows/{run_id}/cancel`` comme un run de canvas.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from nanoid import generate as nanoid_generate
from pydantic import BaseModel

from apowerb.admin.guard import require_admin
from apowerb.auth.dependencies import get_current_user
from apowerb.core import run_main, workflow_main, workflow_suggest_stats
from apowerb.core.workflow_graph import run_graph
from apowerb.core.workflow_suggest_stats import SuggestEvent
from apowerb.routers.workflows import _streaming_run, logger
from apowerb.users import schemas as user_schemas

router = APIRouter(prefix="/workflows/defs", tags=["workflows"])


class WorkflowCreate(BaseModel):
    name: str
    description: Optional[str] = None
    graph: dict = {"version": 1, "nodes": [], "edges": []}


class WorkflowUpdate(BaseModel):
    expected_version: int
    name: Optional[str] = None
    description: Optional[str] = None
    graph: Optional[dict] = None
    status: Optional[str] = None


class GraphBody(BaseModel):
    graph: dict


class RunBody(BaseModel):
    payload: Any = None


class SuggestBody(BaseModel):
    graph: dict
    node_id: str
    name: Optional[str] = None
    description: Optional[str] = None


def _not_found(workflow_id: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown workflow: {workflow_id}")


def _call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except workflow_main.WorkflowNotFound as exc:
        raise _not_found(str(exc)) from exc
    except workflow_main.VersionConflict as exc:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            {
                "message": "Workflow modifié ailleurs : recharge avant d'enregistrer.",
                "current_version": exc.current_version,
            },
        ) from exc
    except workflow_main.InvalidWorkflow as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


@router.get("")
async def list_workflows(current_user: user_schemas.User = Depends(get_current_user)):
    return workflow_main.list_workflows(owner_id=current_user.email)


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_workflow(
    body: WorkflowCreate, current_user: user_schemas.User = Depends(get_current_user)
):
    return _call(
        workflow_main.create_workflow,
        owner_id=current_user.email,
        name=body.name,
        description=body.description,
        graph=body.graph,
    )


@router.post("/validate")
async def validate_graph(
    body: GraphBody, current_user: user_schemas.User = Depends(get_current_user)
):
    return workflow_main.check_workflow(body.graph, owner_id=current_user.email)


@router.post("/suggest-next")
async def suggest_next_node(
    body: SuggestBody, current_user: user_schemas.User = Depends(get_current_user)
):
    """Nœuds proposés par le modèle après ``node_id`` (graphe non enregistré).

    Sans état, comme ``/validate`` : l'éditeur envoie le brouillon tel qu'il
    est à l'écran. 404 si la fonction est éteinte, 402 si le plafond de jetons
    est atteint, 503 si le modèle ne répond pas utilement.
    """
    from apowerb.core import workflow_suggest

    graph = _call(workflow_main.parse_graph, body.graph)
    return await workflow_suggest.suggest_next(
        graph,
        body.node_id,
        owner_id=current_user.email,
        name=body.name,
        description=body.description,
    )


@router.post("/suggest-events", status_code=status.HTTP_204_NO_CONTENT)
async def record_suggest_event(
    body: SuggestEvent, current_user: user_schemas.User = Depends(get_current_user)
):
    """Une étape suivante vue dans l'éditeur, ajoutée aux totaux du jour.

    Connexion exigée, mais rien de l'utilisateur n'est conservé (roadmap#88).
    """
    workflow_suggest_stats.record_event(body)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/suggest-stats")
async def suggest_stats(
    days: int = Query(30, ge=1, le=365),
    current_user: user_schemas.User = Depends(require_admin),
):
    """Adoption des pastilles règles contre IA sur ``days`` jours (admin)."""
    return workflow_suggest_stats.read_totals(days=days)


@router.get("/{workflow_id}")
async def get_workflow(
    workflow_id: str, current_user: user_schemas.User = Depends(get_current_user)
):
    wf = workflow_main.get_workflow(workflow_id, owner_id=current_user.email)
    if wf is None:
        raise _not_found(workflow_id)
    return {
        **wf,
        "validation": workflow_main.check_workflow(
            wf["graph"], workflow_id=workflow_id, owner_id=current_user.email
        ),
    }


@router.put("/{workflow_id}")
async def update_workflow(
    workflow_id: str,
    body: WorkflowUpdate,
    current_user: user_schemas.User = Depends(get_current_user),
):
    return _call(
        workflow_main.update_workflow,
        workflow_id,
        owner_id=current_user.email,
        **body.model_dump(),
    )


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow(
    workflow_id: str, current_user: user_schemas.User = Depends(get_current_user)
):
    _call(workflow_main.delete_workflow, workflow_id, owner_id=current_user.email)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{workflow_id}/duplicate", status_code=status.HTTP_201_CREATED)
async def duplicate_workflow(
    workflow_id: str, current_user: user_schemas.User = Depends(get_current_user)
):
    return _call(
        workflow_main.duplicate_workflow, workflow_id, owner_id=current_user.email
    )


@router.get("/{workflow_id}/revisions")
async def list_revisions(
    workflow_id: str, current_user: user_schemas.User = Depends(get_current_user)
):
    return _call(workflow_main.list_revisions, workflow_id, owner_id=current_user.email)


@router.post("/{workflow_id}/revisions/{revision_id}/restore")
async def restore_revision(
    workflow_id: str,
    revision_id: int,
    current_user: user_schemas.User = Depends(get_current_user),
):
    return _call(
        workflow_main.restore_revision,
        workflow_id,
        revision_id,
        owner_id=current_user.email,
    )


@router.post("/{workflow_id}/run")
async def run_workflow(
    workflow_id: str,
    body: RunBody,
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Exécute la version courante côté serveur et diffuse ses événements (SSE)."""
    from apowerb.core import workflow_runtime
    from apowerb.core.run_gate import resolve_owner_plan

    owner = current_user.email
    wf = workflow_main.get_workflow(workflow_id, owner_id=owner)
    if wf is None:
        raise _not_found(workflow_id)
    report = workflow_main.check_workflow(wf["graph"], workflow_id=workflow_id)
    if not report["valid"]:
        codes = report.get("codes")
        detail = (
            {**codes[0], "message": report["errors"][0]}
            if codes
            else report["errors"][0]
        )
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail)
    graph = workflow_main.parse_graph(wf["graph"])
    run_agent, run_tool, run_rag, run_notify = workflow_runtime.bindings_for(
        owner, await resolve_owner_plan(owner)
    )
    # Sous-workflows : callback owner-scopé (même contrôle d'accès que pour
    # lancer ce workflow directement, cf. get_workflow ci-dessus) — un
    # subworkflow exécute la MÊME version « courante » que ce endpoint
    # (workflow_main.get_workflow n'est pas filtré par statut : brouillon ou
    # publié, c'est ce qui est enregistré maintenant).
    run_subworkflow = workflow_runtime.resolve_workflow_for(owner)

    run_id = nanoid_generate(size=21)
    try:
        run_id = run_main.start_run(
            trigger="workflow",
            owner_id=owner,
            run_id=run_id,
            config={
                "workflow_id": workflow_id,
                "version": wf["version"],
                "payload": body.payload,
            },
        )
    except Exception:  # noqa: BLE001 - même politique que /run-sse
        logger.exception(
            "[workflows] run de graphe non consigne (workflow=%s)", workflow_id
        )

    def _runner(cancel_event):
        return run_graph(
            graph,
            payload=body.payload,
            run_agent=run_agent,
            run_tool=run_tool,
            run_rag=run_rag,
            run_subworkflow=run_subworkflow,
            workflow_id=workflow_id,
            cancel_event=cancel_event,
            run_notify=run_notify,
        )

    return _streaming_run(
        run_id=run_id, agent_ids=[], file_bytes=None, owner=owner, runner=_runner
    )
