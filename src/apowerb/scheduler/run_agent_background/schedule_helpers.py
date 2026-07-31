"""Schedule helpers: cron/preset resolution, start-time offsets and
future-activation background tasks used by the scheduler.

Extracted from the original monolithic ``run_agent_background`` module so
each concern lives in a file < 500 lines. Behaviour is iso-functional.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from apowerb.configs.th2logger import setup_logging

# IMPORTANT: keep the public logger name identical to the legacy module so
# tests that call ``caplog.set_level(logger="apowerb.scheduler.run_agent_background")``
# still capture records emitted from this helper.
logger = setup_logging("apowerb.scheduler.run_agent_background")


async def _activate_trigger_at(schedule_id: int, activate_at: datetime) -> None:
    """
    Background task: sleep until activate_at, then flip the Mage trigger to active.

    IMPORTANT: We also reset start_time to the activation moment.
    If we only set status=active, Mage computes "Next run date" at creation time
    and treats any cron slots between creation and activation as "missed", firing
    them immediately as catchup runs. Resetting start_time prevents this.
    """
    from apowerb.scheduler.mage import get_orchestrator

    now = datetime.now(timezone.utc)
    delay_seconds = (activate_at - now).total_seconds()

    if delay_seconds > 0:
        logger.info(
            f"[SCHEDULE] Trigger {schedule_id} will activate in "
            f"{delay_seconds:.0f}s at {activate_at.isoformat()}"
        )
        await asyncio.sleep(delay_seconds)

    try:
        orchestrator = get_orchestrator()
        # Reset start_time to now (activation moment) AND set active in one call.
        # This prevents Mage from treating cron slots between creation and
        # activation as "missed" and running them as catchup.
        activation_now = datetime.now(timezone.utc)
        activation_ts = activation_now.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        result = orchestrator.client.update_schedule(
            schedule_id=schedule_id,
            status="active",
            start_time=activation_ts,
        )
        if result:
            logger.info(
                f"[SCHEDULE] ✅ Trigger {schedule_id} activated at {activation_ts} "
                f"(start_time reset to prevent catchup runs)"
            )
        else:
            logger.error(f"[SCHEDULE] ❌ Failed to activate trigger {schedule_id}")
    except Exception as e:
        logger.error(f"[SCHEDULE] ❌ Error activating trigger {schedule_id}: {e}", exc_info=True)


def _schedule_activation_if_future(schedule_id: int, start_time_str: str | None) -> bool:
    """
    Helper: if start_time_str is in the future, spawn a background task to activate
    the trigger at that moment and return True. Otherwise return False.

    Used for both newly created AND updated existing triggers so that activation
    is always guaranteed when a future start_time is requested.
    """
    from apowerb.scheduler.mage import get_orchestrator

    if not start_time_str:
        return False

    try:
        ts = start_time_str.rstrip("Z")
        activate_dt = datetime.fromisoformat(ts)
        if activate_dt.tzinfo is None:
            activate_dt = activate_dt.replace(tzinfo=timezone.utc)

        if activate_dt <= datetime.now(timezone.utc):
            return False  # Already in the past, nothing to schedule

        asyncio.create_task(_activate_trigger_at(schedule_id, activate_dt))
        logger.info(
            f"[SCHEDULE] Scheduled activation of trigger {schedule_id} "
            f"at {activate_dt.isoformat()}"
        )
        return True
    except Exception as e:
        logger.warning(
            f"[SCHEDULE] Could not schedule activation task: {e}. "
            f"Activating trigger immediately as fallback."
        )
        get_orchestrator().client.update_schedule(
            schedule_id=schedule_id, status="active"
        )
        return False


def apply_start_time_offset(cron: str, start_time_str: str) -> str:
    """
    Fix */N cron expressions to align with the user's chosen start_time minute.

    Mage fires */N at absolute grid slots (0, N, 2N, ...).
    If start_time is :38 and interval is */5, Mage fires at :40, not :38.

    This converts */N to offset/N so it fires exactly at start_minute and
    every N minutes thereafter:
      */5 + start :38  ->  3/5  (fires :03, :08, ... :33, :38, :43, :48, :53, :58)
      */2 + start :33  ->  1/2  (fires :01, :03, ... :31, :33, :35, ...)
      */5 + start :35  ->  0/5  = */5  (already aligned, no change)

    Only modifies the minute field when it starts with "*/".
    All other cron patterns pass through unchanged.
    """
    if not start_time_str:
        return cron

    parts = cron.split()
    if len(parts) != 5 or not parts[0].startswith("*/"):
        return cron  # Not a */N minute pattern, leave unchanged

    try:
        interval = int(parts[0][2:])
        ts = start_time_str.rstrip("Z")
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        offset = dt.minute % interval
        if offset == 0:
            return cron  # Already aligned, no change needed

        parts[0] = f"{offset}/{interval}"
        result = " ".join(parts)
        logger.info(
            f"[SCHEDULE] Offset cron: '{cron}' + start :{dt.minute:02d} "
            f"-> '{result}' (offset={offset})"
        )
        return result
    except Exception as e:
        logger.warning(f"[SCHEDULE] Could not apply offset to '{cron}': {e}")
        return cron


def resolve_schedule_interval(schedule_interval: str, start_time_str: str | None) -> str:
    """
    1. Convert Mage preset intervals into explicit cron expressions that
       respect the user's chosen start_time HH:MM.
    2. For */N minute patterns, apply a minute offset so the first fire
       lands exactly on start_time rather than the next grid slot.

    Examples (start_time = 09:38):
      @hourly  ->  38 * * * *          (every hour at :38)
      @daily   ->  38 9 * * *          (every day at 09:38)
      @weekly  ->  38 9 * * 4          (every Thursday at 09:38)
      @monthly ->  38 9 13 * *         (every 13th at 09:38)
      */5      ->  3/5 * * * *         (every 5 min, offset :03/:08/...:38)
      */2      ->  0/2 * * * * = */2   (already aligned, unchanged)
      30 */1   ->  30 */1 * * *        (custom, passes through)
    """
    PRESET_CONVERSIONS = {"@hourly", "@daily", "@weekly", "@monthly"}

    resolved = schedule_interval

    # Step 1: convert presets to explicit cron using start_time's HH:MM
    if schedule_interval in PRESET_CONVERSIONS and start_time_str:
        try:
            ts = start_time_str.rstrip("Z")
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)

            minute = dt.minute
            hour = dt.hour
            day = dt.day
            cron_weekday = (dt.weekday() + 1) % 7  # Python Mon=0 -> cron Sun=0

            if schedule_interval == "@hourly":
                resolved = f"{minute} * * * *"
            elif schedule_interval == "@daily":
                resolved = f"{minute} {hour} * * *"
            elif schedule_interval == "@weekly":
                resolved = f"{minute} {hour} * * {cron_weekday}"
            elif schedule_interval == "@monthly":
                resolved = f"{minute} {hour} {day} * *"

            logger.info(
                f"[SCHEDULE] Preset '{schedule_interval}' -> '{resolved}' "
                f"(start_time={start_time_str})"
            )
        except Exception as e:
            logger.warning(
                f"[SCHEDULE] Could not convert preset '{schedule_interval}': {e}"
            )

    # Step 2: apply minute offset to */N patterns so first fire = start_time
    resolved = apply_start_time_offset(resolved, start_time_str)

    return resolved


def calculate_next_run_time(start_time_str: str, schedule_interval: str) -> str | None:
    """
    Calculate the next valid run time if start_time is in the past.

    This function handles cron intervals and ensures the schedule maintains
    its intended pattern even when start_time has passed.

    IMPORTANT: All times are treated as UTC. If start_time has no timezone,
    it's assumed to be UTC.

    Args:
        start_time_str: ISO 8601 datetime string (e.g., "2026-02-13T10:30:00")
        schedule_interval: Cron expression or preset (@hourly, @daily, etc.)

    Returns:
        Adjusted start_time as ISO 8601 string, or None if start_time is in future

    Examples:
        - start_time: "2026-02-13T10:30:00" (in past, UTC)
        - interval: "*/15 * * * *" (every 15 minutes)
        - current: "2026-02-13T11:12:00" (UTC)
        - result: "2026-02-13T11:15:00" (next 15-min interval from 10:30)
    """
    try:
        # Parse the start time - if no timezone, assume UTC
        # Remove 'Z' suffix if present
        if start_time_str.endswith("Z"):
            start_time_str = start_time_str[:-1]

        # Parse as datetime and ensure timezone-aware (UTC)
        start_time = datetime.fromisoformat(start_time_str)
        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)

        logger.info(
            f"[SCHEDULE] Comparing times (UTC): "
            f"start_time={start_time.isoformat()}, now={now.isoformat()}"
        )

        # If start_time is in the future, no adjustment needed
        if start_time > now:
            logger.info(
                f"[SCHEDULE] start_time {start_time_str} is in the future, no adjustment needed"
            )
            return None

        logger.info(
            f"[SCHEDULE] start_time {start_time_str} is in the past (now: {now.isoformat()})"
        )

        # Calculate interval in minutes based on cron expression
        interval_minutes = None

        # Parse cron expression for minute intervals (e.g., */15, */30, */5)
        if schedule_interval.startswith("*/") and " " in schedule_interval:
            parts = schedule_interval.split()
            if parts[0].startswith("*/"):
                try:
                    interval_minutes = int(parts[0][2:])
                    logger.info(
                        f"[SCHEDULE] Detected minute interval: {interval_minutes} minutes"
                    )
                except ValueError:
                    pass

        # Handle preset intervals
        preset_intervals = {
            "@hourly": 60,
            "@daily": 1440,
            "@weekly": 10080,
            "@monthly": 43200,  # Approximate (30 days)
        }

        if schedule_interval in preset_intervals:
            interval_minutes = preset_intervals[schedule_interval]
            logger.info(
                f"[SCHEDULE] Detected preset interval: {schedule_interval} = {interval_minutes} minutes"
            )

        # If we couldn't determine the interval, start from now
        if interval_minutes is None:
            logger.warning(
                f"[SCHEDULE] Could not parse interval '{schedule_interval}', using current time"
            )
            next_run = now + timedelta(minutes=1)  # Start in 1 minute
            return next_run.isoformat()

        # Calculate how many intervals have passed since start_time
        time_diff = now - start_time
        minutes_passed = time_diff.total_seconds() / 60
        intervals_passed = int(minutes_passed / interval_minutes)

        # Calculate the next run time
        next_interval_count = intervals_passed + 1
        next_run = start_time + timedelta(
            minutes=interval_minutes * next_interval_count
        )

        # Ensure next_run is actually in the future (handle edge cases)
        while next_run <= now:
            next_run += timedelta(minutes=interval_minutes)

        next_run_str = next_run.isoformat()
        logger.info(
            f"[SCHEDULE] Calculated next run time: {next_run_str} "
            f"(original: {start_time_str}, {intervals_passed} intervals passed)"
        )

        return next_run_str

    except Exception as e:
        logger.error(
            f"[SCHEDULE] Error calculating next run time: {str(e)}", exc_info=True
        )
        # On error, start from now
        return (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat()
