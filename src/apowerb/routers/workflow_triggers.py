"""API de gestion des triggers de workflow — authentifiée, propriétaire seulement.

``GET  /api/workflows/{wid}/triggers``         — état courant (kind, actif,
raison, URL, échéance, dernier déclenchement).
``POST /api/workflows/{wid}/triggers/rotate``  — régénère le jeton
(webhook/form) et, si actif, le secret HMAC.

Un autre utilisateur que le propriétaire reçoit 404 — jamais 403 : même
politique que ``routers/workflow_defs.py`` (« introuvable », pas «
interdit » — on ne confirme pas l'existence du workflow d'autrui).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from apowerb.auth.dependencies import get_current_user
from apowerb.core import workflow_main
from apowerb.core import workflow_triggers as wt
from apowerb.users import schemas as user_schemas

router = APIRouter(prefix="/workflows", tags=["workflows"])


def _not_found(workflow_id: str) -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, f"Unknown workflow: {workflow_id}")


def _owned_workflow(workflow_id: str, owner_id: str) -> dict:
    wf = workflow_main.get_workflow(workflow_id, owner_id=owner_id)
    if wf is None:
        raise _not_found(workflow_id)
    return wf


@router.get("/{workflow_id}/triggers")
async def get_triggers(
    workflow_id: str, current_user: user_schemas.User = Depends(get_current_user)
):
    wf = _owned_workflow(workflow_id, current_user.email)
    return wt.get_trigger_status(
        workflow_id, current_user.email, workflow_status=wf["status"]
    )


@router.post("/{workflow_id}/triggers/rotate")
async def rotate_triggers(
    workflow_id: str, current_user: user_schemas.User = Depends(get_current_user)
):
    _owned_workflow(workflow_id, current_user.email)
    try:
        result = wt.rotate_trigger(workflow_id, current_user.email)
    except wt.RotateNotApplicable as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Ce trigger ({exc}) ne porte pas de jeton à régénérer.",
        ) from exc
    if result is None:
        # Le workflow existe (vérifié ci-dessus) mais n'a jamais été
        # synchronisé (créé avant ce lot, ou jamais enregistré) : rien à
        # régénérer, même verdict que « workflow introuvable ».
        raise _not_found(workflow_id)
    return result
