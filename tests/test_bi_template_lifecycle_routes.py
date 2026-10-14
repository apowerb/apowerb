"""Cycle de vie d'un tableau issu d'un modele : exigences de colonnes, remplacement
du fichier, mise a niveau. Base SQLite reelle, moteur reel, seul le chargement du
jeu de donnees est remplace."""

from __future__ import annotations

import dataclasses

from apowerb.bi.dashboards.core import DashboardStatus, DashboardVisibility
from apowerb.bi.spec import catalog
from apowerb.bi.spec.templates import stock_pilotage
from tests.helpers.bi_template_env import (
    ALICE,
    BOB,
    dashboard_from_spec,
    db_env,
    sales_frame,
    stock_frame,
)

URL = "/api/v1"
DS = "cccccccc-cccc-cccc-cccc-cccccccccccc"
DS2 = "dddddddd-dddd-dddd-dddd-dddddddddddd"
NATIVE = {"date": "week", "family": "product_group", "sales": "qty"}
STOCK_NATIVE = {**NATIVE, "stock": "on_hand", "safety": "min_level", "lead_time": "lead_weeks"}


async def _create(env, columns, frame, dataset=DS, owner=ALICE) -> str:
    env.datasets[dataset] = (owner, frame)
    with env.patched():
        async with env.client(owner) as client:
            resp = await client.post(
                f"{URL}/dashboard-templates/stock_pilotage/dashboards",
                json={"dataset_id": dataset, "columns": columns, "params": {"horizon": 8}},
            )
    assert resp.status_code == 201, resp.text
    return resp.json()["dashboard_id"]


async def _errors(env, dashboard_id, viewer=ALICE):
    with env.patched():
        async with env.client(viewer) as client:
            data = (await client.post(f"{URL}/dashboards/{dashboard_id}/data", json={})).json()
    return {cid: r["error"] for cid, r in data["components"].items() if r.get("error")}, data


async def _post(env, path, body, viewer=ALICE):
    with env.patched():
        async with env.client(viewer) as client:
            return await client.post(f"{URL}{path}", json=body)


async def _get(env, path, viewer=ALICE):
    async with env.client(viewer) as client:
        return await client.get(f"{URL}{path}")


def _renamed_again(frame):
    return frame.rename(columns={"week": "periode", "product_group": "groupe", "qty": "volume"})


# ---------------------------------------------------------------------------
# GET /dashboards/{id}/source-requirements
# ---------------------------------------------------------------------------


async def test_requirements_with_provenance_list_the_template_columns_and_current_mapping():
    async with db_env() as env:
        dashboard_id = await _create(env, NATIVE, sales_frame(renamed=True))
        resp = await _get(env, f"/dashboards/{dashboard_id}/source-requirements")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert (body["source"], body["dataset_id"]) == ("stocks", DS)
        assert body["template"]["id"] == "stock_pilotage" and body["template"]["version"] == 1
        assert [(c["key"], c["required"], c["current"]) for c in body["columns"]] == [
            ("date", True, "week"), ("family", True, "product_group"), ("sales", True, "qty"),
            ("site", False, None), ("stock", False, None), ("safety", False, None), ("lead_time", False, None),
        ]
        assert {c["type"] for c in body["columns"]} == {"date", "string", "number"}


async def test_requirements_without_provenance_list_the_real_columns_of_the_file_source():
    async with db_env() as env:
        dashboard = await env.save(dashboard_from_spec(stock_pilotage(DS), ALICE))
        body = (await _get(env, f"/dashboards/{dashboard.id}/source-requirements")).json()
        assert body["template"] is None
        assert [(c["key"], c["label"], c["type"], c["required"], c["current"]) for c in body["columns"]] == [
            ("semaine", "Semaine", "date", True, "semaine"),
            ("famille", "Famille", "string", True, "famille"),
            ("ventes", "Ventes", "number", True, "ventes"),
        ]


async def test_requirements_422_without_file_source_and_404_for_someone_else():
    async with db_env() as env:
        from apowerb.bi.dashboards.core import Dashboard

        legacy = await env.save(Dashboard.create("Ancien", created_by=ALICE))
        resp = await _get(env, f"/dashboards/{legacy.id}/source-requirements")
        assert resp.status_code == 422 and resp.json()["detail"]["errors"]
        dashboard = await env.save(dashboard_from_spec(stock_pilotage(DS), ALICE, title="Autre"))
        assert (await _get(env, f"/dashboards/{dashboard.id}/source-requirements", viewer=BOB)).status_code == 404


# ---------------------------------------------------------------------------
# POST /dashboards/{id}/source
# ---------------------------------------------------------------------------


async def test_replace_file_keeps_the_dashboard_and_recomputes_on_the_new_file():
    async with db_env() as env:
        dashboard_id = await _create(env, NATIVE, sales_frame(renamed=True))
        before = await env.load(dashboard_id)
        await env.save(before.model_copy(update={
            "status": DashboardStatus.PUBLISHED, "visibility": DashboardVisibility.ORGANIZATION, "slug": "mon-slug"}))
        env.datasets[DS2] = (ALICE, _renamed_again(sales_frame(renamed=True)))
        columns = {"date": "periode", "family": "groupe", "sales": "volume"}
        resp = await _post(env, f"/dashboards/{dashboard_id}/source", {"dataset_id": DS2, "columns": columns})
        assert resp.status_code == 200, resp.text
        assert resp.json()["missing"] == [] and resp.json()["dashboard"]["id"] == dashboard_id

        after = await env.load(dashboard_id)
        assert (after.title, after.status, after.visibility, after.slug) == (
            before.title, DashboardStatus.PUBLISHED, DashboardVisibility.ORGANIZATION, "mon-slug")
        assert [c.id for c in after.components] == [c.id for c in before.components]
        assert after.sources[0].dataset_id == DS2
        assert after.template.columns == {**columns, "site": None, "stock": None, "safety": None, "lead_time": None}
        errors, _ = await _errors(env, dashboard_id)
        assert errors == {}


async def test_replace_file_twice_still_computes():
    async with db_env() as env:
        dashboard_id = await _create(env, NATIVE, sales_frame(renamed=True))
        env.datasets[DS2] = (ALICE, _renamed_again(sales_frame(renamed=True)))
        first = {"date": "periode", "family": "groupe", "sales": "volume"}
        assert (await _post(env, f"/dashboards/{dashboard_id}/source", {"dataset_id": DS2, "columns": first})).status_code == 200
        env.datasets[DS] = (ALICE, sales_frame(renamed=True))
        resp = await _post(env, f"/dashboards/{dashboard_id}/source", {"dataset_id": DS, "columns": NATIVE})
        assert resp.status_code == 200, resp.text
        assert (await env.load(dashboard_id)).template.columns["date"] == "week"
        errors, _ = await _errors(env, dashboard_id)
        assert errors == {}


async def test_replace_file_with_same_column_names_needs_no_mapping():
    async with db_env() as env:
        dashboard_id = await _create(env, NATIVE, sales_frame(renamed=True))
        env.datasets[DS2] = (ALICE, sales_frame(weeks=100, renamed=True))
        resp = await _post(env, f"/dashboards/{dashboard_id}/source", {"dataset_id": DS2})
        assert resp.status_code == 200, resp.text
        errors, _ = await _errors(env, dashboard_id)
        assert errors == {}


async def test_replace_file_missing_a_required_column_is_422_and_changes_nothing():
    async with db_env() as env:
        dashboard_id = await _create(env, NATIVE, sales_frame(renamed=True))
        env.datasets[DS2] = (ALICE, sales_frame(renamed=True).drop(columns=["qty"]))
        resp = await _post(env, f"/dashboards/{dashboard_id}/source", {"dataset_id": DS2})
        assert resp.status_code == 422, resp.text
        message = " ".join(resp.json()["detail"]["errors"])
        assert "qty" in message and "week" in message and "product_group" in message
        assert (await env.load(dashboard_id)).sources[0].dataset_id == DS


async def test_replace_file_dry_run_does_not_save():
    async with db_env() as env:
        dashboard_id = await _create(env, NATIVE, sales_frame(renamed=True))
        env.datasets[DS2] = (ALICE, sales_frame(renamed=True))
        resp = await _post(env, f"/dashboards/{dashboard_id}/source", {"dataset_id": DS2, "dry_run": True})
        assert resp.status_code == 200
        assert resp.json()["dashboard"]["sources"][0]["dataset_id"] == DS2
        assert (await env.load(dashboard_id)).sources[0].dataset_id == DS


async def test_replace_file_without_provenance_maps_the_current_names_to_the_new_columns():
    async with db_env() as env:
        env.datasets[DS] = (ALICE, sales_frame())
        dashboard = await env.save(dashboard_from_spec(stock_pilotage(DS), ALICE))
        env.datasets[DS2] = (ALICE, sales_frame(renamed=True))
        resp = await _post(env, f"/dashboards/{dashboard.id}/source", {
            "dataset_id": DS2, "columns": {"semaine": "week", "famille": "product_group", "ventes": "qty"}})
        assert resp.status_code == 200, resp.text
        saved = await env.load(dashboard.id)
        assert saved.template is None and saved.sources[0].dataset_id == DS2
        errors, _ = await _errors(env, dashboard.id)
        assert errors == {}
        body = (await _get(env, f"/dashboards/{dashboard.id}/source-requirements")).json()
        assert [(c["key"], c["current"]) for c in body["columns"]] == [
            ("semaine", "week"), ("famille", "product_group"), ("ventes", "qty")]


async def test_replace_file_keeps_the_site_column_of_a_multi_site_dashboard():
    async with db_env() as env:
        sites = ("nord", "sud")
        dashboard_id = await _create(env, {**NATIVE, "site": "site"}, sales_frame(renamed=True, sites=sites))
        env.datasets[DS2] = (ALICE, sales_frame(weeks=100, renamed=True, sites=sites))
        resp = await _post(env, f"/dashboards/{dashboard_id}/source", {"dataset_id": DS2})
        assert resp.status_code == 200, resp.text
        assert (await env.load(dashboard_id)).template.columns["site"] == "site"
        errors, _ = await _errors(env, dashboard_id)
        assert errors == {}
        assert (await _post(env, f"/dashboards/{dashboard_id}/upgrade", {})).status_code == 200
        upgraded = await env.load(dashboard_id)
        assert upgraded.sources[0].transforms[0].op == "aggregate"
        errors, _ = await _errors(env, dashboard_id)
        assert errors == {}


async def test_replace_file_of_a_real_stock_dashboard_keeps_the_three_sources():
    async with db_env() as env:
        dashboard_id = await _create(env, STOCK_NATIVE, stock_frame(renamed=True))
        before = await env.load(dashboard_id)
        names = [s.name for s in before.sources]
        assert names[:3] == ["stocks", "plan", "horizon"]
        env.datasets[DS2] = (ALICE, stock_frame(weeks=70, renamed=True))
        resp = await _post(env, f"/dashboards/{dashboard_id}/source", {"dataset_id": DS2})
        assert resp.status_code == 200, resp.text
        after = await env.load(dashboard_id)
        assert [s.name for s in after.sources] == names
        assert [s.dataset_id for s in after.sources if s.kind == "dataset"] == [DS2]
        errors, data = await _errors(env, dashboard_id)
        assert errors == {}
        horizon = next(c for c in after.components if c.visual.source == "horizon")
        assert data["components"][horizon.id].get("error") is None


async def test_replace_file_of_someone_elses_dashboard_or_dataset_is_404():
    async with db_env() as env:
        dashboard_id = await _create(env, NATIVE, sales_frame(renamed=True))
        env.datasets[DS2] = (ALICE, sales_frame(renamed=True))
        assert (await _post(env, f"/dashboards/{dashboard_id}/source", {"dataset_id": DS2}, viewer=BOB)).status_code == 404
        env.datasets[DS2] = (BOB, sales_frame(renamed=True))
        assert (await _post(env, f"/dashboards/{dashboard_id}/source", {"dataset_id": DS2})).status_code == 404


# ---------------------------------------------------------------------------
# POST /dashboards/{id}/upgrade
# ---------------------------------------------------------------------------


def _bumped(monkeypatch, version=2):
    template = dataclasses.replace(catalog._TEMPLATES["stock_pilotage"], version=version)
    monkeypatch.setattr(catalog, "_TEMPLATES", {template.id: template})


async def test_upgrade_rebuilds_from_the_current_version_and_keeps_identity(monkeypatch):
    async with db_env() as env:
        dashboard_id = await _create(env, NATIVE, sales_frame(renamed=True))
        built = await env.load(dashboard_id)
        edited = built.model_copy(update={
            "components": built.components[:3], "status": DashboardStatus.PUBLISHED,
            "visibility": DashboardVisibility.ORGANIZATION, "slug": "mon-slug"})
        await env.save(edited)
        _bumped(monkeypatch)
        resp = await _post(env, f"/dashboards/{dashboard_id}/upgrade", {})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert (body["from_version"], body["to_version"]) == (1, 2)
        after = await env.load(dashboard_id)
        assert len(after.components) == len(built.components) > 3
        assert (after.id, after.title, after.status, after.visibility, after.slug, after.created_by) == (
            built.id, built.title, DashboardStatus.PUBLISHED, DashboardVisibility.ORGANIZATION, "mon-slug", ALICE)
        assert after.template.version == 2 and after.template.columns["date"] == "week"
        errors, _ = await _errors(env, dashboard_id)
        assert errors == {}


async def test_upgrade_of_a_real_stock_dashboard_keeps_the_three_sources():
    async with db_env() as env:
        dashboard_id = await _create(env, STOCK_NATIVE, stock_frame(renamed=True))
        built = await env.load(dashboard_id)
        await env.save(built.model_copy(update={"components": built.components[:2]}))
        resp = await _post(env, f"/dashboards/{dashboard_id}/upgrade", {})
        assert resp.status_code == 200, resp.text
        after = await env.load(dashboard_id)
        assert [s.name for s in after.sources][:3] == ["stocks", "plan", "horizon"]
        errors, data = await _errors(env, dashboard_id)
        assert errors == {}
        horizon = next(c for c in after.components if c.visual.source == "horizon")
        assert data["components"][horizon.id].get("error") is None


async def test_upgrade_after_a_file_replacement_keeps_the_new_file():
    async with db_env() as env:
        dashboard_id = await _create(env, NATIVE, sales_frame(renamed=True))
        env.datasets[DS2] = (ALICE, _renamed_again(sales_frame(renamed=True)))
        columns = {"date": "periode", "family": "groupe", "sales": "volume"}
        await _post(env, f"/dashboards/{dashboard_id}/source", {"dataset_id": DS2, "columns": columns})
        assert (await _post(env, f"/dashboards/{dashboard_id}/upgrade", {})).status_code == 200
        after = await env.load(dashboard_id)
        assert after.sources[0].dataset_id == DS2
        errors, _ = await _errors(env, dashboard_id)
        assert errors == {}


async def test_upgrade_attaches_a_dashboard_without_provenance_to_a_template():
    async with db_env() as env:
        env.datasets[DS] = (ALICE, sales_frame(renamed=True))
        dashboard = await env.save(dashboard_from_spec(stock_pilotage(DS, date="week", family="product_group", sales="qty"), ALICE))
        assert dashboard.template is None
        resp = await _post(env, f"/dashboards/{dashboard.id}/upgrade", {
            "template_id": "stock_pilotage", "columns": NATIVE, "params": {"horizon": 12}})
        assert resp.status_code == 200, resp.text
        assert resp.json()["from_version"] is None and resp.json()["to_version"] == 1
        after = await env.load(dashboard.id)
        assert after.template.id == "stock_pilotage" and after.template.params == {"horizon": 12}
        assert after.id == dashboard.id and after.title == dashboard.title
        errors, _ = await _errors(env, dashboard.id)
        assert errors == {}


async def test_upgrade_dry_run_does_not_save(monkeypatch):
    async with db_env() as env:
        dashboard_id = await _create(env, NATIVE, sales_frame(renamed=True))
        _bumped(monkeypatch)
        resp = await _post(env, f"/dashboards/{dashboard_id}/upgrade", {"dry_run": True})
        assert resp.status_code == 200 and resp.json()["to_version"] == 2
        assert (await env.load(dashboard_id)).template.version == 1


async def test_upgrade_errors_422_404():
    async with db_env() as env:
        env.datasets[DS] = (ALICE, sales_frame(renamed=True))
        dashboard = await env.save(dashboard_from_spec(stock_pilotage(DS, date="week", family="product_group", sales="qty"), ALICE))
        resp = await _post(env, f"/dashboards/{dashboard.id}/upgrade", {})
        assert resp.status_code == 422 and resp.json()["detail"]["errors"]
        assert (await _post(env, f"/dashboards/{dashboard.id}/upgrade", {"template_id": "inconnu", "columns": NATIVE})).status_code == 404
        missing = await _post(env, f"/dashboards/{dashboard.id}/upgrade", {
            "template_id": "stock_pilotage", "columns": {**NATIVE, "sales": "absente"}})
        assert missing.status_code == 422 and any("absente" in e for e in missing.json()["detail"]["errors"])
        blank = await _post(env, f"/dashboards/{dashboard.id}/upgrade", {"template_id": "stock_pilotage", "columns": {}})
        assert blank.status_code == 422
        assert (await _post(env, f"/dashboards/{dashboard.id}/upgrade", {
            "template_id": "stock_pilotage", "columns": NATIVE}, viewer=BOB)).status_code == 404


# ---------------------------------------------------------------------------
# Language: catalog, creation, upgrade
# ---------------------------------------------------------------------------


async def test_template_list_follows_the_requested_language():
    async with db_env() as env:
        english = {t["id"]: t["label"] for t in (await _get(env, "/dashboard-templates?lang=en")).json()}
        french = {t["id"]: t["label"] for t in (await _get(env, "/dashboard-templates")).json()}
        assert english["stock_pilotage"] == "Inventory steering" and french["stock_pilotage"] == "Pilotage stocks"
        assert (await _get(env, "/dashboard-templates?lang=de")).status_code == 422


async def test_english_dashboard_is_built_and_computed_in_english_and_keeps_it_on_upgrade(monkeypatch):
    async with db_env() as env:
        env.datasets[DS] = (ALICE, sales_frame(renamed=True))
        resp = await _post(env, "/dashboard-templates/stock_pilotage/dashboards",
                           {"dataset_id": DS, "columns": NATIVE, "lang": "en"})
        assert resp.status_code == 201, resp.text
        assert resp.json()["title"] == "Inventory steering"
        dashboard_id = resp.json()["dashboard_id"]
        built = await env.load(dashboard_id)
        assert built.locale == "en"
        assert any(c.visual.title == "Comparison by product family" for c in built.components)
        data = await _post(env, f"/dashboards/{dashboard_id}/data", {})
        assert data.status_code == 200, data.text
        assert data.json()["filters"]["famille"]["label"] == "All product families"

        _bumped(monkeypatch)
        upgraded = await _post(env, f"/dashboards/{dashboard_id}/upgrade", {})
        assert upgraded.status_code == 200, upgraded.text
        after = await env.load(dashboard_id)
        assert after.locale == "en"
        assert any(c.visual.title == "Comparison by product family" for c in after.components)


async def test_upgrade_with_lang_switches_the_dashboard_language_and_keeps_its_mapping():
    async with db_env() as env:
        env.datasets[DS] = (ALICE, sales_frame(renamed=True))
        resp = await _post(env, "/dashboard-templates/stock_pilotage/dashboards", {"dataset_id": DS, "columns": NATIVE})
        dashboard_id = resp.json()["dashboard_id"]
        before = await env.load(dashboard_id)
        assert before.locale == "fr"

        dry = await _post(env, f"/dashboards/{dashboard_id}/upgrade", {"lang": "en", "dry_run": True})
        assert dry.status_code == 200, dry.text
        assert dry.json()["dashboard"]["locale"] == "en"
        assert (await env.load(dashboard_id)).locale == "fr"

        switched = await _post(env, f"/dashboards/{dashboard_id}/upgrade", {"lang": "en"})
        assert switched.status_code == 200, switched.text
        after = await env.load(dashboard_id)
        assert after.locale == "en"
        assert after.title == before.title
        assert after.template.columns == before.template.columns
        assert any(c.visual.title == "Comparison by product family" for c in after.components)
        data = await _post(env, f"/dashboards/{dashboard_id}/data", {})
        assert data.json()["filters"]["famille"]["label"] == "All product families"

        back = await _post(env, f"/dashboards/{dashboard_id}/upgrade", {"lang": "fr"})
        assert back.status_code == 200, back.text
        assert (await env.load(dashboard_id)).locale == "fr"
        assert (await _post(env, f"/dashboards/{dashboard_id}/upgrade", {"lang": "de"})).status_code == 422
