"""Process-local cooldowns for explicitly configured provider quota groups."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime


class QuotaCooldowns:
    """Keep quota deadlines separate; unknown scopes retain a global pause."""

    def __init__(self) -> None:
        self._deadlines: dict[str | None, datetime] = {}

    def defer(self, group: str | None, until: datetime) -> None:
        previous = self._deadlines.get(group)
        self._deadlines[group] = max(previous, until) if previous else until

    def eligible_agent_ids(
        self, groups: Mapping[int, str], now: datetime
    ) -> set[int] | None:
        """None means unrestricted; an empty set means nothing is safe to claim.

        With any active cooldown, an unconfigured agent cannot establish
        independence. An unknown failing scope pauses even configured agents.
        """
        self._deadlines = {
            group: until for group, until in self._deadlines.items() if until > now
        }
        if not self._deadlines:
            return None
        if None in self._deadlines:
            return set()
        return {
            agent_id
            for agent_id, group in groups.items()
            if group not in self._deadlines
        }
