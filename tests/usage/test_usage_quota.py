"""Monthly quota on the shared thaink2 model.

Rules settled with David (27/07/26):
- per USER (owner_id), never per agent -- otherwise the quota multiplies
  with the number of agents created;
- in TOKENS (conversion to euros depends on the per-model rate card, and
  the cache=10%% input trap);
- Europe/Paris CALENDAR month;
- only consumption on the shared key counts: a user who pays for their own
  key has no quota.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from apowerb.core import usage_quota as uq


class _FakeSettings:
    def __init__(self, quota=1_000_000, plan_quotas=None):
        self.default_llm_monthly_token_quota = quota
        self.default_llm_plan_quotas = plan_quotas or {}


@pytest.fixture
def settings(monkeypatch):
    def _apply(**kwargs):
        s = _FakeSettings(**kwargs)
        monkeypatch.setattr(uq, "get_settings", lambda: s)
        return s

    return _apply


# ------------------------------------------------------------ resolution


def test_default_quota_applies_when_plan_is_unknown(settings):
    settings()
    assert uq.resolve_quota("free") == 1_000_000
    assert uq.resolve_quota(None) == 1_000_000
    assert uq.resolve_quota("plan-inexistant") == 1_000_000


def test_plan_specific_quota_wins(settings):
    settings(plan_quotas={"pro": 50_000_000, "free": 1_000_000})
    assert uq.resolve_quota("pro") == 50_000_000
    assert uq.resolve_quota("free") == 1_000_000


def test_zero_or_negative_means_unlimited(settings):
    """A cap of 0 disables the guard -- it's the kill-switch without a
    redeploy if the quota wrongly blocks in production."""
    settings(quota=0)
    assert uq.resolve_quota("free") is None
    settings(quota=-1)
    assert uq.resolve_quota("free") is None
    settings(plan_quotas={"enterprise": 0})
    assert uq.resolve_quota("enterprise") is None


# ---------------------------------------------------------- monthly window


def test_month_window_starts_on_the_first_paris_midnight():
    now = datetime(2026, 7, 27, 14, 30, tzinfo=ZoneInfo("Europe/Paris"))
    start = uq.month_start(now)
    assert start.astimezone(ZoneInfo("Europe/Paris")).day == 1
    assert start.astimezone(ZoneInfo("Europe/Paris")).hour == 0
    assert start.tzinfo is timezone.utc


def test_month_window_is_paris_not_utc_in_summer():
    """In July Paris is at UTC+2: the month therefore starts on June 30 at
    22:00 UTC. Counting in UTC would leak 2h of consumption from the
    previous month."""
    now = datetime(2026, 7, 27, 12, 0, tzinfo=ZoneInfo("Europe/Paris"))
    assert uq.month_start(now) == datetime(2026, 6, 30, 22, 0, tzinfo=timezone.utc)


def test_next_reset_rolls_over_in_december():
    now = datetime(2026, 12, 15, 12, 0, tzinfo=ZoneInfo("Europe/Paris"))
    nxt = uq.next_reset(now)
    paris = nxt.astimezone(ZoneInfo("Europe/Paris"))
    assert (paris.year, paris.month, paris.day) == (2027, 1, 1)


# -------------------------------------------------------------------- status


def test_status_reports_usage_against_the_limit(settings):
    settings(quota=1000)
    st = uq.build_status(used=250, plan="free", now=datetime.now(timezone.utc))
    assert st.limit_tokens == 1000
    assert st.used_tokens == 250
    assert st.remaining_tokens == 750
    assert st.percent_used == 25.0
    assert not st.exceeded
    assert not st.warning


def test_status_warns_at_eighty_percent(settings):
    """Le bandeau d'alerte existe pour eviter l'effet mur."""
    settings(quota=1000)
    assert not uq.build_status(used=799, plan="free", now=None).warning
    assert uq.build_status(used=800, plan="free", now=None).warning
    assert uq.build_status(used=999, plan="free", now=None).warning


def test_status_is_exceeded_only_at_or_above_the_limit(settings):
    settings(quota=1000)
    assert not uq.build_status(used=999, plan="free", now=None).exceeded
    assert uq.build_status(used=1000, plan="free", now=None).exceeded
    assert uq.build_status(used=1500, plan="free", now=None).exceeded


def test_remaining_never_goes_negative(settings):
    settings(quota=1000)
    st = uq.build_status(used=1500, plan="free", now=None)
    assert st.remaining_tokens == 0
    assert st.percent_used == 100.0  # borne, pour ne pas casser une barre de progression


def test_unlimited_plan_is_never_exceeded(settings):
    settings(quota=0)
    st = uq.build_status(used=10**9, plan="free", now=None)
    assert st.limit_tokens is None
    assert st.remaining_tokens is None
    assert not st.exceeded
    assert not st.warning
    assert st.percent_used is None
