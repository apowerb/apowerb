"""Agent runtime entry points invoked from Mage triggers.

Contains the two main coroutines used by the scheduler pipeline:
- ``run_agent_from_jwt``: validates a short-lived access token and runs the
  agent (legacy path, still used by ad-hoc runs).
- ``run_agent_from_refresh_token``: validates a long-lived agent refresh
  token, runs the agent, and rotates the token in the Mage trigger.

IMPORTANT (test contract):
Tests patch symbols via ``patch.object(rab, "run_adk_agent")`` (etc.), where
``rab`` is the ``th2agent.scheduler.run_agent_background`` package. For those
patches to actually affect the code path, we must dereference these symbols
through the package at call-time rather than pin them to a local reference.
"""

import time as _time
from typing import Any, Dict

from th2agent.configs.th2logger import setup_logging
from th2agent.core.invocation_context import set_current_invoker
from th2agent.helpers.security import create_agent_refresh_token
from th2agent.helpers import notify_etl

# Keep logger name stable with the legacy module (used by caplog in tests).
logger = setup_logging("th2agent.scheduler.run_agent_background")


def convert_to_adk_message_format(simple_message: Dict[str, Any]) -> Dict[str, Any]:
    role = simple_message.get("role", "user")
    content = simple_message.get("content", "")

    # If already in ADK format (has "parts" at top level), return as-is
    if "parts" in simple_message and isinstance(simple_message.get("parts"), list):
        return simple_message

    # Convert to ADK format
    return {"role": role, "parts": [{"text": str(content)}]}


async def run_agent_from_jwt(jwt_token: str) -> Dict[str, Any]:
    """
    Run an ADK agent using a JWT token for authentication and parameters.

    This is called by Mage AI when a scheduled trigger fires.
    The JWT token contains all execution parameters.

    UPDATED: Now uses agent refresh tokens instead of short-lived access tokens.
    Each scheduled run gets a fresh session_id so the agent always starts with
    a clean conversation context (prevents "already done" skipping on run 2+).
    """
    # Late-bind attributes through the package so test-time monkeypatches on
    # ``run_agent_background.<symbol>`` are honored.
    from th2agent.scheduler import run_agent_background as _pkg

    try:
        # Decode and validate agent refresh token
        logger.info("Decoding agent refresh token for agent execution")
        token_data = _pkg.decode_agent_refresh_token(jwt_token)

        if not token_data:
            raise ValueError("Invalid or expired agent refresh token")

        # Extract required parameters from token
        agent_name = token_data.get("agent_name")
        user_id = token_data.get("user_id")
        base_session_id = token_data.get("session_id")
        new_message_raw = token_data.get("new_message")
        # run_mode = token_data.get("run_mode", "single")
        streaming = token_data.get("streaming", False)

        # Validate required fields
        if not all([agent_name, user_id, base_session_id, new_message_raw]):
            raise ValueError(
                "JWT token missing required fields: agent_name, user_id, session_id, or new_message"
            )

        # Bind the invoker for THIS task so user-personal integrations
        # (Outlook, Gmail, ...) resolve against the user who scheduled the
        # run — never the racy process-global AGENT_OWNER env var. Two
        # scheduled runs firing at the same minute would otherwise cross
        # each other's mailbox (incident 2026-07-03). ContextVar is
        # task-local, so this cannot leak between concurrent runs.
        set_current_invoker(user_id)

        # Generate a unique session_id for each scheduled run so the agent
        # always starts with a fresh conversation context. Without this, every
        # run reuses the same session (with its full history) and the agent
        # may decide it already completed the task and skip it.
        session_id = f"{base_session_id}_{int(_time.time())}"
        logger.info(
            f"[JWT RUN] Executing agent: agent={agent_name}, user={user_id}, "
            f"session={session_id} (base={base_session_id})"
        )

        # Convert message to ADK format
        new_message = convert_to_adk_message_format(new_message_raw)
        logger.info(f"[JWT RUN] Converted message format: {new_message}")

        # Resolve agent name to folder name (same as /run endpoint)
        folder_name = _pkg.get_agent_folder_name(agent_name)
        logger.info(f"[JWT RUN] Resolved agent name {agent_name} to {folder_name}")

        # ADKAuthMiddleware (main.py) only accepts tokens with type=="access".
        # Mint a short-lived access token from the agent refresh token before
        # calling any ADK-protected endpoint, otherwise the middleware rejects
        # us with 401.
        from th2agent.helpers.security import refresh_access_token_from_agent_refresh
        adk_access_token = refresh_access_token_from_agent_refresh(jwt_token)

        # Import session functions
        from th2agent.core.adk_runner import (
            get_adk_session,
            create_adk_agent_session,
        )

        # Check if session exists, create if not
        session_was_created = False
        try:
            logger.info(f"[JWT RUN] Checking if session exists: {session_id}")
            try:
                await get_adk_session(
                    agent_name=folder_name,
                    user_id=user_id,
                    session_id=session_id,
                    token=adk_access_token,
                )
                logger.info(f"[JWT RUN] Session {session_id} exists")
                session_was_created = False
            except Exception:
                # Session doesn't exist, create it
                logger.info(f"[JWT RUN] Session {session_id} not found, creating it")
                await create_adk_agent_session(
                    agent_name=folder_name,
                    user_id=user_id,
                    session_id=session_id,
                    data={},
                    token=adk_access_token,
                )
                logger.info(f"[JWT RUN] Successfully created session {session_id}")
                session_was_created = True
        except Exception as e:
            logger.error(f"[JWT RUN] Error handling session: {str(e)}", exc_info=True)
            raise ValueError(f"Failed to create/check session: {str(e)}")

        # FIX: Map run_mode to correct ADK endpoint
        # "single" -> "run" (non-streaming)
        # "streaming" -> "run_sse" (streaming)
        adk_run_mode = "run_sse" if streaming else "run"
        logger.info(f"[JWT RUN] Using ADK run_mode: {adk_run_mode}")

        # Execute the agent with folder_name instead of agent_name
        # Ce chemin planifie atteint le serveur ADK directement, sans
        # passer par le routeur /api/adk/run : sans cet appel, un run
        # programme ignorerait le plafond que le chat respecte.
        from th2agent.core.run_gate import apply_run_guards, resolve_owner_plan

        await apply_run_guards(
            agent_name=folder_name,
            owner_id=user_id,
            plan=await resolve_owner_plan(user_id),
        )

        result = await _pkg.run_adk_agent(
            agent_name=folder_name,
            user_id=user_id,
            session_id=session_id,
            run_mode=adk_run_mode,  # FIX: Use mapped run_mode
            new_message=new_message,
            streaming=streaming,
            token=adk_access_token,  # Fresh access token (ADKAuthMiddleware requires type=="access")
        )

        logger.info(f"[JWT RUN] Agent execution completed successfully: {agent_name}")
        return {
            "success": True,
            "result": result,
            "agent_name": agent_name,
            "session_created": session_was_created,
        }

    except ValueError as e:
        logger.error(f"[JWT RUN] Validation error: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"[JWT RUN] Agent execution failed: {str(e)}", exc_info=True)
        await notify_etl.notify_job_failure(
            "scheduled-agent-run", f"{type(e).__name__}: {e}",
            context=locals().get("agent_name"),
        )
        raise


async def run_agent_from_refresh_token(
    agent_refresh_token: str,
    agent_id: str | None = None,
    agent_meta: dict | None = None,
) -> Dict[str, Any]:
    """
    Execute an agent using an agent refresh token with automatic token rotation.

    This function:
    1. Validates the agent refresh token
    2. Executes the agent using the token data
    3. Optionally rotates the token in Mage if agent_id is provided

    Each run uses a fresh session_id derived from the token's base session_id
    so the agent always starts with a clean conversation context.

    Args:
        agent_refresh_token: Long-lived refresh token (90 days)
        agent_id: Optional agent ID for token rotation in Mage
        agent_meta: Optional agent metadata — preserved alongside jwt_token
                    on rotation so agent_id/agent_meta are never lost from
                    the trigger variables after the first rotation.

    Returns:
        Execution result with token_rotated flag
    """
    from th2agent.scheduler import run_agent_background as _pkg
    from th2agent.scheduler.mage import get_orchestrator

    try:
        # Decode and validate agent refresh token
        logger.info("[REFRESH] Decoding agent refresh token")
        token_data = _pkg.decode_agent_refresh_token(agent_refresh_token)

        if not token_data:
            raise ValueError("Invalid or expired agent refresh token")

        # Extract required parameters from token
        agent_name = token_data.get("agent_name")
        user_id = token_data.get("user_id")
        base_session_id = token_data.get("session_id")
        new_message_raw = token_data.get("new_message")
        run_mode = token_data.get("run_mode", "single")
        streaming = token_data.get("streaming", False)
        agent_metadata = token_data.get("agent_metadata")

        # Validate required fields
        if not all([agent_name, user_id, base_session_id, new_message_raw]):
            raise ValueError(
                "Token missing required fields: agent_name, user_id, session_id, or new_message"
            )

        # Bind the invoker for THIS task so user-personal integrations
        # (Outlook, Gmail, ...) resolve against the user who scheduled the
        # run — never the racy process-global AGENT_OWNER env var. Two
        # scheduled runs firing at the same minute would otherwise cross
        # each other's mailbox (incident 2026-07-03). ContextVar is
        # task-local, so this cannot leak between concurrent runs.
        set_current_invoker(user_id)

        # Generate a unique session_id for each scheduled run so the agent
        # always starts with a fresh conversation context. Without this, every
        # run reuses the same session (with its full history) and the agent
        # may decide it already completed the task and skip it.
        session_id = f"{base_session_id}_{int(_time.time())}"
        logger.info(
            f"[REFRESH] Executing agent: agent={agent_name}, user={user_id}, "
            f"session={session_id} (base={base_session_id})"
        )

        # Convert message to ADK format
        new_message = convert_to_adk_message_format(new_message_raw)

        # Resolve agent name to folder name
        folder_name = _pkg.get_agent_folder_name(agent_name)
        logger.info(f"[REFRESH] Resolved agent name {agent_name} to {folder_name}")

        # ADKAuthMiddleware (main.py) only accepts tokens with type=="access".
        # Mint a short-lived access token from the agent refresh token before
        # calling any ADK-protected endpoint, else the middleware returns 401.
        from th2agent.helpers.security import refresh_access_token_from_agent_refresh
        adk_access_token = refresh_access_token_from_agent_refresh(agent_refresh_token)

        # Import session functions
        from th2agent.core.adk_runner import (
            get_adk_session,
            create_adk_agent_session,
        )

        # Check if session exists, create if not
        session_was_created = False
        try:
            logger.info(f"[REFRESH] Checking if session exists: {session_id}")
            try:
                await get_adk_session(
                    agent_name=folder_name,
                    user_id=user_id,
                    session_id=session_id,
                    token=adk_access_token,
                )
                logger.info(f"[REFRESH] Session {session_id} exists")
            except Exception:
                # Session doesn't exist, create it
                logger.info(f"[REFRESH] Session {session_id} not found, creating it")
                await create_adk_agent_session(
                    agent_name=folder_name,
                    user_id=user_id,
                    session_id=session_id,
                    data={},
                    token=adk_access_token,
                )
                logger.info(f"[REFRESH] Successfully created session {session_id}")
                session_was_created = True
        except Exception as e:
            logger.error(f"[REFRESH] Error handling session: {str(e)}", exc_info=True)
            raise ValueError(f"Failed to create/check session: {str(e)}")

        # Map run_mode to correct ADK endpoint
        adk_run_mode = "run_sse" if streaming else "run"
        logger.info(f"[REFRESH] Using ADK run_mode: {adk_run_mode}")

        # Execute the agent
        # Ce chemin planifie atteint le serveur ADK directement, sans
        # passer par le routeur /api/adk/run : sans cet appel, un run
        # programme ignorerait le plafond que le chat respecte.
        from th2agent.core.run_gate import apply_run_guards, resolve_owner_plan

        await apply_run_guards(
            agent_name=folder_name,
            owner_id=user_id,
            plan=await resolve_owner_plan(user_id),
        )

        result = await _pkg.run_adk_agent(
            agent_name=folder_name,
            user_id=user_id,
            session_id=session_id,
            run_mode=adk_run_mode,
            new_message=new_message,
            streaming=streaming,
            token=adk_access_token,  # access token (ADKAuthMiddleware requires type=="access")
        )

        # Token rotation: Create new token and update Mage trigger
        token_rotated = False
        if agent_id:
            try:
                logger.info(f"[REFRESH] Rotating token for agent_id: {agent_id}")

                # Create new refresh token with same data (keeps base_session_id
                # so future runs continue generating fresh timestamped sessions)
                new_refresh_token = create_agent_refresh_token(
                    data={
                        "agent_name": agent_name,
                        "user_id": user_id,
                        "session_id": base_session_id,  # Store base, not timestamped
                        "new_message": new_message_raw,
                        "run_mode": run_mode,
                        "streaming": streaming,
                        "agent_metadata": agent_metadata,
                    },
                    expires_days=90,  # 90 days for agent refresh tokens
                )

                # Update Mage trigger with new token
                orchestrator = get_orchestrator()
                schedules = orchestrator.client.get_pipeline_schedules(
                    orchestrator.PIPELINE_UUID
                )
                existing_trigger = next(
                    (s for s in schedules if s.get("name") == agent_id), None
                )

                if existing_trigger:
                    schedule_id = existing_trigger.get("id")
                    update_result = orchestrator.client.update_schedule_variables(
                        schedule_id=schedule_id,
                        variables={
                            "agent_id": agent_id,
                            "agent_meta": agent_meta or {},
                            "jwt_token": new_refresh_token,
                        },
                    )

                    if update_result:
                        token_rotated = True
                        logger.info(
                            f"[REFRESH] Token rotated successfully for agent_id: {agent_id}"
                        )
                    else:
                        logger.warning(
                            f"[REFRESH] Failed to rotate token for agent_id: {agent_id}"
                        )
                else:
                    logger.warning(
                        f"[REFRESH] No trigger found for agent_id: {agent_id}, skipping rotation"
                    )

            except Exception as e:
                logger.error(
                    f"[REFRESH] Token rotation failed: {str(e)}", exc_info=True
                )
                # Continue even if rotation fails - execution was successful

        logger.info(f"[REFRESH] Agent execution completed successfully: {agent_name}")
        return {
            "success": True,
            "result": result,
            "agent_name": agent_name,
            "session_created": session_was_created,
            "token_rotated": token_rotated,
        }

    except ValueError as e:
        logger.error(f"[REFRESH] Validation error: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"[REFRESH] Agent execution failed: {str(e)}", exc_info=True)
        raise
