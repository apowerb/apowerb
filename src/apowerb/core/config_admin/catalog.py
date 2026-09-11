"""La liste FERMÉE des variables qu'un superadministrateur peut poser.

Une liste blanche de noms, jamais un préfixe. Le 04/09/26, un filtre par
préfixe sur l’environnement d’un service a déversé un mot
de passe ERP en clair : sur 34 noms, 9 étaient des accès à un ERP que
personne n'avait l'intention d'exposer. Un préfixe décrit ce qu'on croit
avoir nommé, une liste décrit ce qu'on a vraiment autorisé.

Ce qui n'est PAS ici, et pourquoi :

- ``DB_*`` — se couper de sa propre base depuis un écran web, ou pointer
  l'instance vers une base qu'on contrôle, est le sabotage le plus direct
  qu'offrirait cet écran.
- ``ENCRYPT_KEY`` — la clé qui chiffre les jetons OAuth *et* les valeurs
  posées ici. La changer rendrait illisibles toutes les intégrations
  existantes, sans rien casser de visible avant le prochain rafraîchissement
  de jeton.
- ``DEFAULT_SUPERADMIN_EMAIL`` / ``DEFAULT_SUPERADMIN_PASSWORD`` — se
  désigner soi-même superadministrateur au prochain démarrage.
- ``BYPASS_AUTH`` — ``auth/dependencies.py`` le lit au niveau module pour
  désactiver l'authentification. Le rendre modifiable depuis un écran web
  reviendrait à offrir un interrupteur « plus d'authentification ».

Les noms retenus sont exactement ceux que ``core/setup_status.py`` nomme
comme manquants dans sa checklist, plus les compagnons qu'on ne peut pas
poser séparément (``SMTP_USER``/``SMTP_PASSWORD`` n'apparaissent pas dans
la checklist parce qu'ils sont facultatifs, mais un serveur SMTP
authentifié est inutilisable sans eux). ``test_config_catalog.py`` vérifie
cette inclusion : la liste ne peut pas dériver de la checklist en silence.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

# Une valeur d'environnement tient sur une ligne. Ce n'est pas une
# convenance : ``overlay.py`` écrit un fichier que l'entrypoint source, et
# une valeur à rallonge ou contenant un saut de ligne y produirait des
# lignes fantômes — donc des variables jamais demandées. La règle de
# validation et le format d'export sont deux moitiés de la même décision.
MAX_VALUE_LENGTH = 4096


class InvalidValue(ValueError):
    """Valeur refusée. Le message nomme la règle, JAMAIS la valeur reçue."""


@dataclass(frozen=True)
class Variable:
    """Une variable modifiable, telle que l'écran la présente."""

    name: str
    # La capacité de `setup_status.py` qu'elle sert — l'écran regroupe par là.
    capability: str
    # Un secret n'est jamais réaffiché, même masqué partiellement : l'écran
    # dit « posé le … », pas « sk-…4f2a ». Les non-secrets (une URL, un nom
    # de modèle) ne sont pas réaffichés non plus — l'invariant d'écriture
    # seule ne fait pas d'exception — mais le drapeau permet à l'écran de
    # prévenir avant de coller une valeur dans un champ visible.
    secret: bool = True
    # Normalise et refuse. Rend la valeur telle qu'elle sera STOCKÉE : le
    # 04/09, un espace final avait passé une garde qui nettoyait sans
    # stocker le nettoyé. Ici c'est le retour qui part en base.
    normalize: Optional[Callable[[str], str]] = None


def _plain(value: str) -> str:
    """Le contrôle que toute valeur subit, quelle que soit sa variable."""
    v = value.strip()
    if not v:
        raise InvalidValue("Une valeur vide n'est pas une configuration : "
                           "supprimez la variable pour revenir au défaut.")
    if len(v) > MAX_VALUE_LENGTH:
        raise InvalidValue(f"Valeur trop longue (maximum {MAX_VALUE_LENGTH} caractères).")
    # Sauts de ligne et caractères de contrôle : cf. MAX_VALUE_LENGTH.
    if any(ord(c) < 32 or ord(c) == 127 for c in v):
        raise InvalidValue("Une valeur ne peut contenir ni saut de ligne "
                           "ni caractère de contrôle.")
    return v


def _url(value: str) -> str:
    v = _plain(value)
    scheme, sep, rest = v.partition("://")
    if not sep or not scheme.isalpha() or not rest:
        raise InvalidValue("Attendu une URL absolue, de la forme https://hôte.")
    return v


def _port(value: str) -> str:
    v = _plain(value)
    if not v.isdigit() or not (1 <= int(v) <= 65535):
        raise InvalidValue("Attendu un numéro de port entre 1 et 65535.")
    return v


def _storage_mode(value: str) -> str:
    v = _plain(value).upper()
    if v not in {"LOCAL", "S3"}:
        raise InvalidValue("Attendu LOCAL ou S3.")
    return v


def _github_repo(value: str) -> str:
    """``organisation/dépôt``, et rien d'autre.

    Ni URL, ni nom seul : le sink construit ses appels d'API à partir de
    cette valeur, et une URL collée depuis la barre du navigateur y
    produirait des chemins comme ``/repos/https://github.com/org/dépôt``.
    On refuse tôt, avec un message qui montre la forme attendue, plutôt
    que de laisser GitHub répondre 404 le jour de la première publication.
    """
    v = _plain(value)
    if v.startswith(("http://", "https://", "git@")):
        raise InvalidValue(
            "Attendu « organisation/dépôt », pas une URL — par exemple "
            "acme/support et non https://github.com/acme/support."
        )
    parts = v.split("/")
    if len(parts) != 2 or not all(parts) or any(c in v for c in " \t"):
        raise InvalidValue("Attendu « organisation/dépôt », par exemple acme/support.")
    for part in parts:
        if not all(c.isalnum() or c in "._-" for c in part):
            raise InvalidValue(
                "Nom de dépôt invalide : lettres, chiffres, point, tiret et "
                "tiret bas seulement."
            )
    return v


def _email(value: str) -> str:
    v = _plain(value)
    # Volontairement minimal : la validation d'adresse appartient au serveur
    # SMTP, pas à nous. On refuse ce qui n'est manifestement pas une adresse.
    if "@" not in v or v.startswith("@") or v.endswith("@"):
        raise InvalidValue("Attendu une adresse électronique.")
    return v


CATALOG: tuple[Variable, ...] = (
    # -- modèle mutualisé ---------------------------------------------------
    Variable("DEFAULT_LLM_MODEL", "default_llm", secret=False, normalize=_plain),
    Variable("DEFAULT_LLM_API_KEY", "default_llm"),
    Variable("DEFAULT_LLM_API_BASE", "default_llm", secret=False, normalize=_url),
    # -- stockage objet -----------------------------------------------------
    Variable("STORAGE_MODE", "object_storage", secret=False, normalize=_storage_mode),
    Variable("S3_BUCKET_NAME", "object_storage", secret=False, normalize=_plain),
    Variable("S3_ENDPOINT", "object_storage", secret=False, normalize=_url),
    Variable("S3_REGION", "object_storage", secret=False, normalize=_plain),
    Variable("S3_ACCESS_KEY", "object_storage"),
    Variable("S3_ACCESS_KEY_SECRET", "object_storage"),
    # -- Microsoft ----------------------------------------------------------
    Variable("MICROSOFT_INTEGRATION_CLIENT_ID", "microsoft_integration", secret=False, normalize=_plain),
    Variable("MICROSOFT_INTEGRATION_CLIENT_SECRET", "microsoft_integration"),
    Variable("MICROSOFT_INTEGRATION_TENANT_ID", "microsoft_integration", secret=False, normalize=_plain),
    # -- Google -------------------------------------------------------------
    Variable("GOOGLE_INTEGRATION_CLIENT_ID", "google_integration", secret=False, normalize=_plain),
    Variable("GOOGLE_INTEGRATION_CLIENT_SECRET", "google_integration"),
    Variable("GOOGLE_WEBHOOK_AUDIENCE", "google_integration", secret=False, normalize=_plain),
    # -- orchestration ------------------------------------------------------
    Variable("TH2ETL_BASE_URL", "orchestration", secret=False, normalize=_url),
    Variable("TH2ETL_API_KEY", "orchestration"),
    # -- observabilité ------------------------------------------------------
    Variable("OTEL_EXPORTER_OTLP_ENDPOINT", "observability", secret=False, normalize=_url),
    # -- courrier système ---------------------------------------------------
    Variable("SMTP_HOST", "system_mail", secret=False, normalize=_plain),
    Variable("SMTP_PORT", "system_mail", secret=False, normalize=_port),
    Variable("SMTP_FROM", "system_mail", secret=False, normalize=_email),
    Variable("SMTP_USER", "system_mail", secret=False, normalize=_plain),
    Variable("SMTP_PASSWORD", "system_mail"),
    # -- signalement de bug -------------------------------------------------
    # Où partent les issues créées depuis l'écran de triage. Chaque
    # déploiement a le sien : laisser ce choix à l'environnement obligeait
    # à passer par celui qui déploie pour une décision qui appartient à
    # l'administrateur du produit.
    #
    # ⚠️ `BUG_REPORT_GITHUB_ALLOW_PUBLIC` n'est PAS ici, et c'est délibéré.
    # Elle désarme le garde qui refuse d'écrire dans un dépôt public, alors
    # qu'un signalement porte des logs serveur, des erreurs client et
    # l'adresse de celui qui l'a envoyé. L'exposer dans cet écran offrirait
    # un interrupteur « publier les captures d'écran de mes utilisateurs »,
    # à deux clics — la même raison qui tient `BYPASS_AUTH` dehors.
    Variable("BUG_REPORT_GITHUB_REPO", "bug_reports", secret=False, normalize=_github_repo),
    Variable("BUG_REPORT_GITHUB_TOKEN", "bug_reports"),
)

BY_NAME: dict[str, Variable] = {v.name: v for v in CATALOG}


def is_writable(name: str) -> bool:
    """Le nom est-il dans la liste ? Comparaison exacte et sensible à la
    casse : les noms d'environnement sont en majuscules, et accepter
    ``encrypt_key`` parce que ``ENCRYPT_KEY`` est refusé serait la même
    faille avec un détour."""
    return name in BY_NAME


def normalize(name: str, value: str) -> str:
    """La valeur telle qu'elle sera stockée, ou ``InvalidValue``.

    Refuse d'abord le nom : un appelant qui n'a pas vérifié l'appartenance
    ne doit pas obtenir une normalisation qui ressemble à une autorisation.
    """
    variable = BY_NAME.get(name)
    if variable is None:
        raise InvalidValue(f"{name} ne fait pas partie des variables modifiables.")
    return (variable.normalize or _plain)(value)
