"""Schémas d'API du signalement de bug.

La forme de la charge utile est la fonctionnalité. Un champ « décrivez
votre problème » produit « ça marche pas » ; trois champs courts — ce que
je faisais, ce que j'attendais, ce que j'ai vu — produisent un rapport
reproductible, parce qu'ils demandent à l'utilisateur les seules choses
que lui seul sait. Tout le reste (route, versions, appels d'API,
erreurs de console, identifiants de corrélation) est joint par le client
sans qu'on le lui demande : c'est ce qu'il ne saurait pas recopier.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from apowerb.bug_reports.areas import BugArea


class BugSeverity(str, Enum):
    """Ce que l'utilisateur ne pouvait plus faire, pas « à quel point il est agacé ».

    Les libellés côté interface parlent d'usage (« je ne peux plus
    travailler », « il y a un contournement ») justement pour que ce champ
    reste comparable d'une personne à l'autre.
    """

    BLOCKER = "blocker"
    MAJOR = "major"
    MINOR = "minor"
    COSMETIC = "cosmetic"


class BugReportStatus(str, Enum):
    NEW = "new"
    TRIAGED = "triaged"
    ISSUE_CREATED = "issue_created"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
    RESOLVED = "resolved"


class ApiCallEntry(BaseModel):
    """Un appel d'API tel que le client l'a vu.

    ``request_id`` est la pièce maîtresse : le serveur renvoie cet
    identifiant sur chaque réponse (``X-Request-ID``), donc le client peut
    nous rendre l'identité exacte des requêtes concernées, et le serveur
    ressortir les lignes de log qui leur correspondent.
    """

    method: Optional[str] = Field(default=None, max_length=10)
    path: Optional[str] = Field(default=None, max_length=2048)
    status: Optional[int] = None
    request_id: Optional[str] = Field(default=None, max_length=128)
    duration_ms: Optional[float] = None
    error: Optional[str] = Field(default=None, max_length=2000)
    at: Optional[datetime] = None


class ConsoleEntry(BaseModel):
    level: Optional[str] = Field(default=None, max_length=20)
    message: Optional[str] = Field(default=None, max_length=4000)
    source: Optional[str] = Field(default=None, max_length=512)
    at: Optional[datetime] = None


class NavigationStep(BaseModel):
    """Un écran traversé avant le défaut.

    Le fil de navigation répond à « d'où je venais », qui est souvent la
    vraie question : un écran qui plante parce qu'on y est arrivé depuis
    un autre — avec un état hérité, un filtre resté posé, une sélection
    vide — est indiscernable d'un écran qui plante toujours, tant qu'on
    ne sait pas par où l'utilisateur est passé. C'est exactement ce que
    personne ne pense à écrire dans un rapport rédigé à la main.
    """

    route: Optional[str] = Field(default=None, max_length=512)
    label: Optional[str] = Field(default=None, max_length=200)
    at: Optional[datetime] = None
    dwell_ms: Optional[float] = None


class LastAction(BaseModel):
    """La dernière chose que l'utilisateur a faite avant que ça casse."""

    label: Optional[str] = Field(default=None, max_length=200)
    kind: Optional[str] = Field(default=None, max_length=40)
    target: Optional[str] = Field(default=None, max_length=300)
    at: Optional[datetime] = None


class ClientContext(BaseModel):
    """L'état de l'application au moment du défaut.

    Rempli par le client, jamais par l'utilisateur : ces champs sont
    exactement ceux qu'une personne ne pense pas à donner et qu'on lui
    redemande ensuite par courriel, une journée plus tard.
    """

    url: Optional[str] = Field(default=None, max_length=2048)
    route: Optional[str] = Field(default=None, max_length=512)
    # « Où j'étais », côté machine : le nom lisible de l'écran, le chemin
    # qui y mène dans l'interface, les écrans traversés avant, et le
    # dernier geste. La route seule dit l'adresse, pas le contexte.
    screen: Optional[str] = Field(default=None, max_length=200)
    section: Optional[str] = Field(default=None, max_length=200)
    navigation_trail: list[NavigationStep] = Field(default_factory=list, max_length=10)
    last_action: Optional[LastAction] = None
    app_version: Optional[str] = Field(default=None, max_length=100)
    user_agent: Optional[str] = Field(default=None, max_length=512)
    viewport: Optional[str] = Field(default=None, max_length=50)
    locale: Optional[str] = Field(default=None, max_length=20)
    theme: Optional[str] = Field(default=None, max_length=20)
    agent_id: Optional[str] = Field(default=None, max_length=200)
    conversation_id: Optional[str] = Field(default=None, max_length=200)
    occurred_at: Optional[datetime] = None
    extra: dict[str, Any] = Field(default_factory=dict)


class BugReportCreate(BaseModel):
    """Ce qu'envoie le bouton « Signaler un problème »."""

    title: Optional[str] = Field(default=None, max_length=200)
    # Pré-rempli par l'interface avec l'écran détecté, et laissé
    # modifiable : la machine sait sur quelle route on était, l'utilisateur
    # sait ce qu'il était en train de faire là — « dans la config de
    # l'agent commercial, onglet Outils ». Les deux comptent, aucune ne
    # remplace l'autre.
    # La fonctionnalité concernée. Laissée à ``None``, elle est déduite
    # de la route côté serveur : un client qui ne l'envoie pas obtient
    # quand même un signalement classé.
    area: Optional[BugArea] = None
    where_i_was: Optional[str] = Field(default=None, max_length=1000)
    what_i_did: str = Field(min_length=1, max_length=4000)
    expected: Optional[str] = Field(default=None, max_length=4000)
    observed: Optional[str] = Field(default=None, max_length=4000)
    severity: BugSeverity = BugSeverity.MAJOR

    context: ClientContext = Field(default_factory=ClientContext)
    api_calls: list[ApiCallEntry] = Field(default_factory=list, max_length=50)
    console: list[ConsoleEntry] = Field(default_factory=list, max_length=100)

    # Image encodée en data URL (``data:image/png;base64,…``). Envoyée
    # seulement si l'utilisateur a vu l'aperçu et confirmé : une capture
    # part avec le contenu de son écran, ce n'est pas une métadonnée.
    screenshot: Optional[str] = Field(default=None)
    screenshot_consent: bool = False


class BugReportSummary(BaseModel):
    id: int
    title: str
    area: BugArea = BugArea.OTHER
    severity: BugSeverity
    status: BugReportStatus
    fingerprint: str
    occurrences: int
    reporter_email: Optional[str] = None
    route: Optional[str] = None
    issue_url: Optional[str] = None
    has_screenshot: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class BugReportDetail(BugReportSummary):
    where_i_was: Optional[str] = None
    what_i_did: Optional[str] = None
    expected: Optional[str] = None
    observed: Optional[str] = None
    context: dict[str, Any] = Field(default_factory=dict)
    api_calls: list[dict[str, Any]] = Field(default_factory=list)
    console: list[dict[str, Any]] = Field(default_factory=list)
    server_logs: list[dict[str, Any]] = Field(default_factory=list)
    request_ids: list[str] = Field(default_factory=list)
    run_id: Optional[str] = None
    admin_note: Optional[str] = None


class BugReportListResponse(BaseModel):
    items: list[BugReportSummary]
    total: int


class BugReportCreated(BaseModel):
    """Réponse à l'envoi.

    ``duplicate_of`` n'est pas une politesse : quand le défaut est déjà
    connu, le dire tout de suite évite à l'utilisateur d'attendre une
    réponse pour un ticket qui existe, et lui donne le numéro à suivre.
    """

    id: int
    fingerprint: str
    occurrences: int
    duplicate_of: Optional[int] = None
    logs_attached: int = 0


class AreaOption(BaseModel):
    """Une entrée du menu déroulant des fonctionnalités."""

    value: str
    label: str


class BugReportUpdate(BaseModel):
    status: Optional[BugReportStatus] = None
    # Le triage corrige le classement quand la déduction s'est trompée ;
    # c'est ce qui garde les compteurs par fonctionnalité honnêtes.
    area: Optional[BugArea] = None
    severity: Optional[BugSeverity] = None
    admin_note: Optional[str] = Field(default=None, max_length=4000)


__all__ = [
    "ApiCallEntry",
    "AreaOption",
    "BugArea",
    "BugReportCreate",
    "BugReportCreated",
    "BugReportDetail",
    "BugReportListResponse",
    "BugReportStatus",
    "BugReportSummary",
    "BugReportUpdate",
    "BugSeverity",
    "ClientContext",
    "ConsoleEntry",
    "LastAction",
    "NavigationStep",
]
