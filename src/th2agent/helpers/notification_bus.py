"""In-process notification bus for real-time SSE push.

Each SSE connection registers an ``asyncio.Queue`` for its user.
When a notification is created anywhere in the backend, call
``notify(user_id, payload)`` to push it to all active connections
for that user.
"""

import asyncio
from collections import defaultdict
from logging import getLogger

logger = getLogger(__name__)

# user_id -> list of asyncio.Queue (one per active SSE connection)
_subscribers: dict[int, list[asyncio.Queue]] = defaultdict(list)


def subscribe(user_id: int) -> asyncio.Queue:
    """Register a new SSE listener for the given user. Returns a Queue."""
    q: asyncio.Queue = asyncio.Queue()
    _subscribers[user_id].append(q)
    logger.debug("[NOTIF BUS] User %s subscribed (%d connections)", user_id, len(_subscribers[user_id]))
    return q


def unsubscribe(user_id: int, q: asyncio.Queue) -> None:
    """Remove an SSE listener."""
    try:
        _subscribers[user_id].remove(q)
    except ValueError:
        pass
    if not _subscribers[user_id]:
        del _subscribers[user_id]
    logger.debug("[NOTIF BUS] User %s unsubscribed", user_id)


async def notify(user_id: int, payload: dict) -> None:
    """Push a notification payload to all active SSE connections for user_id."""
    queues = _subscribers.get(user_id, [])
    for q in queues:
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            logger.warning("[NOTIF BUS] Queue full for user %s, dropping event", user_id)
