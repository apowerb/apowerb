"""Colonnes d'un jeu de donnees renommees : un DeriveStep prepose sur la source
dataset alimente le gabarit sans reecrire le reste de la spec."""

from __future__ import annotations

import pytest

from apowerb.bi.dashboards.core import ComponentType, Dashboard, DashboardComponent, GridPosition
from apowerb.bi.spec import engine, sources
from apowerb.bi.spec.models import DashboardDataRequest, DeriveStep, FilterSpec, SourceSpec, VisualSpec
from apowerb.bi.spec.templates import stock_pilotage
from apowerb.bi.spec.validate import validate_spec
from tests.helpers.bi_spec_data import weekly_sales

OWNER = "owner@example.com"
RENAMED = {"semaine": "week", "famille": "product_group", "ventes": "qty"}


def _dashboard(spec: dict) -> Dashboard:
    return Dashboard.create(
        "Pilotage",
        sources=[SourceSpec.model_validate(s) for s in spec["sources"]],
        filters=[FilterSpec.model_validate(f) for f in spec["filters"]],
        components=[
            DashboardComponent(
                component_type=ComponentType.VISUAL,
                position=GridPosition(**c["position"]),
                visual=VisualSpec.model_validate(c["visual"]),
            )
            for c in spec["components"]
        ],
        spec_version=2,
        created_by=OWNER,
    )


@pytest.fixture(autouse=True)
def _fresh_caches():
    engine.clear_caches()
    yield
    engine.clear_caches()


async def test_prepended_derive_on_renamed_columns_computes_every_component(monkeypatch):
    frame = weekly_sales(weeks=130).rename(columns=RENAMED)

    async def fake_load(dataset_id, owner, sheet=None):
        return frame

    monkeypatch.setattr(sources, "load_dataset", fake_load)
    spec = stock_pilotage("ds-1")
    derives = [DeriveStep(column=model, expr=f'"{user}"').model_dump() for model, user in RENAMED.items()]
    spec["sources"][0]["transforms"] = [*derives, *spec["sources"][0]["transforms"]]
    dashboard = _dashboard(spec)

    assert validate_spec(dashboard) == []
    data = await engine.compute_dashboard(dashboard, DashboardDataRequest(), owner=OWNER)
    broken = {cid: r.error for cid, r in data.components.items() if r.error}
    assert broken == {}
    kpis = [data.components[c.id].value for c in dashboard.components if c.visual and c.visual.type == "kpi"]
    assert kpis and all(k is not None and k.value is not None for k in kpis)
