"""Memory across conversations, in the application database.

Without a ``memory_service_uri``, ADK falls back to ``InMemoryMemoryService``:
RAM only, per worker, lost on every restart -- and nothing ever fed it, so the
agent's "Memory" switch gave ``load_memory`` an empty store (apowerb/roadmap#83).
ADK's persistent services (``rag://``, ``agentengine://``) require Google
Cloud; this one keeps the self-hosted promise.

Scope is the ADK one: ``(app_name, user_id)``, i.e. one root agent AND one
user. ADK resolves both from the current session when ``load_memory`` searches,
so an agent never reads another agent's memory, nor another user's.

Only the text people exchanged is kept: no thoughts, no tool calls or tool
payloads, no partial stream chunks. A session is re-added after every turn;
events are keyed by id, so re-adding never duplicates.

Entries expire after ``MEMORY_RETENTION_DAYS`` (default 90, like ADK events):
search ignores them at once, ``purge_expired`` deletes them.

Search is keyword-based, in portable SQL, with no embedding model -- hence no
cost per remembered turn. It returns the most relevant recent entries.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta, timezone
from logging import getLogger
from typing import Any, Optional

from google.adk.memory.base_memory_service import BaseMemoryService, SearchMemoryResponse
from google.adk.memory.memory_entry import MemoryEntry
from google.genai import types
from sqlalchemy import (
    Column,
    DateTime,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    delete,
    func,
    or_,
    select,
)

logger = getLogger(__name__)

TABLE_NAME = "agent_memory"
_DEFAULT_RETENTION_DAYS = 90
_MAX_CONTENT_CHARS = 8000
_MAX_QUERY_WORDS = 12
_CANDIDATES = 200
_MAX_RESULTS = 10
_WORD = re.compile(r"\w+", re.UNICODE)


def retention_days() -> int:
    """``MEMORY_RETENTION_DAYS``, default 90; an invalid value falls back to it."""
    raw = os.environ.get("MEMORY_RETENTION_DAYS")
    try:
        days = int(raw) if raw is not None else _DEFAULT_RETENTION_DAYS
    except ValueError:
        logger.warning("[MEMORY] invalid MEMORY_RETENTION_DAYS=%r, using %d", raw, _DEFAULT_RETENTION_DAYS)
        return _DEFAULT_RETENTION_DAYS
    return days if days >= 1 else _DEFAULT_RETENTION_DAYS


def _now() -> datetime:
    # Naive UTC, like ADK's own ``events.timestamp``: same comparison on every backend.
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _event_text(event: Any) -> str:
    """The visible text of an event, or "" when there is nothing to remember."""
    if getattr(event, "partial", False) or event.content is None:
        return ""
    texts = [
        p.text.strip()
        for p in (event.content.parts or [])
        if getattr(p, "text", None) and not getattr(p, "thought", False)
    ]
    return "\n".join(t for t in texts if t)[:_MAX_CONTENT_CHARS]


def _default_session_factory():
    from apowerb.helpers.database import sessionmanager

    return sessionmanager.session()


class PersistentMemoryService(BaseMemoryService):
    def __init__(
        self,
        *,
        session_factory: Optional[Callable[[], Any]] = None,
        schema: Optional[str] = None,
        retention_days: Optional[int] = None,
    ) -> None:
        self._session_factory = session_factory or _default_session_factory
        self._retention_days = retention_days
        self._table_ready = False
        metadata = MetaData(schema=schema)
        self._table = Table(
            TABLE_NAME,
            metadata,
            Column("id", Integer, primary_key=True, autoincrement=True),
            Column("app_name", String(255), nullable=False),
            Column("user_id", String(255), nullable=False),
            Column("session_id", String(255), nullable=False),
            Column("event_id", String(255), nullable=False),
            Column("author", String(255)),
            Column("content", Text, nullable=False),
            Column("created_at", DateTime, nullable=False),
            UniqueConstraint("app_name", "user_id", "session_id", "event_id", name="uq_agent_memory_event"),
            Index("ix_agent_memory_scope_created", "app_name", "user_id", "created_at"),
        )
        self._metadata = metadata

    def _cutoff(self) -> datetime:
        return _now() - timedelta(days=self._retention_days or retention_days())

    async def _ensure_table(self, db) -> None:
        if self._table_ready:
            return
        await db.run_sync(lambda s: self._metadata.create_all(s.connection(), checkfirst=True))
        self._table_ready = True

    async def add_session_to_memory(self, session) -> None:
        await self.add_events_to_memory(
            app_name=session.app_name,
            user_id=session.user_id,
            session_id=session.id,
            events=session.events,
        )

    async def add_events_to_memory(
        self,
        *,
        app_name: str,
        user_id: str,
        events: Sequence[Any],
        session_id: str | None = None,
        custom_metadata: Mapping[str, object] | None = None,
    ) -> None:
        rows = []
        for event in events:
            text = _event_text(event)
            if not text or not event.id:
                continue
            created = datetime.fromtimestamp(event.timestamp, tz=timezone.utc).replace(tzinfo=None)
            rows.append({
                "app_name": app_name,
                "user_id": user_id,
                "session_id": session_id or "",
                "event_id": event.id,
                "author": event.author,
                "content": text,
                "created_at": created,
            })
        if not rows:
            return
        t = self._table
        async with self._session_factory() as db:
            await self._ensure_table(db)
            known = set((await db.execute(
                select(t.c.event_id).where(
                    t.c.app_name == app_name,
                    t.c.user_id == user_id,
                    t.c.session_id == (session_id or ""),
                    t.c.event_id.in_([r["event_id"] for r in rows]),
                )
            )).scalars())
            fresh = [r for r in rows if r["event_id"] not in known]
            if fresh:
                await db.execute(t.insert(), fresh)
                await db.commit()

    async def search_memory(self, *, app_name: str, user_id: str, query: str) -> SearchMemoryResponse:
        words = list(dict.fromkeys(w.lower() for w in _WORD.findall(query or "") if len(w) >= 2))
        words = words[:_MAX_QUERY_WORDS]
        if not words:
            return SearchMemoryResponse()
        t = self._table
        content = func.lower(t.c.content)
        async with self._session_factory() as db:
            await self._ensure_table(db)
            rows = (await db.execute(
                select(t.c.event_id, t.c.author, t.c.content, t.c.created_at)
                .where(
                    t.c.app_name == app_name,
                    t.c.user_id == user_id,
                    t.c.created_at >= self._cutoff(),
                    or_(*[content.contains(w, autoescape=True) for w in words]),
                )
                .order_by(t.c.created_at.desc())
                .limit(_CANDIDATES)
            )).all()

        def score(row) -> int:
            text = row.content.lower()
            return sum(1 for w in words if w in text)

        ranked = sorted(rows, key=lambda r: (score(r), r.created_at), reverse=True)[:_MAX_RESULTS]
        return SearchMemoryResponse(memories=[
            MemoryEntry(
                id=r.event_id,
                author=r.author,
                timestamp=r.created_at.replace(tzinfo=timezone.utc).isoformat(),
                content=types.Content(
                    role="user" if r.author == "user" else "model",
                    parts=[types.Part(text=r.content)],
                ),
            )
            for r in ranked
        ])

    async def delete_user_memory(self, *, app_name: str, user_id: str) -> int:
        """Erase what this agent remembers about this user. Returns the row count."""
        t = self._table
        async with self._session_factory() as db:
            await self._ensure_table(db)
            result = await db.execute(delete(t).where(t.c.app_name == app_name, t.c.user_id == user_id))
            await db.commit()
        return result.rowcount or 0

    async def purge_expired(self) -> int:
        """Delete every entry past the retention window. Returns the row count."""
        t = self._table
        async with self._session_factory() as db:
            await self._ensure_table(db)
            result = await db.execute(delete(t).where(t.c.created_at < self._cutoff()))
            await db.commit()
        return result.rowcount or 0


async def memory_after_agent_callback(callback_context) -> None:
    """Remember the session after each agent turn. Never alters, never breaks the run."""
    try:
        await callback_context.add_session_to_memory()
    except Exception as exc:  # noqa: BLE001 -- memory must not cost the user their answer
        logger.warning("[MEMORY] could not remember the session: %r", exc)
    return None


def with_memory_callback(existing):
    """Put the memory callback FIRST, keeping any existing after_agent_callback.

    ADK stops at the first after-agent callback that returns content; ours
    always returns None, so running first never hides the others.
    """
    if existing is None:
        return memory_after_agent_callback
    rest = list(existing) if isinstance(existing, (list, tuple)) else [existing]
    return [memory_after_agent_callback, *rest]


MEMORY_SERVICE_URI = "apowerb-memory://"


def register_memory_service() -> None:
    """Register the ``apowerb-memory`` URI scheme with ADK's service registry.

    Same pattern as ``register_s3_artifact_service``: ADK's factory resolves
    ``memory_service_uri`` by scheme, and only knows ``memory``, ``rag`` and
    ``agentengine`` natively. Idempotent.
    """
    from google.adk.cli.service_registry import get_service_registry

    from apowerb.configs.settings import get_settings

    def _factory(uri: str, **_: Any) -> PersistentMemoryService:
        return PersistentMemoryService(schema=get_settings().db_schema or None)

    get_service_registry().register_memory_service("apowerb-memory", _factory)
