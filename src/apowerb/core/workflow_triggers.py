"""Service des triggers de workflow — socle T1.

Le nœud ``trigger`` du graphe (voir ``workflow_graph.trigger_spec``) reste la
source de vérité DE LA CONFIGURATION choisie par l'utilisateur. Ce module
tient l'état opérationnel qui en découle — jeton haché + chiffré, secret HMAC
chiffré, prochaine échéance, dernier déclenchement — et lance un run au nom
du propriétaire par le MÊME chemin que ``POST /api/workflows/defs/{id}/run``
(``workflow_runtime.bindings_for``, ``run_gate`` — quotas et gardes — via
``routers.workflows._streaming_run``), sans le dupliquer.

Kinds exécutés en T1 : ``webhook`` et ``schedule`` (``AUTOMATABLE_KINDS``).
Les 5 autres (email, agent_tool, form, file, workflow_done) sont déjà
validés en forme par ``workflow_graph.validate_trigger_config`` mais
répondent ``active:false, reason:"not_available"`` tant qu'ils ne sont pas
branchés ici (T2).

Le jeton webhook/formulaire est stocké DEUX fois, pour deux usages
différents :

* ``token_hash`` (SHA-256, jamais inversible) — pour le retrouver et le
  vérifier en temps constant à chaque appel public.
* ``token_encrypted`` (Fernet, ``ENCRYPT_KEY``) — pour que
  ``GET /api/workflows/{wid}/triggers`` puisse réafficher ``webhook_url`` à
  CHAQUE appel (le contrat ne réserve l'affichage unique qu'au secret HMAC,
  jamais mentionné dans la réponse de ``GET``) : un hash seul ne le
  permettrait pas.

Le secret HMAC suit la logique inverse : seul ``hmac_secret_encrypted`` est
conservé, déchiffré uniquement pour vérifier une signature entrante, et
n'est renvoyé à l'appelant QU'à sa génération (création ou ``rotate``).

Sûreté multi-réplica du tick ``schedule`` : ``fire_schedule_trigger`` réserve
le créneau par un ``UPDATE`` conditionné sur le ``next_run_at`` lu avant de
lancer quoi que ce soit — la ligne SQL sert de verrou, pas un objet en
mémoire. Deux processus qui liraient la même échéance échue ne peuvent donc
pas lancer chacun un run pour le même tick. Voir le docstring de
``fire_schedule_trigger`` pour le détail.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from logging import getLogger
from typing import Any, Optional
from zoneinfo import ZoneInfo

from apowerb.agent_store.workflow_trigger_store import WorkflowTriggerStore
from apowerb.configs.settings import get_settings
from apowerb.core.workflow_graph import parse_cron, trigger_spec
from apowerb.helpers.encryptor import decrypt_value, encrypt_value

logger = getLogger(__name__)

# DDL au boot, comme les autres stores (helpers/store_migrations.py) :
# importer ce module ne doit pas toucher la base.
workflow_trigger_store = WorkflowTriggerStore()

# Kinds pour lesquels un trigger PEUT être armé (``active=True``) en T1. Les
# 5 autres du contrat existent déjà (validation, colonnes) mais un workflow
# qui les choisit ne s'exécute pas tout seul avant T2.
AUTOMATABLE_KINDS = frozenset({"webhook", "schedule"})
_TOKEN_BYTES = 32


class TriggerNotActive(LookupError):
    """Le workflow visé n'a pas (ou plus) de trigger armé pour l'exécuter."""


class RotateNotApplicable(ValueError):
    """``rotate`` appelé sur un kind sans jeton (schedule, manual, kinds T2)."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _iso_or_none(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _generate_secret() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def verify_token(token: str, stored_hash: Optional[str]) -> bool:
    """Compare le hash du jeton reçu au hash stocké, en temps constant."""
    if not stored_hash:
        return False
    return hmac.compare_digest(hash_token(token), stored_hash)


def verify_hmac_signature(secret: str, body: bytes, header_value: str) -> bool:
    """``X-Apowerb-Signature: sha256=<hex>``, comparée en temps constant."""
    prefix = "sha256="
    if not header_value.startswith(prefix):
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header_value[len(prefix) :])


def _build_token_url(kind: str, token: str) -> str:
    settings = get_settings()
    if kind == "webhook":
        return f"{settings.public_base_url.rstrip('/')}/api/hooks/workflows/{token}"
    # "form" : page servie par l'UI (T2), pas par cette API.
    return f"{settings.app_public_url.rstrip('/')}/forms/{token}"


def compute_active(kind: str, workflow_status: str) -> tuple[bool, Optional[str]]:
    """(``active``, ``reason``) pour l'API de gestion — dérivé, jamais stocké."""
    if kind == "manual":
        return False, None
    if kind not in AUTOMATABLE_KINDS:
        return False, "not_available"
    if workflow_status != "published":
        return False, "unpublished"
    return True, None


# --- Prochaine échéance (schedule) ------------------------------------------


def compute_next_run(cfg: dict, *, after: datetime) -> Optional[datetime]:
    """Prochain instant (UTC, aware) strictement après ``after``, ou ``None``.

    ``at`` : ``None`` si déjà passé — c'est ce qui rend un trigger ``at``
    déclenché une seule fois : une fois passé, plus aucun tick ne le
    reprend (``due_schedule_triggers`` ne lit que ``next_run_at IS NOT
    NULL``).

    ``cron`` : recherche minute par minute, plafonnée à ~370 jours pour ne
    jamais boucler indéfiniment sur un cron structurellement impossible
    (ex. jour 31 d'un mois à 30 jours combiné à un mois qui l'exclut). Pas
    de dépendance externe : ``croniter``/``apscheduler`` sont absents de
    ``pyproject.toml`` à la date de ce lot (voir le rapport).
    """
    tz = ZoneInfo(cfg.get("timezone") or "Europe/Paris")
    if cfg.get("at"):
        at = datetime.fromisoformat(cfg["at"])
        if at.tzinfo is None:
            at = at.replace(tzinfo=tz)
        at_utc = at.astimezone(timezone.utc)
        return at_utc if at_utc > after else None

    fields = parse_cron(cfg["cron"])
    dom_restricted = len(fields["day"]) < 31
    dow_restricted = len(fields["weekday"]) < 7
    local = after.astimezone(tz)
    candidate = (local + timedelta(minutes=1)).replace(second=0, microsecond=0)
    limit = candidate + timedelta(days=370)
    while candidate <= limit:
        if dom_restricted and dow_restricted:
            # Sémantique cron standard : jour-du-mois OU jour-de-semaine
            # quand les deux sont restreints.
            day_ok = (
                candidate.day in fields["day"]
                or candidate.isoweekday() % 7 in fields["weekday"]
            )
        elif dom_restricted:
            day_ok = candidate.day in fields["day"]
        elif dow_restricted:
            day_ok = candidate.isoweekday() % 7 in fields["weekday"]
        else:
            day_ok = True
        if (
            day_ok
            and candidate.minute in fields["minute"]
            and candidate.hour in fields["hour"]
            and candidate.month in fields["month"]
        ):
            return candidate.astimezone(timezone.utc)
        candidate += timedelta(minutes=1)
    return None


# --- Synchronisation : publication / dépublication / suppression -----------


def sync_trigger_for_workflow(
    *, workflow_id: str, owner_id: str, graph: dict, status: str
) -> None:
    """Fait suivre l'état du trigger à la table dédiée.

    Appelé après CHAQUE écriture réussie d'un workflow (création, édition,
    publication, dépublication, restauration) — voir ``workflow_main``. Le
    trigger reflète toujours le nœud ``trigger`` du graphe le plus récent et
    le statut courant.

    Le jeton webhook reste stable tant que le kind ne change pas : publier
    puis dépublier un workflow à répétition ne doit pas invalider une
    intégration tierce déjà configurée avec l'URL précédente. Seul
    ``POST .../triggers/rotate`` le régénère explicitement.
    """
    from apowerb.core.workflow_graph import WorkflowGraph

    spec = trigger_spec(WorkflowGraph.model_validate(graph))
    kind = spec.get("kind", "manual")
    active, _ = compute_active(kind, status)
    now = _now_iso()
    t = workflow_trigger_store.trigger_table

    with workflow_trigger_store.engine.begin() as conn:
        existing = conn.execute(
            t.select().where(t.c.workflow_id == workflow_id)
        ).fetchone()
        existing_d = dict(existing._mapping) if existing is not None else None

        values: dict[str, Any] = dict(
            owner_id=owner_id,
            kind=kind,
            config=json.dumps(spec, ensure_ascii=False),
            active=active,
            updated_at=now,
        )

        keeps_webhook_identity = (
            existing_d is not None
            and existing_d.get("kind") == "webhook"
            and existing_d.get("token_hash")
        )
        if kind == "webhook":
            if keeps_webhook_identity:
                values["token_hash"] = existing_d["token_hash"]
                values["token_encrypted"] = existing_d["token_encrypted"]
            else:
                token = _generate_secret()
                values["token_hash"] = hash_token(token)
                values["token_encrypted"] = encrypt_value(token)
            hmac_wanted = bool(spec.get("hmac"))
            values["hmac_enabled"] = hmac_wanted
            if (
                hmac_wanted
                and existing_d
                and existing_d.get("hmac_enabled")
                and existing_d.get("hmac_secret_encrypted")
            ):
                # Reste inchangé : régénérer silencieusement rendrait le
                # secret déjà distribué au tiers caduc sans le lui dire.
                values["hmac_secret_encrypted"] = existing_d["hmac_secret_encrypted"]
            elif hmac_wanted:
                # Nouvellement activé : généré maintenant, mais jamais
                # renvoyé par CE chemin — seul ``rotate`` le montre. Documenté
                # comme limite T1 dans le rapport.
                values["hmac_secret_encrypted"] = encrypt_value(_generate_secret())
            else:
                values["hmac_secret_encrypted"] = None
        else:
            values["token_hash"] = None
            values["token_encrypted"] = None
            values["hmac_enabled"] = False
            values["hmac_secret_encrypted"] = None

        if kind == "schedule" and active:
            next_at = compute_next_run(spec, after=datetime.now(timezone.utc))
            values["next_run_at"] = _iso_or_none(next_at)
        else:
            values["next_run_at"] = None

        if existing_d is None:
            conn.execute(
                t.insert().values(workflow_id=workflow_id, created_at=now, **values)
            )
        else:
            conn.execute(
                t.update().where(t.c.workflow_id == workflow_id).values(**values)
            )


def remove_trigger_for_workflow(workflow_id: str) -> None:
    """Supprime la ligne trigger d'un workflow supprimé (contrat : suppression = désactivation définitive)."""
    t = workflow_trigger_store.trigger_table
    with workflow_trigger_store.engine.begin() as conn:
        conn.execute(t.delete().where(t.c.workflow_id == workflow_id))


# --- API de gestion ----------------------------------------------------------


def get_trigger_status(
    workflow_id: str, owner_id: str, *, workflow_status: str
) -> dict:
    """État pour ``GET /api/workflows/{wid}/triggers``.

    L'appelant (le routeur) a déjà vérifié la propriété du workflow via
    ``workflow_main.get_workflow`` — un trigger absent (jamais synchronisé,
    par exemple un workflow créé avant ce lot) répond l'état par défaut
    ("manual", inactif), pas une erreur.
    """
    t = workflow_trigger_store.trigger_table
    with workflow_trigger_store.engine.begin() as conn:
        row = conn.execute(
            t.select().where(t.c.workflow_id == workflow_id, t.c.owner_id == owner_id)
        ).fetchone()
    d = dict(row._mapping) if row is not None else {}
    kind = d.get("kind", "manual")
    active, reason = compute_active(kind, workflow_status)

    webhook_url = form_url = None
    if kind == "webhook" and d.get("token_encrypted"):
        webhook_url = _build_token_url("webhook", decrypt_value(d["token_encrypted"]))
    if kind == "form" and d.get("token_encrypted"):
        form_url = _build_token_url("form", decrypt_value(d["token_encrypted"]))

    return {
        "kind": kind,
        "active": active,
        "reason": reason,
        "webhook_url": webhook_url,
        "form_url": form_url,
        "hmac_enabled": bool(d.get("hmac_enabled")),
        "next_run_at": d.get("next_run_at"),
        "last_fired_at": d.get("last_fired_at"),
        "last_status": d.get("last_status"),
    }


def rotate_trigger(workflow_id: str, owner_id: str) -> Optional[dict]:
    """Régénère le jeton (webhook/form) et, si actif, le secret HMAC.

    Renvoie ``None`` si aucune ligne trigger n'appartient à ``owner_id`` pour
    ce workflow (le routeur en fait un 404 — même si le workflow lui-même
    existe : sans trigger synchronisé, rien à faire tourner). Lève
    ``RotateNotApplicable`` pour un kind sans jeton (schedule, manual, un
    kind T2 non encore choisi comme webhook/form).
    """
    t = workflow_trigger_store.trigger_table
    with workflow_trigger_store.engine.begin() as conn:
        row = conn.execute(
            t.select().where(t.c.workflow_id == workflow_id, t.c.owner_id == owner_id)
        ).fetchone()
        if row is None:
            return None
        d = dict(row._mapping)
        kind = d["kind"]
        if kind not in ("webhook", "form"):
            raise RotateNotApplicable(kind)

        token = _generate_secret()
        values: dict[str, Any] = dict(
            token_hash=hash_token(token),
            token_encrypted=encrypt_value(token),
            updated_at=_now_iso(),
        )
        hmac_secret: Optional[str] = None
        if kind == "webhook" and d.get("hmac_enabled"):
            hmac_secret = _generate_secret()
            values["hmac_secret_encrypted"] = encrypt_value(hmac_secret)

        conn.execute(t.update().where(t.c.workflow_id == workflow_id).values(**values))

    url_field = "webhook_url" if kind == "webhook" else "form_url"
    return {url_field: _build_token_url(kind, token), "hmac_secret": hmac_secret}


def find_active_webhook_trigger(token: str) -> Optional[dict]:
    """La ligne dont le hash du jeton correspond ET qui est armée, sinon ``None``.

    Une seule fonction pour les deux causes de 404 opaque du contrat (jeton
    inconnu / workflow non publié) : le routeur n'a pas à les distinguer, ni
    nous à le lui permettre.
    """
    token_hash = hash_token(token)
    t = workflow_trigger_store.trigger_table
    with workflow_trigger_store.engine.begin() as conn:
        row = conn.execute(
            t.select().where(t.c.kind == "webhook", t.c.token_hash == token_hash)
        ).fetchone()
    if row is None:
        return None
    d = dict(row._mapping)
    # Défense en profondeur : l'égalité SQL ci-dessus porte déjà sur un hash
    # SHA-256 d'un jeton à haute entropie (rien à exploiter par
    # chronométrage) ; cette seconde comparaison est, elle, explicitement en
    # temps constant.
    if not hmac.compare_digest(d["token_hash"], token_hash):
        return None
    if not d.get("active"):
        return None
    return d


# --- Lancement d'un run déclenché (partagé webhook / schedule) -------------


async def launch_triggered_run(
    *, workflow_id: str, owner_id: str, kind: str, detail: dict, payload: Any
) -> tuple[str, "asyncio.Task"]:
    """Lance un run de la version PUBLIÉE, au nom du propriétaire.

    Réutilise TEL QUEL le chemin de ``POST /api/workflows/defs/{id}/run`` :
    ``workflow_main.get_workflow`` + ``workflow_runtime.bindings_for``
    (agents/outils du propriétaire, jetons d'accès) + ``run_graph`` +
    ``routers.workflows._streaming_run`` — qui consigne le run et son issue
    dans ``agent_runs`` (les quotas/gardes de ``run_gate`` s'appliquent à
    l'intérieur de ce chemin, comme pour tout run). Rien n'est dupliqué.

    Différence avec un run interactif : personne n'écoute le flux SSE, donc
    il est "drainé" en tâche de fond au lieu d'être renvoyé à un client — le
    ``run_id`` est disponible immédiatement (``202``), le run continue après
    la réponse HTTP. La même fonction, attendue (``await``) au lieu d'être
    lancée en tâche, porterait une exécution SYNCHRONE : c'est le crochet
    prévu pour ``agent_tool`` (T2, timeout 120 s côté appelant).

    Lève ``TriggerNotActive`` si le workflow n'est plus publié — dernier
    filet juste avant l'exécution (la ligne trigger peut être en retard d'un
    tick sur une dépublication qui vient de survenir).
    """
    from nanoid import generate as nanoid_generate

    from apowerb.core import run_main, workflow_main
    from apowerb.core import workflow_runtime as rt
    from apowerb.core.run_gate import resolve_owner_plan
    from apowerb.core.workflow_graph import run_graph
    from apowerb.routers.workflows import _streaming_run

    wf = workflow_main.get_workflow(workflow_id, owner_id=owner_id)
    if wf is None or wf.get("status") != "published":
        raise TriggerNotActive(workflow_id)
    graph = workflow_main.parse_graph(wf["graph"])

    plan = await resolve_owner_plan(owner_id)
    run_agent, run_tool, run_rag, run_notify = rt.bindings_for(owner_id, plan)
    # Même branchement que POST /defs/{id}/run (routers.workflow_defs).
    run_subworkflow = rt.resolve_workflow_for(owner_id)

    run_id = run_main.start_run(
        trigger=json.dumps({"kind": kind, "detail": detail}, ensure_ascii=False),
        owner_id=owner_id,
        run_id=nanoid_generate(size=21),
        config={
            "workflow_id": workflow_id,
            "version": wf["version"],
            "payload": payload,
        },
    )

    def _runner(cancel_event):
        return run_graph(
            graph,
            payload=payload,
            run_agent=run_agent,
            run_tool=run_tool,
            run_rag=run_rag,
            run_subworkflow=run_subworkflow,
            workflow_id=workflow_id,
            cancel_event=cancel_event,
            run_notify=run_notify,
        )

    response = _streaming_run(
        run_id=run_id, agent_ids=[], file_bytes=None, owner=owner_id, runner=_runner
    )

    async def _drain() -> None:
        async for _ in response.body_iterator:
            pass

    task = asyncio.create_task(_drain())
    return run_id, task


# --- Tick "schedule" (voir apowerb.core.flow_scheduler) --------------------


def due_schedule_triggers(now: datetime) -> list[dict]:
    """Triggers ``schedule`` actifs dont l'échéance est passée."""
    t = workflow_trigger_store.trigger_table
    with workflow_trigger_store.engine.begin() as conn:
        rows = conn.execute(
            t.select().where(
                t.c.kind == "schedule",
                t.c.active.is_(True),
                t.c.next_run_at.isnot(None),
                t.c.next_run_at <= now.isoformat(),
            )
        ).fetchall()
    return [dict(r._mapping) for r in rows]


def _update_trigger_row(workflow_id: str, **values: Any) -> None:
    t = workflow_trigger_store.trigger_table
    with workflow_trigger_store.engine.begin() as conn:
        conn.execute(
            t.update()
            .where(t.c.workflow_id == workflow_id)
            .values(**values, updated_at=_now_iso())
        )


async def fire_schedule_trigger(row: dict, *, now: datetime) -> bool:
    """Un tick pour UN trigger ``schedule`` échu. Renvoie si un run a démarré.

    ``False`` couvre trois cas où rien n'a été lancé : chevauchement (le run
    précédent n'a pas fini), réservation perdue (un autre processus/réplica a
    déjà pris ce créneau) et workflow dépublié entre-temps — dans les trois
    cas le tick est sauté, pas une erreur.

    Pas de chevauchement : si la ligne porte encore ``last_status="running"``
    (le run précédent n'a pas fini), ce tick est sauté et journalisé — seule
    l'échéance avance, pour ne pas retenter indéfiniment le même créneau
    manqué à chaque passage de la boucle.

    Réservation atomique (sûr multi-réplica) : AVANT de lancer quoi que ce
    soit, un ``UPDATE ... WHERE workflow_id=:id AND next_run_at=:lu`` tente
    d'avancer l'échéance depuis EXACTEMENT la valeur lue par cet appelant. Un
    seul processus peut gagner cette comparaison-et-échange — c'est la ligne
    elle-même qui sert de verrou, pas ``asyncio.Lock`` (qui ne protège qu'à
    l'intérieur d'un seul processus, voir ``flow_scheduler``). Si
    ``rowcount != 1``, un autre appelant a déjà gagné ce tick entre notre
    lecture et maintenant : on se retire, sans rien lancer ni rien journaliser
    de plus — ce n'est pas une erreur, c'est la course perdue.
    """
    workflow_id = row["workflow_id"]
    cfg = json.loads(row["config"] or "{}")

    if row.get("last_status") == "running":
        logger.info(
            "[triggers] tick sauté (run précédent en cours) workflow=%s",
            workflow_id,
        )
        _update_trigger_row(
            workflow_id, next_run_at=_iso_or_none(compute_next_run(cfg, after=now))
        )
        return False

    next_at = _iso_or_none(compute_next_run(cfg, after=now))
    t = workflow_trigger_store.trigger_table
    with workflow_trigger_store.engine.begin() as conn:
        result = conn.execute(
            t.update()
            .where(
                t.c.workflow_id == workflow_id,
                t.c.next_run_at == row.get("next_run_at"),
            )
            .values(next_run_at=next_at, updated_at=_now_iso())
        )
        reserved = result.rowcount == 1
    if not reserved:
        return False

    _update_trigger_row(workflow_id, last_status="running")
    try:
        run_id, task = await launch_triggered_run(
            workflow_id=workflow_id,
            owner_id=row["owner_id"],
            kind="schedule",
            detail={"cron": cfg.get("cron"), "at": cfg.get("at")},
            payload={"scheduled_at": now.isoformat()},
        )
    except TriggerNotActive:
        logger.info(
            "[triggers] workflow %s non publié — tick ignoré, trigger désarmé",
            workflow_id,
        )
        _update_trigger_row(
            workflow_id, active=False, last_status=None, next_run_at=None
        )
        return False

    _update_trigger_row(workflow_id, last_run_id=run_id, last_fired_at=now.isoformat())

    def _on_done(_task: "asyncio.Task") -> None:
        from apowerb.core import run_main

        final = run_main.get_run(run_id, owner_id=row["owner_id"])
        final_status = (final or {}).get("status", "error")
        _update_trigger_row(
            workflow_id,
            last_status=final_status,
            next_run_at=_iso_or_none(compute_next_run(cfg, after=now)),
        )

    task.add_done_callback(_on_done)
    return True
