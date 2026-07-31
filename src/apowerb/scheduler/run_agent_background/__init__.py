"""Scheduler runtime for agent background runs.

This package re-exports the public surface that used to live in a single
``run_agent_background.py`` module. Consumers keep importing from
``apowerb.scheduler.run_agent_background`` with no change.

Test contract (important):
The test suite monkey-patches certain upstream helpers as attributes of this
package, e.g.::

    patch.object(rab, "decode_agent_refresh_token", return_value=...)
    patch.object(rab, "get_agent_folder_name", return_value=...)
    patch.object(rab, "run_adk_agent", new=AsyncMock(...))
    patch("apowerb.scheduler.run_agent_background.get_agent_by_id")

For those patches to affect the real call sites, submodules dereference
these symbols through the package (``from . import ... as _pkg`` then
``_pkg.<symbol>(...)``) rather than binding them to local names.
"""

from apowerb.configs.settings import get_settings
from apowerb.configs.th2logger import setup_logging
from apowerb.core.adk_runner import run_adk_agent
from apowerb.core.agent_main import get_agent_by_id, get_agent_folder_name
from apowerb.helpers.security import (
    create_agent_refresh_token,
    decode_agent_refresh_token,
)
from apowerb.scheduler.mage import get_orchestrator

# Public helpers
from .schedule_helpers import (
    _activate_trigger_at,
    _schedule_activation_if_future,
    apply_start_time_offset,
    calculate_next_run_time,
    resolve_schedule_interval,
)
from .agent_runner import (
    convert_to_adk_message_format,
    run_agent_from_jwt,
    run_agent_from_refresh_token,
)
from .token_issuer import create_agent_run_token
from .scheduler_api import schedule_agent_run, trigger_agent_run_now

logger = setup_logging(__name__)
settings = get_settings()

__all__ = [
    # Upstream symbols re-exported for patch targets
    "decode_agent_refresh_token",
    "create_agent_refresh_token",
    "get_agent_by_id",
    "get_agent_folder_name",
    "run_adk_agent",
    "get_orchestrator",
    # Schedule helpers
    "_activate_trigger_at",
    "_schedule_activation_if_future",
    "apply_start_time_offset",
    "resolve_schedule_interval",
    "calculate_next_run_time",
    # Agent runners
    "convert_to_adk_message_format",
    "run_agent_from_jwt",
    "run_agent_from_refresh_token",
    # Token issuer
    "create_agent_run_token",
    # High-level API
    "schedule_agent_run",
    "trigger_agent_run_now",
]
