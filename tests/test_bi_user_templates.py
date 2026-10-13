"""Modeles de tableaux utilisateur : creation depuis un tableau, utilisation,
visibilite, suppression. Base SQLite reelle, moteur reel."""

from __future__ import annotations

from apowerb.bi.dashboards.core import Dashboard
from tests.helpers.bi_template_env import ALICE, BOB, CAROL, dashboard_from_spec, db_env, sales_frame, stock_frame
from apowerb.bi.spec.templates import stock_pilotage

URL = "/api/v1"
DS_WEEKS = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
DS_OTHER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
NATIVE = {"date": "week", "family": "product_group", "sales": "qty"}


async def _stock_dashboard(env, owner=ALICE) -> str:
    """A 'Pilotage stocks' dashboard created through the catalogue on a file with renamed columns."""
    env.datasets[DS_WEEKS] = (owner, sales_frame(renamed=True))
    with env.patched():
        async with env.client(owner) as client:
            resp = await client.post(
                f"{URL}/dashboard-templates/stock_pilotage/dashboards",
                json={"dataset_id": DS_WEEKS, "columns": NATIVE, "params": {"horizon": 8}},
            )
    assert resp.status_code == 201, resp.text
    return resp.json()["dashboard_id"]


async def _make_model(env, dashboard_id, owner=ALICE, label="Mon modele", visibility="private"):
    async with env.client(owner) as client:
        return await client.post(
            f"{URL}/dashboards/{dashboard_id}/template",
            json={"label": label, "description": "d", "visibility": visibility},
        )


async def _list(env, viewer):
    async with env.client(viewer) as client:
        resp = await client.get(f"{URL}/dashboard-templates")
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _user_models(env, viewer):
    return [t for t in await _list(env, viewer) if t["origin"] == "user"]


async def test_model_created_from_a_dashboard_then_dashboard_created_from_the_model():
    async with db_env() as env:
        dashboard_id = await _stock_dashboard(env)
        resp = await _make_model(env, dashboard_id)
        assert resp.status_code == 201, resp.text
        model = resp.json()
        assert (model["origin"], model["owner"], model["visibility"], model["can_delete"], model["version"]) == (
            "user", ALICE, "private", True, 1,
        )
        assert model["label"] == "Mon modele" and model["description"] == "d" and model["params"] == []
        assert model["columns"] == [
            {"key": "week", "label": "Week", "type": "date", "required": True, "default": "week"},
            {"key": "product_group", "label": "Product group", "type": "string", "required": True,
             "default": "product_group"},
            {"key": "qty", "label": "Ventes", "type": "number", "required": True, "default": "qty"},
        ]

        env.datasets[DS_OTHER] = (ALICE, sales_frame().rename(
            columns={"semaine": "periode", "famille": "groupe", "ventes": "volume"}))
        with env.patched():
            async with env.client() as client:
                created = await client.post(
                    f"{URL}/dashboard-templates/{model['id']}/dashboards",
                    json={"dataset_id": DS_OTHER, "title": "Depuis modele",
                          "columns": {"week": "periode", "product_group": "groupe", "qty": "volume"}},
                )
                assert created.status_code == 201, created.text
                new_id = created.json()["dashboard_id"]
                data = (await client.post(f"{URL}/dashboards/{new_id}/data", json={})).json()
        assert {cid: r["error"] for cid, r in data["components"].items() if r.get("error")} == {}
        saved = await env.load(new_id)
        assert saved.title == "Depuis modele"
        assert saved.template.id == model["id"] and saved.template.version == 1
        assert saved.template.columns == {"week": "periode", "product_group": "groupe", "qty": "volume"}
        assert len(saved.components) == len((await env.load(dashboard_id)).components)


async def test_model_ignores_a_previous_rename_of_the_source_file():
    async with db_env() as env:
        spec = stock_pilotage("ds-x")
        spec["sources"][0]["transforms"] = [
            {"op": "derive", "column": "ventes", "expr": '"qty"'}, *spec["sources"][0]["transforms"]]
        await env.save(dashboard_from_spec(spec, ALICE))
        dashboard = dashboard_from_spec(spec, ALICE, title="Renomme")
        await env.save(dashboard)
        model = (await _make_model(env, dashboard.id)).json()
        by_key = {c["key"]: c for c in model["columns"]}
        assert by_key["ventes"]["default"] == "ventes"


async def test_label_already_taken_is_409():
    async with db_env() as env:
        dashboard_id = await _stock_dashboard(env)
        assert (await _make_model(env, dashboard_id)).status_code == 201
        resp = await _make_model(env, dashboard_id)
        assert resp.status_code == 409, resp.text
        assert len(await _user_models(env, ALICE)) == 1


async def test_dashboard_without_spec_or_with_two_files_is_422():
    async with db_env() as env:
        legacy = await env.save(Dashboard.create("Ancien", created_by=ALICE))
        assert (await _make_model(env, legacy.id)).status_code == 422
        spec = stock_pilotage("ds-1")
        two = dashboard_from_spec(spec, ALICE, title="Deux")
        extra = two.sources[0].model_copy(update={"name": "second", "transforms": [], "measures": []})
        two = two.model_copy(update={"sources": [*two.sources, extra]})
        await env.save(two)
        resp = await _make_model(env, two.id)
        assert resp.status_code == 422
        assert resp.json()["detail"]["errors"]


async def test_only_the_owner_of_the_dashboard_can_make_a_model():
    async with db_env() as env:
        dashboard_id = await _stock_dashboard(env)
        assert (await _make_model(env, dashboard_id, owner=BOB)).status_code == 404
        assert await _user_models(env, ALICE) == []


async def test_private_model_of_someone_else_is_invisible_and_unusable():
    async with db_env() as env:
        model = (await _make_model(env, await _stock_dashboard(env, owner=BOB), owner=BOB)).json()
        assert await _user_models(env, ALICE) == []
        assert [t["id"] for t in await _user_models(env, BOB)] == [model["id"]]
        env.datasets[DS_OTHER] = (ALICE, sales_frame(renamed=True))
        with env.patched():
            async with env.client(ALICE) as client:
                resp = await client.post(
                    f"{URL}/dashboard-templates/{model['id']}/dashboards", json={"dataset_id": DS_OTHER, "columns": NATIVE})
                assert resp.status_code == 404
                assert (await client.delete(f"{URL}/dashboard-templates/{model['id']}")).status_code == 404


async def test_organization_model_is_visible_to_the_same_domain_only():
    async with db_env() as env:
        model = (await _make_model(env, await _stock_dashboard(env, owner=BOB), owner=BOB,
                                   visibility="organization")).json()
        seen = {t["id"]: t for t in await _list(env, ALICE)}
        assert seen[model["id"]]["owner"] == BOB and seen[model["id"]]["visibility"] == "organization"
        assert seen[model["id"]]["can_delete"] is False
        assert model["id"] not in {t["id"] for t in await _list(env, CAROL)}


async def test_delete_model_204_then_403_404():
    async with db_env() as env:
        mine = (await _make_model(env, await _stock_dashboard(env))).json()
        theirs = (await _make_model(env, await _stock_dashboard(env, owner=BOB), owner=BOB,
                                    label="Org BOB", visibility="organization")).json()
        async with env.client(ALICE) as client:
            assert (await client.delete(f"{URL}/dashboard-templates/stock_pilotage")).status_code == 403
            assert (await client.delete(f"{URL}/dashboard-templates/{theirs['id']}")).status_code == 403
            assert (await client.delete(f"{URL}/dashboard-templates/inconnu")).status_code == 404
            assert (await client.delete(f"{URL}/dashboard-templates/{mine['id']}")).status_code == 204
            assert (await client.delete(f"{URL}/dashboard-templates/{mine['id']}")).status_code == 404
        assert mine["id"] not in {t["id"] for t in await _list(env, ALICE)}
        assert (await _make_model(env, await _stock_dashboard(env))).status_code == 201


async def test_model_of_a_stock_dashboard_keeps_the_real_stock_columns():
    async with db_env() as env:
        env.datasets[DS_WEEKS] = (ALICE, stock_frame())
        with env.patched():
            async with env.client() as client:
                resp = await client.post(
                    f"{URL}/dashboard-templates/stock_pilotage/dashboards",
                    json={"dataset_id": DS_WEEKS, "columns": {
                        "date": "semaine", "family": "famille", "sales": "ventes", "stock": "stock",
                        "safety": "seuil_securite", "lead_time": "delai_fournisseur_sem"}},
                )
        model = (await _make_model(env, resp.json()["dashboard_id"])).json()
        assert {c["key"] for c in model["columns"]} == {
            "semaine", "famille", "ventes", "stock", "seuil_securite", "delai_fournisseur_sem"}
