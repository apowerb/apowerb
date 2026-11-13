"""Références opaques de pièces jointes pour les workflows.

Un workflow ne manipule jamais un chemin disque : le trigger ``email`` (ou un
mail simulé) publie pour chaque pièce jointe une ``ref`` (``att_`` + 32 hex),
et le nœud ``document`` la résout ici, pour le propriétaire du run SEULEMENT.
``TH2_ATTACHMENT_ROOT`` est partagé par tous les comptes : accepter un chemin
venu d'un gabarit permettrait de lire les pièces jointes d'un autre compte.

Les fichiers PRODUITS par un nœud ``file`` passent par ``store_generated`` :
mêmes refs, mêmes contrôles, dossier ``workflow_generated``, types limités
aux formats que le moteur sait produire (``workflow_files.FORMATS``) — ils
n'ouvrent pas ces types aux envois de fichiers (``store``).

Une ref est un petit fichier JSON (« sidecar ») sous
``<racine>/workflow_refs/<ref>.json`` : propriétaire, chemin, nom, type,
taille. Le chemin est vérifié sous la racine à l'enregistrement ET à la
lecture. Une ref d'un autre propriétaire répond comme une ref inconnue.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from pathlib import Path
from typing import Optional

from apowerb.core import workflow_files
from apowerb.core.workflow_engine import WorkflowUserError
from apowerb.storage.filename import sanitize_filename

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
ALLOWED_CONTENT_TYPES = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
}
MAX_GENERATED_BYTES = 5 * 1024 * 1024
GENERATED_CONTENT_TYPES = frozenset(ct for _, ct in workflow_files.FORMATS.values())
_REF = re.compile(r"^att_[0-9a-f]{32}$")
_MAX_NAME = 120  # bien sous la limite des systèmes de fichiers (255 octets)


class AttachmentError(WorkflowUserError):
    pass


def root() -> Path:
    """Lue à chaque appel : les tests et les déploiements la règlent par env."""
    from apowerb.storage import webhook_attachments

    return Path(
        os.environ.get("TH2_ATTACHMENT_ROOT", str(webhook_attachments.ATTACHMENT_ROOT))
    ).resolve()


def _inside_root(path: str | Path) -> Optional[Path]:
    base = root()
    try:
        resolved = Path(path).resolve()
    except (OSError, RuntimeError):
        return None
    return resolved if resolved.is_relative_to(base) and resolved != base else None


def _sidecar(ref: str) -> Path:
    return root() / "workflow_refs" / f"{ref}.json"


def register(
    owner: str, path: str | Path, name: str, content_type: str, size: int
) -> str:
    """Une nouvelle ref vers un fichier DÉJÀ stocké sous la racine."""
    if not owner:
        raise AttachmentError("propriétaire manquant", code="attachment_owner_missing")
    target = _inside_root(path)
    if target is None or not target.is_file():
        raise AttachmentError(
            "pièce jointe hors du stockage autorisé", code="attachment_outside_root"
        )
    ref = "att_" + secrets.token_hex(16)
    side = _sidecar(ref)
    side.parent.mkdir(parents=True, exist_ok=True)
    side.write_text(
        json.dumps(
            {
                "owner": owner,
                "path": str(target),
                "name": name,
                "content_type": content_type,
                "size": size,
            }
        ),
        encoding="utf-8",
    )
    return ref


def store(owner: str, name: str, content_type: str, data: bytes) -> dict:
    """Enregistre un fichier envoyé (mail simulé) et renvoie l'entrée
    d'enveloppe ``{name, size, content_type, ref}``."""
    ext = ALLOWED_CONTENT_TYPES.get(content_type)
    if ext is None:
        raise AttachmentError(
            f"type de fichier refusé : {content_type} "
            f"(acceptés : {', '.join(ALLOWED_CONTENT_TYPES)})",
            code="attachment_type",
        )
    if not data or len(data) > MAX_ATTACHMENT_BYTES:
        raise AttachmentError(
            f"fichier vide ou trop gros (max {MAX_ATTACHMENT_BYTES} octets)",
            code="attachment_size",
        )
    safe = _bounded_name(name, f"piece{ext}")
    if not safe.lower().endswith(ext) and not (
        ext == ".jpg" and safe.lower().endswith(".jpeg")
    ):
        safe += ext
    folder = root() / "workflow_uploads" / secrets.token_hex(16)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / safe
    path.write_bytes(data)
    ref = register(owner, path, safe, content_type, len(data))
    return {"name": safe, "size": len(data), "content_type": content_type, "ref": ref}


def _bounded_name(name: Optional[str], default: str) -> str:
    safe = sanitize_filename(name or default) or default
    if len(safe) > _MAX_NAME:
        stem, dot, suffix = safe.rpartition(".")
        safe = (
            stem[: _MAX_NAME - len(suffix) - 1] + "." + suffix
            if dot and len(suffix) <= 10
            else safe[:_MAX_NAME]
        )
    return safe


def store_generated(owner: str, name: str, content_type: str, data: bytes) -> dict:
    """Enregistre un fichier produit par un nœud ``file`` et renvoie
    ``{name, size, content_type, ref}``."""
    if content_type not in GENERATED_CONTENT_TYPES:
        raise AttachmentError(
            f"type de fichier produit refusé : {content_type}",
            code="attachment_type",
        )
    if not data or len(data) > MAX_GENERATED_BYTES:
        raise AttachmentError(
            f"fichier vide ou trop gros (max {MAX_GENERATED_BYTES} octets)",
            code="attachment_size",
        )
    safe = _bounded_name(name, "fichier")
    folder = root() / "workflow_generated" / secrets.token_hex(16)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / safe
    path.write_bytes(data)
    ref = register(owner, path, safe, content_type, len(data))
    return {"name": safe, "size": len(data), "content_type": content_type, "ref": ref}


def resolve(ref: str, owner: str) -> dict:
    """``{path, name, content_type, size}`` de la ref, pour ``owner`` seul."""
    not_found = AttachmentError(
        f"pièce jointe introuvable : {str(ref)[:40]}",
        code="attachment_not_found",
        params={"ref": str(ref)[:40]},
    )
    if not isinstance(ref, str) or not _REF.match(ref):
        raise not_found
    try:
        meta = json.loads(_sidecar(ref).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise not_found from None
    if meta.get("owner") != owner:
        raise not_found
    path = _inside_root(meta.get("path", ""))
    if path is None or not path.is_file():
        raise not_found
    return {**meta, "path": str(path)}
