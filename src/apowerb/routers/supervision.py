"""Supervision: what the agents actually did, session by session.

Lived in the evaluations router until evaluation became a commercial
brick. It never belonged there — it watches agent runs, which is AgentOps,
while that file scores them — and it stays in the core, so it moves out
rather than leaving with the brick.

Reads `sessions`/`events` directly instead of fanning out one ADK HTTP
call per agent: two queries whatever the number of agents, the N+1 shape
this codebase has paid for more than once.
"""

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.auth.dependencies import get_current_user
from apowerb.configs.settings import get_settings
from apowerb.helpers.database import get_db
from apowerb.helpers.ownership import list_owned_agents, may_supervise_across_accounts
from apowerb.users import schemas as user_schemas

router = APIRouter(tags=["supervision"])


def _schema() -> str:
    return get_settings().db_schema or "public"


class SupervisionSessionOut(BaseModel):
    """One row of the supervision list.

    `preview` is the first thing the user asked — without it every row reads
    the same and the list cannot be scanned, which is what it looked like
    with 422 copies of one test harness in it.
    """

    session_id: str
    app_name: str
    agent_name: str | None = None
    user_id: str | None = None
    create_time: datetime | None = None
    update_time: datetime | None = None
    preview: str | None = None
    steps: int = 0
    tool_calls: int = 0
    has_error: bool = False


class SupervisionSessionsResponse(BaseModel):
    items: list[SupervisionSessionOut]
    total: int


@router.get("/supervision/sessions", tags=["adk"])
async def list_supervision_sessions(
    limit: int = 200,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(get_current_user),
) -> SupervisionSessionsResponse:
    """Sessions to supervise: every agent's for an admin, one's own otherwise.

    Reads `sessions`/`events` directly instead of fanning out one ADK HTTP
    call per agent. Two queries total, whatever the number of agents -- the
    N+1 shape this codebase has paid for more than once.
    """
    # Crossing accounts is a superadmin's business, not every admin's: an
    # administrator with the role for operational reasons was reading his
    # colleagues' sessions. The core asks the brick that owns that notion.
    agents = await list_owned_agents(
        current_user,
        admin_sees_all=await may_supervise_across_accounts(db, current_user),
    )
    if not agents:
        return SupervisionSessionsResponse(items=[], total=0)

    app_names = [f"agent{agent_id}" for agent_id, _, _ in agents]
    name_by_app = {f"agent{agent_id}": name for agent_id, name, _ in agents}
    schema = _schema()

    total = (
        await db.execute(
            text(
                f"SELECT count(*) FROM {schema}.sessions WHERE app_name = ANY(:apps)"
            ),
            {"apps": app_names},
        )
    ).scalar() or 0

    rows = (
        await db.execute(
            text(
                f"SELECT id, app_name, user_id, create_time, update_time "
                f"FROM {schema}.sessions WHERE app_name = ANY(:apps) "
                "ORDER BY update_time DESC NULLS LAST LIMIT :limit OFFSET :offset"
            ),
            {"apps": app_names, "limit": limit, "offset": offset},
        )
    ).all()
    if not rows:
        return SupervisionSessionsResponse(items=[], total=total)

    session_ids = [r[0] for r in rows]

    # One pass over this page's events: the opening question, the counts, and
    # whether anything errored. Grouped in SQL rather than per session.
    event_rows = (
        await db.execute(
            text(
                f"SELECT session_id, event_data FROM {schema}.events "
                "WHERE session_id = ANY(:ids) ORDER BY session_id, timestamp ASC"
            ),
            {"ids": session_ids},
        )
    ).all()

    summary: dict[str, dict] = {}
    for session_id, event_data in event_rows:
        entry = summary.setdefault(
            session_id, {"preview": None, "steps": 0, "tool_calls": 0, "has_error": False}
        )
        entry["steps"] += 1
        data = event_data or {}
        content = data.get("content") or {}
        role = content.get("role") or data.get("author")
        for part in content.get("parts") or []:
            if part.get("function_call"):
                entry["tool_calls"] += 1
            if part.get("function_response"):
                response = part["function_response"].get("response")
                # A tool that failed says so in its own payload; ADK leaves
                # the span status UNSET, so this is the only honest signal.
                if isinstance(response, dict) and (
                    response.get("error") or response.get("status") == "error"
                ):
                    entry["has_error"] = True
            if entry["preview"] is None and part.get("text") and role == "user":
                entry["preview"] = part["text"].strip()[:160]

    items = [
        SupervisionSessionOut(
            session_id=r[0],
            app_name=r[1],
            agent_name=name_by_app.get(r[1]),
            user_id=r[2],
            create_time=r[3],
            update_time=r[4],
            **summary.get(
                r[0], {"preview": None, "steps": 0, "tool_calls": 0, "has_error": False}
            ),
        )
        for r in rows
    ]
    return SupervisionSessionsResponse(items=items, total=total)
