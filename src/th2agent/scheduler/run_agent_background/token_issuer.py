"""Agent refresh token issuer.

Wraps :func:`th2agent.helpers.security.create_agent_refresh_token` with the
payload shape expected by scheduled runs.
"""

from typing import Any, Dict

from th2agent.configs.th2logger import setup_logging
from th2agent.helpers.security import create_agent_refresh_token

logger = setup_logging("th2agent.scheduler.run_agent_background")


def create_agent_run_token(
    agent_name: str,
    user_id: str,
    session_id: str,
    new_message: Dict[str, Any],
    run_mode: str = "single",
    streaming: bool = False,
    agent_metadata: Dict[str, Any] | None = None,
    expires_days: int = 90,  # UPDATED: 90 days for agent refresh tokens
) -> str:
    """
    Create an agent refresh token containing all parameters needed to run an agent.

    Now uses create_agent_refresh_token() for long-lived tokens (90 days default).
    These tokens are stored in the trigger and refreshed on each /schedule_run call.

    Args:
        agent_name: Name of the agent to run
        user_id: User identifier
        session_id: Session identifier (base — each run appends a timestamp)
        new_message: Message to send to agent
        run_mode: Run mode (default: "single")
        streaming: Whether to stream responses
        agent_metadata: Optional agent metadata
        expires_days: Token expiration in days (default: 90 for scheduled runs)

    Returns:
        JWT agent refresh token string
    """
    token_payload = {
        "agent_name": agent_name,
        "user_id": user_id,
        "session_id": session_id,
        "new_message": new_message,
        "run_mode": run_mode,
        "streaming": streaming,
    }

    # Add metadata if provided
    if agent_metadata:
        token_payload["agent_metadata"] = agent_metadata

    logger.info(
        f"Creating agent refresh token for agent run: {agent_name} (expires in {expires_days} days)"
    )

    # Create long-lived agent refresh token for scheduled runs
    return create_agent_refresh_token(token_payload, expires_days=expires_days)
