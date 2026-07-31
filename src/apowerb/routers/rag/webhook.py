"""POST /rag/webhook — th2llm webhook receiver with HMAC-SHA256 verification."""

import hashlib
import hmac
import json
from logging import getLogger

from fastapi import APIRouter, HTTPException, Request

from apowerb.configs.settings import get_settings
from apowerb.core.knowledge_map import update_status

from .schemas import WebhookPayload

logger = getLogger(__name__)

router = APIRouter()


def _verify_webhook_signature(body: bytes, signature: str, secret: str) -> bool:
    """Verify HMAC-SHA256 signature of the webhook payload.

    Args:
        body: Raw request body bytes.
        signature: The signature sent by th2llm in the X-Webhook-Signature header.
        secret: The shared secret for HMAC computation.

    Returns:
        True if the signature is valid, False otherwise.
    """
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/rag/webhook", tags=["rag"])
async def rag_webhook(request: Request):
    """Receive indexation status updates from th2llm via webhook.

    This endpoint is NOT behind auth middleware — it uses HMAC-SHA256 signature
    verification instead. th2llm signs the payload with the shared secret and
    sends the signature in the ``X-Webhook-Signature`` header.
    """
    from apowerb.routers import rag as _pkg

    settings = get_settings()

    # 1. Read raw body for HMAC verification
    raw_body = await request.body()

    # 2. Verify HMAC-SHA256 signature
    signature = request.headers.get("X-Webhook-Signature", "")
    if not signature:
        logger.warning("[WEBHOOK] Missing X-Webhook-Signature header")
        raise HTTPException(status_code=401, detail="Missing webhook signature")

    if not _verify_webhook_signature(raw_body, signature, settings.rag_webhook_secret):
        logger.warning("[WEBHOOK] Invalid webhook signature")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # 3. Parse payload
    try:
        payload_data = json.loads(raw_body)
        payload = WebhookPayload(**payload_data)
    except Exception as exc:
        logger.error("[WEBHOOK] Failed to parse webhook payload: %s", exc)
        raise HTTPException(status_code=400, detail=f"Invalid payload: {exc}")

    logger.info(
        "[WEBHOOK] Received event=%s knowledge_id=%s status=%s",
        payload.event,
        payload.knowledge_id,
        payload.status,
    )

    # 4. Retrieve scope (agent_id, session_id) for this knowledge_id
    scope = await _pkg.rag_manager.get_scope(payload.knowledge_id)
    if scope is None:
        logger.warning(
            "[WEBHOOK] Unknown knowledge_id=%s — no registered scope",
            payload.knowledge_id,
        )
        # Accept the webhook anyway (idempotent) but cannot route it
        return {"status": "received", "routed": False}

    agent_id, session_id = scope

    # 5. Update the knowledge map on disk
    new_status = payload.status
    if new_status in ("complete", "completed", "COMPLETED"):
        new_status = "complete"
    elif new_status in ("error", "failed", "FAILED"):
        new_status = "error"

    update_status(agent_id, payload.knowledge_id, new_status, session_id=session_id)
    logger.info(
        "[WEBHOOK] Updated knowledge_map: kid=%s -> %s (agent=%s, session=%s)",
        payload.knowledge_id,
        new_status,
        agent_id,
        session_id,
    )

    # 6. Notify SSE subscribers
    event_data = {
        "event": payload.event,
        "knowledge_id": payload.knowledge_id,
        "status": new_status,
        "timestamp": payload.timestamp,
        "processing": payload.processing,
    }
    notified = await _pkg.rag_manager.notify(payload.knowledge_id, event_data)

    # 7. Cleanup if terminal state
    if new_status in ("complete", "error"):
        await _pkg.rag_manager.cleanup_knowledge(payload.knowledge_id)

    return {"status": "received", "routed": True, "subscribers_notified": notified}
