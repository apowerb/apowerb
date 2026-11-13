"""``tool_get_dashboard_data`` doit lire un dashboard spec v2.

Un dashboard créé depuis un gabarit BI range ses composants dans ``visual``.
La lecture ne regardait que ``chart`` et ``key_value`` : elle sautait tout et
renvoyait ``components: []``, et l'agent répondait que le dashboard était vide.
Moteur et base sont simulés (fixture ``env`` de ``test_bi_spec_tools``).
"""
import asyncio
import contextlib

import pytest

import apowerb.bi.dashboards.service as dash_service
import apowerb.bi.db_stores as db_stores
import apowerb.bi.charts.service as chart_service
import apowerb.bi.data.service as data_service
import apowerb.tools_store.portfolio.business_intelligence as bi
from tests.test_bi_spec_tools import ALICE, BOB, FakeStore, _create, env  # noqa: F401  (fixture)


@pytest.fixture
def base_simulee(env, monkeypatch):
    """La lecture classique lit la même table simulée que les outils spec v2."""

    @contextlib.asynccontextmanager
    async def session():
        yield None

    monkeypatch.setattr(bi, "_get_session", session)
    monkeypatch.setattr(db_stores, "DatabaseDashboardStore", lambda _db, owner: FakeStore(env.db, owner))
    monkeypatch.setattr(db_stores, "DatabaseChartStore", lambda *_a, **_kw: None)
    monkeypatch.setattr(dash_service, "DashboardService", lambda store: store)
    monkeypatch.setattr(chart_service, "ChartService", lambda *_a, **_kw: None)
    monkeypatch.setattr(data_service, "ChartDataService", lambda *_a, **_kw: None)
    return env


def _lire(dash_id, owner):
    return asyncio.run(bi._async_get_dashboard_data(dashboard_id=dash_id, owner_email=owner))


def test_un_dashboard_spec_v2_rend_ses_valeurs(base_simulee):
    dash_id = _create()["dashboard_id"]

    res = _lire(dash_id, ALICE)

    assert res["success"] is True
    assert res["dashboard_title"] == "Test"
    assert sorted(c["type"] for c in res["components"]) == ["bar", "hbar", "kpi"]
    kpi = next(c for c in res["components"] if c["type"] == "kpi")
    assert kpi["value"] == "98,7 %"
    assert base_simulee.computed[-1]["owner"] == ALICE


def test_un_autre_proprietaire_ne_recoit_pas_les_valeurs(base_simulee):
    dash_id = _create()["dashboard_id"]
    calculs = len(base_simulee.computed)

    with pytest.raises(Exception):
        _lire(dash_id, BOB)
    assert len(base_simulee.computed) == calculs, "aucun calcul pour un dashboard d'un autre propriétaire"
