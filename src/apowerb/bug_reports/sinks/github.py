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


# Couleurs des étiquettes que ce module pose, quand le dépôt ne les a pas
# déjà. Reprises du code couleur de `apowerb/roadmap`, choisi par l'équipe :
# rouge foncé pour ce qui bloque, orange pour ce qui casse, jaune pour ce qui
# gêne, gris-bleu pour le cosmétique. Un dépôt qui définit déjà l'étiquette
# garde la sienne — on ne réécrit jamais une couleur existante.
_LABEL_COLORS: dict[str, str] = {
    "bug": "d73a4a",
    "severity:blocker": "B60205",
    "severity:major": "D93F0B",
    "severity:minor": "FBCA04",
    "severity:cosmetic": "BFDADC",
    "from:app": "C5DEF5",
}

# Teinte unique pour les zones fonctionnelles : elles sont dix-huit, et leur
# donner dix-huit couleurs rendrait la liste illisible. Ce qui doit sauter aux
# yeux, c'est la sévérité.
_AREA_COLOR = "1D76DB"

_LABEL_DESCRIPTIONS: dict[str, str] = {
    "from:app": "Remonté par un utilisateur depuis l'application",
    "severity:blocker": "Bloque l'utilisateur : il ne peut pas continuer",
    "severity:major": "Cassé, contournement pénible",
    "severity:minor": "Gêne, contournement simple",
    "severity:cosmetic": "Affichage seulement",
}


def _label_color(name: str) -> str:
    """La couleur prévue, celle des zones, ou un gris neutre assumé."""
    if name in _LABEL_COLORS:
        return _LABEL_COLORS[name]
    if name.startswith("area:"):
        return _AREA_COLOR
    return "CFD3D7"


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
        project_number: int | None = None,
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
        # Le tableau de projet où ranger les tickets, s'il y en a un. Mesuré
        # le 11/09/2026 : un Project v2 automatise le statut d'une carte, pas
        # son entrée — les issues de `apowerb/roadmap` y étaient ajoutées à la
        # main, une par une. Facultatif : un déploiement sans tableau reste un
        # déploiement valide.
        self._project_number = project_number

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

    def _existing_label_names(self) -> set[str] | None:
        """Les étiquettes déjà définies dans le dépôt, ou ``None``.

        ``None`` veut dire « je n'ai pas pu savoir » : dans ce cas on ne crée
        rien et on laisse GitHub faire ce qu'il faisait avant. Ne pas pouvoir
        lire les étiquettes n'est pas une raison de refuser un ticket.
        """
        try:
            response = self._session.get(
                f"{self._api}/repos/{self.repo}/labels",
                headers=self._headers(),
                params={"per_page": 100},
                timeout=_TIMEOUT,
            )
        except Exception:  # noqa: BLE001 — une étiquette ne vaut pas un ticket
            return None
        if response.status_code != 200:
            return None
        try:
            return {item["name"] for item in response.json()}
        except Exception:  # noqa: BLE001
            return None

    def _ensure_labels(self, labels: list[str]) -> None:
        """Crée les étiquettes manquantes avec une couleur choisie.

        Sans cela, GitHub les invente au premier usage — toutes en gris
        ``#ededed``. Mesuré le 11/09/2026 sur `apowerb/roadmap` : les trois
        étiquettes de ce module y sont arrivées en gris, `severity:blocker`
        compris, dans un dépôt dont le code couleur distinguait justement
        l'urgence.

        Une étiquette déjà présente n'est jamais réécrite : sa couleur
        appartient au dépôt, pas à ce module.
        """
        existing = self._existing_label_names()
        if existing is None:
            return
        for name in labels:
            if name in existing:
                continue
            try:
                self._session.post(
                    f"{self._api}/repos/{self.repo}/labels",
                    headers=self._headers(),
                    json={
                        "name": name,
                        "color": _label_color(name),
                        "description": _LABEL_DESCRIPTIONS.get(name, ""),
                    },
                    timeout=_TIMEOUT,
                )
            except Exception:  # noqa: BLE001 — le ticket passe avant sa couleur
                continue

    def create_issue(
        self,
        *,
        title: str,
        body: str,
        labels: Mapping[str, Any] | list[str] | None = None,
        issue_type: str | None = None,
    ) -> dict[str, Any]:
        self.assert_repository_is_private()
        payload: dict[str, Any] = {"title": title, "body": body}
        if issue_type:
            # Le type d'issue de l'organisation (Task / Bug / Feature). Les
            # tableaux de projet filtrent dessus : sans type, un bug n'y
            # apparaît pas. `gh issue create` ne l'expose pas encore, l'API si.
            payload["type"] = issue_type
        if labels:
            labels = list(labels)
            self._ensure_labels(labels)
            payload["labels"] = labels
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
        created = response.json()
        self._add_to_project(created.get("node_id"))
        return created

    def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any] | None:
        """Un appel GraphQL qui ne lève jamais. ``None`` si ça n'a pas marché.

        Les tableaux de projet ne sont accessibles que par GraphQL, et le
        jeton peut très bien ne pas porter le droit `project` — c'est un droit
        distinct de celui d'écrire des issues. Un ticket créé mais non rangé
        reste un ticket ; un ticket perdu parce que le rangement a échoué
        serait une régression.
        """
        try:
            response = self._session.post(
                f"{self._api}/graphql",
                headers=self._headers(),
                json={"query": query, "variables": variables},
                timeout=_TIMEOUT,
            )
        except Exception:  # noqa: BLE001
            return None
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except Exception:  # noqa: BLE001
            return None
        if payload.get("errors"):
            return None
        return payload.get("data")

    def _add_to_project(self, node_id: str | None) -> None:
        """Range l'issue dans le tableau configuré, si les deux existent."""
        if not self._project_number or not node_id:
            return
        owner = self.repo.split("/", 1)[0]
        data = self._graphql(
            "query($owner:String!,$number:Int!){"
            "organization(login:$owner){projectV2(number:$number){id}}}",
            {"owner": owner, "number": self._project_number},
        )
        project_id = (
            ((data or {}).get("organization") or {}).get("projectV2") or {}
        ).get("id")
        if not project_id:
            return
        self._graphql(
            "mutation($projectId:ID!,$contentId:ID!){"
            "addProjectV2ItemById(input:{projectId:$projectId,contentId:$contentId})"
            "{item{id}}}",
            {"projectId": project_id, "contentId": node_id},
        )

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
