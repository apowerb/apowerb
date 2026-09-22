"""Endpoints publics de déclenchement — AUCUNE authentification utilisateur.

``POST /api/hooks/workflows/{token}`` : webhook générique du contrat
triggers. Vérifié par jeton (haché, comparé en temps constant) et, si
activé, par signature HMAC — jamais par ``Depends(get_current_user)``, comme
``POST /api/webhooks/{service}/notifications`` (``routers/webhooks.py``) et
``POST /rag/webhook`` (``routers/rag/webhook.py``) : ni l'une ni l'autre ne
porte cette dépendance non plus. Rien d'autre n'est ouvert ici — un seul
endpoint, un seul chemin d'entrée.

Toute réponse pour un jeton absent ou un workflow non publié est un 404
opaque et identique (le contrat l'exige) : rien ne doit permettre de
distinguer « ce jeton n'existe pas » de « ce jeton existe mais son workflow
est dépublié ».
"""

from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from logging import getLogger

from fastapi import APIRouter, HTTPException, Request, status

from apowerb.core import workflow_triggers as wt
from apowerb.helpers.encryptor import decrypt_value

logger = getLogger(__name__)

router = APIRouter(prefix="/hooks", tags=["hooks"])

MAX_BODY_BYTES = 256 * 1024
RATE_LIMIT_PER_MINUTE = 60
_RATE_WINDOW_SECONDS = 60.0

# Fenêtre glissante EN MÉMOIRE, par workflow_id. Limite assumée : ce compteur
# est PAR PROCESSUS (contrairement au tick ``schedule``, dont le lancement
# est désormais protégé au niveau de la ligne — voir
# ``workflow_triggers.fire_schedule_trigger`` — ce compteur-ci n'a pas
# d'équivalent en base). Avec plusieurs workers, chacun tient son propre
# compteur — la limite réelle devient "60 * nombre de workers" par minute et
# par workflow, pas 60. Un compteur partagé (Redis, ou une table avec verrou)
# serait nécessaire pour une limite exacte en déploiement multi-worker ; hors
# périmètre T1.
_calls: dict[str, deque] = defaultdict(deque)
# Signatures HMAC refusées, comptées à part : qui connaît le jeton sans le
# secret ne doit pas pouvoir épuiser le débit de l'intégrateur légitime.
_failed_calls: dict[str, deque] = defaultdict(deque)


def _rate_limited(workflow_id: str, buckets: dict[str, deque] = _calls) -> bool:
    now = time.monotonic()
    calls = buckets[workflow_id]
    while calls and now - calls[0] > _RATE_WINDOW_SECONDS:
        calls.popleft()
    if len(calls) >= RATE_LIMIT_PER_MINUTE:
        return True
    calls.append(now)
    return False


def _too_many() -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS, "Trop d'appels, réessaie plus tard."
    )


def _opaque_404() -> HTTPException:
    # Pas de detail : un message, même générique, distinguerait "jeton
    # inconnu" d'une autre 404 de l'API par sa forme.
    return HTTPException(status.HTTP_404_NOT_FOUND)


async def _read_capped(request: Request, limit: int) -> bytes:
    """Lit le corps borné au flux, sans jamais tamponner plus que ``limit``.

    ``Content-Length`` peut mentir (absent en chunked, ou falsifié) : on
    compte les octets réellement reçus plutôt que de faire confiance à
    l'en-tête, et on coupe dès que la limite est dépassée au lieu de
    laisser ``await request.body()`` accumuler un corps arbitrairement gros
    en mémoire avant de le rejeter.
    """
    total = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise HTTPException(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"Payload trop volumineux (max {limit} octets).",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/workflows/{token}", status_code=status.HTTP_202_ACCEPTED)
async def workflow_webhook(token: str, request: Request):
    """Déclenche la version publiée du workflow propriétaire de ``token``."""
    trigger = wt.find_active_webhook_trigger(token)
    if trigger is None:
        raise _opaque_404()

    workflow_id = trigger["workflow_id"]
    hmac_enabled = bool(trigger.get("hmac_enabled"))
    # Sans HMAC, le jeton est le seul secret : on compte avant de lire le
    # corps. Avec HMAC, seul un appel correctement signé consomme le débit.
    if not hmac_enabled and _rate_limited(workflow_id):
        raise _too_many()

    raw_body = await _read_capped(request, MAX_BODY_BYTES)

    if hmac_enabled:
        signature = request.headers.get("X-Apowerb-Signature", "")
        secret = (
            decrypt_value(trigger["hmac_secret_encrypted"])
            if trigger.get("hmac_secret_encrypted")
            else None
        )
        if (
            not secret
            or not signature
            or not wt.verify_hmac_signature(secret, raw_body, signature)
        ):
            logger.warning(
                "[hooks] signature HMAC refusée pour workflow=%s", workflow_id
            )
            if _rate_limited(workflow_id, _failed_calls):
                raise _too_many()
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "Signature manquante ou invalide."
            )
        if _rate_limited(workflow_id):
            raise _too_many()

    try:
        parsed = json.loads(raw_body) if raw_body else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, f"Corps JSON invalide : {exc}"
        ) from exc
    payload = parsed if isinstance(parsed, dict) else {"body": parsed}

    try:
        run_id, _task = await wt.launch_triggered_run(
            workflow_id=workflow_id,
            owner_id=trigger["owner_id"],
            kind="webhook",
            detail={},
            payload=payload,
        )
    except wt.TriggerNotActive as exc:
        # Dépublié entre la lecture du trigger (ci-dessus) et l'exécution :
        # même 404 opaque que "jamais publié", jamais un 500.
        raise _opaque_404() from exc

    return {"run_id": run_id}
