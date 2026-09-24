"""Client for Jev, TypeSafe's decision model (typed questions -> probabilities).

Jev does not write text: it answers typed questions about a ``state`` with
calibrated probabilities (``noul`` = yes/no, ``choice`` = one of N labels).
It is reached either through OpenRouter (``/api/alpha/decisions``) or
TypeSafe's own API (``/v1/systemone``); both take the same body
``{model, state, questions}`` and return ``{answers, usage}``.

Configured server-side only (``JEV_API_KEY``, ``JEV_TRANSPORT``, ``JEV_MODEL``,
``JEV_TIMEOUT_S``): an agent never sees nor passes the key. Error messages are
built without the key, even when the provider echoes it back.

This module deliberately has no other apowerb imports besides ``get_settings``,
so it can be unit-tested with ``requests`` mocked in isolation.
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from apowerb.configs.settings import get_settings

logger = logging.getLogger(__name__)

# transport -> (base URL + path, default model)
_TRANSPORTS: dict[str, tuple[str, str]] = {
    "openrouter": ("https://openrouter.ai/api/alpha/decisions", "typesafe/jev-1.13"),
    "typesafe": ("https://api.typesafe.ai/v1/systemone", "jev-1.13.0"),
}
_DEFAULT_TIMEOUT_S = 30.0
# Provider errors that mean "fix the configuration or the account", relayed
# to the agent with an actionable message instead of a generic outage.
_API_ERROR_MESSAGES = {
    401: "Clé Jev refusée par le fournisseur : vérifier JEV_API_KEY et JEV_TRANSPORT.",
    402: "Crédit Jev épuisé sur le compte du fournisseur.",
    403: "Clé Jev sans accès au modèle demandé : vérifier JEV_MODEL et les droits de la clé.",
    429: "Trop de requêtes Jev : limite du fournisseur atteinte, réessayer plus tard.",
}


class JevNotConfigured(RuntimeError):
    """No key, or an unknown transport — Jev is off on this server."""


class JevAPIError(RuntimeError):
    """The provider refused the call (401/402/403/429).

    ``user_message`` is safe to show to an agent or a user: it never carries
    the key nor the provider's raw body.
    """

    def __init__(self, status_code: int, user_message: str):
        self.status_code = status_code
        self.user_message = user_message
        super().__init__(f"Jev answered {status_code}: {user_message}")


class JevUnavailable(RuntimeError):
    """Jev could not be reached, or answered something unusable."""


class JevClient:
    """Synchronous HTTP client for Jev decisions."""

    def __init__(
        self,
        api_key: str | None = None,
        transport: str | None = None,
        model: str | None = None,
        timeout_s: float | None = None,
    ) -> None:
        # get_settings() only when something is left to the server config, so
        # tests that pass every argument never build a real Settings().
        needs_settings = (
            api_key is None or transport is None or model is None or timeout_s is None
        )
        settings = get_settings() if needs_settings else None

        key = api_key if api_key is not None else (settings.jev_api_key or "")
        name = (
            (transport if transport is not None else settings.jev_transport or "")
            .strip()
            .lower()
        )
        chosen_model = model if model is not None else (settings.jev_model or "")
        timeout = timeout_s if timeout_s is not None else settings.jev_timeout_s

        if not key.strip():
            raise JevNotConfigured(
                "Jev n'est pas configuré sur ce serveur (JEV_API_KEY absente)."
            )
        if name not in _TRANSPORTS:
            raise JevNotConfigured(
                f"JEV_TRANSPORT inconnu : {name!r} (attendu : {', '.join(sorted(_TRANSPORTS))})."
            )
        self._key = key.strip()
        self.url, default_model = _TRANSPORTS[name]
        self.model = chosen_model.strip() or default_model
        self.timeout_s = float(_DEFAULT_TIMEOUT_S if timeout is None else timeout)
        if self.timeout_s <= 0:
            raise JevNotConfigured(
                f"JEV_TIMEOUT_S doit être positif, reçu {self.timeout_s:g}."
            )

    def _scrub(self, text: str) -> str:
        return text.replace(self._key, "***")

    def decide(
        self, state: dict[str, Any], questions: dict[str, Any]
    ) -> dict[str, Any]:
        """Ask ``questions`` about ``state``; return Jev's ``{answers, usage}``."""
        body = {"model": self.model, "state": state, "questions": questions}
        headers = {
            "Authorization": f"Bearer {self._key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            response = requests.post(
                self.url, json=body, headers=headers, timeout=self.timeout_s
            )
        except requests.RequestException as exc:
            raise JevUnavailable(
                self._scrub(f"Jev injoignable ({type(exc).__name__}) : {exc}")
            ) from None

        if response.status_code in _API_ERROR_MESSAGES:
            raise JevAPIError(
                response.status_code, _API_ERROR_MESSAGES[response.status_code]
            )
        if response.status_code >= 400:
            raise JevUnavailable(f"Jev a répondu {response.status_code}.")
        try:
            payload = response.json()
        except ValueError:
            raise JevUnavailable(
                "Jev a renvoyé une réponse illisible (pas du JSON)."
            ) from None
        if not isinstance(payload, dict) or not isinstance(
            payload.get("answers"), dict
        ):
            raise JevUnavailable("Jev a renvoyé une réponse sans « answers ».")
        return payload
