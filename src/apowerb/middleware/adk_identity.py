"""The user_id an ADK-native route acts on must be the token's user.

``ADKAuthMiddleware`` checked that a valid access token was present, not WHO
the request acted for. ADK takes the user from the request itself -- the
``/apps/{app}/users/{user_id}/...`` path, or ``user_id`` / ``userId`` in the
body of ``/run`` and ``/run_sse`` -- so any authenticated user could read
another user's sessions, run an agent in their name and, since memory across
conversations (apowerb/roadmap#82), search their memories. The ``/api/adk``
wrapper already enforced the match (``enforce_user_id_match``); these are the
routes it does not cover.

``/run_live`` is a WebSocket: ``BaseHTTPMiddleware`` never sees it, so it had
no authentication at all. ``ADKLiveAuthMiddleware`` closes the handshake
unless the token is a valid access token for the requested ``user_id``.

Every internal caller already sends a token whose ``sub`` is the ``user_id``
it acts for (the /api/adk wrapper, webhooks, scheduled runs): enforcing the
match changes nothing for them.
"""

from __future__ import annotations

import json
import re
from logging import getLogger
from typing import Optional

import jwt
from jwt import InvalidTokenError
from starlette.requests import Request
from starlette.websockets import WebSocket

from apowerb.core.invocation_context import set_current_invoker
from apowerb.helpers.security import get_algorithm, get_secret_key

logger = getLogger(__name__)

# ADK's own path shape; the segment is matched on the DECODED path, the one
# FastAPI routes on, so "alice%40ex.com" is compared as "alice@ex.com".
_APPS_USER_PATH = re.compile(r"^/apps/[^/]+/users/([^/]+)(?:/|$)")
_RUN_BODY_ROUTES = frozenset({"/run", "/run_sse"})
# RunAgentRequest accepts the snake_case name and its camelCase alias.
_BODY_USER_FIELDS = ("user_id", "userId")
LIVE_ROUTE = "/run_live"
POLICY_VIOLATION = 1008


def access_token_subject(token: str) -> Optional[str]:
    """The ``sub`` of a valid access token, or ``None``.

    Same rules as ``ADKAuthMiddleware``: signature, ``type == "access"``, a
    ``sub`` claim. A missing ENCRYPT_KEY raises -- the server is broken, the
    caller is not at fault.
    """
    secret = get_secret_key()
    try:
        payload = jwt.decode(token, secret, algorithms=[get_algorithm()])
    except InvalidTokenError:
        return None
    if payload.get("type") != "access" or not payload.get("sub"):
        return None
    return str(payload["sub"])


async def requested_user_ids(request: Request) -> list[str]:
    """Every user id this request asks ADK to act for."""
    ids: list[str] = []
    path = request.scope.get("path", "")
    match = _APPS_USER_PATH.match(path)
    if match:
        ids.append(match.group(1))
    if request.method == "POST" and path in _RUN_BODY_ROUTES:
        # Starlette caches the body: ADK still receives it whole.
        try:
            body = json.loads(await request.body() or b"{}")
        except ValueError:
            body = None  # not JSON: ADK answers 422 itself
        if isinstance(body, dict):
            ids.extend(str(body[f]) for f in _BODY_USER_FIELDS if f in body)
    return ids


async def foreign_user_id(request: Request, subject: str) -> Optional[str]:
    """The first requested user id that is not ``subject``, or ``None``."""
    for user_id in await requested_user_ids(request):
        if user_id != subject:
            logger.warning(
                "[ADK AUTH] %s %s denied: token user %s asked to act for %s",
                request.method, request.scope.get("path"), subject, user_id,
            )
            return user_id
    return None


class ADKLiveAuthMiddleware:
    """Authenticate the ``/run_live`` WebSocket, which HTTP middleware never sees.

    The token comes from the ``Authorization`` header or a ``token`` query
    parameter (browsers cannot set headers on a WebSocket). Nothing in apowerb
    uses this route -- voice goes through ``/api/audio/ws`` -- so a strict
    refusal breaks no caller.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "websocket" or scope.get("path") != LIVE_ROUTE:
            await self.app(scope, receive, send)
            return
        websocket = WebSocket(scope, receive, send)
        auth = websocket.headers.get("authorization", "")
        token = auth.split(" ", 1)[1].strip() if auth.startswith("Bearer ") else websocket.query_params.get("token")
        subject = access_token_subject(token) if token else None
        requested = websocket.query_params.get("user_id")
        if subject is None or requested != subject:
            logger.warning("[ADK AUTH] %s refused (token user=%s, user_id=%s)", LIVE_ROUTE, subject, requested)
            await websocket.close(code=POLICY_VIOLATION)
            return
        set_current_invoker(subject)
        await self.app(scope, receive, send)
