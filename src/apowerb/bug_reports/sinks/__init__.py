"""Sorties d'un signalement validé.

Un signalement vit d'abord dans la base du déploiement. Le porter
ailleurs — une issue GitHub, un webhook — est un geste supplémentaire,
déclenché par un administrateur après relecture, jamais par l'envoi
lui-même. C'est la seule barrière qui empêche une capture d'écran ou une
ligne de log client de partir vers un service tiers sans que personne
l'ait regardée.
"""

from apowerb.bug_reports.sinks.github import (
    GitHubIssueSink,
    SinkConfigurationError,
    SinkDeliveryError,
    SinkRefusal,
)

__all__ = [
    "GitHubIssueSink",
    "SinkConfigurationError",
    "SinkDeliveryError",
    "SinkRefusal",
]
