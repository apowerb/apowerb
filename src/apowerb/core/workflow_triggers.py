"""Service des triggers de workflow — socle T1.

Le nœud ``trigger`` du graphe (voir ``workflow_graph.trigger_spec``) reste la
source de vérité DE LA CONFIGURATION choisie par l'utilisateur. Ce module
tient l'état opérationnel qui en découle — jeton haché + chiffré, secret HMAC
chiffré, prochaine échéance, dernier déclenchement — et lance un run au nom
du propriétaire par le MÊME chemin que ``POST /api/workflows/defs/{id}/run``
(``workflow_runtime.bindings_for``, ``run_gate`` — quotas et gardes — via
``routers.workflows._streaming_run``), sans le dupliquer.

Kinds exécutés : les 7 de ``AUTOMATABLE_KINDS`` (``webhook``, ``schedule`` —
T1 — puis ``email``, ``agent_tool``, ``form``, ``file``, ``workflow_done`` —
T2). ``manual`` reste le seul jamais armé.

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

from sqlalchemy import Column, Integer, MetaData, String, Table, select

from apowerb.agent_store.workflow_trigger_store import WorkflowTriggerStore
from apowerb.configs.settings import get_settings
from apowerb.core.workflow_graph import parse_cron, trigger_spec
from apowerb.helpers.encryptor import decrypt_value, encrypt_value

logger = getLogger(__name__)

# DDL au boot, comme les autres stores (helpers/store_migrations.py) :
# importer ce module ne doit pas toucher la base.
workflow_trigger_store = WorkflowTriggerStore()

# Kinds pour lesquels un trigger PEUT être armé (``active=True``). Les 5
# kinds T2 (email, agent_tool, form, file, workflow_done) sont désormais
# exécutés par ce module.
AUTOMATABLE_KINDS = frozenset(
    {"webhook", "schedule", "email", "agent_tool", "form", "file", "workflow_done"}
)
_TOKEN_BYTES = 32

# Longueur maximale de la chaîne de déclenchement (``run.trigger.detail.chain``)
# — ``agent_tool`` (un workflow qui s'appelle via son propre outil) et
# ``workflow_done`` (A termine -> déclenche B -> déclenche C -> ...) partagent
# la même borne anti-boucle : voir ``next_trigger_chain``.
MAX_TRIGGER_CHAIN = 5

# provider du nœud trigger -> provider stocké sur ``integrations.provider``.
EMAIL_INTEGRATION_PROVIDER = {"outlook": "microsoft_outlook", "gmail": "google_gmail"}
FILE_INTEGRATION_PROVIDER = {
    "onedrive": "microsoft_onedrive",
    "google_drive": "google_drive",
}

# Table Core minimale pour vérifier la présence d'une intégration, SANS
# passer par l'ORM async (``apowerb.models``) — ce module tourne entièrement
# sur le moteur SYNC de ``workflow_trigger_store`` (même base, même schéma).
# Jamais ``create_all`` sur ces deux tables : elles existent déjà (migrations
# ORM), on ne fait que les LIRE.
_ext_metadata = MetaData(schema=workflow_trigger_store.db_schema or None)
_user_table = Table(
    "user",
    _ext_metadata,
    Column("user_id", Integer, primary_key=True),
    Column("email", String),
)
_integration_table = Table(
    "integrations",
    _ext_metadata,
    Column("id", Integer, primary_key=True),
    Column("user_id", Integer),
    Column("provider", String),
)


def integration_present(owner_id: str, provider: Optional[str]) -> bool:
    """``True`` si ``owner_id`` (email) a une intégration active de ``provider``.

    ``provider`` est déjà la valeur ``integrations.provider`` (résolue par
    l'appelant via ``EMAIL_INTEGRATION_PROVIDER``/``FILE_INTEGRATION_PROVIDER``).
    """
    if not provider:
        return True
    with workflow_trigger_store.engine.begin() as conn:
        row = conn.execute(
            select(_integration_table.c.id)
            .select_from(
                _integration_table.join(
                    _user_table, _user_table.c.user_id == _integration_table.c.user_id
                )
            )
            .where(
                _user_table.c.email == owner_id,
                _integration_table.c.provider == provider,
            )
        ).fetchone()
    return row is not None


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


def compute_active(
    cfg: dict, workflow_status: str, *, owner_id: Optional[str] = None
) -> tuple[bool, Optional[str]]:
    """(``active``, ``reason``) pour l'API de gestion — dérivé, jamais stocké.

    ``email``/``file`` ont besoin de la base (l'intégration du propriétaire) :
    ``owner_id`` doit être fourni pour que ces deux kinds puissent répondre
    ``reason:"integration_missing"`` plutôt que ``active:True`` sur une
    intégration absente/révoquée. Sans ``owner_id``, ces deux kinds sont
    traités comme n'importe quel autre kind branché (intégration supposée
    présente) — utilisé uniquement par le code qui n'a pas encore ce contexte.
    """
    kind = cfg.get("kind", "manual")
    if kind == "manual":
        return False, None
    if kind not in AUTOMATABLE_KINDS:
        return False, "not_available"
    if workflow_status != "published":
        return False, "unpublished"
    if owner_id is not None:
        provider = None
        if kind == "email":
            provider = EMAIL_INTEGRATION_PROVIDER.get(cfg.get("provider"))
        elif kind == "file":
            provider = FILE_INTEGRATION_PROVIDER.get(cfg.get("provider"))
        if provider is not None and not integration_present(owner_id, provider):
            return False, "integration_missing"
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
    active, _ = compute_active(spec, status, owner_id=owner_id)
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

        # webhook ET form portent un jeton (même mécanisme, T2 réutilise T1
        # tel quel) ; seul webhook porte un secret HMAC.
        keeps_token_identity = (
            existing_d is not None
            and existing_d.get("kind") == kind
            and existing_d.get("token_hash")
        )
        if kind in ("webhook", "form"):
            if keeps_token_identity:
                values["token_hash"] = existing_d["token_hash"]
                values["token_encrypted"] = existing_d["token_encrypted"]
            else:
                token = _generate_secret()
                values["token_hash"] = hash_token(token)
                values["token_encrypted"] = encrypt_value(token)
        else:
            values["token_hash"] = None
            values["token_encrypted"] = None

        if kind == "webhook":
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
            values["hmac_enabled"] = False
            values["hmac_secret_encrypted"] = None

        if kind == "schedule" and active:
            next_at = compute_next_run(spec, after=datetime.now(timezone.utc))
            values["next_run_at"] = _iso_or_none(next_at)
        elif kind == "file" and active:
            # ``next_run_at`` porte ici la prochaine ÉCHÉANCE DE SONDAGE (pas
            # un instant de déclenchement unique comme pour "schedule") —
            # voir ``flow_scheduler.tick_once`` / ``poll_file_trigger``.
            interval = int(spec.get("interval_min") or 15)
            values["next_run_at"] = _iso_or_none(
                datetime.now(timezone.utc) + timedelta(minutes=interval)
            )
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
    cfg = json.loads(d["config"]) if d.get("config") else {"kind": kind}
    active, reason = compute_active(cfg, workflow_status, owner_id=owner_id)

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
    run_agent, run_tool = rt.bindings_for(owner_id, plan)

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
            cancel_event=cancel_event,
        )

    response = _streaming_run(
        run_id=run_id, agent_ids=[], file_bytes=None, owner=owner_id, runner=_runner
    )

    async def _drain() -> dict:
        """Consomme le flux SSE et renvoie son événement terminal.

        Webhook/schedule (T1) ignorent le résultat de la tâche (fire-and-
        forget). ``agent_tool`` (T2) l'attend (``await task``, borné) pour
        obtenir la sortie du run déclenché ; ``notify_run_finished`` (appelé
        depuis ``_streaming_run`` lui-même, pas ici) couvre ``workflow_done``
        pour TOUT run de graphe, y compris celui-ci.
        """
        terminal: dict = {"event": "error", "detail": "aucun événement terminal reçu"}
        async for chunk in response.body_iterator:
            text = chunk.decode() if isinstance(chunk, bytes) else chunk
            if not isinstance(text, str) or not text.startswith("data: "):
                continue
            try:
                payload = json.loads(text[len("data: ") :])
            except ValueError:
                continue
            if isinstance(payload, dict) and payload.get("event") in (
                "done",
                "error",
                "cancelled",
            ):
                terminal = payload
        return terminal

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


def due_file_triggers(now: datetime) -> list[dict]:
    """Triggers ``file`` actifs dont la prochaine échéance de SONDAGE est
    passée. ``next_run_at`` porte ici une échéance de sondage périodique
    (voir ``sync_trigger_for_workflow``), pas un instant de déclenchement
    unique — symétrique de ``due_schedule_triggers``."""
    t = workflow_trigger_store.trigger_table
    with workflow_trigger_store.engine.begin() as conn:
        rows = conn.execute(
            t.select().where(
                t.c.kind == "file",
                t.c.active.is_(True),
                t.c.next_run_at.isnot(None),
                t.c.next_run_at <= now.isoformat(),
            )
        ).fetchall()
    return [dict(r._mapping) for r in rows]


def reserve_next_poll(workflow_id: str, *, prior_next_run_at, next_run_at: str) -> bool:
    """CAS sur ``next_run_at`` : réserve CE sondage avant de le lancer.

    Même mécanisme que ``fire_schedule_trigger`` (voir son docstring) :
    ``UPDATE ... WHERE next_run_at=<valeur lue>`` — un seul appelant gagne la
    comparaison-et-échange, sûr multi-réplica sans verrou distribué dédié.
    """
    t = workflow_trigger_store.trigger_table
    with workflow_trigger_store.engine.begin() as conn:
        result = conn.execute(
            t.update()
            .where(t.c.workflow_id == workflow_id, t.c.next_run_at == prior_next_run_at)
            .values(next_run_at=next_run_at, updated_at=_now_iso())
        )
    return result.rowcount == 1


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


# --- T2 : partagé agent_tool / workflow_done --------------------------------


def tool_name_taken(
    tool_name: str, *, owner_id: str, exclude_workflow_id: Optional[str] = None
) -> bool:
    """``True`` si un AUTRE workflow du même propriétaire porte déjà ce
    ``tool_name`` (kind ``agent_tool``), publié ou non — un brouillon réserve
    aussi le nom pour éviter une collision surprise à la publication."""
    t = workflow_trigger_store.trigger_table
    with workflow_trigger_store.engine.begin() as conn:
        rows = conn.execute(
            t.select().where(t.c.kind == "agent_tool", t.c.owner_id == owner_id)
        ).fetchall()
    for r in rows:
        d = dict(r._mapping)
        if exclude_workflow_id is not None and d["workflow_id"] == exclude_workflow_id:
            continue
        cfg = json.loads(d["config"] or "{}")
        if cfg.get("tool_name") == tool_name:
            return True
    return False


def list_active_triggers(kind: str, *, owner_id: Optional[str] = None) -> list[dict]:
    """Lignes actives d'un ``kind`` donné, éventuellement filtrées par propriétaire."""
    t = workflow_trigger_store.trigger_table
    conds = [t.c.kind == kind, t.c.active.is_(True)]
    if owner_id is not None:
        conds.append(t.c.owner_id == owner_id)
    with workflow_trigger_store.engine.begin() as conn:
        rows = conn.execute(t.select().where(*conds)).fetchall()
    return [dict(r._mapping) for r in rows]


def next_trigger_chain(prior_chain: list[str], workflow_id: str) -> Optional[list[str]]:
    """``None`` si ajouter ``workflow_id`` créerait un cycle ou dépasserait
    ``MAX_TRIGGER_CHAIN`` ; sinon la chaîne étendue.

    Pure et testable sans base : ``agent_tool`` (un workflow qui s'appelle
    via son propre outil) et ``workflow_done`` (A -> B -> C -> ...) partagent
    cette même garde, portée par ``run.trigger.detail.chain``.
    """
    chain = list(prior_chain or [])
    if workflow_id in chain:
        logger.warning(
            "[triggers] cycle de déclenchement bloqué : %s -> %s", chain, workflow_id
        )
        return None
    if len(chain) >= MAX_TRIGGER_CHAIN:
        logger.warning(
            "[triggers] chaîne de déclenchement plafonnée à %d : %s",
            MAX_TRIGGER_CHAIN,
            chain,
        )
        return None
    return chain + [workflow_id]


# --- T2 : workflow_done ------------------------------------------------------


def _error_detail(status: str, error_message: Optional[str]) -> Optional[dict]:
    if status != "error" or not error_message:
        return None
    return {"code": "run_failed", "detail": error_message}


async def notify_run_finished(
    *,
    run_id: str,
    owner_id: str,
    status: str,
    output: Any,
    error_message: Optional[str],
) -> None:
    """Déclenche les triggers ``workflow_done`` en écoute sur CE run.

    Appelé depuis ``routers.workflows._streaming_run`` pour TOUT run de
    graphe persisté qui se termine — interactif, rejeu ou déclenché (webhook,
    schedule, email, file, agent_tool, un autre workflow_done) — un seul
    point d'accroche couvre donc toutes les origines. Un run sans
    ``config.workflow_id`` (canvas legacy) est ignoré : ce n'est pas un
    workflow persisté, rien ne peut l'écouter par ce kind.
    """
    from apowerb.core import run_main

    t = run_main.run_store.run_table
    with run_main.run_store.engine.begin() as conn:
        row = conn.execute(t.select().where(t.c.run_id == run_id)).fetchone()
    if row is None:
        return
    d = dict(row._mapping)
    try:
        config = json.loads(d.get("config") or "{}")
    except json.JSONDecodeError:
        config = {}
    source_workflow_id = config.get("workflow_id")
    if not source_workflow_id:
        return

    prior_chain: list[str] = []
    try:
        own_trigger = json.loads(d.get("trigger") or "{}")
    except json.JSONDecodeError:
        own_trigger = {}
    if isinstance(own_trigger, dict):
        prior_chain = list((own_trigger.get("detail") or {}).get("chain") or [])

    await fire_workflow_done_triggers(
        source_workflow_id=source_workflow_id,
        source_owner_id=owner_id,
        source_run_id=run_id,
        status=status,
        output=output,
        error=_error_detail(status, error_message),
        prior_chain=prior_chain,
    )


async def fire_workflow_done_triggers(
    *,
    source_workflow_id: str,
    source_owner_id: str,
    source_run_id: str,
    status: str,
    output: Any,
    error: Optional[dict],
    prior_chain: list[str],
) -> list[str]:
    """Lance un run pour chaque trigger ``workflow_done`` en écoute sur
    ``source_workflow_id``. Renvoie les ``run_id`` effectivement lancés.

    Filtré par ``owner_id`` ET ``config.workflow_id == source_workflow_id`` :
    la validation (T2, ``workflow_graph.validate_trigger_config``) refuse déjà
    un ``workflow_id`` source d'un autre propriétaire — ce filtre est une
    défense en profondeur, pas la seule protection.
    """
    started: list[str] = []
    for row in list_active_triggers("workflow_done", owner_id=source_owner_id):
        cfg = json.loads(row["config"] or "{}")
        if cfg.get("workflow_id") != source_workflow_id:
            continue
        on = cfg.get("on", "any")
        if on not in ("any", status):
            continue
        chain = next_trigger_chain(prior_chain, source_workflow_id)
        if chain is None:
            continue
        dest_workflow_id = row["workflow_id"]
        try:
            run_id, _task = await launch_triggered_run(
                workflow_id=dest_workflow_id,
                owner_id=source_owner_id,
                kind="workflow_done",
                detail={
                    "workflow_id": source_workflow_id,
                    "on": status,
                    "chain": chain,
                },
                payload={
                    "workflow_id": source_workflow_id,
                    "run_id": source_run_id,
                    "status": status,
                    "output": output,
                    "error": error,
                },
            )
        except TriggerNotActive:
            logger.info(
                "[triggers] workflow_done cible %s non publié — ignoré",
                dest_workflow_id,
            )
            continue
        started.append(run_id)
    return started


# --- T2 : agent_tool ----------------------------------------------------------

AGENT_TOOL_TIMEOUT_SECONDS = 120


async def call_agent_tool(
    *,
    workflow_id: str,
    owner_id: str,
    tool_name: str,
    arguments: dict,
    prior_chain: list[str],
) -> dict:
    """Exécution SYNCHRONE (bornée à ``AGENT_TOOL_TIMEOUT_SECONDS``) d'un
    workflow choisi comme outil d'agent (``workflow:<tool_name>``).

    Renvoie toujours un dict, jamais une exception : un outil d'agent qui
    lève casse le tour de function-calling de l'appelant — la garde
    anti-récursion, l'expiration et l'échec du run sont donc des CLÉS du
    retour (``error``), pas des levées.
    """
    chain = next_trigger_chain(prior_chain, workflow_id)
    if chain is None:
        return {
            "error": "recursion_blocked",
            "detail": (
                f"chaîne de déclenchement de {tool_name!r} bloquée "
                f"(cycle ou profondeur > {MAX_TRIGGER_CHAIN})"
            ),
        }
    try:
        run_id, task = await launch_triggered_run(
            workflow_id=workflow_id,
            owner_id=owner_id,
            kind="agent_tool",
            detail={"tool_name": tool_name, "chain": chain},
            payload=arguments,
        )
    except TriggerNotActive:
        return {"error": "not_active", "detail": "ce workflow n'est plus publié"}

    try:
        terminal = await asyncio.wait_for(task, timeout=AGENT_TOOL_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        return {
            "error": "timeout",
            "run_id": run_id,
            "detail": f"pas de réponse en {AGENT_TOOL_TIMEOUT_SECONDS}s",
        }

    event = terminal.get("event")
    if event == "done":
        return {"run_id": run_id, "output": terminal.get("output")}
    if event == "cancelled":
        return {"error": "cancelled", "run_id": run_id}
    return {"error": "run_failed", "run_id": run_id, "detail": terminal.get("detail")}
