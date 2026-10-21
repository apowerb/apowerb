"""Aperçu d'un gabarit rendu avec les sorties du dernier run de test.

``POST /api/workflows/render-preview`` sert le panneau « Données disponibles »
du Studio : il montre l'Entrée d'un nœud telle que le moteur la rendrait, en
appelant ``render`` de ``workflow_graph`` sur les sorties que l'UI a gardées.
Aucun nœud n'est exécuté ; les erreurs ``template_ref_invalid`` sont celles du
run, pour que l'aperçu ne diverge jamais du moteur.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException, status
from fastapi.testclient import TestClient

URL = "/api/workflows/render-preview"
OUTPUTS = {
    "start": {"client": "Acme", "attachments": [{"name": "f.pdf", "size": 12}]},
    "lignes": [{"sku": "A1", "qty": 2}, {"sku": "B2", "qty": 5}],
    "redac": "Bonjour",
}


def _app(user_dep):
    from apowerb.auth.dependencies import get_current_user
    from apowerb.routers import workflows as workflows_module

    app = FastAPI()
    app.include_router(workflows_module.router, prefix="/api")
    app.dependency_overrides[get_current_user] = user_dep
    return TestClient(app)


@pytest.fixture()
def client():
    async def _user():
        u = MagicMock()
        u.email, u.user_id, u.role = "alice@exemple.test", 1, "USER"
        return u

    return _app(_user)


def test_text_template_is_rendered_like_the_engine(client):
    resp = client.post(
        URL,
        json={
            "template": "Client {{start.client}}, ligne {{lignes.1.sku}} : {{redac}}",
            "outputs": OUTPUTS,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"rendered": "Client Acme, ligne B2 : Bonjour"}


def test_object_inside_text_is_json_encoded(client):
    resp = client.post(
        URL, json={"template": "PJ {{start.attachments.0}}", "outputs": OUTPUTS}
    )
    assert resp.json()["rendered"] == 'PJ {"name": "f.pdf", "size": 12}'


def test_whole_template_keeps_the_value_shape(client):
    resp = client.post(URL, json={"template": "{{lignes}}", "outputs": OUTPUTS})
    assert resp.json() == {"rendered": OUTPUTS["lignes"]}


def test_dict_template_is_rendered_recursively(client):
    resp = client.post(
        URL,
        json={"template": {"sku": "{{lignes.0.sku}}", "n": 3}, "outputs": OUTPUTS},
    )
    assert resp.json() == {"rendered": {"sku": "A1", "n": 3}}


def test_missing_reference_renders_empty_without_error(client):
    resp = client.post(
        URL, json={"template": "[{{start.absent}}][{{ghost.x}}]", "outputs": OUTPUTS}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"rendered": "[][]"}


def test_path_through_a_scalar_returns_the_run_error(client):
    resp = client.post(
        URL,
        json={"template": "{{redac.text}}", "outputs": OUTPUTS, "node_id": "suivant"},
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "template_ref_invalid"
    assert detail["params"] == {"node": "suivant", "ref": "redac.text"}
    assert "redac" in detail["message"]


def test_oversized_body_is_refused(client):
    from apowerb.routers import workflows as workflows_module

    big = "x" * (workflows_module.RENDER_PREVIEW_MAX_BYTES + 1)
    resp = client.post(URL, json={"template": "{{a}}", "outputs": {"a": big}})
    assert resp.status_code == 413, resp.text
    assert resp.json()["detail"]["code"] == "render_preview_too_large"


def test_amplified_template_is_refused_before_rendering(client, monkeypatch):
    # Un petit corps peut demander un rendu géant : la même grande sortie
    # référencée des milliers de fois. Refusé avant tout rendu.
    from apowerb.core import workflow_graph

    def _boom(*_a, **_k):  # pragma: no cover - ne doit jamais être appelé
        raise AssertionError("le rendu géant ne doit pas être produit")

    monkeypatch.setattr(workflow_graph, "render", _boom)
    resp = client.post(
        URL, json={"template": "{{a}}" * 1000, "outputs": {"a": "x" * 10_000}}
    )
    assert resp.status_code == 413, resp.text
    assert resp.json()["detail"]["code"] == "render_preview_too_large"


def test_amplification_bound_counts_nested_templates(client):
    big = "y" * 300_000
    resp = client.post(
        URL,
        json={
            "template": {"a": "{{s}}", "b": ["{{s}}", "{{s.x}}"], "c": "{{s}}"},
            "outputs": {"s": big},
        },
    )
    assert resp.status_code == 413, resp.text


def test_reasonable_template_still_renders(client):
    resp = client.post(
        URL,
        json={"template": "{{a}} " * 20, "outputs": {"a": "z" * 10_000}},
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["rendered"]) == 20 * 10_001


def test_nothing_is_executed(client, monkeypatch):
    from apowerb.core import workflow_graph

    def _boom(*_a, **_k):  # pragma: no cover - ne doit jamais être appelé
        raise AssertionError("aucun nœud ne doit tourner")

    monkeypatch.setattr(workflow_graph, "run_graph", _boom)
    resp = client.post(URL, json={"template": "{{redac}}", "outputs": OUTPUTS})
    assert resp.json() == {"rendered": "Bonjour"}


def test_route_does_not_fall_into_cancel(client):
    # /{wid}/cancel a deux segments : render-preview ne doit pas y aboutir.
    resp = client.post(URL, json={"template": "ok", "outputs": {}})
    assert resp.status_code == 200
    assert resp.json() == {"rendered": "ok"}


def test_without_auth_returns_401():
    async def _deny():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated"
        )

    resp = _app(_deny).post(URL, json={"template": "x", "outputs": {}})
    assert resp.status_code == 401, resp.text
