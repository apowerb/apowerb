"""Lire, poser et supprimer une valeur — sans jamais la rendre lisible.

Ce module est le seul endroit qui manipule une valeur en clair, et il n'a
aucune fonction qui en renvoie une à un appelant HTTP. ``read_all_decrypted``
existe pour ``overlay.py`` seul, qui tourne AVANT le serveur, dans le
processus d'entrypoint : elle n'est jamais atteignable depuis une requête.
"""

from __future__ import annotations

import os
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


def env_holds(name: str) -> bool:
    """L'environnement du processus impose-t-il cette variable ?

    Non vide, pas seulement présent : une variable déclarée vide dans un
    déploiement se lit comme configurée et se comporte comme rien — le plus
    vieux piège de ce dépôt (cf. ``_usable_as_a_base`` dans settings.py).
    """
    return bool((os.environ.get(name) or "").strip())


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
        if env_holds(variable.name):
            # Précédence : l'environnement gagne. Une ligne en base peut
            # exister par-dessous — elle est inerte, et l'écran doit le dire
            # plutôt que laisser croire qu'elle s'applique.
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
