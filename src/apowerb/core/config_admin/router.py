"""L'API de l'écran de configuration. Écriture seule.

Aucune route de ce module ne rend une valeur, et il n'importe pas
``read_all_decrypted`` : la seule fonction du store capable de déchiffrer
appartient à l'entrypoint, pas au serveur HTTP. ``test_config_write_only.py``
le vérifie plutôt que de le promettre.

Le contrat de ``GET /api/config/setup`` — des noms, jamais des valeurs —
reste vrai : ce routeur ne le touche pas, il vit à côté.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from apowerb.admin.guard import require_superadmin
from apowerb.configs.settings import get_settings
from apowerb.core.config_admin import store
from apowerb.core.config_admin.catalog import (
    MAX_VALUE_LENGTH,
    InvalidValue,
    is_writable,
)
from apowerb.helpers.database import get_db
from apowerb.users import schemas as user_schemas

router = APIRouter(prefix="/admin/config", tags=["admin", "config"])


class VariableOut(BaseModel):
    """Une variable telle que l'écran la voit. Il n'y a pas de champ pour
    la valeur, et ce n'est pas un oubli : le modèle interdit les extras,
    donc personne ne pourra en glisser une plus tard sans le voir."""

    model_config = ConfigDict(extra="forbid")

    name: str
    capability: str
    secret: bool
    # "env" | "database" | "unset". Dire d'où vient une variable n'apprend
    # rien de son contenu : `GET /api/config/setup` publie déjà posé/pas posé
    # via `missing`. Sans ce champ, l'écran accepterait silencieusement une
    # écriture qu'un déploiement rend inerte.
    source: str
    updated_at: datetime | None = None
    updated_by: str | None = None
    pending_restart: bool = False


class VariablesOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[VariableOut]
    # Les NOMS en attente de redémarrage, pour le bandeau de l'écran.
    pending_restart: list[str]
    # Vrai quand une écriture serait acceptée mais inerte faute de superadmin
    # nommé — voir `_assert_superadmin_named`.
    superadmin_named: bool


class ValueIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # `repr=False` : un Pydantic ValidationError affiche l'objet reçu. Sans
    # ça, une valeur trop longue partirait dans la trace, donc dans les
    # journaux — exactement ce que cet écran doit rendre impossible.
    value: str = Field(min_length=1, max_length=MAX_VALUE_LENGTH, repr=False)


class AuditOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    action: str
    actor: str
    at: datetime


def _as_out(state: store.VariableState) -> VariableOut:
    return VariableOut(**state.__dict__)


async def _assert_writable(name: str) -> None:
    """404 et pas 403 pour un nom hors liste : répondre « interdit » sur
    ``ENCRYPT_KEY`` et « inconnu » sur ``FOO`` confirmerait au sondeur
    lesquels de ses noms existent chez nous."""
    if not is_writable(name):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Cette variable ne fait pas partie des variables modifiables.",
        )


async def _assert_superadmin_named(db: AsyncSession) -> None:
    """Refuse d'écrire tant qu'aucun superadministrateur n'est nommé.

    ``admin/guard.py::is_superadmin`` rend ``True`` pour tout ADMIN tant que
    ``admin_superadmin`` est vide — un repli délibéré, sans lequel une
    installation neuve ne pourrait jamais créer sa première organisation :
    la table qui accorde le droit ne peut être écrite que par quelqu'un qui
    l'a déjà.

    Ce repli est acceptable pour créer une organisation. Il ne l'est pas
    pour poser une clé d'API : il ferait de « superadministrateur seul » un
    invariant qui ne tient pas sur une installation neuve, c'est-à-dire
    exactement celle qui a le plus de variables à poser. On garde donc
    ``require_superadmin`` — réécrire une seconde définition du rang serait
    un second endroit où se tromper — et on ajoute ce verrou, avec le geste
    qui le lève.
    """
    schema = get_settings().db_schema
    named = (await db.execute(text(
        f"SELECT count(*) FROM {schema}.admin_superadmin"
    ))).scalar() or 0
    if named == 0:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "Aucun superadministrateur n'est nommé sur cette installation : "
                "tant que c'est le cas, tout administrateur en serait un. Posez "
                "DEFAULT_SUPERADMIN_EMAIL dans le déploiement et redémarrez, "
                "puis revenez ici."
            ),
        )


@router.get("/variables", response_model=VariablesOut)
async def list_variables(
    db: AsyncSession = Depends(get_db),
    _: user_schemas.User = Depends(require_superadmin),
) -> VariablesOut:
    """Ce qui est modifiable, et d'où vient chaque variable. Jamais une valeur."""
    states = await store.list_states(db)
    schema = get_settings().db_schema
    named = (await db.execute(text(
        f"SELECT count(*) FROM {schema}.admin_superadmin"
    ))).scalar() or 0
    return VariablesOut(
        items=[_as_out(s) for s in states],
        pending_restart=[s.name for s in states if s.pending_restart],
        superadmin_named=named > 0,
    )


@router.put("/variables/{name}", response_model=VariableOut)
async def put_variable(
    name: str,
    payload: ValueIn,
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(require_superadmin),
) -> VariableOut:
    """Pose une valeur. La réponse dit ce qui a été posé et quand elle
    s'appliquera — jamais ce qui a été posé."""
    await _assert_writable(name)
    await _assert_superadmin_named(db)
    try:
        state = await store.put(
            db, name=name, value=payload.value, actor=current_user.email
        )
    except InvalidValue as exc:
        # `exc` est construit par le catalogue, qui nomme la règle et jamais
        # la valeur reçue. Le relayer tel quel est sûr, et c'est ce qui rend
        # le refus actionnable.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from None
    return _as_out(state)


@router.delete("/variables/{name}", response_model=VariableOut)
async def delete_variable(
    name: str,
    db: AsyncSession = Depends(get_db),
    current_user: user_schemas.User = Depends(require_superadmin),
) -> VariableOut:
    """Retire la valeur posée : la variable retombe sur l'environnement du
    déploiement, ou sur son défaut."""
    await _assert_writable(name)
    await _assert_superadmin_named(db)
    return _as_out(await store.delete(db, name=name, actor=current_user.email))


@router.get("/audit", response_model=list[AuditOut])
async def list_audit(
    limit: int = 50,
    db: AsyncSession = Depends(get_db),
    _: user_schemas.User = Depends(require_superadmin),
) -> list[AuditOut]:
    """Qui a changé quoi, et quand. Le nom de la variable, jamais son contenu."""
    return [AuditOut(**row) for row in await store.recent_audit(db, limit)]
