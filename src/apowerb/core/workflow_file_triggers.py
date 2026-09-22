"""Trigger ``file`` (T2) — sondage périodique d'un dossier OneDrive/Google Drive.

Contrairement aux autres kinds, ``file`` n'est pas notifié en push : il est
sondé par ``core.flow_scheduler`` (même réservation atomique CAS que
``schedule``, voir ``workflow_triggers.due_file_triggers`` /
``reserve_next_poll``). Ce module fait deux choses :

1. ``poll_file_trigger`` — orchestration pure côté logique (déduplication,
   premier passage sans déclenchement, un run par fichier nouveau/modifié).
   Elle prend un ``list_files`` INJECTÉ : c'est la seule façon de la tester
   sans réseau ni identifiants réels.
2. ``list_files_for_trigger`` — le résolveur RÉEL, qui va chercher le
   refresh_token de l'intégration du owner et appelle l'API Microsoft Graph
   / Google Drive. Il est utilisé par ``flow_scheduler`` en production mais
   N'EST PAS exercé par les tests (pas de compte OneDrive/Drive réel en CI) —
   même limite que documentée pour les triggers ``email``.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Optional

from apowerb.agent_store.workflow_trigger_seen_file_store import (
    WorkflowTriggerSeenFileStore,
)
from apowerb.configs.th2logger import setup_logging
from apowerb.core import workflow_triggers as wt

logger = setup_logging(__name__)

seen_file_store = WorkflowTriggerSeenFileStore()

ListFiles = Callable[..., Awaitable[list[dict[str, Any]]]]


def dedupe_new_files(*, seen: dict[str, str], current: list[dict]) -> list[dict]:
    """Fichiers absents de ``seen``, ou dont ``modified_at`` a changé.

    Fonction pure : ``seen`` est ``{file_id: modified_at}`` tel que rendu par
    ``get_seen_files``, ``current`` la liste brute renvoyée par le fournisseur.
    """
    return [f for f in current if seen.get(f["id"]) != f.get("modified_at")]


def get_seen_files(workflow_id: str) -> dict[str, str]:
    """État de dédup connu pour ce workflow : ``{file_id: modified_at}``.

    Dictionnaire vide == jamais sondé avec succès — c'est ce qui distingue
    le premier passage (amorçage sans déclenchement) des suivants. Limite
    documentée : un dossier réellement vide au premier sondage reste
    indiscernable d'un « jamais sondé » tant qu'aucun fichier n'y apparaît ;
    un fichier déposé ensuite sera alors traité comme faisant partie de
    l'amorçage, pas comme un déclenchement. Cas marginal, accepté ici.
    """
    t = seen_file_store.seen_file_table
    with seen_file_store.engine.begin() as conn:
        rows = conn.execute(t.select().where(t.c.workflow_id == workflow_id)).fetchall()
    return {r.file_id: r.modified_at for r in rows}


def _persist_seen_files(workflow_id: str, files: list[dict]) -> None:
    t = seen_file_store.seen_file_table
    now = wt._now_iso()
    with seen_file_store.engine.begin() as conn:
        conn.execute(t.delete().where(t.c.workflow_id == workflow_id))
        if files:
            conn.execute(
                t.insert(),
                [
                    {
                        "workflow_id": workflow_id,
                        "file_id": f["id"],
                        "modified_at": f.get("modified_at"),
                        "seen_at": now,
                    }
                    for f in files
                ],
            )


async def poll_file_trigger(row: dict, *, list_files: ListFiles) -> int:
    """Sonde UN trigger ``file`` déjà réservé, lance un run par fichier neuf.

    ``list_files`` est appelé ``list_files(provider=..., owner_id=...,
    folder_id=...)`` et doit renvoyer une liste de dicts
    ``{id, name, path, url, size, modified_at}``. Ne lève jamais : une
    intégration manquante ou une erreur du fournisseur se traduit par
    ``0`` fichiers déclenchés (best-effort, cohérent avec les autres kinds
    T2 en dispatch).
    """
    workflow_id = row["workflow_id"]
    owner_id = row["owner_id"]
    cfg = json.loads(row["config"] or "{}")
    provider = cfg.get("provider")

    integration_provider = wt.FILE_INTEGRATION_PROVIDER.get(provider)
    if integration_provider and not wt.integration_present(
        owner_id, integration_provider
    ):
        logger.info(
            "[file_triggers] intégration '%s' absente pour owner_id=%s, workflow_id=%s : sondage ignoré.",
            integration_provider,
            owner_id,
            workflow_id,
        )
        return 0

    try:
        current = await list_files(
            provider=provider, owner_id=owner_id, folder_id=cfg.get("folder_id")
        )
    except Exception:
        logger.exception(
            "[file_triggers] échec de sondage workflow_id=%s (fournisseur=%s)",
            workflow_id,
            provider,
        )
        return 0

    seen = get_seen_files(workflow_id)
    is_first_pass = not seen
    new_files = dedupe_new_files(seen=seen, current=current)

    fired = 0
    if not is_first_pass:
        for f in new_files:
            try:
                await wt.launch_triggered_run(
                    workflow_id=workflow_id,
                    owner_id=owner_id,
                    kind="file",
                    detail={"provider": provider, "folder_id": cfg.get("folder_id")},
                    payload={
                        "id": f["id"],
                        "name": f.get("name"),
                        "path": f.get("path"),
                        "url": f.get("url"),
                        "size": f.get("size"),
                        "modified_at": f.get("modified_at"),
                    },
                )
                fired += 1
            except wt.TriggerNotActive:
                continue

    _persist_seen_files(workflow_id, current)
    return fired


# ── Résolveur RÉEL (non exercé par les tests) ────────────────────────────────


async def list_files_for_trigger(
    *, provider: str, owner_id: str, folder_id: Optional[str]
) -> list[dict[str, Any]]:
    """Résolveur réel pour ``poll_file_trigger`` en production.

    NON EXERCÉ par les tests (nécessite un compte OneDrive/Google Drive réel
    et un refresh_token valide) — voir la même limite documentée pour la
    réception e-mail réelle dans ``routers.webhook_handlers``. Réutilise le
    chemin d'authentification déjà existant pour les navigateurs de fichiers
    (``routers.onedrive_browser`` / ``routers.google_drive_browser``) :
    résout l'``Integration`` du owner, appelle l'outil de listing sous
    ``env_scope`` (jamais deux utilisateurs ne partagent un refresh_token en
    vol), normalise vers le format commun ``{id,name,path,url,size,
    modified_at}``.
    """
    import asyncio

    from sqlalchemy import select

    from apowerb.helpers.database import sessionmanager
    from apowerb.helpers.encryptor import decrypt_value
    from apowerb.helpers.env_scope import env_scope
    from apowerb.models import Integration, User
    from apowerb.tools_store.portfolio.integration_status import IntegrationStatusError

    integration_provider = wt.FILE_INTEGRATION_PROVIDER.get(provider)
    if integration_provider is None:
        return []

    async with sessionmanager.session() as db:
        user_row = (
            await db.execute(select(User).where(User.email == owner_id))
        ).scalar_one_or_none()
        if user_row is None:
            return []
        integration = (
            await db.execute(
                select(Integration).where(
                    Integration.user_id == user_row.user_id,
                    Integration.provider == integration_provider,
                )
            )
        ).scalar_one_or_none()
        if integration is None or not integration.refresh_token:
            return []
        refresh_token = decrypt_value(integration.refresh_token)

    try:
        if provider == "onedrive":
            from apowerb.tools_store.portfolio.onedrive_read import (
                tool_list_files as _tool_list_files,
            )

            async with env_scope({"ONEDRIVE_REFRESH_TOKEN": refresh_token}):
                result = await asyncio.to_thread(_tool_list_files, folder_id=folder_id)
            if result.get("status") != "success":
                return []
            return [
                {
                    "id": item["id"],
                    "name": item.get("name"),
                    "path": item.get("parentPath"),
                    "url": item.get("webUrl"),
                    "size": item.get("size"),
                    "modified_at": item.get("lastModified"),
                }
                for item in result.get("items", [])
                if item.get("type") != "folder"
            ]
        if provider == "google_drive":
            from apowerb.tools_store.portfolio.google_drive import (
                tool_list_files as _tool_list_files,
            )

            async with env_scope({"GOOGLE_DRIVE_REFRESH_TOKEN": refresh_token}):
                result = await asyncio.to_thread(_tool_list_files, folder_id=folder_id)
            if result.get("status") != "success":
                return []
            return [
                {
                    "id": item["id"],
                    "name": item.get("name"),
                    "path": None,
                    "url": item.get("webViewLink"),
                    "size": item.get("size"),
                    "modified_at": item.get("modifiedTime"),
                }
                for item in result.get("files", [])
            ]
    except IntegrationStatusError:
        return []
    return []
