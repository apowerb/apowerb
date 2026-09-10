"""La fonctionnalité concernée : liste fermée, et déduction depuis la route.

Pourquoi une liste et pas un champ libre. Le triage a besoin de compter :
« sept bugs sur les intégrations cette semaine » est une décision
d'équipe, « sept formulations différentes du mot intégration » n'est
rien. Un champ libre produit le second — mesurable sur n'importe quel
outil de ticket où le champ « composant » a été laissé ouvert.

Pourquoi une liste **pré-remplie**. Demander à l'utilisateur de classer
son propre bug le fait se tromper : il choisit la fonctionnalité qu'il
croyait utiliser, pas celle qui a cassé. La route, elle, ne se trompe
pas. On déduit donc la valeur, on la présente sélectionnée, et on la
laisse modifiable — parce que l'écran affiché n'est pas toujours le
coupable, et que l'utilisateur voit parfois ce que la route ignore.

⚠️ Ajouter une zone ici ne suffit pas : l'interface affiche le libellé
traduit correspondant. Une valeur inconnue du front retomberait sur son
identifiant brut — visible, pas cassé.
"""

from __future__ import annotations

import re
from enum import Enum


class BugArea(str, Enum):
    CHAT = "chat"
    AGENTS = "agents"
    TOOLS = "tools"
    SKILLS = "skills"
    KNOWLEDGE = "knowledge"          # RAG, documents indexés
    INTEGRATIONS = "integrations"    # Google, Microsoft, GitHub…
    EMAILING = "emailing"
    WORKFLOWS = "workflows"
    SCHEDULER = "scheduler"
    WEBHOOKS = "webhooks"
    FILES = "files"                  # fichiers, artefacts, téléversements
    ANALYTICS = "analytics"          # BI, graphiques, tableaux de bord
    HUB = "hub"
    MODELS = "models"                # fournisseurs et clés de modèles
    ADMIN = "admin"                  # panneau d'administration, utilisateurs
    ACCOUNT = "account"              # connexion, mot de passe, profil
    UI = "ui"                        # affichage, navigation, traduction
    OTHER = "other"


# Libellés de secours, en français, pour un client qui n'a pas de
# traduction : un identifiant technique dans une liste déroulante fait
# choisir au hasard.
AREA_LABELS: dict[BugArea, str] = {
    BugArea.CHAT: "Conversation avec un agent",
    BugArea.AGENTS: "Agents (création, configuration)",
    BugArea.TOOLS: "Outils",
    BugArea.SKILLS: "Compétences",
    BugArea.KNOWLEDGE: "Base de connaissances / documents",
    BugArea.INTEGRATIONS: "Intégrations (Google, Microsoft, GitHub…)",
    BugArea.EMAILING: "Courriel et campagnes",
    BugArea.WORKFLOWS: "Workflows",
    BugArea.SCHEDULER: "Planification et exécutions programmées",
    BugArea.WEBHOOKS: "Webhooks",
    BugArea.FILES: "Fichiers et artefacts",
    BugArea.ANALYTICS: "Tableaux de bord et graphiques",
    BugArea.HUB: "Hub",
    BugArea.MODELS: "Modèles et clés d'API",
    BugArea.ADMIN: "Administration et utilisateurs",
    BugArea.ACCOUNT: "Mon compte et connexion",
    BugArea.UI: "Affichage, navigation, traduction",
    BugArea.OTHER: "Autre / je ne sais pas",
}

# Motifs testés dans l'ordre : le PREMIER qui correspond gagne, donc les
# plus spécifiques d'abord. `/api/agents/x/tools` est un problème d'outil,
# pas d'agent — l'ordre le dit, et c'est le seul endroit où il est dit.
_ROUTE_RULES: tuple[tuple[re.Pattern[str], BugArea], ...] = (
    (re.compile(r"/(tools|tool-config|tools-manager)\b"), BugArea.TOOLS),
    (re.compile(r"/(skills)\b"), BugArea.SKILLS),
    (re.compile(r"/(rag|knowledge|documents|data-lake|datasets?)\b"), BugArea.KNOWLEDGE),
    (re.compile(r"/(integrations|onedrive|google-drive|oauth)\b"), BugArea.INTEGRATIONS),
    (re.compile(r"/(emailing|mail|campaign)"), BugArea.EMAILING),
    (re.compile(r"/(workflows?)\b"), BugArea.WORKFLOWS),
    (re.compile(r"/(scheduler|schedules?|pipelines?)\b"), BugArea.SCHEDULER),
    (re.compile(r"/(webhooks?)\b"), BugArea.WEBHOOKS),
    (re.compile(r"/(files?|artifacts?|uploads?)\b"), BugArea.FILES),
    (re.compile(r"/(bi|charts?|dashboards?|analytics|stats)\b"), BugArea.ANALYTICS),
    (re.compile(r"/(hub)\b"), BugArea.HUB),
    (re.compile(r"/(models?|providers?|api-keys?|saved-api-keys)\b"), BugArea.MODELS),
    (re.compile(r"/(admin|supervision|users?|logging)\b"), BugArea.ADMIN),
    (re.compile(r"/(auth|login|register|password|profile|account|settings)\b"), BugArea.ACCOUNT),
    (re.compile(r"/(chat|chatbot|conversations?|sessions?|runs?|adk)\b"), BugArea.CHAT),
    (re.compile(r"/(agents?|superagents?)\b"), BugArea.AGENTS),
)


def infer_area(*candidates: str | None) -> BugArea:
    """Déduit la zone à partir des routes fournies, la plus précise d'abord.

    On passe volontiers plusieurs candidats — la route de l'écran ET le
    chemin de l'appel qui a échoué. Le second est le plus fiable quand il
    existe : un écran de chat qui plante en enregistrant une clé d'API
    est un bug de « modèles », et seul le chemin de l'appel le dit.
    """
    for candidate in candidates:
        if not candidate:
            continue
        path = candidate.split("?", 1)[0].lower()
        for pattern, area in _ROUTE_RULES:
            if pattern.search(path):
                return area
    return BugArea.OTHER


def area_options() -> list[dict[str, str]]:
    """La liste servie à l'interface pour peupler le menu déroulant."""
    return [{"value": area.value, "label": AREA_LABELS[area]} for area in BugArea]


__all__ = ["AREA_LABELS", "BugArea", "area_options", "infer_area"]
