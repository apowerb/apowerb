"""GET /rag/stream/{agent_id} — SSE stream for indexation progress."""

import asyncio
import json
import time
from logging import getLogger
from typing import AsyncGenerator, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from th2agent.auth.dependencies import get_current_user
from th2agent.core.knowledge_map import read_knowledge_map, update_status
from th2agent.tools_store.portfolio.rag import tool_get_knowledge
from th2agent.users import schemas as user_schemas

from .validators import _validate_agent_ownership, _validate_session_id

logger = getLogger(__name__)

router = APIRouter()

_SSE_MAX_DURATION = 5 * 60  # 5 minutes
_SSE_KEEPALIVE_INTERVAL = 2  # seconds
_SSE_POLL_FALLBACK_INTERVAL = 10  # seconds


async def _sse_generator(
    agent_id: str,
    session_id: str | None,
) -> AsyncGenerator[str, None]:
    """Async generator that yields SSE events for RAG indexation progress.

    Flow:
    1. Send initial ``connected`` event with current source statuses.
    2. If nothing is processing, send ``complete`` and close.
    3. Subscribe to webhook events for each processing knowledge_id.
    4. Main loop (max 5 min):
       - Check for webhook events (non-blocking).
       - Fallback poll every 10s if no webhook received.
       - Send keep-alive every 2s.
       - Close when all sources reach terminal state.
    5. Cleanup subscriptions in ``finally``.
    """
    from th2agent.routers import rag as _pkg

    kmap = read_knowledge_map(agent_id, session_id=session_id)
    sources = kmap.get("sources", [])

    # 1. Send initial connected event
    yield f"event: connected\ndata: {json.dumps({'sources': sources})}\n\n"

    # 2. Identify processing sources
    processing_kids: dict[str, str] = {}  # knowledge_id -> current status
    for src in sources:
        if src.get("status") == "processing" and src.get("knowledge_id"):
            processing_kids[src["knowledge_id"]] = "processing"

    if not processing_kids:
        yield f"event: complete\ndata: {json.dumps({'message': 'All sources already complete'})}\n\n"
        return

    # 3. Subscribe to event queues for each processing knowledge_id
    subscriptions: dict[str, asyncio.Queue] = {}
    try:
        for kid in processing_kids:
            queue = await _pkg.rag_manager.subscribe(kid)
            subscriptions[kid] = queue

        # 4. Main loop
        start_time = time.monotonic()
        last_poll_time = start_time

        while time.monotonic() - start_time < _SSE_MAX_DURATION:
            now = time.monotonic()
            event_received = False

            # Drain all subscription queues (non-blocking)
            for kid, queue in list(subscriptions.items()):
                while not queue.empty():
                    try:
                        event = queue.get_nowait()
                        event_received = True
                        new_status = event.get("status", "processing")
                        processing_kids[kid] = new_status

                        yield f"event: status\ndata: {json.dumps(event)}\n\n"

                        logger.info(
                            "[RAG_STREAM] SSE sent status event kid=%s -> %s (agent=%s)",
                            kid,
                            new_status,
                            agent_id,
                        )
                    except asyncio.QueueEmpty:
                        break

            # Check if all are done
            all_terminal = all(
                s in ("complete", "error") for s in processing_kids.values()
            )
            if all_terminal:
                yield f"event: complete\ndata: {json.dumps({'message': 'All sources processed', 'statuses': processing_kids})}\n\n"
                return

            # Fallback poll every 10s if no webhook events
            if not event_received and (now - last_poll_time) >= _SSE_POLL_FALLBACK_INTERVAL:
                last_poll_time = now
                for kid, current_status in list(processing_kids.items()):
                    if current_status in ("complete", "error"):
                        continue
                    try:
                        result = await asyncio.to_thread(tool_get_knowledge, kid)
                        if result.get("status") == "error":
                            processing_kids[kid] = "error"
                            update_status(agent_id, kid, "error", session_id=session_id)
                            poll_event = {"event": "poll_update", "knowledge_id": kid, "status": "error"}
                            yield f"event: status\ndata: {json.dumps(poll_event)}\n\n"
                        elif result.get("is_complete"):
                            processing_kids[kid] = "complete"
                            update_status(agent_id, kid, "complete", session_id=session_id)
                            poll_event = {"event": "poll_update", "knowledge_id": kid, "status": "complete"}
                            yield f"event: status\ndata: {json.dumps(poll_event)}\n\n"
                    except Exception as exc:
                        logger.warning(
                            "[RAG_STREAM] Poll fallback failed for kid=%s: %s", kid, exc,
                        )

                # Re-check after poll
                all_terminal = all(
                    s in ("complete", "error") for s in processing_kids.values()
                )
                if all_terminal:
                    yield f"event: complete\ndata: {json.dumps({'message': 'All sources processed (poll)', 'statuses': processing_kids})}\n\n"
                    return

            # Send keepalive and sleep — serves both as SSE keepalive and rate-limit
            # for queue checks (avoids busy-waiting at sub-second intervals)
            yield ": keepalive\n\n"
            await asyncio.sleep(_SSE_KEEPALIVE_INTERVAL)

        # Timeout reached
        yield f"event: timeout\ndata: {json.dumps({'message': 'SSE stream timeout (5 min)', 'statuses': processing_kids})}\n\n"

    finally:
        # 5. Cleanup all subscriptions
        for kid, queue in subscriptions.items():
            await _pkg.rag_manager.unsubscribe(kid, queue)
        logger.info(
            "[RAG_STREAM] SSE stream closed for agent=%s session=%s (%d subscriptions cleaned)",
            agent_id,
            session_id,
            len(subscriptions),
        )


@router.get("/rag/stream/{agent_id}", tags=["rag"])
async def rag_stream(
    agent_id: str,
    session_id: Optional[str] = Query(None),
    current_user: user_schemas.User = Depends(get_current_user),
):
    """SSE endpoint streaming RAG indexation progress for an agent's sources.

    Opens a server-sent event stream that:
    - Sends current source statuses on connect.
    - Pushes real-time updates from th2llm webhooks.
    - Falls back to polling every 10s if webhooks are slow.
    - Sends keep-alive comments every 2s.
    - Auto-closes when all sources are complete or after 5 minutes.

    Requires authentication (JWT via current_user).
    """
    await _validate_agent_ownership(agent_id, current_user)
    _validate_session_id(session_id)

    logger.info("[RAG_STREAM] SSE stream opened for agent=%s session=%s", agent_id, session_id)

    return StreamingResponse(
        _sse_generator(agent_id, session_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
