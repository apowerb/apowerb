"""Aucun chemin d'authentification ne doit accepter une clé de signature vide.

Contexte. Rendre `apowerb` importable comme library a imposé de donner une
valeur par défaut aux champs de configuration, dont ``encrypt_key``. Un premier
correctif a traité ``helpers/security.py`` — la constante ``SECRET_KEY`` y était
figée à l'import — en la remplaçant par ``get_secret_key()``, qui refuse une clé
vide au moment de s'en servir.

Ce correctif était incomplet : cinq autres sites lisent ``settings.encrypt_key``
directement, sans passer par ce garde. Ils couvrent l'authentification
principale de l'API, l'accès aux endpoints natifs d'ADK (exécution d'agents), le
WebSocket audio et le rafraîchissement de session. Sans ``ENCRYPT_KEY``, chacun
valide un JWT signé avec la chaîne vide — donc forgeable par n'importe qui.

Le serveur refuse de démarrer sans la clé, la production n'est donc pas exposée.
Mais ces symboles sont précisément ce qu'un consommateur de la library importe
pour les monter sur sa propre application, sans reproduire le cycle de vie de
``main.py``. Le défaut silencieux serait alors une usurpation d'identité
arbitraire.

Ces tests forcent la clé à vide et vérifient que chaque site échoue bruyamment
plutôt que d'accepter un jeton forgé.
"""

from __future__ import annotations

import ast
import pathlib

import pytest
from jose import jwt

from apowerb.configs.settings import get_settings

ALGO = "HS256"


def forge(payload: dict) -> str:
    """Un jeton signé avec la chaîne vide — ce qu'un attaquant produirait."""
    return jwt.encode(payload, "", algorithm=ALGO)


@pytest.fixture
def clé_vide(monkeypatch):
    """Simule un déploiement sans ENCRYPT_KEY.

    ``get_settings`` est mis en cache : l'objet renvoyé est celui-là même que les
    modules ont capturé à l'import. Le patcher couvre donc à la fois les lectures
    directes de ``settings.encrypt_key`` et les appels à ``get_secret_key()``.
    """
    monkeypatch.setattr(get_settings(), "encrypt_key", "", raising=False)
    return forge({"sub": "victime@example.com", "type": "access"})


class TestAuthentificationPrincipale:
    @pytest.mark.asyncio
    async def test_get_current_user_refuse_un_jeton_forgé(self, clé_vide):
        from fastapi.security import HTTPAuthorizationCredentials

        from apowerb.auth import dependencies

        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=clé_vide)
        with pytest.raises(RuntimeError, match="ENCRYPT_KEY"):
            await dependencies.get_current_user(credentials=creds, db=None)

    @pytest.mark.asyncio
    async def test_get_optional_user_refuse_un_jeton_forgé(self, clé_vide):
        """Le chemin « auth facultative » ne doit pas dégrader en anonyme non plus.

        Retourner ``None`` serait déjà mieux qu'accepter, mais masquerait une
        configuration cassée. On veut le bruit.
        """
        from fastapi.security import HTTPAuthorizationCredentials

        from apowerb.auth import dependencies

        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=clé_vide)
        with pytest.raises(RuntimeError, match="ENCRYPT_KEY"):
            await dependencies.get_optional_user(credentials=creds, db=None)


class TestEndpointsNatifsADK:
    @pytest.mark.asyncio
    async def test_le_middleware_adk_refuse_un_jeton_forgé(self, clé_vide):
        """``/run_sse`` exécute des agents : c'est le pire endroit où céder."""
        from starlette.requests import Request

        from apowerb.main import ADKAuthMiddleware

        scope = {
            "type": "http",
            "method": "POST",
            "scheme": "http",
            "server": ("test", 80),
            "path": "/run_sse",
            "query_string": b"",
            "headers": [(b"authorization", f"Bearer {clé_vide}".encode())],
        }

        async def call_next(_request):  # pragma: no cover - ne doit pas être atteint
            raise AssertionError("un jeton forgé a franchi le middleware")

        middleware = ADKAuthMiddleware(app=None)
        with pytest.raises(RuntimeError, match="ENCRYPT_KEY"):
            await middleware.dispatch(Request(scope), call_next)


class TestWebSocketAudio:
    @pytest.mark.asyncio
    async def test_le_ws_refuse_un_jeton_forgé(self, clé_vide):
        from apowerb.routers import audio_stream

        with pytest.raises(RuntimeError, match="ENCRYPT_KEY"):
            await audio_stream._validate_ws_token(clé_vide)

    @pytest.mark.asyncio
    async def test_la_connexion_se_ferme_proprement(self, clé_vide):
        """Refuser ne suffit pas : il faut refuser lisiblement.

        Starlette ne convertit pas les exceptions en réponse sur un scope
        WebSocket — ``ServerErrorMiddleware`` ignore tout scope autre que
        ``http``. Laisser le RuntimeError remonter ferait s'effondrer la
        connexion sans code de fermeture, alors qu'un jeton simplement
        invalide, lui, obtient un ``close(4003)`` propre.

        Le code doit aussi être *distinct* de 4003 : « ta clé est mauvaise » et
        « mon serveur est mal configuré » ne sont pas le même diagnostic.
        """
        from apowerb.routers import audio_stream

        class WebSocketFactice:
            def __init__(self):
                self.query_params = {"token": clé_vide}
                self.fermeture = None

            async def close(self, code=None, reason=None):
                self.fermeture = (code, reason)

            async def accept(self):  # pragma: no cover - ne doit pas être atteint
                raise AssertionError("la connexion a été acceptée sans clé")

        ws = WebSocketFactice()
        await audio_stream.audio_websocket(ws, "session-1")

        code, _raison = ws.fermeture
        assert code == 4500, f"fermeture attendue en 4500, obtenue {ws.fermeture}"


class TestRafraîchissementDeSession:
    @pytest.mark.asyncio
    async def test_refresh_refuse_un_jeton_forgé(self, monkeypatch):
        """Piège spécifique : ``refresh()`` enveloppe le décodage dans un
        ``except Exception``. Une erreur de configuration levée *dans* le ``try``
        y serait convertie en « identifiants invalides » — un 401 trompeur qui
        laisserait croire à un problème de jeton. La clé doit donc être résolue
        avant d'entrer dans le bloc.
        """
        from apowerb.auth import service

        monkeypatch.setattr(get_settings(), "encrypt_key", "", raising=False)
        token = forge({"sub": "victime@example.com", "type": "refresh"})

        class RequêteFactice:
            cookies = {"refresh_token": token}

        with pytest.raises(RuntimeError, match="ENCRYPT_KEY"):
            await service.refresh(RequêteFactice(), db=None)


class TestMiddlewareDormant:
    def test_le_middleware_de_test_refuse_un_jeton_vide(self, monkeypatch):
        """``middleware/auth.py`` compare le jeton reçu à ``settings.test_token``.

        Ce champ vaut désormais la chaîne vide par défaut : une requête
        ``Authorization: Bearer`` sans jeton s'authentifierait. Ce middleware
        n'est monté nulle part aujourd'hui, mais il porte le même nom de classe
        que celui d'``auth/middleware.py``, qui, lui, est réel — la confusion est
        un accident qui attend son heure.
        """
        import asyncio

        from starlette.requests import Request

        from apowerb.middleware.auth import AuthMiddleware

        monkeypatch.setattr(get_settings(), "test_token", "", raising=False)
        scope = {
            "type": "http",
            "method": "GET",
            "scheme": "http",
            "server": ("test", 80),
            "path": "/api/whatever",
            "query_string": b"",
            "headers": [(b"authorization", b"Bearer ")],
        }

        async def call_next(_request):  # pragma: no cover - ne doit pas être atteint
            raise AssertionError("un jeton vide a franchi le middleware")

        réponse = asyncio.run(AuthMiddleware(app=None).dispatch(Request(scope), call_next))
        assert réponse.status_code == 401


# Seuls modules autorisés à lire ``encrypt_key`` : celui qui la déclare, celui
# qui expose le garde, et celui qui construit le Fernet (lequel a son propre
# garde, ``_require_fernet``). Partout ailleurs, on passe par un accesseur.
_MODULES_AUTORISÉS = {
    "configs/settings.py",
    "helpers/security.py",
    "helpers/encryptor.py",
}


class TestGardeStructurelle:
    def test_encrypt_key_n_est_lue_que_par_les_modules_autorisés(self):
        """Empêche la réapparition du motif, ailleurs et plus tard.

        Le défaut corrigé ici n'était pas visible à la relecture du diff : il
        vivait dans des fichiers que le changement ne touchait pas.

        Une première version de cette garde n'inspectait que les arguments
        passés *directement* à ``jwt.encode``/``jwt.decode``. Une relecture l'a
        mise en défaut en deux lignes :

            key = settings.encrypt_key
            jwt.decode(token, key, algorithms=["HS256"])

        Rien n'était détecté. Un simple alias d'import (``from jose import jwt
        as jose_jwt``) suffisait aussi. Une garde qu'un refactor anodin
        désarme est pire qu'aucune garde : elle rassure.

        On interdit donc la *lecture* de l'attribut, où qu'elle serve — un
        critère que ni l'extraction en variable ni le renommage du module JWT
        ne contournent.
        """
        racine = pathlib.Path(__file__).resolve().parent.parent / "src" / "apowerb"
        coupables: list[str] = []

        for fichier in sorted(racine.rglob("*.py")):
            relatif = fichier.relative_to(racine).as_posix()
            if relatif in _MODULES_AUTORISÉS:
                continue
            arbre = ast.parse(fichier.read_text(encoding="utf-8"), filename=str(fichier))
            for nœud in ast.walk(arbre):
                if isinstance(nœud, ast.Attribute) and nœud.attr == "encrypt_key":
                    coupables.append(f"apowerb/{relatif}:{nœud.lineno}")

        assert not coupables, (
            "encrypt_key ne doit être lue que par get_secret_key(), qui refuse "
            "une clé vide : " + ", ".join(coupables)
        )

    def test_la_garde_attrape_bien_l_indirection(self, tmp_path):
        """La garde doit détecter ce que sa première version laissait passer.

        Sans ce test, rien ne dit que la garde garde encore quoi que ce soit :
        elle passerait au vert sur un dépôt sain comme sur une garde cassée.
        """
        piège = tmp_path / "src" / "apowerb" / "faux.py"
        piège.parent.mkdir(parents=True)
        piège.write_text(
            "key = settings.encrypt_key\n"
            "jwt.decode(token, key, algorithms=['HS256'])\n",
            encoding="utf-8",
        )

        arbre = ast.parse(piège.read_text(encoding="utf-8"))
        détecté = [
            n for n in ast.walk(arbre)
            if isinstance(n, ast.Attribute) and n.attr == "encrypt_key"
        ]

        assert détecté, "la garde ne verrait pas une lecture via variable locale"
