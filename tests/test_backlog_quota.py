"""Quota identity, expiry and independent-deadline contracts."""

from datetime import datetime, timedelta, timezone

from apowerb.configs.settings import Settings
from apowerb.scheduler.backlog_quota import QuotaCooldowns


def test_independent_deadlines_expire_without_clearing_or_shortening_each_other():
    now = datetime.now(timezone.utc)
    groups = {42: "a", 43: "a", 44: "b", 45: "c"}
    cooldowns = QuotaCooldowns()
    cooldowns.defer("a", now + timedelta(seconds=10))
    cooldowns.defer("b", now + timedelta(seconds=30))
    cooldowns.defer("a", now + timedelta(seconds=20))
    cooldowns.defer("a", now + timedelta(seconds=5))
    assert cooldowns.eligible_agent_ids(groups, now + timedelta(seconds=15)) == {45}
    assert cooldowns.eligible_agent_ids(groups, now + timedelta(seconds=20)) == {
        42,
        43,
        45,
    }
    assert cooldowns.eligible_agent_ids(groups, now + timedelta(seconds=30)) is None


def test_unknown_scope_pause_preserves_a_longer_known_scope_deadline():
    now = datetime.now(timezone.utc)
    cooldowns = QuotaCooldowns()
    cooldowns.defer("a", now + timedelta(seconds=30))
    cooldowns.defer(None, now + timedelta(seconds=10))
    assert cooldowns.eligible_agent_ids({42: "a", 44: "b"}, now) == set()
    assert cooldowns.eligible_agent_ids(
        {42: "a", 44: "b"}, now + timedelta(seconds=10)
    ) == {44}


def test_operator_quota_groups_are_loaded_from_environment(monkeypatch):
    monkeypatch.setenv(
        "BACKLOG_QUOTA_GROUPS",
        '{"42":"provider-project-a","43":"provider-project-a","44":"provider-project-b"}',
    )
    configured = Settings(_env_file=None)
    assert configured.backlog_quota_groups == {
        42: "provider-project-a",
        43: "provider-project-a",
        44: "provider-project-b",
    }
