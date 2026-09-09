"""Quota guard at the entry of a run.

Two opposing requirements meet here:
- a user who has exhausted their quota must NOT be able to launch another
  run on the shared key;
- the guard must NEVER make the product mute because of its own failure.
  Any internal error lets the run through.
"""
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from apowerb.core.usage_quota import QuotaStatus
from apowerb.helpers import quota_guard as qg


def _status(exceeded: bool, used=1500, limit=1000) -> QuotaStatus:
    return QuotaStatus(
        used_tokens=used,
        limit_tokens=limit,
        remaining_tokens=max(0, limit - used),
        percent_used=100.0 if exceeded else 50.0,
        exceeded=exceeded,
        warning=exceeded,
        plan="free",
        resets_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def wired(monkeypatch):
    """Wires the guard to test doubles: no DB, no agent store."""

    def _apply(uses_default=True, status=None, raise_on_status=None):
        monkeypatch.setattr(qg, "agent_uses_default_llm", lambda _n: uses_default)

        class _Ctx:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, *a):
                return False

        class _SessionManager:
            def session(self):
                return _Ctx()

        async def _fake_get_quota_status(db, owner_id, plan):
            if raise_on_status:
                raise raise_on_status
            return status

        import apowerb.core.usage_quota as uq
        import apowerb.helpers.database as dbmod

        monkeypatch.setattr(uq, "get_quota_status", _fake_get_quota_status)
        monkeypatch.setattr(dbmod, "sessionmanager", _SessionManager())

    return _apply


@pytest.mark.asyncio
async def test_passes_when_under_quota(wired):
    wired(status=_status(exceeded=False, used=100))
    await qg.enforce_run_quota("agent1", owner_id="a@b.c", plan="free")


@pytest.mark.asyncio
async def test_refuses_with_402_when_exceeded(wired):
    wired(status=_status(exceeded=True))
    with pytest.raises(HTTPException) as exc:
        await qg.enforce_run_quota("agent1", owner_id="a@b.c", plan="free")
    assert exc.value.status_code == 402
    detail = exc.value.detail
    assert detail["code"] == "QUOTA_EXCEEDED"
    # The frontend needs the numbers to explain, not just to refuse.
    assert detail["used_tokens"] == 1500
    assert detail["limit_tokens"] == 1000
    assert detail["resets_at"].startswith("2026-08-01")


@pytest.mark.asyncio
async def test_agent_on_its_own_key_is_never_capped(wired):
    """A personal API key is paid for by its owner: no cap, even if the
    shared counter is at its limit."""
    wired(uses_default=False, status=_status(exceeded=True))
    await qg.enforce_run_quota("agent1", owner_id="a@b.c", plan="free")


@pytest.mark.asyncio
async def test_a_broken_quota_check_lets_the_run_through(wired):
    """Losing a cap is less serious than making the product mute."""
    wired(raise_on_status=RuntimeError("DB down"))
    await qg.enforce_run_quota("agent1", owner_id="a@b.c", plan="free")


@pytest.mark.asyncio
async def test_the_402_is_not_swallowed_by_the_safety_net(wired):
    """The failure-safety net must not swallow the refusal itself."""
    wired(status=_status(exceeded=True))
    with pytest.raises(HTTPException):
        await qg.enforce_run_quota("agent1", owner_id="a@b.c", plan="free")


def test_unresolvable_agent_is_treated_as_not_default(monkeypatch):
    """Agent not found -> let it through, don't block."""
    import apowerb.core.agent_helpers.agent_utils as au

    def _boom(**_kw):
        raise RuntimeError("store down")

    monkeypatch.setattr(au, "get_agent_details", _boom)
    assert qg.agent_uses_default_llm("agent999") is False
    assert qg.agent_uses_default_llm("not-an-id") is False
