"""Tests pour B8 — non-régression du fix `helpers/security.py:41`.

Avant le fix, ``create_access_token`` écrasait systématiquement le champ
``type`` du payload avec ``"access"``. Conséquence : le refresh cookie émis
par ``auth/service.py`` avait en réalité ``type=access`` au lieu de
``type=refresh`` → la route ``/api/auth/refresh-token`` rejetait le cookie.

Ces tests verrouillent le contrat suivant :

1. Si l'appelant ne fournit pas de ``type`` → défaut à ``access``.
2. Si l'appelant fournit ``type=refresh`` → le token est décodé avec
   ``type=refresh``.
3. Si l'appelant fournit ``type=download`` → respecté.
4. ``create_access_token`` n'injecte plus silencieusement ``type=access``
   au point d'écraser une valeur explicite.
"""

from datetime import timedelta

from jose import jwt

from apowerb.configs.settings import get_settings
from apowerb.helpers.security import create_access_token


settings = get_settings()
SIGNING_KEY = settings.encrypt_key
ALGO = settings.algorithm


def _decode(token: str) -> dict:
    return jwt.decode(token, SIGNING_KEY, algorithms=[ALGO])


class TestCreateAccessTokenType:
    def test_default_type_is_access_when_not_provided(self):
        """Par défaut, pas de type dans data → token de type access."""
        token = create_access_token(
            data={"sub": "alice@example.com"},
            expires_delta=timedelta(minutes=5),
        )
        payload = _decode(token)
        assert payload.get("type") == "access"
        assert payload.get("sub") == "alice@example.com"

    def test_explicit_refresh_type_is_preserved(self):
        """Un type=refresh passé explicitement ne doit PAS être écrasé.

        C'est le cas de ``auth/service.py`` qui émet le refresh cookie via
        ``create_access_token(data={..., "type": "refresh"})``. Avant le
        fix B8, ce token se retrouvait en ``type=access`` → le endpoint
        ``/auth/refresh-token`` renvoyait "Invalid token type".
        """
        token = create_access_token(
            data={"sub": "alice@example.com", "type": "refresh"},
            expires_delta=timedelta(days=30),
        )
        payload = _decode(token)
        assert payload.get("type") == "refresh", (
            "create_access_token ne doit pas écraser un type explicite"
        )

    def test_explicit_download_type_is_preserved(self):
        """Idem pour type=download (scope restreint)."""
        token = create_access_token(
            data={"sub": "alice@example.com", "type": "download"},
            expires_delta=timedelta(minutes=10),
        )
        payload = _decode(token)
        assert payload.get("type") == "download"

    def test_explicit_access_type_still_works(self):
        """Non-régression : les appelants qui mettent déjà ``type=access``
        explicitement (users/router.py, auth/service.py) continuent de
        fonctionner à l'identique."""
        token = create_access_token(
            data={"sub": "alice@example.com", "type": "access", "role": "USER"},
            expires_delta=timedelta(minutes=5),
        )
        payload = _decode(token)
        assert payload.get("type") == "access"
        assert payload.get("role") == "USER"
