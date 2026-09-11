"""Lire, poser et supprimer une valeur — sans jamais la rendre lisible.

Ce module est le seul endroit qui manipule une valeur en clair, et il n'a
aucune fonction qui en renvoie une à un appelant HTTP. ``read_all_decrypted``
existe pour ``overlay.py`` seul, qui tourne AVANT le serveur, dans le
processus d'entrypoint : elle n'est jamais atteignable depuis une requête.
"""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.configs.settings import get_settings
from apowerb.core.config_admin.catalog import CATALOG, BY_NAME, normalize
from apowerb.helpers import encryptor
from apowerb.helpers.audit_log import audit

# L'instant où CE processus a construit sa configuration. Une ligne posée
# après cet instant n'est pas appliquée : rien ne la relit à chaud (voir
# ``overlay.py``). Sert à dire « en attente de redémarrage » sans jamais
# comparer des valeurs.
PROCESS_STARTED_AT = datetime.now(timezone.utc)

# Ce que l'écran a le droit de savoir d'une variable.
SOURCE_ENV = "env"
SOURCE_DATABASE = "database"
SOURCE_UNSET = "unset"

# Les NOMS que notre propre entrypoint a exportés au démarrage, jamais leurs
# valeurs. Sans cette trace, le processus ne peut pas distinguer sa propre
# empreinte de celle du déploiement : au redémarrage, une valeur posée depuis
# l'écran EST dans `os.environ` (c'est tout l'objet de l'overlay), et une
# lecture naïve la déclarerait « imposée par le déploiement » — l'écran
# retirerait alors le champ de la variable qu'on venait d'y poser, sans plus
# aucun moyen de la corriger depuis l'interface.
#
# Limite assumée : un opérateur qui poserait CETTE variable à la main dans son
# déploiement, en y nommant une variable qu'il pose aussi lui-même et qui a une
# ligne en base, verrait l'écran lui offrir un champ dont l'écriture resterait
# inerte. Il faut pour cela nommer soi-même une variable interne ; la
# précédence, elle, reste intacte — `overlay_lines` n'exporte jamais par-dessus
# une variable présente dans l'environnement.
OVERLAY_MARKER = "APOWERB_CONFIG_APPLIED"


@dataclass(frozen=True)
class VariableState:
    """L'état d'une variable, tel qu'il part vers l'écran. Pas de valeur,
    pas de longueur, pas d'empreinte : rien dont on puisse déduire quoi
    que ce soit du contenu."""

    name: str
    capability: str
    secret: bool
    source: str
    updated_at: datetime | None = None
    updated_by: str | None = None
    # Une valeur posée après le démarrage de ce processus ne sert encore à
    # rien. Le dire est la seule façon d'éviter qu'un administrateur croie
    # avoir réparé une intégration qui continue d'échouer.
    pending_restart: bool = False


def _schema() -> str:
    # Lu à l'appel, pas capturé au niveau module : 33 modules de ce dépôt
    # capturent ``get_settings()`` à l'import, et c'est exactement ce qui
    # rend un rechargement à chaud illusoire (cf. le commentaire d'overlay).
    return get_settings().db_schema


def env_holds(name: str, env=None) -> bool:
    """L'environnement du processus porte-t-il une valeur pour ce nom ?

    Non vide, pas seulement présent : une variable déclarée vide dans un
    déploiement se lit comme configurée et se comporte comme rien — le plus
    vieux piège de ce dépôt (cf. ``_usable_as_a_base`` dans settings.py).
    Un ``secretKeyRef`` en ``optional: true`` dont la clé manque au Secret ne
    déclare rien du tout : la variable est absente, et reste donc posable.

    C'est LA règle de précédence, et ``overlay.overlay_lines`` applique la
    même : ce qui est ici ne sera jamais recouvert par l'overlay.
    """
    e = os.environ if env is None else env
    return bool((e.get(name) or "").strip())


def overlay_applied(name: str, env=None) -> bool:
    """Est-ce NOTRE entrypoint qui a mis ce nom dans l'environnement ?

    Répond à partir de ``OVERLAY_MARKER``, une liste de noms — jamais de
    valeurs. Sépare « le déploiement impose » de « nous avons appliqué ce que
    l'écran a posé », deux situations identiques vues de ``os.environ`` et
    opposées pour l'administrateur.
    """
    e = os.environ if env is None else env
    poses = (e.get(OVERLAY_MARKER) or "").split(",")
    return name in {pose.strip() for pose in poses if pose.strip()}


async def list_states(db: AsyncSession) -> list[VariableState]:
    """L'état des variables du catalogue. Une seule requête, quel que soit
    le nombre de variables."""
    rows = (await db.execute(text(
        f"SELECT name, updated_at, updated_by FROM {_schema()}.admin_config_variable"
    ))).all()
    stored = {r[0]: (r[1], r[2]) for r in rows}

    states: list[VariableState] = []
    for variable in CATALOG:
        posed = stored.get(variable.name)
        # Trois cas, et le second est celui qui a manqué au premier jet :
        #
        # - le déploiement porte une valeur que nous n'avons pas exportée :
        #   il gagne, et une ligne en base par-dessous est inerte. L'écran le
        #   dit plutôt que de laisser croire qu'elle s'applique ;
        # - le déploiement porte une valeur, mais c'est NOTRE overlay qui l'y
        #   a mise au démarrage, et la ligne en base existe : c'est donc bien
        #   une valeur posée depuis l'écran, encore modifiable depuis l'écran ;
        # - rien dans l'environnement : la ligne en base décide, ou rien.
        #
        # La ligne en base est exigée dans le second cas : un marqueur qui
        # nommerait une variable sans ligne correspondante ne prouve rien, et
        # le déploiement doit alors garder la main.
        notre_empreinte = overlay_applied(variable.name) and posed is not None
        if env_holds(variable.name) and not notre_empreinte:
            source = SOURCE_ENV
        elif posed is not None:
            source = SOURCE_DATABASE
        else:
            source = SOURCE_UNSET

        updated_at = posed[0] if posed else None
        states.append(VariableState(
            name=variable.name,
            capability=variable.capability,
            secret=variable.secret,
            source=source,
            updated_at=updated_at,
            updated_by=posed[1] if posed else None,
            pending_restart=(
                source == SOURCE_DATABASE
                and updated_at is not None
                and updated_at > PROCESS_STARTED_AT
                # Sauf celles que leur consommateur relit à l'usage : leur
                # annoncer un redémarrage ferait redémarrer un service pour
                # rien, et décrédibiliserait le même message quand il est vrai.
                and not variable.applied_live
            ),
        ))
    return states


async def put(db: AsyncSession, *, name: str, value: str, actor: str) -> VariableState:
    """Pose une valeur. Rend l'état, jamais la valeur.

    ``normalize`` refuse d'abord le nom : un appelant qui aurait oublié le
    contrôle d'appartenance n'obtient pas une écriture par inadvertance.
    Et c'est la valeur NORMALISÉE qui part en base — le 04/09, une garde
    qui nettoyait sans stocker le nettoyé a laissé passer un espace final.
    """
    clean = normalize(name, value)
    # Pas de repli en clair : même règle que les jetons OAuth (B7). Sans
    # ENCRYPT_KEY, on refuse d'écrire.
    ciphertext = encryptor.encrypt_value(clean)
    # `clean` ne doit plus apparaître nulle part après cette ligne.
    del clean, value

    await db.execute(text(
        f"INSERT INTO {_schema()}.admin_config_variable "
        "(name, value_enc, updated_at, updated_by) "
        "VALUES (:n, :v, NOW(), :a) "
        "ON CONFLICT (name) DO UPDATE SET "
        "value_enc = EXCLUDED.value_enc, updated_at = NOW(), updated_by = EXCLUDED.updated_by"
    ), {"n": name, "v": ciphertext, "a": actor})
    await _record(db, name=name, action="set", actor=actor)
    await db.commit()
    return await state_of(db, name)


async def delete(db: AsyncSession, *, name: str, actor: str) -> VariableState:
    """Retire la valeur posée. La variable retombe sur l'environnement, ou
    sur rien — c'est le chemin de sortie sans passer par du SQL."""
    await db.execute(text(
        f"DELETE FROM {_schema()}.admin_config_variable WHERE name = :n"
    ), {"n": name})
    await _record(db, name=name, action="delete", actor=actor)
    await db.commit()
    return await state_of(db, name)


async def state_of(db: AsyncSession, name: str) -> VariableState:
    return next(s for s in await list_states(db) if s.name == name)


async def _record(db: AsyncSession, *, name: str, action: str, actor: str) -> None:
    """Le nom, l'action, l'acteur, l'instant. Jamais la valeur — ni en clair,
    ni chiffrée, ni résumée. Deux destinations : la table (qui survit à la
    rotation des journaux) et le logger d'audit dédié (que les pipelines
    routent déjà à part)."""
    await db.execute(text(
        f"INSERT INTO {_schema()}.admin_config_audit (name, action, actor) "
        "VALUES (:n, :act, :a)"
    ), {"n": name, "act": action, "a": actor})
    audit(f"config.{action}", user_id=actor, variable=name)


async def recent_audit(db: AsyncSession, limit: int = 50) -> list[dict]:
    rows = (await db.execute(text(
        f"SELECT name, action, actor, at FROM {_schema()}.admin_config_audit "
        "ORDER BY at DESC LIMIT :l"
    ), {"l": max(1, min(limit, 200))})).all()
    return [{"name": r[0], "action": r[1], "actor": r[2], "at": r[3]} for r in rows]


def read_all_decrypted(schema: str, connection) -> dict[str, str]:
    """Les valeurs posées, déchiffrées — POUR ``overlay.py`` UNIQUEMENT.

    Synchrone et prenant sa connexion en paramètre parce qu'elle tourne
    dans le processus d'entrypoint, avant que l'application n'existe.
    Aucune route ne l'appelle, et ``test_config_write_only.py`` vérifie
    qu'aucun module de ``routers/`` ni de ``config_admin/router.py`` ne
    l'importe.

    Filtre sur le catalogue à la LECTURE aussi : une ligne écrite dans la
    table par un autre chemin (SQL direct, restauration d'une sauvegarde
    plus ancienne dont la liste blanche était plus large) ne doit pas
    devenir une variable d'environnement pour autant. La liste fermée
    protège l'entrée comme la sortie.
    """
    rows = connection.execute(text(
        f"SELECT name, value_enc FROM {schema}.admin_config_variable"
    )).all()
    return {
        r[0]: encryptor.decrypt_value(r[1])
        for r in rows
        if r[0] in BY_NAME
    }


async def read_posed(db: AsyncSession, names: Sequence[str]) -> dict[str, str]:
    """Les valeurs posées pour CES noms, déchiffrées, au moment de l'usage.

    Existe pour les rares consommateurs qui peuvent appliquer une valeur sans
    redémarrer — aujourd'hui la sortie GitHub des signalements, dont le sink
    est construit à chaque création d'issue. Voir `overlay.py` pour la raison
    générale du refus de recharger à chaud, et `bug_reports/service.py` pour
    la raison précise de cette exception.

    Trois garde-fous, dans cet ordre :

    - **liste blanche d'appel** — l'appelant nomme ce qu'il veut lire, et rien
      d'autre ne sort ; il n'existe pas de « lis-moi tout » ici ;
    - **filtre du catalogue** — un nom hors catalogue est ignoré même s'il est
      demandé, comme dans `read_all_decrypted` : une ligne arrivée par un autre
      chemin ne devient pas une valeur applicable ;
    - **rien dans les journaux** — cette fonction ne trace pas ce qu'elle rend.
    """
    wanted = [n for n in names if n in BY_NAME]
    if not wanted:
        return {}
    rows = (await db.execute(
        text(
            f"SELECT name, value_enc FROM {_schema()}.admin_config_variable "
            "WHERE name = ANY(:names)"
        ),
        {"names": wanted},
    )).all()
    return {r[0]: encryptor.decrypt_value(r[1]) for r in rows if r[0] in BY_NAME}
