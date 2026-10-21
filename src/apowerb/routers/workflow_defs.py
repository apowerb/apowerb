"""Workflows persistés : CRUD, validation, historique, exécution serveur.

Complète ``routers/workflows.py`` (runs, annulation, rejeu), dont il réutilise
le flux SSE : un run de graphe est consigné dans ``agent_runs`` et annulable
par ``POST /api/workflows/{run_id}/cancel`` comme un run de canvas.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from nanoid import generate as nanoid_generate
from pydantic import BaseModel

from apowerb.admin.guard import require_admin
from apowerb.auth.dependencies import get_current_user
from apowerb.core import run_main, workflow_main, workflow_suggest_stats
from apowerb.core.workflow_graph import run_graph
from apowerb.core.workflow_suggest_stats import SuggestEvent
from apowerb.routers.workflows import _streaming_run, logger, pause_saver
from apowerb.users import schemas as user_schemas

router = APIRouter(prefix="/workflows/defs", tags=["workflows"])

_MAX_SIMULATED_FILES = 10


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
    # « Tester jusqu'ici » : seuls ce nœud et son amont s'exécutent.
    until: Optional[str] = None


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


@router.post("/node-shapes")
async def node_shapes(
    body: GraphBody, current_user: user_schemas.User = Depends(get_current_user)
):
    """Forme connue de la sortie de chaque nœud du brouillon, sans rien exécuter.

    Lecture seule, comme ``/validate`` : la configuration du graphe, plus la
    forme apprise de la réponse des outils de l'appelant (``workflow_shapes``).
    """
    from apowerb.core import workflow_shapes

    graph = _call(workflow_main.parse_graph, body.graph)
    tool_refs = [n.config.get("tool") for n in graph.nodes if n.type == "tool"]
    learned = workflow_shapes.read_tool_shapes(current_user.email, tool_refs)
    return {"shapes": workflow_shapes.node_shapes(graph, tool_shapes=learned)}


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
    """Exécute la version courante côté serveur et diffuse ses événements (SSE).

    ``until`` limite le run à ce nœud et à son amont (``run_graph``) ; un tel
    run partiel ne réveille aucun trigger ``workflow_done``.
    """
    return await _run_current_version(
        workflow_id,
        body.payload,
        current_user.email,
        trigger="workflow",
        until=body.until,
    )


@router.post("/{workflow_id}/simulate-email")
async def simulate_email(
    workflow_id: str,
    sender: str = Form(..., alias="from"),
    subject: str = Form(""),
    body: str = Form(""),
    to: str = Form(""),
    files: list[UploadFile] = File(default=[]),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """Lance le workflow comme si ce mail venait d'arriver (SSE).

    Même enveloppe que le trigger ``email`` (``workflow_email_triggers.
    email_envelope``) : les pièces jointes sont enregistrées pour le
    demandeur et n'apparaissent que sous forme de ``ref`` opaque. Sert à
    tester un workflow mail sans boîte branchée ; la version courante
    (brouillon compris) s'exécute, comme ``POST /run``.
    """
    from apowerb.core import workflow_attachments
    from apowerb.core.workflow_email_triggers import email_envelope

    owner = current_user.email
    if workflow_main.get_workflow(workflow_id, owner_id=owner) is None:
        raise _not_found(workflow_id)
    if len(files) > _MAX_SIMULATED_FILES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"{_MAX_SIMULATED_FILES} pièces jointes au plus",
        )
    attachments = []
    for upload in files:
        data = await upload.read(workflow_attachments.MAX_ATTACHMENT_BYTES + 1)
        try:
            attachments.append(
                workflow_attachments.store(
                    owner, upload.filename or "", upload.content_type or "", data
                )
            )
        except workflow_attachments.AttachmentError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                {"code": exc.code, "message": str(exc)},
            ) from exc
    payload = email_envelope(
        sender=sender,
        to=to,
        subject=subject,
        body=body,
        received_at=datetime.now(timezone.utc).isoformat(),
        attachments=attachments,
    )
    return await _run_current_version(
        workflow_id, payload, owner, trigger="workflow_simulated_email"
    )


async def _run_current_version(
    workflow_id: str,
    payload: Any,
    owner: str,
    *,
    trigger: str,
    until: Optional[str] = None,
):
    from apowerb.core import workflow_runtime
    from apowerb.core.run_gate import resolve_owner_plan

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
    if until is not None and until not in {n.id for n in graph.nodes}:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {
                "code": "until_unknown_node",
                "message": f"nœud inconnu pour un test partiel : {until}",
                "params": {"node": until},
            },
        )
    from apowerb.core.workflow_shapes import learning_run_tool

    run_agent, run_tool, run_rag, run_notify = workflow_runtime.bindings_for(
        owner, await resolve_owner_plan(owner)
    )
    # La forme de chaque réponse d'outil est retenue (jamais ses valeurs) pour
    # proposer ses champs au nœud suivant la prochaine fois.
    run_tool = learning_run_tool(owner, run_tool)
    # Sous-workflows : callback owner-scopé (même contrôle d'accès que pour
    # lancer ce workflow directement, cf. get_workflow ci-dessus) — un
    # subworkflow exécute la MÊME version « courante » que ce endpoint
    # (workflow_main.get_workflow n'est pas filtré par statut : brouillon ou
    # publié, c'est ce qui est enregistré maintenant).
    run_subworkflow = workflow_runtime.resolve_workflow_for(owner)

    run_id = nanoid_generate(size=21)
    try:
        run_id = run_main.start_run(
            trigger=trigger,
            owner_id=owner,
            run_id=run_id,
            config={
                "workflow_id": workflow_id,
                "version": wf["version"],
                "payload": payload,
                **({"until": until} if until is not None else {}),
            },
        )
    except Exception:  # noqa: BLE001 - même politique que /run-sse
        logger.exception(
            "[workflows] run de graphe non consigne (workflow=%s)", workflow_id
        )

    def _runner(cancel_event):
        return run_graph(
            graph,
            payload=payload,
            run_agent=run_agent,
            run_tool=run_tool,
            run_rag=run_rag,
            run_subworkflow=run_subworkflow,
            workflow_id=workflow_id,
            cancel_event=cancel_event,
            run_notify=run_notify,
            services=workflow_runtime.services_for(owner),
            until=until,
            on_await=pause_saver(run_id),
            run_id=run_id,
        )

    return _streaming_run(
        run_id=run_id, agent_ids=[], file_bytes=None, owner=owner, runner=_runner
    )
