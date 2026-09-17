"""Enregistrement et rejeu des exécutions d'agents.

Ce qui manquait pour rejouer un run n'était pas l'observation — les spans ADK
partent déjà en OTLP et l'usage LLM est compté — mais **l'entrée**. Un run se
consigne ici au moment où il démarre, avec ce qu'il a reçu ; son issue est
écrite quand il se termine. Rejouer, c'est relire cette entrée et repartir.

Le rejeu crée toujours un nouveau run qui cite l'original (``replay_of``) :
l'historique n'est pas réécrit, et un rejeu raté se distingue de l'échec
d'origine.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from nanoid import generate as nanoid_generate
from sqlalchemy.exc import IntegrityError

from apowerb.agent_store.run_store import RunStore
from apowerb.configs.paths import scope_upload_dir
from apowerb.configs.th2logger import setup_logging

logger = setup_logging(__name__)

# DDL au boot, comme les autres stores : importer ce module ne touche pas la base.
run_store = RunStore()

STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_ERROR = "error"
STATUS_CANCELLED = "cancelled"

# Statuts depuis lesquels un rejeu va de soi. Un run réussi a déjà produit ses
# effets de bord et un run en vol n'a pas encore rendu son verdict : les deux
# demandent un geste explicite (``force``) ou un refus.
_REPLAYABLE = (STATUS_ERROR, STATUS_CANCELLED)


def _now() -> str:
    """Horodatage à la microseconde près.

    Les autres tables s'arrêtent à la seconde, ce qui suffit à afficher une
    date. Ici l'horodatage sert aussi à **ordonner** : deux runs lancés dans
    la même seconde — un rejeu juste après son original, par exemple —
    ressortiraient dans un ordre indéterminé. Le format reste triable
    lexicographiquement.
    """
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")


def _run_input_dir(run_id: str) -> Path:
    """Où l'entrée d'un run est conservée sur disque.

    Passe par ``scope_upload_dir`` plutôt que par un chemin en dur : c'est ce
    qui rend le dossier configurable comme le reste du runtime.
    """
    return scope_upload_dir(f"run_{run_id}")


def _store_input_file(run_id: str, file_bytes: bytes, file_name: str) -> str:
    """Écrit le fichier d'entrée et renvoie son chemin.

    Sans cette copie, rejouer un run dont le fichier a disparu de la machine
    de l'utilisateur est impossible — c'est la lecon du chemin Outlook, qui
    garde ses pièces jointes pour pouvoir rejouer un mail supprimé depuis.
    """
    directory = _run_input_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    # ``Path(...).name`` : un nom de fichier venu du client ne doit pas pouvoir
    # remonter l'arborescence.
    target = directory / Path(file_name).name
    target.write_bytes(file_bytes)
    return str(target)


def start_run(
    trigger: str,
    owner_id: str,
    agent_ids: list | None = None,
    config: dict | None = None,
    file_bytes: bytes | None = None,
    file_name: str | None = None,
    run_id: str | None = None,
    organization_id: str | None = None,
    replay_of: str | None = None,
) -> str:
    """Consigne un run qui démarre et renvoie son identifiant."""
    run_id = run_id or nanoid_generate(size=21)

    input_file_path = None
    if file_bytes is not None and file_name:
        try:
            input_file_path = _store_input_file(run_id, file_bytes, file_name)
        except OSError as exc:
            # Un run qui ne peut pas conserver son entrée reste un run : on
            # perd le rejeu, pas l'exécution. Mais on le dit.
            logger.warning(
                "[RUNS] run_id=%s : entrée non conservée (%s) — rejeu indisponible",
                run_id,
                exc,
            )

    values = dict(
        trigger=trigger,
        owner_id=owner_id,
        organization_id=organization_id,
        agent_ids=json.dumps(agent_ids or []),
        config=json.dumps(config or {}),
        input_file_name=file_name,
        input_file_path=input_file_path,
        status=STATUS_RUNNING,
        attempts=1,
        created_at=_now(),
        replay_of=replay_of,
    )
    try:
        with run_store.engine.begin() as conn:
            conn.execute(run_store.run_table.insert().values(run_id=run_id, **values))
    except IntegrityError:
        # L'identifiant vient du client : rien ne garantit qu'il soit neuf, et
        # un run refusé pour cause de doublon serait une panne visible là où
        # un identifiant de secours suffit.
        fresh_id = nanoid_generate(size=21)
        logger.warning(
            "[RUNS] run_id=%s deja pris — le run est consigne sous %s",
            run_id,
            fresh_id,
        )
        with run_store.engine.begin() as conn:
            conn.execute(run_store.run_table.insert().values(run_id=fresh_id, **values))
        run_id = fresh_id
    return run_id


def finish_run(run_id: str, status: str, error_message: str | None = None) -> None:
    """Écrit l'issue d'un run.

    Ne filtre pas par propriétaire : l'appelant est le code qui a lancé le run,
    pas une requête utilisateur.
    """
    with run_store.engine.begin() as conn:
        conn.execute(
            run_store.run_table.update()
            .where(run_store.run_table.c.run_id == run_id)
            .values(
                status=status,
                error_message=error_message,
                finished_at=_now(),
            )
        )


def _row_to_dict(row) -> dict:
    run = row._asdict()
    run["agent_ids"] = json.loads(run.get("agent_ids") or "[]")
    try:
        run["config"] = json.loads(run.get("config") or "{}")
    except json.JSONDecodeError:
        run["config"] = {}
    # Le chemin disque ne regarde pas l'appelant : il dit où vivent les données
    # sur le serveur. On expose seulement s'il reste rejouable.
    run["input_available"] = bool(run.pop("input_file_path", None))
    return run


def get_run(run_id: str, owner_id: str) -> dict | None:
    """Un run donné, à condition qu'il appartienne à ce propriétaire."""
    with run_store.engine.begin() as conn:
        row = conn.execute(
            run_store.run_table.select().where(
                run_store.run_table.c.run_id == run_id,
                run_store.run_table.c.owner_id == owner_id,
            )
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_runs(owner_id: str, limit: int = 50) -> list[dict]:
    """Les runs d'un propriétaire, du plus récent au plus ancien."""
    with run_store.engine.begin() as conn:
        rows = conn.execute(
            run_store.run_table.select()
            .where(run_store.run_table.c.owner_id == owner_id)
            .order_by(run_store.run_table.c.created_at.desc())
            .limit(limit)
        ).fetchall()
    return [_row_to_dict(row) for row in rows]


def prepare_replay(run_id: str, owner_id: str, force: bool = False) -> dict:
    """Ouvre un nouveau run à partir de l'entrée conservée par un run passé.

    Renvoie de quoi relancer : ``run_id`` (le neuf), ``agent_ids``, ``config``,
    ``file_bytes`` et ``file_name``. Le run d'origine n'est pas modifié.
    """
    with run_store.engine.begin() as conn:
        row = conn.execute(
            run_store.run_table.select().where(
                run_store.run_table.c.run_id == run_id,
                run_store.run_table.c.owner_id == owner_id,
            )
        ).fetchone()

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Run not found, or you do not have permission to replay it.",
        )

    original = row._asdict()
    status = original.get("status")
    if status not in _REPLAYABLE and not force:
        if status == STATUS_RUNNING:
            raise HTTPException(
                status_code=409,
                detail="This run is still in flight — wait for it to settle.",
            )
        raise HTTPException(
            status_code=409,
            detail=(
                f"This run ended in '{status}': its side effects already "
                "happened. Pass force=true to run it again anyway."
            ),
        )

    file_bytes = None
    file_name = original.get("input_file_name")
    stored_path = original.get("input_file_path")
    if stored_path:
        try:
            file_bytes = Path(stored_path).read_bytes()
        except OSError as exc:
            # L'entrée a disparu du disque. On ne rejoue pas « presque » :
            # relancer sans le fichier donnerait un run silencieusement
            # différent de celui qu'on croit reproduire.
            raise HTTPException(
                status_code=410,
                detail=(
                    "The preserved input for this run is no longer on disk "
                    f"({exc.__class__.__name__}) — it cannot be replayed."
                ),
            )

    agent_ids = json.loads(original.get("agent_ids") or "[]")
    try:
        config = json.loads(original.get("config") or "{}")
    except json.JSONDecodeError:
        config = {}

    new_run_id = start_run(
        trigger=original.get("trigger") or "workflow",
        owner_id=owner_id,
        agent_ids=agent_ids,
        config=config,
        file_bytes=file_bytes,
        file_name=file_name,
        organization_id=original.get("organization_id"),
        replay_of=run_id,
    )

    logger.info("[RUNS] rejeu de run_id=%s -> nouveau run_id=%s", run_id, new_run_id)

    return {
        "run_id": new_run_id,
        "replay_of": run_id,
        "agent_ids": agent_ids,
        "config": config,
        "file_bytes": file_bytes,
        "file_name": file_name,
    }
