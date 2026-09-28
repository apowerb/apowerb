"""``make_bi_tools`` doit lire un dashboard au nom de l'``owner_email`` qu'il a lie.

La closure ``tool_get_dashboard_data`` lisait le proprietaire dans ``AGENT_OWNER``
au lieu de l'``owner_email`` injecte par la fabrique, contrairement a toutes les
autres closures. Sans la variable, le proprietaire arrivait vide et
``_BaseBIStore._base_filters`` n'ajoutait aucun filtre : lecture hors perimetre.
"""
import pytest

import apowerb.tools_store.portfolio.business_intelligence as bi


@pytest.fixture
def proprietaires_transmis(monkeypatch):
    recus: list[str] = []

    async def faux_lecteur(dashboard_id: str, owner_email: str, **_kw) -> dict:
        recus.append(owner_email)
        return {"success": True, "dashboard_id": dashboard_id}

    monkeypatch.setattr(bi, "_async_get_dashboard_data", faux_lecteur)
    return recus


def _outil_lecture(owner_email: str):
    outils = bi.make_bi_tools("agent1", owner_email, "org1", "proj1")
    return next(t for t in outils if t.__name__ == "tool_get_dashboard_data")


def test_sans_agent_owner_le_proprietaire_lie_est_transmis(monkeypatch, proprietaires_transmis):
    monkeypatch.delenv("AGENT_OWNER", raising=False)
    resultat = _outil_lecture("alice@example.com")(dashboard_id="d1")
    assert resultat["success"] is True
    assert proprietaires_transmis == ["alice@example.com"]


def test_le_proprietaire_lie_prime_sur_agent_owner(monkeypatch, proprietaires_transmis):
    monkeypatch.setenv("AGENT_OWNER", "bob@example.com")
    _outil_lecture("alice@example.com")(dashboard_id="d1")
    assert proprietaires_transmis == ["alice@example.com"]
