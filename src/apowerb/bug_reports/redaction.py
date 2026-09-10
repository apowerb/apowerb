"""Expurgation des pièces jointes à un signalement de bug.

Un rapport de bug est la seule fonctionnalité du produit qui demande à
l'utilisateur de nous envoyer *ce qu'il avait sous les yeux* : l'URL en
cours, les derniers appels d'API, les erreurs de la console. Ces trois
sources transportent des jetons — un ``access_token`` dans une query
string d'OAuth, un ``Authorization: Bearer`` recopié par un log de
navigateur, une clé d'API collée dans un message d'erreur. Sans le
filtre ci-dessous, la fonctionnalité censée aider le support devient le
chemin le plus court entre un secret et une issue GitHub.

Deux règles, apprises d'un incident réel (04/09/2026 : un filtre par
*préfixe* sur l'environnement d'un service a sorti un mot de passe ERP
en clair) :

- **On masque la valeur, jamais on ne retire la clé.** Voir
  ``password=<redacted>`` dit au relecteur qu'un mot de passe circulait
  à cet endroit ; supprimer la ligne le lui cache.
- **On liste les noms sensibles, on ne devine pas par préfixe.** Un
  préfixe attrape trop peu (il rate ``pwd``) et laisse croire qu'il
  attrape tout.

Le filtre est délibérément grossier sur les *formes* de jetons connues
(``sk-``, ``ghp_``, JWT) : un faux positif coûte un mot masqué dans une
description, un faux négatif coûte un secret publié.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "<redacted>"

# Noms de paramètres dont la VALEUR ne doit jamais être conservée, qu'ils
# apparaissent dans une query string, un en-tête ou un corps JSON. Liste
# explicite : un préfixe (`grep ^SECRET_`) donne l'illusion de la
# couverture sans l'avoir.
SENSITIVE_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "client_secret",
        "code",
        "credential",
        "id_token",
        "key",
        "passwd",
        "password",
        "pwd",
        "refresh_token",
        "secret",
        "session",
        "signature",
        "token",
        "x-api-key",
    }
)

# Formes de jetons reconnaissables hors de tout contexte clé/valeur : ce
# qui traîne dans un message d'erreur brut ou une trace collée.
_TOKEN_SHAPES = (
    # JWT — trois segments base64url séparés par des points.
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b"),
    # Jetons GitHub (ghp_, gho_, ghu_, ghs_, ghr_, github_pat_).
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    # Clés OpenAI / Anthropic et cousines.
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{16,}\b"),
    # AWS access key id.
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    # « Bearer <quelque chose de long> », y compris quand le jeton lui-même
    # n'a aucune forme reconnaissable.
    re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
)

# `clé=valeur` et `"clé": "valeur"` dans du texte libre. Le nom est
# capturé pour être conservé ; seule la valeur part.
_ASSIGNMENT = re.compile(
    r"""(?P<key>[A-Za-z_][A-Za-z0-9_-]*)     # nom
        (?P<sep>\s*[=:]\s*)                  # = ou :
        (?P<quote>["']?)                     # guillemet éventuel
        (?P<value>[^\s"'&,;}]{1,4096})       # valeur
        (?P=quote)""",
    re.VERBOSE,
)


def _is_sensitive(name: str) -> bool:
    return name.strip().lower().lstrip("-") in SENSITIVE_KEYS


def redact_text(value: str | None, *, max_length: int = 4000) -> str | None:
    """Masque les secrets d'un texte libre, puis le borne.

    ``max_length`` n'est pas cosmétique : un signalement peut embarquer
    une trace de 2 Mo, et une table n'est pas un puits sans fond.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)

    for pattern in _TOKEN_SHAPES:
        value = pattern.sub(REDACTED, value)

    def _mask_assignment(match: re.Match[str]) -> str:
        if not _is_sensitive(match.group("key")):
            return match.group(0)
        quote = match.group("quote")
        return f"{match.group('key')}{match.group('sep')}{quote}{REDACTED}{quote}"

    value = _ASSIGNMENT.sub(_mask_assignment, value)

    if len(value) > max_length:
        value = value[:max_length] + f"… [tronqué à {max_length} caractères]"
    return value


def redact_url(url: str | None) -> str | None:
    """Masque la valeur des paramètres sensibles d'une URL.

    Le chemin est conservé tel quel : c'est lui qui dit *où* le bug s'est
    produit, et c'est la première chose que lira celui qui corrige.
    """
    if not url:
        return url
    head, sep, query = url.partition("?")
    if not sep:
        return head
    parts = []
    for pair in query.split("&"):
        name, eq, _value = pair.partition("=")
        if eq and _is_sensitive(name):
            parts.append(f"{name}={REDACTED}")
        else:
            parts.append(pair)
    return f"{head}?{'&'.join(parts)}"


def redact_mapping(data: Any, *, depth: int = 0) -> Any:
    """Version récursive pour les objets JSON (contexte client, en-têtes).

    ``depth`` borne la récursion : la charge vient du navigateur, donc
    d'une source qu'on ne contrôle pas.
    """
    if depth > 6:
        return REDACTED
    if isinstance(data, dict):
        out = {}
        for key, value in data.items():
            if _is_sensitive(str(key)):
                out[key] = REDACTED
            else:
                out[key] = redact_mapping(value, depth=depth + 1)
        return out
    if isinstance(data, (list, tuple)):
        return [redact_mapping(item, depth=depth + 1) for item in data[:200]]
    if isinstance(data, str):
        return redact_text(data)
    return data


__all__ = [
    "REDACTED",
    "SENSITIVE_KEYS",
    "redact_mapping",
    "redact_text",
    "redact_url",
]
