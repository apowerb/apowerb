"""Création d'une issue GitHub à partir d'un signalement relu.

Un garde domine ce module et justifie son existence séparée : **refuser
un dépôt public**. Le corps d'une issue produit par ce code contient la
route empruntée, les logs de la requête, les messages d'erreur du
navigateur et l'adresse de celui qui a signalé. Sur un dépôt public,
c'est indexé par les moteurs de recherche dans l'heure, et un `git push
--force` ne l'efface pas : une issue supprimée reste dans les
notifications déjà envoyées et dans les caches.

Le contrôle n'est pas mis en cache. La visibilité d'un dépôt change d'un
clic, et un déploiement qui a ouvert son dépôt hier ne doit pas
bénéficier d'un « il était privé la semaine dernière ».

⚠️ Ce garde a été écrit contre un mode d'échec vécu : le 20/08/2026, des
documents clients réels se sont retrouvés dans un dépôt public parce que
personne n'avait posé la question au moment d'écrire le code.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional

import requests

GITHUB_API = "https://api.github.com"
_REPO_SHAPE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_TIMEOUT = 15


class SinkConfigurationError(RuntimeError):
    """La sortie n'est pas configurée, ou l'est mal. 400/501, pas 500."""


class SinkRefusal(RuntimeError):
    """La sortie refuse d'agir — dépôt public. Jamais contournable par retry."""


class SinkDeliveryError(RuntimeError):
    """GitHub a répondu autre chose qu'un succès."""


class GitHubIssueSink:
    """Crée (ou commente) une issue sur un dépôt **privé**.

    ``session`` est injectable pour que les tests éprouvent le garde sans
    réseau : c'est le garde qu'on veut prouver, pas la bibliothèque HTTP.
    """

    def __init__(
        self,
        *,
        repo: str,
        token: str,
        api_url: str = GITHUB_API,
        session: Any | None = None,
        allow_public_repo: bool = False,
    ) -> None:
        if not repo or not _REPO_SHAPE.match(repo):
            raise SinkConfigurationError(
                "BUG_REPORT_GITHUB_REPO doit valoir « organisation/dépôt » "
                f"— reçu {repo!r}."
            )
        if not token:
            raise SinkConfigurationError(
                "BUG_REPORT_GITHUB_TOKEN est vide : aucune issue ne peut être créée."
            )
        self.repo = repo
        self._token = token
        self._api = api_url.rstrip("/")
        self._session = session or requests
        # Échappatoire explicite, jamais le défaut, et nommée pour ce
        # qu'elle est : elle n'existe que pour un dépôt public de projet
        # où les signalements ne contiennent rien de client (une démo).
        self._allow_public = allow_public_repo

    # -- garde ------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def assert_repository_is_private(self) -> dict[str, Any]:
        """Lit la visibilité réelle du dépôt, maintenant. Lève sinon."""
        response = self._session.get(
            f"{self._api}/repos/{self.repo}",
            headers=self._headers(),
            timeout=_TIMEOUT,
        )
        if response.status_code == 404:
            raise SinkConfigurationError(
                f"Dépôt {self.repo} introuvable, ou le jeton n'y a pas accès. "
                "Un 404 de GitHub couvre les deux cas."
            )
        if response.status_code != 200:
            raise SinkDeliveryError(
                f"GitHub a répondu {response.status_code} en lisant {self.repo}."
            )
        payload = response.json()
        # `private` est le champ historique, `visibility` le moderne
        # (public / private / internal). On exige que les DEUX disent privé
        # quand les deux sont là : un dépôt « internal » d'entreprise est
        # `private: true` mais visible de toute l'organisation, ce qui reste
        # une décision à prendre en connaissance de cause.
        is_private = bool(payload.get("private"))
        visibility = payload.get("visibility")
        if not is_private or visibility == "public":
            if not self._allow_public:
                raise SinkRefusal(
                    f"Refus d'écrire dans {self.repo} : le dépôt est "
                    f"{visibility or 'public'}. Un signalement contient des "
                    "logs, des messages d'erreur et l'adresse de celui qui "
                    "l'a envoyé — sur un dépôt public, c'est indexé et "
                    "irrécupérable. Utilisez un dépôt privé, ou activez "
                    "explicitement BUG_REPORT_GITHUB_ALLOW_PUBLIC si ce "
                    "déploiement ne traite aucune donnée client."
                )
        return payload

    # -- écriture ---------------------------------------------------------

    def find_existing_issue(self, fingerprint: str) -> Optional[dict[str, Any]]:
        """Cherche l'issue déjà ouverte pour cette empreinte.

        La recherche porte sur le marqueur d'empreinte que ce module écrit
        dans le corps, pas sur le titre : un titre se réécrit à la main, et
        la déduplication ne doit pas dépendre de la discipline d'un
        relecteur.
        """
        query = f'repo:{self.repo} is:issue "{fingerprint}" in:body'
        response = self._session.get(
            f"{self._api}/search/issues",
            headers=self._headers(),
            params={"q": query, "per_page": 5},
            timeout=_TIMEOUT,
        )
        if response.status_code != 200:
            # Une recherche indisponible ne doit pas empêcher de créer le
            # ticket : au pire, on crée un doublon, ce qui se corrige.
            return None
        items = response.json().get("items") or []
        return items[0] if items else None

    def create_issue(
        self,
        *,
        title: str,
        body: str,
        labels: Mapping[str, Any] | list[str] | None = None,
    ) -> dict[str, Any]:
        self.assert_repository_is_private()
        payload: dict[str, Any] = {"title": title, "body": body}
        if labels:
            payload["labels"] = list(labels)
        response = self._session.post(
            f"{self._api}/repos/{self.repo}/issues",
            headers=self._headers(),
            json=payload,
            timeout=_TIMEOUT,
        )
        if response.status_code not in (200, 201):
            raise SinkDeliveryError(
                f"GitHub a refusé la création de l'issue ({response.status_code}) : "
                f"{response.text[:300]}"
            )
        return response.json()

    def comment_on_issue(self, issue_number: int, body: str) -> dict[str, Any]:
        self.assert_repository_is_private()
        response = self._session.post(
            f"{self._api}/repos/{self.repo}/issues/{issue_number}/comments",
            headers=self._headers(),
            json={"body": body},
            timeout=_TIMEOUT,
        )
        if response.status_code not in (200, 201):
            raise SinkDeliveryError(
                f"GitHub a refusé le commentaire ({response.status_code}) : "
                f"{response.text[:300]}"
            )
        return response.json()


__all__ = [
    "GITHUB_API",
    "GitHubIssueSink",
    "SinkConfigurationError",
    "SinkDeliveryError",
    "SinkRefusal",
]
