"""High-level scheduling API: schedule_agent_run + trigger_agent_run_now.

These two coroutines are the public entry points called by the HTTP routers.
They orchestrate agent validation, session bootstrap, Mage trigger
creation/update, and immediate invocation.
"""

from datetime import datetime, timezone
from typing import Any, Dict

from th2agent.configs.th2logger import setup_logging

from .schedule_helpers import (
    _schedule_activation_if_future,
    calculate_next_run_time,
    resolve_schedule_interval,
)
from .token_issuer import create_agent_run_token

logger = setup_logging("th2agent.scheduler.run_agent_background")


async def schedule_agent_run(
    agent_id: str,
    user_id: str,
    session_id: str,
    new_message: Dict[str, Any],
    run_mode: str = "single",
    streaming: bool = False,
    agent_metadata: Dict[str, Any] | None = None,
    schedule_interval: str = "@hourly",  # Schedule interval
    start_time: str | None = None,  # Optional start time
) -> Dict[str, Any]:
    """
    Schedule an agent to run via Mage AI.

    This function will:
    1. Validate agent exists and get agent_name
    2. Create session if it doesn't exist
    3. Check if schedule trigger exists for agent_id
    4. If not, create a schedule trigger with the specified interval
    5. If the trigger is new OR updated with a future start_time, spawn a
       background task to flip it to active at the right moment
    6. Create/update a long-lived agent refresh token (90 day expiration)
    7. Store the agent refresh token in the trigger for future scheduled runs
    8. Return success without triggering an immediate run

    Args:
        agent_id: Agent identifier (e.g., "agent95")
        user_id: User identifier
        session_id: Session identifier (base — each run appends a timestamp)
        new_message: Message to send to agent
        run_mode: Run mode (default: "single")
        streaming: Whether to stream responses
        agent_metadata: Optional agent metadata
        schedule_interval: Cron expression or preset (@hourly, @daily, @weekly, @monthly)
        start_time: Optional ISO 8601 datetime for first run (e.g., "2026-02-13T10:30:00")

    Returns:
        Schedule information including schedule_id, agent_id, and status
    """
    # Late-bind through the package so tests can patch these via
    # ``patch.object(run_agent_background, "get_agent_by_id")``.
    from th2agent.scheduler import run_agent_background as _pkg

    try:
        logger.info(
            f"[SCHEDULE] Starting schedule for agent_id: {agent_id}, interval: {schedule_interval}"
        )

        # 1. Validate agent exists and get agent_name
        agent_info = _pkg.get_agent_by_id(agent_id, user_id)
        if not agent_info:
            raise ValueError(f"Agent not found: {agent_id}")

        agent_name = agent_info.get("agent_name")
        logger.info(f"[SCHEDULE] Found agent: {agent_name} (ID: {agent_id})")

        # Get folder name for session creation
        folder_name = _pkg.get_agent_folder_name(agent_name)
        logger.info(f"[SCHEDULE] Resolved to folder: {folder_name}")

        # Resolve interval: convert presets to time-aware cron AND apply
        # minute offset so */N patterns fire at exactly start_time, not the
        # next grid slot (e.g. */5 + 9:38 -> 3/5 * * * * fires at :38, not :40)
        schedule_interval = resolve_schedule_interval(schedule_interval, start_time)

        # 2. Create session if it doesn't exist
        from th2agent.core.adk_runner import (
            get_adk_session,
            create_adk_agent_session,
        )

        # Create the agent refresh token (90-day) — stored in Mage trigger
        # variables, used by run_agent_from_jwt() at trigger fire time.
        schedule_token = create_agent_run_token(
            agent_name=agent_name,
            user_id=user_id,
            session_id=session_id,
            new_message=new_message,
            run_mode=run_mode,
            streaming=streaming,
            agent_metadata=agent_metadata,
            expires_days=90,
        )

        # ADKAuthMiddleware (main.py) only accepts tokens with type=="access".
        # Mint a short-lived access token from the refresh token for the
        # session-creation calls below — the long-lived refresh stays in
        # the Mage trigger.
        from th2agent.helpers.security import refresh_access_token_from_agent_refresh
        adk_access_token = refresh_access_token_from_agent_refresh(schedule_token)

        session_was_created = False
        try:
            logger.info(f"[SCHEDULE] Checking if session exists: {session_id}")
            try:
                await get_adk_session(
                    agent_name=folder_name,
                    user_id=user_id,
                    session_id=session_id,
                    token=adk_access_token,
                )
                logger.info(f"[SCHEDULE] Session {session_id} already exists")
                session_was_created = False
            except Exception:
                # Session doesn't exist, create it
                logger.info(f"[SCHEDULE] Creating new session: {session_id}")
                await create_adk_agent_session(
                    agent_name=folder_name,
                    user_id=user_id,
                    session_id=session_id,
                    data={},
                    token=adk_access_token,
                )
                logger.info(f"[SCHEDULE] Successfully created session {session_id}")
                session_was_created = True
        except Exception as e:
            logger.error(f"[SCHEDULE] Error handling session: {str(e)}", exc_info=True)
            raise ValueError(f"Failed to create/check session: {str(e)}")

        # 3. Check if schedule trigger exists
        from th2agent.scheduler.mage import get_orchestrator

        orchestrator = get_orchestrator()
        schedules = orchestrator.client.get_pipeline_schedules(
            orchestrator.PIPELINE_UUID
        )
        existing_trigger = next(
            (s for s in schedules if s.get("name") == agent_id), None
        )

        schedule_id = None
        trigger_created = False

        # Adjust start_time if it's in the past
        adjusted_start_time = start_time
        if start_time:
            calculated_start_time = calculate_next_run_time(
                start_time, schedule_interval
            )
            if calculated_start_time:
                adjusted_start_time = calculated_start_time
                logger.info(
                    f"[SCHEDULE] Adjusted start_time from {start_time} to {adjusted_start_time} "
                    f"to maintain schedule pattern"
                )

        # Prepare agent metadata — needed by both paths (new trigger AND
        # variable update for existing triggers).
        agent_meta = {
            "agent_name": agent_name,
            "agent_model": agent_info.get("agent_model"),
            "agent_description": agent_info.get("agent_description"),
            "owner_id": agent_info.get("owner_id"),
            "organization_id": agent_info.get("organization_id"),
        }

        if existing_trigger:
            schedule_id = existing_trigger.get("id")
            old_interval = existing_trigger.get("schedule_interval")
            logger.info(
                f"[SCHEDULE] Found existing trigger: schedule_id={schedule_id}, "
                f"interval={old_interval}"
            )

            # Update interval and/or start_time if changed
            need_update_interval = old_interval != schedule_interval
            need_update_start = adjusted_start_time is not None

            if need_update_interval or need_update_start:
                logger.info(
                    f"[SCHEDULE] Updating existing trigger: "
                    f"interval={old_interval}->{schedule_interval}, "
                    f"start_time={adjusted_start_time}"
                )

                # Determine whether the updated start_time is in the future.
                # If so, set the trigger inactive now and let the background
                # task flip it active at the right moment (same logic as new triggers).
                starts_in_future = False
                if adjusted_start_time:
                    try:
                        ts = adjusted_start_time.rstrip("Z")
                        dt = datetime.fromisoformat(ts)
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        starts_in_future = dt > datetime.now(timezone.utc)
                    except Exception:
                        starts_in_future = False

                updated = orchestrator.client.update_schedule(
                    schedule_id=schedule_id,
                    schedule_interval=schedule_interval if need_update_interval else None,
                    start_time=adjusted_start_time if need_update_start else None,
                    status="inactive" if starts_in_future else "active",
                )
                if updated:
                    logger.info("[SCHEDULE] Trigger updated successfully")
                    # Spawn background activation task if start_time is in the future
                    if starts_in_future:
                        _schedule_activation_if_future(schedule_id, adjusted_start_time)
                else:
                    logger.warning(
                        "[SCHEDULE] Failed to update trigger, continuing with existing"
                    )
        else:
            # Create new schedule trigger (ONLY if not found!)
            logger.info(
                f"[SCHEDULE] No trigger found. Creating schedule trigger with interval: {schedule_interval}"
            )

            # Create schedule trigger — jwt_token is baked in at creation so
            # the first scheduled run already has a valid token even if the
            # update_schedule_variables() call below were to fail.
            trigger_result = orchestrator.create_schedule_trigger_for_agent(
                agent_id=agent_id,
                agent_meta=agent_meta,
                schedule_interval=schedule_interval,
                start_time=adjusted_start_time,
                jwt_token=schedule_token,
            )

            if not trigger_result:
                raise Exception(
                    f"Failed to create schedule trigger for agent_id: {agent_id}"
                )

            schedule_id = trigger_result.get("schedule_id") or trigger_result.get("id")
            trigger_created = True

            logger.info(
                f"[SCHEDULE] ✅ Created new schedule trigger: "
                f"schedule_id={schedule_id}, interval={schedule_interval}"
            )

            # If the trigger was created as inactive (future start_time),
            # spawn a background task to activate it at the right moment.
            if trigger_result.get("starts_in_future") and adjusted_start_time:
                _schedule_activation_if_future(schedule_id, adjusted_start_time)

        # Re-use the token already created above — no need to mint a new one.
        # Update the trigger with all three variables together so that every
        # subsequent scheduled run gets a consistent kwargs dict:
        #   { agent_id, agent_meta, jwt_token }
        # NOTE: agent_id and agent_meta are kept at the schedule level (trigger
        # variables) so Mage shows them in the UI. jwt_token is also stored
        # here so that time-based runs (which read from schedule variables, not
        # run-level variables) always have a valid token in kwargs.
        logger.info("[SCHEDULE] Updating trigger variables with jwt_token")
        update_result = orchestrator.client.update_schedule_variables(
            schedule_id=schedule_id,
            variables={
                "agent_id": agent_id,
                "agent_meta": agent_meta,
                "jwt_token": schedule_token,
            },
        )

        if not update_result:
            logger.warning(
                "[SCHEDULE] Failed to store jwt_token in trigger variables. "
                "The Mage oauth_token may be expired — set "
                "MAGE_ACCESS_TOKEN_EXPIRY_TIME=315360000 in Mage's environment."
            )
        logger.info("[SCHEDULE] ✅ Trigger variables updated (agent_id, agent_meta, jwt_token)")

        # The trigger will run automatically based on schedule_interval

        logger.info(
            f"[SCHEDULE] ✅ Successfully configured schedule: "
            f"agent_id={agent_id}, schedule_id={schedule_id}, trigger_created={trigger_created}"
        )

        response = {
            "success": True,
            "schedule_id": schedule_id,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "session_created": session_was_created,
            "trigger_created": trigger_created,  # Indicates if trigger was just created
            "schedule_interval": schedule_interval,
            "schedule_type": "time",  # All new triggers are time-based
            "message": "Agent scheduled successfully. It will run based on the schedule interval. No immediate execution.",
        }

        # Include start_time information
        if start_time:
            response["start_time_requested"] = start_time
            response["start_time_actual"] = adjusted_start_time
            if adjusted_start_time != start_time:
                response["start_time_adjusted"] = True
                response["message"] = (
                    f"Agent scheduled successfully. The requested start time ({start_time}) was in the past, "
                    f"so it was adjusted to the next valid run time ({adjusted_start_time}) to maintain the schedule pattern. "
                    f"It will run based on the schedule interval from that time."
                )
            else:
                response["start_time_adjusted"] = False

        return response

    except ValueError as e:
        logger.error(f"[SCHEDULE] Validation error: {str(e)}")
        raise
    except Exception as e:
        logger.error(
            f"[SCHEDULE] Failed to schedule agent run: {str(e)}", exc_info=True
        )
        raise


async def trigger_agent_run_now(
    agent_id: str,  # CHANGED: Now accepts agent_id directly
    user_id: str,
    session_id: str,
    new_message: Dict[str, Any],
    run_mode: str = "single",
    streaming: bool = False,
) -> Dict[str, Any]:
    """
    Immediately trigger an agent run (for agents with existing triggers).

    SIMPLIFIED: Now accepts agent_id directly instead of agent_name.
    """
    from th2agent.scheduler import run_agent_background as _pkg

    try:
        logger.info(f"[TRIGGER NOW] Triggering agent: {agent_id}")

        # Get agent info to validate it exists and get agent_name
        agent_info = _pkg.get_agent_by_id(agent_id, user_id)
        if not agent_info:
            raise ValueError(f"Agent not found: {agent_id}")

        agent_name = agent_info.get("agent_name")
        logger.info(f"[TRIGGER NOW] Found agent: {agent_name} (ID: {agent_id})")

        # Get folder name for session creation
        folder_name = _pkg.get_agent_folder_name(agent_name)
        logger.info(f"[TRIGGER NOW] Resolved to folder: {folder_name}")

        # Import session functions
        from th2agent.core.adk_runner import (
            get_adk_session,
            create_adk_agent_session,
        )

        # Create agent refresh token (90-day) — stored in Mage trigger
        # variables for subsequent scheduled runs.
        jwt_token = create_agent_run_token(
            agent_name=agent_name,
            user_id=user_id,
            session_id=session_id,
            new_message=new_message,
            run_mode=run_mode,
            streaming=streaming,
        )

        # ADKAuthMiddleware (main.py) only accepts tokens with type=="access".
        # Mint a short-lived access token from the refresh token for the
        # session-creation calls below.
        from th2agent.helpers.security import refresh_access_token_from_agent_refresh
        adk_access_token = refresh_access_token_from_agent_refresh(jwt_token)

        # Check if session exists, create if not
        session_was_created = False
        try:
            logger.info(f"[TRIGGER NOW] Checking if session exists: {session_id}")
            try:
                await get_adk_session(
                    agent_name=folder_name,
                    user_id=user_id,
                    session_id=session_id,
                    token=adk_access_token,
                )
                logger.info(f"[TRIGGER NOW] Session {session_id} already exists")
                session_was_created = False
            except Exception:
                # Session doesn't exist, create it
                logger.info(f"[TRIGGER NOW] Creating new session: {session_id}")
                await create_adk_agent_session(
                    agent_name=folder_name,
                    user_id=user_id,
                    session_id=session_id,
                    data={},
                    token=adk_access_token,
                )
                logger.info(f"[TRIGGER NOW] Successfully created session {session_id}")
                session_was_created = True
        except Exception as e:
            logger.error(
                f"[TRIGGER NOW] Error handling session: {str(e)}", exc_info=True
            )
            raise ValueError(f"Failed to create/check session: {str(e)}")

        # Get orchestrator and find existing trigger by agent_id
        from th2agent.scheduler.mage import get_orchestrator

        orchestrator = get_orchestrator()
        schedules = orchestrator.client.get_pipeline_schedules(
            orchestrator.PIPELINE_UUID
        )
        existing_trigger = next(
            (s for s in schedules if s.get("name") == agent_id), None
        )

        if not existing_trigger:
            raise ValueError(
                f"No trigger found for agent_id: {agent_id}. "
                f"Use schedule_agent_run() first."
            )

        schedule_id = existing_trigger.get("id")
        trigger_token = existing_trigger.get("token")

        # Trigger the pipeline with agent refresh token (message already included in JWT)
        execution_result = orchestrator.client.trigger_pipeline(
            schedule_id=schedule_id,
            trigger_token=trigger_token,
            run_variables={"jwt_token": jwt_token},
        )

        if not execution_result:
            raise Exception(f"Failed to trigger agent_id: {agent_id}")

        run_info = execution_result.get("pipeline_run", {})
        run_id = run_info.get("id")
        status = run_info.get("status")

        logger.info(
            f"[TRIGGER NOW] Successfully triggered: run_id={run_id}, status={status}"
        )

        return {
            "success": True,
            "run_id": run_id,
            "status": status,
            "agent_id": agent_id,
            "agent_name": agent_name,
            "session_created": session_was_created,
        }

    except ValueError as e:
        logger.error(f"[TRIGGER NOW] Validation error: {str(e)}")
        raise
    except Exception as e:
        logger.error(f"[TRIGGER NOW] Failed to trigger agent: {str(e)}", exc_info=True)
        raise
