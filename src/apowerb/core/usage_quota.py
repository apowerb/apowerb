"""Monthly token quota on the shared thaink2 model.

What is capped is ONLY what thaink2 pays for: the ``llm_usage`` rows marked
``billed_to_thaink2``. A user who supplies their own API key pays for their
own consumption, so they have no quota here.

Design choices (settled with David, 27/07/26):

- **Per user**, via ``owner_id`` -- not per agent: a quota per agent would
  multiply with the number of agents created, which caps nothing anymore.
- **In tokens**, not euros: the conversion depends on the per-model rate
  card (and cache is worth 10%% of input). Euros will come with billing,
  on ``User.credits`` which already exists.
- **Europe/Paris calendar month**: that's the billing timezone. In summer
  Paris is UTC+2, so counting a month in UTC would pull 2h of the previous
  month's consumption into the current month.

Warning: ``llm_usage`` is documented as *best-effort* ("not billing-grade
audit", see the model's docstring): rows can be missing after a DB incident.
That's acceptable for a guard -- worst case we let a bit too much through --
but **never** as the source of truth for an invoice.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from logging import getLogger
from typing import Optional
from zoneinfo import ZoneInfo

from apowerb.configs.settings import get_settings

logger = getLogger(__name__)

BILLING_TZ = ZoneInfo("Europe/Paris")

# Quota top-up providers. The billing extension registers one (purchased
# credits become tokens); without it the list stays empty and the quota is
# the plan's alone.
_topup_providers: list = []


def register_quota_topup(provider) -> None:
    """Registers a top-up provider -- ``async fn(db, owner_id) -> int``."""
    _topup_providers.append(provider)


async def available_topup(db, owner_id: str) -> int:
    """Extra tokens brought in by purchased credits.

    Best-effort: a read failure yields 0, so the plan's quota alone applies.
    It can neither inflate the quota nor remove it.
    """
    total = 0
    for provider in _topup_providers:
        try:
            total += int(await provider(db, owner_id) or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[QUOTA] unreadable top-up for %s: %s", owner_id, exc
            )
    return max(0, total)

# Warning threshold: we warn before blocking, rather than letting the user
# discover the wall mid-conversation.
WARNING_RATIO = 0.8


@dataclass(frozen=True)
class QuotaStatus:
    """Snapshot of a user's quota for the current month.

    ``limit_tokens``/``remaining_tokens``/``percent_used`` are ``None``
    when the plan is unlimited -- a numeric value would suggest a quota
    that doesn't exist.
    """

    used_tokens: int
    limit_tokens: Optional[int]
    remaining_tokens: Optional[int]
    percent_used: Optional[float]
    exceeded: bool
    warning: bool
    plan: Optional[str]
    resets_at: Optional[datetime]


def resolve_quota(plan: Optional[str]) -> Optional[int]:
    """Monthly token cap for this plan, or ``None`` if unlimited.

    A cap of 0 (or negative) means "unlimited": it's the kill-switch to use
    without a redeploy if the guard wrongly blocks in production.
    """
    settings = get_settings()
    plan_quotas = getattr(settings, "default_llm_plan_quotas", None) or {}
    limit = plan_quotas.get((plan or "").strip()) if plan else None
    if limit is None:
        limit = getattr(settings, "default_llm_monthly_token_quota", 0) or 0
    return int(limit) if int(limit) > 0 else None


def month_start(now: Optional[datetime] = None) -> datetime:
    """Start of the current calendar month (midnight in Paris), returned in UTC."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(BILLING_TZ)
    first = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first.astimezone(timezone.utc)


def next_reset(now: Optional[datetime] = None) -> datetime:
    """Start of the NEXT month (reset date), returned in UTC."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(BILLING_TZ)
    year, month = local.year, local.month
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    nxt = local.replace(
        year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0
    )
    return nxt.astimezone(timezone.utc)


def build_status(
    used: int,
    plan: Optional[str],
    now: Optional[datetime],
    topup_tokens: int = 0,
) -> QuotaStatus:
    """Builds the status from an already-aggregated consumption figure.

    *topup_tokens* is what purchased credits add to the plan's quota. An
    already-unlimited plan stays unlimited: extending infinity makes no
    sense, and doing so would make a quota appear where there isn't one.
    """
    limit = resolve_quota(plan)
    used = max(0, int(used or 0))
    if limit is not None and topup_tokens > 0:
        limit += int(topup_tokens)

    if limit is None:
        return QuotaStatus(
            used_tokens=used,
            limit_tokens=None,
            remaining_tokens=None,
            percent_used=None,
            exceeded=False,
            warning=False,
            plan=plan,
            resets_at=None,
        )

    ratio = used / limit
    return QuotaStatus(
        used_tokens=used,
        limit_tokens=limit,
        remaining_tokens=max(0, limit - used),
        # Capped at 100: a progress bar must not overflow.
        percent_used=round(min(ratio, 1.0) * 100, 2),
        exceeded=used >= limit,
        warning=ratio >= WARNING_RATIO,
        plan=plan,
        resets_at=next_reset(now),
    )


async def get_quota_status(db, owner_id: str, plan: Optional[str]) -> QuotaStatus:
    """Quota status for *owner_id*, computed over the current month.

    The sum is done in SQL. It relies on the existing index
    ``ix_llm_usage_owner_created`` (owner_id, created_at) -- deliberately no
    index is added for ``billed_to_thaink2``: it only serves as a residual
    filter, and the ``llm_usage`` migration explicitly warns that a
    non-CONCURRENTLY CREATE INDEX on this now-large table would block
    INSERTs for the duration of the build.
    """
    from sqlalchemy import func, select

    from apowerb.models import LlmUsage

    now = datetime.now(timezone.utc)
    stmt = select(func.coalesce(func.sum(LlmUsage.total_tokens), 0)).where(
        LlmUsage.owner_id == owner_id,
        LlmUsage.billed_to_thaink2.is_(True),
        LlmUsage.created_at >= month_start(now),
    )
    used = (await db.execute(stmt)).scalar() or 0
    return build_status(
        used=used,
        plan=plan,
        now=now,
        topup_tokens=await available_topup(db, owner_id),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Le plafond que la jauge annonce — le même que la garde applique.
#
# Passé au noyau le 09/09/26 : compteur ET barre sont open source. Les deux
# moitiés lisent le même quota de plan et le même rechargement acheté, donc ce
# qu'on montre à l'utilisateur est ce qui l'arrêtera vraiment. Une jauge qui
# annoncerait un autre nombre laisserait quelqu'un heurter le mur en pleine
# conversation — précisément ce que l'alerte à 80 % existe pour éviter.
#
# Contrat attendu par ``registry.register_default_llm_cap`` :
#
#     async fn(db, *, owner_id, plan) -> int | None      # None = illimité
#
# Ne répond que le PLAFOND. La consommation est la connaissance du noyau, et la
# recalculer ici serait une seconde vérité à tenir synchrone.
# ─────────────────────────────────────────────────────────────────────────────
async def default_llm_cap(
    db, *, owner_id: str, plan: Optional[str] = None
) -> Optional[int]:
    """Plafond mensuel en jetons pour *owner_id*, ou ``None`` si illimité.

    Un plan illimité le reste quels que soient les crédits : prolonger l'infini
    ne veut rien dire, et ferait apparaître un quota là où il n'y en a pas.
    """
    limit = resolve_quota(plan)
    if limit is None:
        return None
    return limit + await available_topup(db, owner_id)
