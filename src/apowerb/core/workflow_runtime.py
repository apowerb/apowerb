"""Branchement de production des nœuds d'un graphe de workflow.

``workflow_graph`` ne sait pas exécuter un agent ni un outil : il les reçoit
(``run_agent``, ``run_tool``). Ce module les fournit pour un propriétaire
donné, avec les mêmes règles que le reste du produit :

* un nœud agent ne peut viser qu'un agent **du même propriétaire** (même
  filtre que ``get_agent``) ; il s'exécute par ``/run`` sous son jeton ;
* un nœud outil passe par ``load_agent_tools_functions``, qui filtre déjà
  les ``tool_config{id}`` par propriétaire. Référence : ``categorie.outil``,
  ou ``tool_config{id}:nom_de_fonction`` quand la configuration en expose
  plusieurs ;
* un nœud notification (canal ``app``) écrit dans les notifications du
  **propriétaire du run**, jamais celles d'un tiers ; ``workflow_graph`` ne
  connaît pas non plus l'identité de ce propriétaire, d'où l'injection ;
* un nœud notification (canal ``teams``) poste sur le webhook Teams entrant
  du propriétaire, résolu et déchiffré depuis son intégration
  (``Integration``, provider ``teams_webhook`` — voir
  ``apowerb.integrations.teams``) : l'URL n'est jamais écrite dans le
  graphe, ni renvoyée dans un événement ou une erreur.

``workflow_graph`` ne sait pas non plus faire de requête HTTP sortante : ce
nœud (``http``) n'a en revanche besoin d'aucune ressource par propriétaire
(pas d'authentification branchée pour l'instant, voir son message de
validation), il reste donc exécuté directement par ``workflow_graph``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from typing import Any, Optional

from apowerb.core.workflow_engine import access_token_factory, run_agent_message
from apowerb.core.workflow_graph import GraphError, UpstreamArgs


def _agent_number(agent_id: str) -> int:
    raw = str(agent_id)
    numeric = raw[len("agent") :] if raw.startswith("agent") else raw
    if not numeric.isdigit():
        raise GraphError(f"identifiant d'agent invalide : {raw!r}")
    return int(numeric)


def check_agent_owner(agent_id: str, owner_email: str) -> str:
    """Nom de dossier ADK de l'agent s'il appartient à ``owner_email``."""
    from apowerb.core.agent_helpers import get_agent_details

    number = _agent_number(agent_id)
    details = get_agent_details(agent_id=number) or {}
    if details.get("owner_id") != owner_email:
        # Introuvable plutôt qu'interdit : on ne confirme pas l'existence d'un
        # agent d'autrui.
        raise GraphError(
            f"agent introuvable : agent{number}",
            code="agent_not_found",
            params={"agent": f"agent{number}"},
        )
    return f"agent{number}"


def resolve_tool(tool_ref: str, owner_email: str):
    """La fonction Python d'un outil, résolue pour ``owner_email``."""
    from apowerb.tools_store.tools_helpers import load_agent_tools_functions

    ref, _, wanted = tool_ref.partition(":")
    names, funcs = load_agent_tools_functions(tools=[ref], owner_id=owner_email)
    if wanted:
        pairs = [
            (n, f)
            for n, f in zip(names, funcs)
            if n.split(".")[-1] == wanted or n == wanted
        ]
    else:
        pairs = list(zip(names, funcs))
    if not pairs:
        raise GraphError(
            f"outil introuvable : {tool_ref}",
            code="tool_not_found",
            params={"tool": tool_ref},
        )
    if len(pairs) > 1:
        options = ", ".join(n for n, _ in pairs)
        raise GraphError(
            f"{tool_ref} expose plusieurs fonctions ({options}) : précise ref:fonction",
            code="tool_ambiguous",
            params={"tool": tool_ref, "options": options},
        )
    return pairs[0][1]


async def call_tool(func, args: dict) -> Any:
    name = getattr(func, "__name__", str(func))
    signature = inspect.signature(func)
    params = signature.parameters
    if (
        "tool_context" in params
        and params["tool_context"].default is inspect.Parameter.empty
    ):
        raise GraphError(
            f"{name} dépend du contexte d'un agent : utilise-le dans un nœud agent",
            code="tool_needs_agent_context",
            params={"tool": name},
        )
    if isinstance(args, UpstreamArgs) and not any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    ):
        args = {k: v for k, v in args.items() if k in params}
    try:
        # Lier avant d'appeler : un TypeError levé *dans* l'outil n'est pas
        # un problème d'arguments et ne doit pas se déguiser en l'un d'eux.
        signature.bind(**args)
    except TypeError as exc:
        raise GraphError(
            f"{name} : arguments refusés ({exc})",
            code="tool_arguments",
            params={"tool": name, "problem": str(exc)},
        ) from None
    if inspect.iscoroutinefunction(func):
        return await func(**args)
    # Les outils du portfolio sont synchrones et souvent bloquants (HTTP, SQL).
    return await asyncio.to_thread(func, **args)


# --- Nœud notification -------------------------------------------------------
#
# Débit : 30 envois / heure par propriétaire, fenêtre glissante tenue en
# mémoire du PROCESS courant (un dict module-level). Ce n'est PAS un compteur
# partagé entre workers ou instances : un déploiement multi-process laisse
# chaque process appliquer sa propre limite. Documenté ici plutôt que corrigé,
# une limite exacte demanderait un compteur externe (Redis) hors périmètre de
# ce lot.
_SEND_WINDOW_S = 3600.0
_SEND_LIMIT = 30
_send_history: dict[str, list[float]] = {}


def _reserve_sends(owner_email: str, node_id: str, count: int) -> None:
    """Réserve ``count`` envois pour ``owner_email`` ou refuse tout le lot.

    Tout ou rien : un nœud qui enverrait 5 emails ne doit pas en envoyer 3
    puis échouer sur le 4e, ce qui rendrait ``sent`` menteur.
    """
    now = time.monotonic()
    history = _send_history.setdefault(owner_email, [])
    history[:] = [t for t in history if now - t < _SEND_WINDOW_S]
    if len(history) + count > _SEND_LIMIT:
        raise GraphError(
            f"{node_id} : limite de {_SEND_LIMIT} envois par heure dépassée",
            code="notification_rate_limited",
            params={"node": node_id, "limit": str(_SEND_LIMIT)},
        )
    history.extend([now] * count)


def _checked_email(node_id: str, address: str) -> str:
    """``address`` si c'est une adresse plausible, sinon lève le code produit."""
    from pydantic import EmailStr, TypeAdapter
    from pydantic import ValidationError as _PydanticValidationError

    try:
        return TypeAdapter(EmailStr).validate_python(address)
    except _PydanticValidationError:
        raise GraphError(
            f"{node_id} : destinataire invalide ({address!r})",
            code="notification_bad_recipient",
            params={"node": node_id, "recipient": str(address)},
        ) from None


async def _notify_owner_in_app(owner_email: str, title: str, message: str) -> None:
    """Notification en base + poussée SSE, même schéma que
    ``webhook_handlers._common.create_webhook_notification`` et
    ``bug_reports.tracking._notify_in_app`` : on réutilise le modèle et le bus
    existants, on n'invente pas un second mécanisme de notification."""
    from apowerb.helpers.database import sessionmanager
    from apowerb.helpers.notification_bus import notify as push_notification
    from apowerb.models import Notification
    from apowerb.users.service import get_user_by_email

    async with sessionmanager.session() as db:
        user = await get_user_by_email(owner_email, db)
        notification = Notification(
            user_id=user.user_id,
            title=title[:255],
            message=message,
            type="workflow",
            link=None,
            metadata_json=json.dumps({"source": "workflow"}),
            is_read=False,
        )
        db.add(notification)
        await db.commit()
        await db.refresh(notification)
        await push_notification(
            user.user_id,
            {
                "id": notification.id,
                "title": notification.title,
                "message": notification.message,
                "type": notification.type,
                "link": notification.link,
                "is_read": False,
                "created_at": (
                    notification.created_at.isoformat()
                    if notification.created_at
                    else None
                ),
            },
        )


def _teams_card(subject: str, body: str) -> dict:
    """Corps POST au format Workflows (Power Automate) : une Adaptive Card
    minimale, titre + texte, aucune mise en forme superflue."""
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "type": "AdaptiveCard",
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "version": "1.4",
                    "body": [
                        {
                            "type": "TextBlock",
                            "text": subject,
                            "weight": "Bolder",
                            "size": "Medium",
                            "wrap": True,
                        },
                        {"type": "TextBlock", "text": body, "wrap": True},
                    ],
                },
            }
        ],
    }


async def _teams_webhook_url(owner_email: str):
    """URL déchiffrée (jamais loguée) du webhook Teams du propriétaire, ou
    ``None`` si l'intégration n'est pas configurée. Fonction à part pour que
    les tests substituent la résolution sans monter de session DB."""
    from apowerb.integrations.teams import get_teams_webhook_url_for_owner

    return await get_teams_webhook_url_for_owner(owner_email)


async def _notify_teams(
    owner_email: str, node_id: str, subject: str, body: str
) -> None:
    """POST la carte sur le webhook Teams du propriétaire.

    Revalide l'URL à l'envoi (même liste blanche qu'à l'enregistrement, voir
    ``apowerb.integrations.teams.validate_teams_webhook_url``) : une
    intégration enregistrée avant un durcissement de la liste, ou dont la
    résolution DNS a changé depuis, ne doit pas rester utilisable
    silencieusement. httpx, 10 s, aucune redirection suivie : un webhook
    Teams ne redirige jamais légitimement, un saut serait un signe de
    détournement plutôt qu'un cas à servir.
    """
    import httpx

    from apowerb.integrations.teams import (
        TeamsWebhookRefused,
        validate_teams_webhook_url,
    )

    url = await _teams_webhook_url(owner_email)
    if not url:
        raise GraphError(
            f"{node_id} : aucun webhook Teams configuré",
            code="teams_not_configured",
            params={"node": node_id},
        )
    try:
        url = validate_teams_webhook_url(url)
    except TeamsWebhookRefused:
        raise GraphError(
            f"{node_id} : webhook Teams refusé",
            code="teams_failed",
            params={"node": node_id, "status": None},
        ) from None

    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=False) as client:
            resp = await client.post(url, json=_teams_card(subject, body))
    except httpx.TimeoutException:
        raise GraphError(
            f"{node_id} : délai Teams dépassé",
            code="teams_failed",
            params={"node": node_id, "status": None},
        ) from None
    except httpx.HTTPError:
        # Erreur réseau : jamais le message brut (peut porter l'hôte visé).
        raise GraphError(
            f"{node_id} : échec réseau Teams",
            code="teams_failed",
            params={"node": node_id, "status": None},
        ) from None
    if not (200 <= resp.status_code < 300):
        raise GraphError(
            f"{node_id} : Teams a refusé l'envoi",
            code="teams_failed",
            params={"node": node_id, "status": resp.status_code},
        ) from None


def _notify_for(owner_email: str):
    """Le callback ``run_notify`` injecté dans le compilateur pour ce propriétaire."""

    async def run_notify(
        node_id: str, channel: str, to: list, subject: str, body: str
    ) -> int:
        from apowerb.helpers.email_sender import send_email

        if channel == "app":
            _reserve_sends(owner_email, node_id, 1)
            await _notify_owner_in_app(owner_email, subject, body)
            return 1

        if channel == "teams":
            _reserve_sends(owner_email, node_id, 1)
            await _notify_teams(owner_email, node_id, subject, body)
            return 1

        addresses = [_checked_email(node_id, addr) for addr in to]
        _reserve_sends(owner_email, node_id, len(addresses))
        for addr in addresses:
            await send_email(to=addr, subject=subject, body=body)
        return len(addresses)

    return run_notify


def bindings_for(owner_email: str, plan: Optional[str]):
    """(run_agent, run_tool, run_notify) pour exécuter un graphe au nom de
    ``owner_email``."""
    token_factory = access_token_factory(owner_email)

    async def run_agent(agent_id: str, message: str) -> Any:
        folder = check_agent_owner(agent_id, owner_email)
        return await run_agent_message(
            folder,
            message,
            owner_email=owner_email,
            plan=plan,
            token_factory=token_factory,
        )

    async def run_tool(tool_ref: str, args: dict) -> Any:
        return await call_tool(resolve_tool(tool_ref, owner_email), args or {})

    return run_agent, run_tool, _notify_for(owner_email)
