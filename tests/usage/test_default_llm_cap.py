"""Le plafond que la jauge annonce est celui que le garde applique.

Le coeur COMPTE la consommation du modele mutualise et sert la jauge ; il ne
plafonne pas. Sans brique, la jauge n'affiche qu'un compteur -- c'est l'offre
open source. Avec cette brique, elle doit annoncer EXACTEMENT le mur que
``enforce_run_quota`` appliquera : une jauge qui montre un autre chiffre ferait
decouvrir la limite au milieu d'une conversation, ce que le seuil d'alerte a
80 % existe precisement pour eviter.
"""
import pytest

from apowerb.core.usage_quota import default_llm_cap
from apowerb.core.usage_quota import build_status, register_quota_topup


@pytest.fixture(autouse=True)
def _quota_settings(monkeypatch):
    class _Settings:
        default_llm_monthly_token_quota = 1_000_000
        default_llm_plan_quotas = {"pro": 5_000_000, "free": 100_000}

    monkeypatch.setattr(
        "apowerb.configs.settings.get_settings", lambda: _Settings(), raising=False
    )
    monkeypatch.setattr(
        "apowerb.core.usage_quota.get_settings", lambda: _Settings(), raising=False
    )


async def test_rend_le_plafond_du_plan():
    assert await default_llm_cap(None, owner_id="a@b.c", plan="pro") == 5_000_000
    assert await default_llm_cap(None, owner_id="a@b.c", plan="free") == 100_000


async def test_plan_inconnu_retombe_sur_le_plafond_general():
    assert await default_llm_cap(None, owner_id="a@b.c", plan="mystere") == 1_000_000


async def test_un_plafond_a_zero_signifie_illimite(monkeypatch):
    """Le coupe-circuit de production : 0 desactive le plafond sans redeploy.

    ``None`` remonte au coeur, qui n'affiche alors ni barre ni pourcentage --
    la jauge cesse de promettre une limite que plus personne n'applique.
    """
    class _Unlimited:
        default_llm_monthly_token_quota = 0
        default_llm_plan_quotas = {}

    monkeypatch.setattr(
        "apowerb.core.usage_quota.get_settings", lambda: _Unlimited(), raising=False
    )
    assert await default_llm_cap(None, owner_id="a@b.c", plan="pro") is None


async def test_les_credits_achetes_relevent_le_plafond():
    """Le modele est le rechargement : plafond du plan + credits restants."""
    async def topup(db, owner_id):
        return 250_000

    register_quota_topup(topup)
    try:
        assert await default_llm_cap(None, owner_id="a@b.c", plan="free") == 350_000
    finally:
        from apowerb.core import usage_quota as quota

        quota._topup_providers.remove(topup)


async def test_un_plan_illimite_le_reste_malgre_les_credits():
    """Etendre l'infini ne veut rien dire, et ferait apparaitre un quota."""
    async def topup(db, owner_id):
        return 250_000

    class _Unlimited:
        default_llm_monthly_token_quota = 0
        default_llm_plan_quotas = {}

    register_quota_topup(topup)
    try:
        import apowerb.core.usage_quota as quota

        original = quota.get_settings
        quota.get_settings = lambda: _Unlimited()
        try:
            assert await default_llm_cap(None, owner_id="a@b.c", plan="pro") is None
        finally:
            quota.get_settings = original
    finally:
        from apowerb.core import usage_quota as q

        q._topup_providers.remove(topup)


async def test_le_meme_chiffre_que_le_garde():
    """La jauge et le garde ne peuvent pas diverger : meme source, meme total."""
    cap = await default_llm_cap(None, owner_id="a@b.c", plan="pro")
    status = build_status(used=0, plan="pro", now=None, topup_tokens=0)
    assert cap == status.limit_tokens
