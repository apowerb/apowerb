"""Le refus « pas configuré », de la route jusqu'au corps de la réponse.

David, 08/09/26 : *les utilisateurs ne doivent jamais rencontrer un 4xx/5xx
pour une fonctionnalité qui n'est simplement pas configurée* — ils voient
« pas encore configuré, contactez votre administrateur ».

Le contrat existe depuis apowerb#113 : un 503 portant
``{"code": "NOT_CONFIGURED", "capability": <clé>}``, que l'interface reconnaît
par son **code** (`parseNotConfiguredError` dans `src/lib/setup.js`) et traduit
en cet état silencieux. Tout autre 503 reste une panne, et doit le rester.

Ce que ces tests ajoutent : le contrat n'était éprouvé que par capacité, sur
des objets. Ici l'application **réelle** est montée — `apowerb.main.app`, tous
ses routeurs — avec un objet `Settings` réel construit depuis l'environnement,
et le corps servi est lu tel qu'un navigateur le recevrait.

Le contre-exemple compte autant : la même route, contre le même orchestrateur
injoignable, doit répondre **autrement** dès que l'installation en a nommé un.
Sans lui, un garde qui renverrait NOT_CONFIGURED à tout propos passerait pour
vert, et l'écran dirait « pas configuré » devant une vraie panne.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from apowerb.configs.settings import Settings
from apowerb.core import setup_status as ss
from apowerb.core.setup_status import NOT_CONFIGURED_CODE
from apowerb.main import app
from apowerb.auth.dependencies import get_current_user
from apowerb.helpers.database import get_db
from apowerb.routers import emailing as emailing_mod
from apowerb.routers import scheduler as scheduler_mod

# Les familles de variables qui décident d'une capacité. Vidées avant de
# construire `Settings`, sinon la machine qui joue la suite décide du résultat
# — et une machine configurée rendrait ces tests verts sans rien prouver.
_FAMILIES = (
    "MICROSOFT_", "GOOGLE_", "OUTLOOK_", "S3_", "SMTP_", "TH2ETL_",
    "DEFAULT_LLM_", "STORAGE_MODE", "ORCHESTRATOR", "OTEL_",
    "APP_PUBLIC_URL", "PUBLIC_BASE_URL", "BASE_URL", "API_KEY", "OAUTH_TOKEN",
)


class _User:
    """Un administrateur : le refus testé ici est en aval de l'authentification,
    et un 401 masquerait tout ce que ces tests regardent."""

    email = "someone@example.test"
    role = "ADMIN"
    plan = None
    user_id = "u-1"
    id = "u-1"


async def _no_db():
    """Les dépendances d'une route sont résolues AVANT son corps : sans cela,
    `require_configured` ne serait jamais atteint, la base n'existant pas ici."""
    yield None


@pytest.fixture
def settings_from(monkeypatch):
    """Construit un `Settings` réel depuis un environnement dont on maîtrise
    chaque variable, et le pose là où chaque route le lit.

    Trois points de pose, chacun étant la couture que le code désigne lui-même :
    `setup_status.get_settings` pour `require_configured`, le `get_settings` du
    routeur de planification (son propre module, comme le dit la docstring de
    `outage_response`), et l'objet `settings` que `emailing` fige à l'import.

    Surtout pas `get_settings.cache_clear()` : le cache tient l'instance que
    d'autres suites ont déjà configurée, et le vider ici leur fait perdre leur
    configuration à des centaines de lignes de là (mesuré le 08/09 sur
    `test_s3_artifact_service`). Une pose locale se défait toute seule.
    """

    def _build(**env):
        for name in list(__import__("os").environ):
            if name.startswith(_FAMILIES) or name in _FAMILIES:
                monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        # `_env_file=None` : le `.env` d'un poste de développement n'a pas à
        # décider de ce que ce test mesure.
        cfg = Settings(_env_file=None)
        monkeypatch.setattr(ss, "get_settings", lambda: cfg)
        monkeypatch.setattr(scheduler_mod, "get_settings", lambda: cfg)
        monkeypatch.setattr(emailing_mod, "settings", cfg)
        return cfg

    return _build


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: _User()
    app.dependency_overrides[get_db] = _no_db
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


def _refusal(response):
    """Le refus tel que l'interface le lit : `detail.code` et `detail.capability`.
    Rend `None` pour tout le reste — c'est exactement ce que fait
    `parseNotConfiguredError` côté interface, et c'est ce qui sépare
    « pas configuré » d'une panne."""
    if response.status_code != 503:
        return None
    detail = response.json().get("detail")
    if not isinstance(detail, dict) or detail.get("code") != NOT_CONFIGURED_CODE:
        return None
    return detail


# --------------------------------------------------------------------------- #
# Rien de configuré : chaque route d'une capacité éteinte se refuse proprement.
# --------------------------------------------------------------------------- #

GATED = [
    ("/api/integrations/microsoft/outlook/connect", "microsoft_integration"),
    ("/api/integrations/google/connect?service=google_drive", "google_integration"),
    ("/api/emailing/microsoft/auth-url", "microsoft_integration"),
    ("/api/pipelines", "orchestration"),
]


@pytest.mark.parametrize("path,capability", GATED, ids=[c for _, c in GATED])
def test_an_unconfigured_route_refuses_with_the_code_the_front_reads(
    client, settings_from, path, capability
):
    """Ni 500 ni 503 muet : le code que l'écran reconnaît, et le nom de la
    capacité, pour que l'interface sache de laquelle elle parle."""
    settings_from()
    response = client.get(path)
    assert response.status_code == 503, (
        f"{path} a répondu {response.status_code} : "
        f"{response.text[:300]}"
    )
    refusal = _refusal(response)
    assert refusal is not None, (
        f"{path} rend un 503 que l'interface ne reconnaît pas comme "
        f"« pas configuré » — elle l'affichera en panne. Corps : "
        f"{response.text[:300]}"
    )
    assert refusal["capability"] == capability
    assert refusal.get("message")


def test_no_gated_route_answers_a_server_error(client, settings_from):
    """Le 500 est ce que ce contrat existe pour supprimer. Contrôle séparé :
    un 500 fait échouer le test ci-dessus par son premier `assert`, et sa
    disparition mérite d'être nommée."""
    settings_from()
    for path, _ in GATED:
        assert client.get(path).status_code != 500, path


# --------------------------------------------------------------------------- #
# Configuré : la même route ne dit plus « pas configuré ».
# --------------------------------------------------------------------------- #

def test_a_configured_capability_stops_refusing(client, settings_from):
    """Sans ce contre-exemple, un garde qui refuserait tout serait vert."""
    settings_from(
        MICROSOFT_INTEGRATION_CLIENT_ID="client-id",
        MICROSOFT_INTEGRATION_CLIENT_SECRET="client-secret",
    )
    for path in (
        "/api/integrations/microsoft/outlook/connect",
        "/api/emailing/microsoft/auth-url",
    ):
        assert _refusal(client.get(path)) is None, (
            f"{path} dit encore « pas configuré » alors que "
            "MICROSOFT_INTEGRATION_CLIENT_ID et _SECRET sont posés"
        )


def test_a_reachable_orchestrator_that_is_down_is_a_failure_not_a_setup_hole(
    client, settings_from
):
    """La distinction que l'écran doit pouvoir faire, sur UNE seule route.

    Même orchestrateur injoignable dans les deux cas — il n'y a rien sur
    `localhost:6789` — mais une installation qui en a nommé un est en PANNE, et
    l'écran doit le dire. Une qui n'en a jamais nommé n'a rien de cassé.
    `orchestrator_is_configured` lit `model_fields_set`, donc c'est bien le fait
    d'avoir fourni la variable qui bascule la réponse, et pas sa valeur.
    """
    settings_from()
    assert _refusal(client.get("/api/pipelines")) is not None

    settings_from(ORCHESTRATOR="th2etl", TH2ETL_BASE_URL="http://th2etl.invalid:8000")
    response = client.get("/api/pipelines")
    assert response.status_code == 503
    assert _refusal(response) is None, (
        "une installation qui a nommé son orchestrateur et ne l'atteint pas "
        "est en panne ; la dire « pas configurée » cache la panne derrière un "
        "écran d'installation"
    )
