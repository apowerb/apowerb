"""Consommation du modele mutualise « thaink2/default » : ce que le coeur SAIT.

Le coeur COMPTE (colonne ``llm_usage.billed_to_thaink2``, posee par
``agent_utils`` a chaque tour qui a consomme la cle mutualisee) ; il ne
PLAFONNE pas -- le plafond, le 402 et la refacturation sont l'affaire d'une
brique (cf. ``registry.register_run_guard``). Grille du 19/08/26 : voir est
open source, plafonner se vend.

La jauge qui en decoule est donc a deux etages :

- sans brique, un compteur : jetons du mois calendaire et date de remise a
  zero. Aucune limite n'est affichee, parce qu'aucune n'existe -- une valeur
  numerique suggererait un quota qui n'est pas la ;
- avec une brique enregistree par ``registry.register_default_llm_cap``, la
  limite mensuelle (plan + credits achetes) s'ajoute, et avec elle le reste,
  le pourcentage, l'alerte a 80 % et le depassement.

Le mois est celui de Paris : c'est le fuseau de facturation. En ete Paris
est UTC+2 ; compter en UTC tirerait 2 h du mois precedent dans le courant.
"""

from __future__ import annotations

from datetime import datetime, timezone
from logging import getLogger
from typing import Any, Optional
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from apowerb.core.agent_helpers.default_llm import default_llm_available
from apowerb.core.extensions.registry import registry

logger = getLogger(__name__)

BILLING_TZ = ZoneInfo("Europe/Paris")

# On previent avant de bloquer : l'utilisateur ne doit pas decouvrir le mur
# au milieu d'une conversation.
WARNING_RATIO = 0.8


class DefaultLlmUsage(BaseModel):
    """Photographie du mois pour un utilisateur.

    ``limit_tokens`` / ``remaining_tokens`` / ``percent_used`` restent
    ``None`` tant qu'aucune brique ne fournit de plafond.

    Le schema est ferme (``extra="forbid"``) : y glisser une cle, un modele ou
    un api_base -- meme par erreur de construction -- leve, au lieu de partir
    silencieusement vers le navigateur.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    used_tokens: int = 0
    period_start: Optional[datetime] = None
    resets_at: Optional[datetime] = None
    limit_tokens: Optional[int] = None
    remaining_tokens: Optional[int] = None
    percent_used: Optional[float] = None
    warning: bool = False
    exceeded: bool = False


def month_start(now: Optional[datetime] = None) -> datetime:
    """Debut du mois calendaire courant (minuit a Paris), rendu en UTC."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(BILLING_TZ)
    first = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return first.astimezone(timezone.utc)


def next_reset(now: Optional[datetime] = None) -> datetime:
    """Debut du mois SUIVANT (date de remise a zero), rendu en UTC."""
    now = now or datetime.now(timezone.utc)
    local = now.astimezone(BILLING_TZ)
    year, month = local.year, local.month
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    nxt = local.replace(
        year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0
    )
    return nxt.astimezone(timezone.utc)


async def used_tokens_this_month(
    db: Any,
    owner_id: str,
    now: Optional[datetime] = None,
    start: Optional[datetime] = None,
) -> int:
    """Somme SQL des jetons factures a thaink2 pour *owner_id* ce mois-ci.

    La fenetre est fermee des deux cotes : [month_start, next_reset). Sans
    borne superieure, une ligne datee dans le futur -- horloge desynchronisee,
    import, restauration -- gonflerait le mois courant (releve par un
    examinateur independant le 07/09/26).

    S'appuie sur l'index existant ``ix_llm_usage_owner_created``
    (owner_id, created_at) ; ``billed_to_thaink2`` n'est qu'un filtre
    residuel, volontairement sans index (la migration de ``llm_usage``
    previent qu'un CREATE INDEX non CONCURRENTLY bloquerait les INSERT).

    *start* : debut effectif deja resolu par l'appelant (mois ou remise a
    zero) ; absent, il est lu ici.
    """
    from sqlalchemy import func, select

    from apowerb.core.usage_quota import counted_tokens_sum, usage_counted_since
    from apowerb.models import LlmUsage

    stmt = select(counted_tokens_sum()).where(
        LlmUsage.owner_id == owner_id,
        LlmUsage.billed_to_thaink2.is_(True),
        LlmUsage.created_at
        >= (start or await usage_counted_since(db, owner_id, month_start(now))),
        LlmUsage.created_at < next_reset(now),
    )
    return max(0, int((await db.execute(stmt)).scalar() or 0))


def build_usage(
    used: int,
    limit: Optional[int],
    now: Optional[datetime] = None,
    period_start: Optional[datetime] = None,
) -> DefaultLlmUsage:
    """Assemble la photographie a partir d'un compteur deja agrege.

    *period_start* : debut effectif du compte (remise a zero comprise) ;
    absent, le debut du mois.
    """
    now = now or datetime.now(timezone.utc)
    used = max(0, int(used or 0))
    base = DefaultLlmUsage(
        enabled=True,
        used_tokens=used,
        period_start=period_start or month_start(now),
        resets_at=next_reset(now),
    )
    if limit is None or int(limit) <= 0:
        return base
    limit = int(limit)
    ratio = used / limit
    return base.model_copy(
        update={
            "limit_tokens": limit,
            "remaining_tokens": max(0, limit - used),
            # Plafonne a 100 : une barre de progression ne deborde pas.
            "percent_used": round(min(ratio, 1.0) * 100, 2),
            "warning": ratio >= WARNING_RATIO,
            "exceeded": used >= limit,
        }
    )


async def default_llm_usage(
    db: Any, *, owner_id: str, plan: Optional[str], now: Optional[datetime] = None
) -> DefaultLlmUsage:
    """Compteur du coeur, complete par le plafond d'une brique s'il y en a une.

    Un plafond illisible ne casse pas la jauge : elle retombe sur le compteur
    seul, et le dit dans le journal. Il ne peut ni gonfler ni faire
    apparaitre une limite par accident.
    """
    if not default_llm_available():
        return DefaultLlmUsage(enabled=False)
    now = now or datetime.now(timezone.utc)
    from apowerb.core.usage_quota import usage_counted_since

    start = await usage_counted_since(db, owner_id, month_start(now))
    used = await used_tokens_this_month(db, owner_id, now, start)
    cap_provider = registry.default_llm_cap()
    limit: Optional[int] = None
    if cap_provider is not None:
        try:
            limit = await cap_provider(db, owner_id=owner_id, plan=plan)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[DEFAULT LLM USAGE] plafond illisible pour %s: %s", owner_id, exc
            )
    return build_usage(used, limit, now, start)
