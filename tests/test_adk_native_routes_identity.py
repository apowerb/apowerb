"""Les routes natives d'ADK n'acceptent que le user_id du jeton.

L'enveloppe ``/api/adk/run(_sse)`` vérifiait déjà que le ``user_id`` demandé est
celui de l'utilisateur connecté. Les routes natives, non : ``ADKAuthMiddleware``
contrôlait seulement que le jeton était valide. Un utilisateur connecté pouvait
donc lire les sessions d'un autre (``/apps/{app}/users/{autre}/...``), lancer un
agent en son nom (``/run``, ``/run_sse``) et, depuis la mémoire entre
conversations (apowerb/roadmap#82), interroger ses souvenirs. Et ``/run_live``,
un WebSocket, échappait complètement au middleware : aucun jeton exigé.

Tout est exercé contre la vraie application : les routes sont celles d'ADK.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from jose import jwt
from starlette.websockets import WebSocketDisconnect

from apowerb.configs.settings import get_settings

ALICE = "alice@example.com"
BOB = "bob@example.com"


def _token(sub: str = ALICE, **claims) -> str:
    settings = get_settings()
    payload = {"sub": sub, "type": "access", "exp": datetime.now(timezone.utc) + timedelta(minutes=30)}
    payload.update(claims)
    return jwt.encode(payload, settings.encrypt_key, algorithm=settings.algorithm)


@pytest.fixture(scope="module")
def client():
    # Sessions en mémoire le temps de ces tests : un appel légitime obtient une
    # vraie réponse d'ADK au lieu d'échouer sur un Postgres absent -- « non
    # refusé » se prouve alors par ce qu'ADK répond, pas par une erreur 500.
    from google.adk.sessions import InMemorySessionService

    import apowerb.main as main

    server = main._ADK_HANDLES["web_server"]
    original = server.session_service
    server.session_service = InMemorySessionService()
    # get_settings() est en cache : une clé posée par variable d'environnement
    # arriverait trop tard si un autre module a déjà chargé les settings. Même
    # méthode que test_expired_session_is_refused_not_crashed.py.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(get_settings(), "encrypt_key", "k" * 32, raising=False)
        yield TestClient(main.app)
    server.session_service = original


def _auth(sub: str = ALICE) -> dict:
    return {"Authorization": f"Bearer {_token(sub)}"}


# --- chemins /apps/{app}/users/{user_id}/... --------------------------------

def test_lire_les_sessions_d_un_autre_est_refuse(client):
    r = client.get(f"/apps/agent1/users/{BOB}/sessions", headers=_auth())
    assert r.status_code == 403


def test_lire_ses_propres_sessions_passe(client):
    r = client.get(f"/apps/agent1/users/{ALICE}/sessions", headers=_auth())
    assert r.status_code == 200, r.text


def test_un_user_id_encode_dans_l_url_est_compare_decode(client):
    assert client.get("/apps/agent1/users/bob%40example.com/sessions", headers=_auth()).status_code == 403
    assert client.get("/apps/agent1/users/alice%40example.com/sessions", headers=_auth()).status_code == 200


def test_alimenter_la_memoire_d_un_autre_est_refuse(client):
    r = client.patch(f"/apps/agent1/users/{BOB}/memory", json={"sessionId": "s1"}, headers=_auth())
    assert r.status_code == 403


# --- corps de /run et /run_sse -----------------------------------------------

@pytest.mark.parametrize("route", ["/run", "/run_sse"])
@pytest.mark.parametrize("champ", ["user_id", "userId"])
def test_lancer_un_agent_au_nom_d_un_autre_est_refuse(client, route, champ):
    body = {"app_name": "agent1", champ: BOB, "session_id": "s1",
            "new_message": {"role": "user", "parts": [{"text": "bonjour"}]}}
    assert client.post(route, json=body, headers=_auth()).status_code == 403


def test_deux_champs_contradictoires_sont_refuses(client):
    body = {"app_name": "agent1", "user_id": ALICE, "userId": BOB, "session_id": "s1"}
    assert client.post("/run", json=body, headers=_auth()).status_code == 403


def test_son_propre_run_atteint_adk_avec_son_corps_intact(client):
    # Le middleware lit le corps : ADK doit encore le recevoir entier. Il répond
    # « Agent not found: 'agent1' » -- il a donc lu app_name DANS le corps. Un
    # corps perdu donnerait 422, un refus 403.
    body = {"app_name": "agent1", "user_id": ALICE, "session_id": "s1",
            "new_message": {"role": "user", "parts": [{"text": "bonjour"}]}}
    r = client.post("/run", json=body, headers=_auth())
    assert r.status_code == 404 and "Agent not found: 'agent1'" in r.text, r.text


def test_un_corps_illisible_est_laisse_a_adk(client):
    r = client.post("/run", content=b"pas du json", headers={**_auth(), "Content-Type": "application/json"})
    assert r.status_code == 422


# --- /run_live (WebSocket) ---------------------------------------------------

def _live(client, user_id: str, token: str | None):
    url = f"/run_live?app_name=agent1&user_id={user_id}&session_id=s1"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    with client.websocket_connect(url, headers=headers) as ws:
        ws.close()


def test_run_live_sans_jeton_est_refuse(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        _live(client, ALICE, None)
    assert exc.value.code == 1008


def test_run_live_avec_un_jeton_invalide_est_refuse(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        _live(client, ALICE, "pas-un-jwt")
    assert exc.value.code == 1008


def test_run_live_au_nom_d_un_autre_est_refuse(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        _live(client, BOB, _token(ALICE))
    assert exc.value.code == 1008


def test_run_live_avec_un_jeton_longue_duree_est_refuse(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        _live(client, ALICE, _token(ALICE, type="agent_refresh"))
    assert exc.value.code == 1008


def test_run_live_pour_soi_passe_le_controle(client):
    # Le contrôle laisse passer : ce qui suit appartient à ADK. On vérifie
    # seulement que la fermeture, s'il y en a une, ne vient pas de notre refus.
    try:
        _live(client, ALICE, _token(ALICE))
    except WebSocketDisconnect as exc:
        assert exc.code != 1008


# --- appelants internes : rien ne doit casser ------------------------------------

def test_un_run_planifie_passe_encore(client):
    # Chemin réel des runs planifiés : jeton longue durée émis à la
    # planification, puis jeton d'accès fabriqué à l'exécution. Son sub doit
    # être le user_id qu'il envoie à /run.
    from apowerb.helpers.security import refresh_access_token_from_agent_refresh
    from apowerb.scheduler.run_agent_background.token_issuer import create_agent_run_token

    longue_duree = create_agent_run_token(
        agent_name="agent1", user_id=ALICE, session_id="s1",
        new_message={"role": "user", "parts": [{"text": "rapport"}]},
    )
    acces = refresh_access_token_from_agent_refresh(longue_duree)
    body = {"app_name": "agent1", "user_id": ALICE, "session_id": "s1",
            "new_message": {"role": "user", "parts": [{"text": "rapport"}]}}

    r = client.post("/run", json=body, headers={"Authorization": f"Bearer {acces}"})

    assert r.status_code == 404 and "Agent not found: 'agent1'" in r.text, r.text
