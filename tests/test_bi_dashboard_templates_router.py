"""Creation d'un tableau de bord depuis un gabarit : GET /dashboard-templates et
POST /dashboard-templates/{id}/dashboards. Le moteur est reel ; seuls le chargement du
jeu de donnees et le store (avec sa contrainte d'unicite de nom) sont remplaces."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from apowerb.bi.dashboards.core import Dashboard, DashboardStatus
from apowerb.bi.dashboards.service import DashboardService, InMemoryDashboardStore
from apowerb.bi.spec import engine, sources
from apowerb.bi.spec.errors import SpecError
from apowerb.bi.spec.router import router as spec_router
from tests.helpers.bi_spec_data import weekly_sales
from tests.helpers.bi_template_env import stock_frame

ALICE = "alice@example.com"
BOB = "bob@example.com"
DATASET = "22222222-2222-2222-2222-222222222222"
OTHER_DATASET = "33333333-3333-3333-3333-333333333333"
URL = "/api/v1/dashboard-templates"

EXPECTED_LIST = [
    {
        "id": "stock_pilotage",
        "label": "Pilotage stocks",
        "version": 1,
        "origin": "builtin",
        "owner": None,
        "visibility": None,
        "can_delete": False,
        "columns": [
            {"key": "date", "label": "Semaine", "type": "date", "required": True, "default": "semaine"},
            {"key": "family", "label": "Famille", "type": "string", "required": True, "default": "famille"},
            {"key": "sales", "label": "Ventes", "type": "number", "required": True, "default": "ventes"},
            {"key": "site", "label": "Site", "type": "string", "required": False, "default": None},
            {"key": "stock", "label": "Stock", "type": "number", "required": False, "default": "stock"},
            {"key": "safety", "label": "Seuil de sécurité", "type": "number", "required": False,
             "default": "seuil_securite"},
            {"key": "lead_time", "label": "Délai fournisseur (semaines)", "type": "number", "required": False,
             "default": "delai_fournisseur_sem"},
        ],
        "params": [
            {"key": "horizon", "label": "Horizon de prévision (semaines)", "type": "integer",
             "default": 8, "min": 1, "max": 26},
        ],
    }
]


class UniqueScopedStore(InMemoryDashboardStore):
    """Store partage, cloisonne par proprietaire, avec l'unicite (proprietaire, titre) de la base."""

    def __init__(self, shared: dict, owner: str) -> None:
        self._data = shared
        self._owner = owner

    async def get(self, dashboard_id):
        d = self._data.get(dashboard_id)
        return d if d and (d.created_by or "").lower() == self._owner.lower() else None

    async def save(self, dashboard):
        taken = any(
            d.created_by == dashboard.created_by and d.title == dashboard.title and d.id != dashboard.id
            for d in self._data.values()
        )
        if taken:
            raise IntegrityError("INSERT", {}, Exception("duplicate key value violates unique constraint"))
        self._data[dashboard.id] = dashboard
        return dashboard

    async def get_by_slug(self, slug):
        return next((d for d in self._data.values() if d.slug == slug), None)


def _user(email):
    user = MagicMock()
    user.email = email
    return user


class Env:
    def __init__(self) -> None:
        self.shared: dict[str, Dashboard] = {}
        self.owners_seen: list[str] = []
        self.frame = weekly_sales(weeks=130).rename(
            columns={"semaine": "week", "famille": "product_group", "ventes": "qty"}
        )

    def client(self, viewer: str = ALICE) -> TestClient:
        from apowerb.auth.dependencies import get_current_user
        from apowerb.bi.dependencies import get_dashboard_service
        from apowerb.helpers.database import get_db

        app = FastAPI()
        app.include_router(spec_router, prefix="/api/v1")

        no_user_templates = MagicMock()
        no_user_templates.scalars.return_value.all.return_value = []

        async def fake_db():
            session = AsyncMock()
            session.execute.return_value = no_user_templates
            yield session

        app.dependency_overrides[get_db] = fake_db
        app.dependency_overrides[get_current_user] = lambda: _user(viewer)
        app.dependency_overrides[get_dashboard_service] = lambda: DashboardService(
            UniqueScopedStore(self.shared, viewer)
        )
        return TestClient(app)

    def patched(self):
        env = self

        async def fake_load(dataset_id, owner, sheet=None):
            env.owners_seen.append(owner)
            if dataset_id != DATASET or owner != ALICE:
                raise SpecError(f"Jeu de données introuvable : {dataset_id}")
            return env.frame

        return patch.object(sources, "load_dataset", fake_load)


@pytest.fixture(autouse=True)
def _fresh_caches():
    engine.clear_caches()
    yield
    engine.clear_caches()


@pytest.fixture
def env():
    return Env()


COLUMNS = {"date": "week", "family": "product_group", "sales": "qty", "site": None}


def _create(env, body=None, viewer=ALICE, template="stock_pilotage"):
    payload = {"dataset_id": DATASET, "columns": COLUMNS, "params": {"horizon": 8}}
    payload.update(body or {})
    with env.patched():
        return env.client(viewer).post(f"{URL}/{template}/dashboards", json=payload)


def test_list_templates_contract(env):
    resp = env.client().get(URL)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    descriptions = [t.pop("description") for t in body]
    assert body[: len(EXPECTED_LIST)] == EXPECTED_LIST
    assert all(isinstance(d, str) and len(d) > 20 for d in descriptions)


def test_create_with_non_default_columns_computes_every_component(env):
    resp = _create(env)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert set(body) == {"dashboard_id", "title", "url"}
    assert body["title"] == "Pilotage stocks"
    assert body["url"] == f"/bi/{body['dashboard_id']}"

    saved = env.shared[body["dashboard_id"]]
    assert saved.created_by == ALICE and saved.status == DashboardStatus.DRAFT
    assert saved.spec_version == 2 and saved.sources[0].dataset_id == DATASET

    with env.patched():
        data = env.client().post(f"/api/v1/dashboards/{saved.id}/data", json={}).json()
    broken = {cid: r["error"] for cid, r in data["components"].items() if r.get("error")}
    assert broken == {}
    kpis = [data["components"][c.id]["value"] for c in saved.components if c.visual.type == "kpi"]
    assert len(kpis) == 6 and all(v["value"] is not None for v in kpis)
    assert ALICE in env.owners_seen


def test_custom_title_and_horizon_are_applied(env):
    resp = _create(env, {"title": "Mes stocks", "params": {"horizon": 12}})
    assert resp.status_code == 201, resp.text
    saved = env.shared[resp.json()["dashboard_id"]]
    assert saved.title == "Mes stocks"
    future = next(s for s in saved.sources if s.name == "prevision_future")
    assert future.transforms[0].horizon == 12


def test_missing_column_is_422_and_names_the_column(env):
    resp = _create(env, {"columns": {**COLUMNS, "sales": "ventes_inconnues"}})
    assert resp.status_code == 422, resp.text
    errors = resp.json()["detail"]["errors"]
    assert any("ventes_inconnues" in e for e in errors)
    assert env.shared == {}


def test_missing_optional_site_column_is_422(env):
    resp = _create(env, {"columns": {**COLUMNS, "site": "entrepot"}})
    assert resp.status_code == 422
    assert any("entrepot" in e for e in resp.json()["detail"]["errors"])


def test_invalid_column_identifier_is_422(env):
    resp = _create(env, {"columns": {**COLUMNS, "date": "Week"}})
    assert resp.status_code == 422
    assert any("Week" in e for e in resp.json()["detail"]["errors"])
    assert env.shared == {}


def test_required_column_not_given_is_422(env):
    resp = _create(env, {"columns": {"date": "week", "family": "product_group"}})
    assert resp.status_code == 422
    assert any("sales" in e or "Ventes" in e for e in resp.json()["detail"]["errors"])


def test_horizon_out_of_range_is_422(env):
    resp = _create(env, {"params": {"horizon": 40}})
    assert resp.status_code == 422
    assert resp.json()["detail"]["errors"]
    assert env.shared == {}


def test_unknown_template_is_404(env):
    assert _create(env, template="inconnu").status_code == 404
    assert env.shared == {}


def test_unknown_or_foreign_dataset_is_404(env):
    assert _create(env, {"dataset_id": OTHER_DATASET}).status_code == 404
    assert _create(env, viewer=BOB).status_code == 404
    assert env.shared == {}


def test_duplicate_title_gets_a_number_in_the_store(env):
    titles = [_create(env).json()["title"] for _ in range(3)]
    assert titles == ["Pilotage stocks", "Pilotage stocks (2)", "Pilotage stocks (3)"]
    assert sorted(d.title for d in env.shared.values()) == titles


def test_creation_records_the_template_provenance(env):
    resp = _create(env, {"params": {"horizon": 12}})
    assert resp.status_code == 201, resp.text
    saved = env.shared[resp.json()["dashboard_id"]]
    assert saved.template.model_dump() == {
        "id": "stock_pilotage",
        "version": 1,
        "columns": {"date": "week", "family": "product_group", "sales": "qty", "site": None,
                    "stock": None, "safety": None, "lead_time": None},
        "params": {"horizon": 12},
    }


def test_creation_with_a_stock_column_builds_the_real_stock_spec(env):
    env.frame = stock_frame(renamed=True)
    columns = {**COLUMNS, "stock": "on_hand", "safety": "min_level", "lead_time": "lead_weeks"}
    resp = _create(env, {"columns": columns})
    assert resp.status_code == 201, resp.text
    saved = env.shared[resp.json()["dashboard_id"]]
    assert [s.name for s in saved.sources][:3] == ["stocks", "plan", "horizon"]
    assert any(c.visual.title == "Stock actuel" for c in saved.components)
    with env.patched():
        data = env.client().post(f"/api/v1/dashboards/{saved.id}/data", json={}).json()
    assert {cid: r["error"] for cid, r in data["components"].items() if r.get("error")} == {}
    stock_kpi = next(c for c in saved.components if c.visual.title == "Stock actuel")
    assert data["components"][stock_kpi.id]["value"]["value"] is not None


def test_stock_column_missing_from_the_file_is_422(env):
    resp = _create(env, {"columns": {**COLUMNS, "stock": "on_hand"}})
    assert resp.status_code == 422
    assert any("on_hand" in e for e in resp.json()["detail"]["errors"])
