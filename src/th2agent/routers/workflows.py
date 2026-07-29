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

Run state lives in an in-memory dict — fine for a single-process deployment,
and anything cluster-wide would need a shared store. Each run carries:

    ``{"cancel_event": asyncio.Event, "task": asyncio.Task, "owner": email}``

The default runner is a thin stub: workflow execution itself is still owned
by the frontend (see ``DiagramEditor.jsx``'s ``runWorkflow``). The SSE route
exists primarily so the browser can centrally kill an in-flight run through
a single wid. The runner is swappable via ``_workflow_runner`` so tests —
and a future full backend implementation — can drop-in their own logic.
"""

from __future__ import annotations

import asyncio
import json
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

from th2agent.auth.dependencies import get_current_user
from th2agent.users import schemas as user_schemas


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


# Swappable hook (tests override this).
_workflow_runner: Callable[
    [str, asyncio.Event, List[str], Optional[bytes]],
    AsyncGenerator[str, None],
] = _default_workflow_runner


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


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
    cancel_event = asyncio.Event()

    _runs[wid] = {
        "cancel_event": cancel_event,
        "owner": current_user.email,
        "task": None,
    }

    logger.info(
        "[workflows] run-sse started wid=%s agents=%d owner=%s",
        wid,
        len(agent_ids),
        current_user.email,
    )

    async def _event_generator() -> AsyncGenerator[bytes, None]:
        try:
            # ``wid`` is always emitted up front so the client can reconcile.
            yield f"data: {json.dumps({'event': 'run_started', 'wid': wid})}\n\n".encode()
            async for chunk in _workflow_runner(
                wid, cancel_event, agent_ids, file_bytes
            ):
                if isinstance(chunk, str):
                    yield chunk.encode()
                else:
                    yield chunk
                if cancel_event.is_set():
                    # Drain one more iteration if the runner hasn't noticed.
                    continue
        except asyncio.CancelledError:
            yield f"data: {json.dumps({'event': 'cancelled'})}\n\n".encode()
            raise
        finally:
            _runs.pop(wid, None)

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Workflow-Id": wid,
        },
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

    entry["cancel_event"].set()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
