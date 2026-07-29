"""RAG Indexation Manager — in-memory event bus for webhook-to-SSE streaming.

Manages subscriptions between webhook events (from th2llm) and SSE streams
(to frontend clients). Each knowledge_id being indexed can have multiple
SSE subscribers waiting for status updates.

Singleton instance: ``rag_manager`` (module-level).
"""

import asyncio
from logging import getLogger
from typing import Any

logger = getLogger(__name__)


class RagIndexationManager:
    """In-memory event bus bridging RAG webhook notifications to SSE subscribers.

    Thread-safe via ``asyncio.Lock`` for all mutable state access.

    Attributes:
        _subscribers: Mapping of knowledge_id to list of asyncio.Queue for SSE clients.
        _kid_to_scope: Mapping of knowledge_id to (agent_id, session_id) tuple.
        _lock: Async lock protecting all mutable state.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        self._kid_to_scope: dict[str, tuple[str, str | None]] = {}
        self._lock = asyncio.Lock()

    async def register_knowledge(
        self,
        knowledge_id: str,
        agent_id: str,
        session_id: str | None = None,
    ) -> None:
        """Register a knowledge_id with its owning agent/session scope.

        Called right after ``append_source()`` in indexation endpoints so that
        incoming webhooks can be routed to the correct scope.

        Args:
            knowledge_id: The RAG knowledge base ID being indexed.
            agent_id: The agent that owns this knowledge.
            session_id: Optional session scope (None for agent-level knowledge).
        """
        async with self._lock:
            self._kid_to_scope[knowledge_id] = (agent_id, session_id)
        logger.info(
            "[RAG_STREAM] Registered knowledge_id=%s -> agent=%s session=%s",
            knowledge_id,
            agent_id,
            session_id,
        )

    async def get_scope(self, knowledge_id: str) -> tuple[str, str | None] | None:
        """Return the (agent_id, session_id) scope for a knowledge_id.

        Returns:
            Tuple of (agent_id, session_id) or None if the knowledge_id is unknown.
        """
        async with self._lock:
            return self._kid_to_scope.get(knowledge_id)

    async def subscribe(self, knowledge_id: str) -> asyncio.Queue:
        """Create and return a new subscription queue for a knowledge_id.

        The returned queue will receive events (dicts) whenever ``notify()``
        is called for this knowledge_id.

        Args:
            knowledge_id: The RAG knowledge base ID to subscribe to.

        Returns:
            An asyncio.Queue that will receive event dicts.
        """
        queue: asyncio.Queue[Any] = asyncio.Queue()
        async with self._lock:
            if knowledge_id not in self._subscribers:
                self._subscribers[knowledge_id] = []
            self._subscribers[knowledge_id].append(queue)
        logger.debug("[RAG_STREAM] New subscriber for knowledge_id=%s", knowledge_id)
        return queue

    async def unsubscribe(self, knowledge_id: str, queue: asyncio.Queue) -> None:
        """Remove a subscription queue for a knowledge_id.

        Safe to call even if the queue was already removed or the knowledge_id
        has no subscribers.

        Args:
            knowledge_id: The RAG knowledge base ID to unsubscribe from.
            queue: The queue to remove from the subscribers list.
        """
        async with self._lock:
            if knowledge_id in self._subscribers:
                try:
                    self._subscribers[knowledge_id].remove(queue)
                except ValueError:
                    pass  # Already removed
                # Clean up empty lists
                if not self._subscribers[knowledge_id]:
                    del self._subscribers[knowledge_id]
        logger.debug("[RAG_STREAM] Unsubscribed from knowledge_id=%s", knowledge_id)

    async def notify(self, knowledge_id: str, event: dict) -> int:
        """Push an event to all subscribers of a given knowledge_id.

        Non-blocking: uses ``put_nowait`` so the event loop is never blocked
        even if a subscriber queue is full (unlikely with unbounded queues).

        Args:
            knowledge_id: The RAG knowledge base ID that received an update.
            event: The event dict to broadcast (typically the webhook payload).

        Returns:
            Number of subscribers notified.
        """
        notified = 0
        async with self._lock:
            queues = self._subscribers.get(knowledge_id, [])
            for q in queues:
                try:
                    q.put_nowait(event)
                    notified += 1
                except asyncio.QueueFull:
                    logger.warning(
                        "[RAG_STREAM] Queue full for knowledge_id=%s, dropping event",
                        knowledge_id,
                    )
        if notified > 0:
            logger.info(
                "[RAG_STREAM] Notified %d subscriber(s) for knowledge_id=%s event=%s",
                notified,
                knowledge_id,
                event.get("event", "unknown"),
            )
        return notified

    async def cleanup_knowledge(self, knowledge_id: str) -> None:
        """Remove all state for a knowledge_id (scope mapping + subscriber queues).

        Should be called when indexing is definitively complete or failed and
        no further updates are expected.

        Args:
            knowledge_id: The RAG knowledge base ID to clean up.
        """
        async with self._lock:
            self._kid_to_scope.pop(knowledge_id, None)
            queues = self._subscribers.pop(knowledge_id, [])
        if queues:
            logger.info(
                "[RAG_STREAM] Cleaned up knowledge_id=%s (%d remaining subscribers removed)",
                knowledge_id,
                len(queues),
            )
        else:
            logger.debug("[RAG_STREAM] Cleaned up knowledge_id=%s (no subscribers)", knowledge_id)


# ---------------------------------------------------------------------------
# Singleton instance — import this from other modules
# ---------------------------------------------------------------------------
rag_manager = RagIndexationManager()
