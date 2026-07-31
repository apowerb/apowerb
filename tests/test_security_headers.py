"""Tests pour B8 / H4 — headers de sécurité systématiques.

Un middleware ``SecurityHeadersMiddleware`` doit ajouter sur TOUTES les
réponses HTTP :

- ``Content-Security-Policy`` : politique stricte par défaut
- ``Strict-Transport-Security`` : HSTS 1 an, includeSubDomains
- ``X-Frame-Options: DENY``
- ``X-Content-Type-Options: nosniff``
- ``Referrer-Policy: no-referrer-when-downgrade``

Ces tests montent une app FastAPI minimale avec uniquement le middleware
(isolation). La présence des headers sur la vraie ``apowerb.main.app``
n'est pas vérifiée ici (coûteux au boot) — le test d'intégration en amont
s'en charge via les tests E2E.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from apowerb.helpers.security_headers import SecurityHeadersMiddleware


def _build_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/ping")
    async def ping():
        return {"ok": True}

    return app


class TestSecurityHeaders:
    def test_content_security_policy_present(self):
        client = TestClient(_build_app())
        resp = client.get("/ping")
        assert resp.status_code == 200
        csp = resp.headers.get("content-security-policy")
        assert csp is not None
        assert "default-src" in csp

    def test_strict_transport_security_present(self):
        client = TestClient(_build_app())
        resp = client.get("/ping")
        hsts = resp.headers.get("strict-transport-security")
        assert hsts is not None
        assert "max-age=" in hsts
        # 1 year minimum
        assert "max-age=31536000" in hsts
        assert "includeSubDomains" in hsts

    def test_x_frame_options_deny(self):
        client = TestClient(_build_app())
        resp = client.get("/ping")
        assert resp.headers.get("x-frame-options") == "DENY"

    def test_x_content_type_options_nosniff(self):
        client = TestClient(_build_app())
        resp = client.get("/ping")
        assert resp.headers.get("x-content-type-options") == "nosniff"

    def test_referrer_policy_present(self):
        client = TestClient(_build_app())
        resp = client.get("/ping")
        assert resp.headers.get("referrer-policy") == "no-referrer-when-downgrade"
