"""Empreinte de regroupement d'un signalement.

Vingt personnes rencontrant le *même* défaut produisent vingt
signalements, et vingt issues ouvertes disent vingt bugs là où il y en a
un. L'empreinte ci-dessous répond à une seule question : « est-ce
déjà connu ? » — pour que le vingt-et-unième rapport commente le ticket
existant au lieu d'en créer un de plus, et que le compteur de signalements
devienne ce qu'il devrait être : une mesure de l'impact.

Ce qu'elle prend, et pourquoi :

- **la route, normalisée** — ``/api/agents/42/runs`` et
  ``/api/agents/77/runs`` sont le même chemin de code ; garder l'id ferait
  une empreinte par agent ;
- **le statut** — un 500 et un 403 sur la même route sont deux défauts
  différents, pas deux occurrences d'un seul ;
- **la signature d'erreur, normalisée** — le message sans ses nombres,
  identifiants et chemins absolus, qui varient d'une occurrence à l'autre
  sans que le bug change.

Ce qu'elle ne prend PAS : la description écrite par l'utilisateur. Deux
personnes décrivent le même défaut avec des mots différents ; regrouper
sur la prose ne regrouperait jamais rien.
"""

from __future__ import annotations

import hashlib
import re

FINGERPRINT_LENGTH = 16

# Segments d'URL à remplacer : entiers, UUID, hexadécimal long, et les
# identifiants du produit (agent201, session_xxx…).
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)
_HEX_BLOB = re.compile(r"^[0-9a-f]{12,}$", re.IGNORECASE)
_TRAILING_DIGITS = re.compile(r"\d+$")

# Dans un message d'erreur : nombres, uuid, adresses mémoire et chemins
# absolus. Tout ce qui bouge sans que le défaut change.
_NOISE = (
    (re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I), "<id>"),
    (re.compile(r"0x[0-9a-f]+", re.IGNORECASE), "<addr>"),
    (re.compile(r"(?:/[\w.\-]+){2,}"), "<path>"),
    (re.compile(r"\b\d[\d_.,]*\b"), "<n>"),
    (re.compile(r"\s+"), " "),
)


def normalise_route(route: str | None) -> str:
    """``/api/agents/42/runs`` → ``/api/agents/{id}/runs``."""
    if not route:
        return ""
    path = route.split("?", 1)[0].split("#", 1)[0]
    segments = []
    for segment in path.split("/"):
        if not segment:
            segments.append(segment)
            continue
        if segment.isdigit() or _UUID.match(segment) or _HEX_BLOB.match(segment):
            segments.append("{id}")
        elif _TRAILING_DIGITS.search(segment) and not segment.isalpha():
            # agent201, run_17 : le préfixe porte le sens, le nombre non.
            segments.append(_TRAILING_DIGITS.sub("{id}", segment))
        else:
            segments.append(segment)
    return "/".join(segments)


def normalise_error(message: str | None) -> str:
    """Retire d'un message ce qui change à chaque occurrence."""
    if not message:
        return ""
    # La première ligne suffit : les suivantes sont la pile, qui porte les
    # mêmes informations avec plus de bruit.
    text = message.strip().splitlines()[0] if message.strip() else ""
    for pattern, replacement in _NOISE:
        text = pattern.sub(replacement, text)
    return text.strip().lower()[:300]


def compute_fingerprint(
    *,
    route: str | None,
    status: int | str | None,
    error_signature: str | None,
) -> str:
    """Empreinte stable et courte des trois composantes ci-dessus.

    Aucune des trois n'est obligatoire : un signalement purement narratif
    (« le bouton ne fait rien ») n'a ni statut ni erreur, et obtient tout
    de même une empreinte — celle de sa route. Deux rapports sans aucune
    des trois retombent sur la même empreinte vide, ce qui est le
    comportement voulu : ils ne sont distingués par rien.
    """
    parts = [
        normalise_route(route),
        str(status or ""),
        normalise_error(error_signature),
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return digest[:FINGERPRINT_LENGTH]


__all__ = [
    "FINGERPRINT_LENGTH",
    "compute_fingerprint",
    "normalise_error",
    "normalise_route",
]
