"""Enregistrement et rejeu des exécutions d'agents.

Ce qui manquait pour rejouer un run n'était pas l'observation — les spans ADK
partent déjà en OTLP et l'usage LLM est compté — mais **l'entrée**. Un run se
consigne ici au moment où il démarre, avec ce qu'il a reçu ; son issue est
écrite quand il se termine. Rejouer, c'est relire cette entrée et repartir.

Le rejeu crée toujours un nouveau run qui cite l'original (``replay_of``) :
l'historique n'est pas réécrit, et un rejeu raté se distingue de l'échec
d'origine.

Ce qu'un rejeu refait, selon le déclencheur :

- ``workflow`` : le canvas, avec les agents, la configuration et le fichier
  conservés.
- ``schedule`` et ``chat`` : le message conservé, envoyé au même agent, sous
  l'identité du propriétaire, dans une session ADK **neuve** — la session
  d'origine peut contenir un échange à moitié fait, et pour le chat c'est la
  conversation de l'utilisateur, où un rejeu n'a pas à s'écrire. Conséquence :
  l'historique de la conversation n'est pas rejoué, seul le message l'est.

Les effets de bord : un agent agit par ses outils (envoyer un mail, écrire en
base). Un run d'agent consigne donc les outils qu'il a appelés
(``tools_executed``), et un échec n'est rejoué d'office que si l'on sait
qu'aucun outil n'a été appelé. Si un outil l'a été, ou si on ne peut pas le
savoir (``NULL``), le rejeu est refusé (409) : rien ne permet de rejouer un
agent en sautant les appels déjà faits, donc rejouer referait l'effet. Seul
``force=true`` passe outre, en connaissance de cause.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from collections.abc import AsyncIterator
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
# Arrêté sur une décision humaine : ni fini, ni en vol, ni orphelin.
STATUS_AWAITING = "awaiting"

# Statuts depuis lesquels un rejeu va de soi. Un run réussi a déjà produit ses
# effets de bord et un run en vol n'a pas encore rendu son verdict : les deux
# demandent un geste explicite (``force``) ou un refus.
_REPLAYABLE = (STATUS_ERROR, STATUS_CANCELLED)

# Déclencheurs dont le run est un agent ADK, qui agit par ses outils : ceux-là
# ne sont rejoués sans ``force`` que si leurs outils sont connus et vides.
AGENT_TRIGGERS = ("schedule", "chat")

# Un run tué en vol (process arrêté, pod redémarré) ne passe jamais par le
# ``finally`` de ``track_agent_stream`` : il reste ``running`` sans ``finished_at``.
# Passé ce délai, on le règle pour qu'il reflète la réalité et redevienne
# rejouable. Généreux pour ne jamais clore un vrai run long (même borne que la
# réconciliation côté déclencheur schedule).
RUN_STALE_AFTER = timedelta(hours=6)


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

    # Audit « quelle définition d'agent a produit ce run » (demande Dipankar) :
    # on épingle l'empreinte des agents dans config, comme un run de workflow y
    # épingle déjà sa version. Dans config (JSON), pas en colonne : create_all
    # n'ajoute pas de colonne à une table existante (jamais en prod). Best-effort
    # et jamais bloquant ; on écrase toujours la clé pour qu'un rejeu n'hérite
    # pas des versions du run d'origine.
    cfg = dict(config or {})
    cfg.pop("_agent_versions", None)
    try:
        from apowerb.core.agent_runtime_sync import agent_version_records

        versions = agent_version_records(agent_ids or [])
    except Exception:  # l'audit ne doit jamais empêcher un run de partir
        versions = {}
    if versions:
        cfg["_agent_versions"] = versions

    values = dict(
        trigger=trigger,
        owner_id=owner_id,
        organization_id=organization_id,
        agent_ids=json.dumps(agent_ids or []),
        config=json.dumps(cfg),
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


def start_run_safely(**kwargs) -> str | None:
    """``start_run``, sans jamais empêcher le run de partir.

    Même politique que ``/workflows/run-sse`` : une base indisponible dégrade
    le suivi, pas l'exécution. Le run part sans trace — donc sans rejeu — et
    on le dit au niveau qui réveille quelqu'un.
    """
    try:
        return start_run(**kwargs)
    except Exception:  # la trace ne doit pas arrêter le run
        logger.exception(
            "[RUNS] run %s non consigne — il s'execute sans trace, donc sans rejeu",
            kwargs.get("trigger"),
        )
        return None


def settle_run(
    run_id: str | None,
    status: str,
    error_message: str | None = None,
    tools_executed: list[str] | None = None,
) -> None:
    """Écrit l'issue d'un run et ses outils, sans jamais masquer cette issue."""
    if run_id is None:
        return
    try:
        finish_run(run_id, status=status, error_message=error_message)
        record_tools_executed(run_id, tools_executed)
    except Exception:  # la trace ne doit pas masquer l'issue
        logger.exception("[RUNS] issue du run_id=%s non consignee", run_id)


def failure_cause(exc: BaseException) -> str:
    """La cause consignée d'un échec.

    Un routeur traduit l'exception d'origine en ``HTTPException`` au message
    générique, pour ne rien divulguer au client ; la trace, elle, est lue par
    le propriétaire du run et doit dire ce qui s'est passé.
    """
    origin = (
        exc.__context__ if isinstance(exc, HTTPException) and exc.__context__ else exc
    )
    return f"{type(origin).__name__}: {origin}"


def finish_run(run_id: str, status: str, error_message: str | None = None) -> None:
    """Écrit l'issue d'un run.

    Ne filtre pas par propriétaire : l'appelant est le code qui a lancé le run,
    pas une requête utilisateur.
    """
    t = run_store.run_table
    with run_store.engine.begin() as conn:
        conn.execute(
            t.update()
            # Une pause consignée (``pause_run``) n'est jamais une fin : seule
            # la reprise ou l'abandon la font sortir de ``awaiting``.
            .where(t.c.run_id == run_id, t.c.status != STATUS_AWAITING)
            .values(
                status=status,
                error_message=error_message,
                finished_at=_now(),
                paused_state=None,
                assignee=None,
                decision=None,
            )
        )


def record_tools_executed(run_id: str, tools: list[str] | None) -> None:
    """Consigne les outils qu'un run a appelés. ``None`` laisse « inconnu »."""
    if tools is None:
        return
    with run_store.engine.begin() as conn:
        conn.execute(
            run_store.run_table.update()
            .where(run_store.run_table.c.run_id == run_id)
            .values(tools_executed=json.dumps(tools))
        )


def executed_tools(events) -> list[str]:
    """Les outils appelés dans une liste d'événements ADK, dans l'ordre.

    C'est l'appel (``functionCall``) qui compte, pas la réponse : un outil
    qui plante à mi-course a pu agir sans jamais répondre.
    """
    names: list[str] = []
    for event in events if isinstance(events, list) else []:
        content = event.get("content") if isinstance(event, dict) else None
        for part in (content or {}).get("parts") or []:
            call = part.get("functionCall") or part.get("function_call")
            if isinstance(call, dict) and call.get("name"):
                names.append(call["name"])
    return names


async def executed_tools_in_session(
    agent_name: str, user_id: str, session_id: str, token: str | None
) -> list[str] | None:
    """Les outils appelés dans une session ADK, ou ``None`` si illisible.

    Sert après un échec : l'appel ``/run`` a levé, sa réponse est perdue,
    mais la session a gardé ce qui s'est passé avant. Seul compte ce qui suit
    le dernier message de l'utilisateur : une conversation porte les outils
    de ses tours précédents, qui ne sont pas des effets de celui-ci.
    """
    from apowerb.core import adk_runner

    try:
        session = await adk_runner.get_adk_session(
            agent_name=agent_name, user_id=user_id, session_id=session_id, token=token
        )
    except Exception as exc:  # noqa: BLE001 - inconnu, pas « aucun »
        logger.warning(
            "[RUNS] session %s illisible (%s) — outils appelés inconnus",
            session_id,
            exc.__class__.__name__,
        )
        return None
    events = (session or {}).get("events") or []
    last_user_turn = max(
        (
            i
            for i, e in enumerate(events)
            if isinstance(e, dict) and e.get("author") == "user"
        ),
        default=-1,
    )
    return executed_tools(events[last_user_turn + 1 :])


async def track_agent_stream(
    run_id: str | None, stream: AsyncIterator[str]
) -> AsyncIterator[str]:
    """Relaie un flux SSE d'agent tel quel et consigne son issue.

    Le relais transmet des octets bruts : un événement peut être coupé
    n'importe où, d'où le tampon découpé sur ``\n\n``. Le flux se termine
    normalement même quand l'agent a échoué — sur une enveloppe
    ``{"error": ...}`` — et c'est elle qui fait l'échec. Un client qui ferme
    le flux annule le run.
    """
    status, error, tools, buffer = STATUS_SUCCESS, None, [], ""
    try:
        async for chunk in stream:
            yield chunk
            buffer += chunk.decode() if isinstance(chunk, bytes) else chunk
            *blocks, buffer = buffer.split("\n\n")
            for block in blocks:
                event = _sse_event(block)
                if event is None:
                    continue
                if event.get("error"):
                    status, error = STATUS_ERROR, str(event["error"])
                elif not event.get("partial"):
                    # Un événement partiel est répété en entier ensuite.
                    tools.extend(executed_tools([event]))
    except (GeneratorExit, asyncio.CancelledError):
        status = STATUS_CANCELLED
        raise
    except Exception as exc:
        status, error = STATUS_ERROR, failure_cause(exc)
        raise
    finally:
        settle_run(run_id, status, error, tools)


def _sse_event(block: str) -> dict | None:
    data = "".join(
        line[len("data:") :].strip()
        for line in block.splitlines()
        if line.startswith("data:")
    )
    try:
        event = json.loads(data) if data else None
    except ValueError:
        return None
    return event if isinstance(event, dict) else None


def _reconcile_if_orphaned(conn, row):
    """Règle un run resté ``running`` faute d'avoir pu consigner son issue.

    ``settle_run`` vit dans un ``finally`` : un arrêt brutal ne l'exécute
    jamais, et le run reste ``running`` à vie. Passé ``RUN_STALE_AFTER``, on
    l'écrit ``error`` (dans ``_REPLAYABLE``, et ``cancelled`` mentirait sur la
    cause) avec un ``finished_at`` et un message explicite. Le ``WHERE status``
    rend l'écriture idempotente et sûre entre réplicas ; la ligne est relue
    pour refléter l'écriture. Sans ``created_at`` lisible, on ne touche rien
    (repli vers l'inaction). Opère dans la transaction de lecture de l'appelant.
    """
    if row is None:
        return row
    data = row._asdict()
    if data.get("status") != STATUS_RUNNING:
        return row
    # Un run repris après une décision a pu attendre des jours : son âge
    # compte depuis la reprise.
    created = data.get("resumed_at") or data.get("created_at")
    if not created:
        return row
    try:
        age = datetime.now() - datetime.fromisoformat(created)
    except ValueError:
        return row
    if age < RUN_STALE_AFTER:
        return row
    if data.get("paused_state"):
        # Repris puis arrêté avant que la réponse soit prise (l'instantané
        # n'est effacé qu'à ce moment, voir ``mark_decided``) : rien n'a été
        # fait après la pause, le run attend toujours sa décision.
        conn.execute(
            run_store.run_table.update()
            .where(
                run_store.run_table.c.run_id == data["run_id"],
                run_store.run_table.c.status == STATUS_RUNNING,
            )
            .values(status=STATUS_AWAITING, resumed_at=None)
        )
        return conn.execute(
            run_store.run_table.select().where(
                run_store.run_table.c.run_id == data["run_id"]
            )
        ).fetchone()
    hours = int(RUN_STALE_AFTER.total_seconds() // 3600)
    message = (
        "Run settled by reconciliation: no completion was recorded within "
        f"{hours}h; its actual outcome is unknown."
    )
    conn.execute(
        run_store.run_table.update()
        .where(
            run_store.run_table.c.run_id == data["run_id"],
            run_store.run_table.c.status == STATUS_RUNNING,
        )
        .values(status=STATUS_ERROR, error_message=message, finished_at=_now())
    )
    return conn.execute(
        run_store.run_table.select().where(
            run_store.run_table.c.run_id == data["run_id"]
        )
    ).fetchone()


def _row_to_dict(row) -> dict:
    run = row._asdict()
    # L'instantané d'une pause peut peser plusieurs Mio et ne regarde que la
    # reprise : on n'expose que la question en attente.
    run.pop("paused_state", None)
    run.pop("assignee", None)
    run.pop("resumed_at", None)
    decision = run.pop("decision", None)
    if run.get("status") == STATUS_AWAITING and decision:
        run["decision"] = json.loads(decision)
    run["agent_ids"] = json.loads(run.get("agent_ids") or "[]")
    tools = run.get("tools_executed")
    run["tools_executed"] = json.loads(tools) if tools else None
    try:
        run["config"] = json.loads(run.get("config") or "{}")
    except json.JSONDecodeError:
        run["config"] = {}
    # Le chemin disque ne regarde pas l'appelant : il dit où vivent les données
    # sur le serveur. On expose seulement s'il reste rejouable.
    has_file = bool(run.pop("input_file_path", None))
    if run.get("trigger") in AGENT_TRIGGERS:
        # Un run d'agent garde son entrée dans ``config``, jamais sur disque :
        # c'est ce que relit le rejeu (_agent_replay_runner).
        config = run["config"]
        run["input_available"] = bool(
            config.get("agent_name") and config.get("new_message")
        )
    elif (
        not run["agent_ids"]
        and "workflow_id" in run["config"]
        and "version" in run["config"]
    ):
        # Un run de workflow persisté se rejoue depuis le graphe de sa version
        # (même critère que routers.workflows._is_graph_run).
        run["input_available"] = True
    else:
        run["input_available"] = has_file
    return run


def get_run(run_id: str, owner_id: str) -> dict | None:
    """Un run donné, à condition qu'il appartienne à ce propriétaire."""
    with run_store.engine.begin() as conn:
        _expire_due(conn)
        row = conn.execute(
            run_store.run_table.select().where(
                run_store.run_table.c.run_id == run_id,
                run_store.run_table.c.owner_id == owner_id,
            )
        ).fetchone()
        row = _reconcile_if_orphaned(conn, row)
    return _row_to_dict(row) if row else None


def list_runs(owner_id: str, limit: int = 50) -> list[dict]:
    """Les runs d'un propriétaire, du plus récent au plus ancien."""
    with run_store.engine.begin() as conn:
        _expire_due(conn)
        rows = conn.execute(
            run_store.run_table.select()
            .where(run_store.run_table.c.owner_id == owner_id)
            .order_by(run_store.run_table.c.created_at.desc())
            .limit(limit)
        ).fetchall()
        rows = [_reconcile_if_orphaned(conn, row) for row in rows]
    return [_row_to_dict(row) for row in rows]


def _refuse_if_side_effects(tools_executed: str | None) -> None:
    """409 si l'échec a pu laisser un effet de bord qu'un rejeu referait."""
    if tools_executed is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "It is unknown whether this run called any tool before failing, "
                "so a replay could repeat a side effect. Pass force=true to "
                "run it again anyway."
            ),
        )
    tools = json.loads(tools_executed or "[]")
    if tools:
        raise HTTPException(
            status_code=409,
            detail=(
                f"This run already called {', '.join(tools)} before failing: "
                "a replay would call them again. Pass force=true to run it "
                "again anyway."
            ),
        )


def prepare_replay(run_id: str, owner_id: str, force: bool = False) -> dict:
    """Ouvre un nouveau run à partir de l'entrée conservée par un run passé.

    Renvoie de quoi relancer : ``run_id`` (le neuf), ``agent_ids``, ``config``,
    ``file_bytes`` et ``file_name``. Le run d'origine n'est pas modifié.
    """
    with run_store.engine.begin() as conn:
        _expire_due(conn)
        row = conn.execute(
            run_store.run_table.select().where(
                run_store.run_table.c.run_id == run_id,
                run_store.run_table.c.owner_id == owner_id,
            )
        ).fetchone()
        row = _reconcile_if_orphaned(conn, row)

    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Run not found, or you do not have permission to replay it.",
        )

    original = row._asdict()
    status = original.get("status")
    if status == STATUS_AWAITING:
        # Rejouer referait tout ce que la reprise rejouera sans l'exécuter.
        raise HTTPException(
            status_code=409,
            detail="This run is waiting for a human decision — answer or abandon it.",
        )
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

    if (
        not force
        and status in _REPLAYABLE
        and original.get("trigger") in AGENT_TRIGGERS
    ):
        _refuse_if_side_effects(original.get("tools_executed"))

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
        "trigger": original.get("trigger") or "workflow",
        "agent_ids": agent_ids,
        "config": config,
        "file_bytes": file_bytes,
        "file_name": file_name,
    }


# --- Décision humaine ---------------------------------------------------------
#
# Un run arrêté sur un nœud ``approval`` garde son ``run_id`` : la pause écrit
# l'instantané et le statut ``awaiting``, la reprise le reprend atomiquement
# (``awaiting`` -> ``running``) puis le run finit comme n'importe quel autre.


EXPIRED_MESSAGE = "Décision humaine expirée : personne n'a répondu avant l'échéance."
UNREADABLE_MESSAGE = (
    "Décision humaine illisible : l'instantané ne se déchiffre plus "
    "(ENCRYPT_KEY changée depuis la pause ?)."
)
ABANDONED_MESSAGE = "Décision humaine abandonnée"
STOPPED_MESSAGE = "Décision humaine abandonnée : run arrêté avant que la réponse soit prise."
# ``end_notice`` d'une fin sans réponse que ``core.decision_notices`` annonce.
END_NOTICE_PENDING = "pending"
# Runs clos sans réponse possible : un retardataire apprend pourquoi (409).
_CLOSED_DECISIONS = {
    EXPIRED_MESSAGE: "decision_expired",
    UNREADABLE_MESSAGE: "decision_state_unreadable",
}


def _instant(stamp: str) -> datetime:
    """Une échéance stockée, en instant avec fuseau.

    Les pauses écrivent de l'ISO UTC. Celles d'avant gardent le format
    ``_now``, en heure locale du serveur : lue avec les règles de la date
    elle-même, elle reste juste de part et d'autre d'un changement d'heure.
    """
    if "T" in stamp:
        moment = datetime.fromisoformat(stamp)
        return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)
    return datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S.%f").astimezone()


def _iso(stamp: str | None) -> str | None:
    """Une échéance stockée en ISO avec fuseau, pour le navigateur."""
    if not stamp:
        return stamp
    return _instant(stamp).isoformat()


def _decision_of(state: dict) -> dict:
    return {"node_id": state.get("node_id"), **(state.get("prompt") or {})}


def _seal(state: dict) -> str:
    """L'instantané au repos : il porte les sorties du run, chiffré (Fernet).

    Sans ``ENCRYPT_KEY`` le chiffrement lève, la pause échoue
    (``decision_not_saved``) : jamais de repli en clair.
    """
    from apowerb.helpers.encryptor import encrypt_value

    return encrypt_value(json.dumps(state, ensure_ascii=False, default=str))


def _unseal(raw: str) -> dict:
    from apowerb.helpers.encryptor import decrypt_value

    # Pauses écrites en clair avant le chiffrement : un JSON, pas un jeton.
    return json.loads(raw if raw.lstrip().startswith("{") else decrypt_value(raw))


def _expire_due(conn, now: datetime | None = None) -> None:
    """Clôt en ``error`` les décisions dont l'échéance est passée.

    Appliquée à la lecture (listes, run, reprise, abandon, rejeu), donc
    avant que quiconque s'appuie sur le statut — le planificateur compris ;
    ``core.decision_notices`` l'applique aussi à chaque passage pour
    annoncer l'expiration. La comparaison se fait en instants, pas en
    chaînes : les échéances anciennes (heure locale) et nouvelles (UTC)
    n'ont pas le même format. Les décisions en attente sont peu nombreuses.
    Le ``WHERE status`` et l'échéance lue rendent l'écriture idempotente
    entre réplicas.
    """
    t = run_store.run_table
    now = now or datetime.now(timezone.utc)
    pending = conn.execute(
        t.select()
        .with_only_columns(t.c.run_id, t.c.expires_at)
        .where(t.c.status == STATUS_AWAITING, t.c.expires_at.isnot(None))
    ).fetchall()
    for run_id, expires_at in pending:
        try:
            due = _instant(expires_at) <= now
        except ValueError:
            # Une échéance illisible ne doit pas casser toutes les lectures.
            logger.error("[RUNS] échéance illisible pour run_id=%s : %r", run_id, expires_at)
            continue
        if not due:
            continue
        conn.execute(
            t.update()
            .where(
                t.c.run_id == run_id,
                t.c.status == STATUS_AWAITING,
                t.c.expires_at == expires_at,
            )
            .values(
                status=STATUS_ERROR,
                error_message=EXPIRED_MESSAGE,
                finished_at=_now(),
                paused_state=None,
                decision=None,
                end_notice=END_NOTICE_PENDING,
                # L'assigné reste : sa réponse tardive s'entend dire
                # « expirée » (409), pas « inconnue » (404).
            )
        )


def _release_dead_resumptions(conn) -> None:
    """Remet en attente les reprises mortes avant d'avoir pris la décision.

    Même règle que ``_reconcile_if_orphaned`` (instantané encore là, âge
    compté depuis la reprise), appliquée sans que le propriétaire ouvre le
    run : la liste de l'assigné, la reprise et l'abandon en dépendent.
    """
    t = run_store.run_table
    stale = (datetime.now() - RUN_STALE_AFTER).strftime("%Y-%m-%d %H:%M:%S.%f")
    conn.execute(
        t.update()
        .where(
            t.c.status == STATUS_RUNNING,
            t.c.paused_state.isnot(None),
            t.c.resumed_at.isnot(None),
            t.c.resumed_at <= stale,
        )
        .values(status=STATUS_AWAITING, resumed_at=None)
    )


def remind_moment(paused: datetime, expires: datetime) -> datetime:
    """Quand rappeler la décision : 24 h avant l'échéance, à mi-délai si
    le délai ne laisse pas 48 h."""
    delay = expires - paused
    return expires - min(timedelta(hours=24), delay / 2)


def pause_run(run_id: str, snapshot: dict, now: datetime | None = None) -> None:
    """Consigne un run arrêté sur une décision humaine, avec son instantané.

    L'échéance est calculée et stockée en UTC : une heure locale à décalage
    figé se tromperait d'une heure à travers un changement d'heure.
    """
    from apowerb.core.workflow_graph import DEFAULT_DECISION_HOURS

    prompt = snapshot.get("prompt") or {}
    assignee = prompt.get("assignee")
    hours = prompt.get("expires_after_hours") or DEFAULT_DECISION_HOURS
    paused = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    expires = paused + timedelta(hours=hours)
    snapshot = {**snapshot, "paused_at": paused.isoformat()}
    decision = {
        **_decision_of(snapshot),
        "paused_at": snapshot["paused_at"],
        "expires_at": expires.isoformat(),
    }
    with run_store.engine.begin() as conn:
        result = conn.execute(
            run_store.run_table.update()
            .where(run_store.run_table.c.run_id == run_id)
            .values(
                status=STATUS_AWAITING,
                paused_state=_seal(snapshot),
                decision=json.dumps(decision, ensure_ascii=False, default=str),
                assignee=assignee.strip().lower() if isinstance(assignee, str) else None,
                expires_at=expires.isoformat(),
                finished_at=None,
                resumed_at=None,
                # Une nouvelle décision : elle sera annoncée, puis rappelée.
                remind_at=remind_moment(paused, expires).isoformat(),
                notified_at=None,
                reminded_at=None,
                end_notice=None,
            )
        )
    if result.rowcount != 1:
        # Un run sans ligne ne pourrait jamais être repris : mieux vaut un
        # échec visible (``decision_not_saved``) qu'une attente sans fin.
        raise LookupError(f"run {run_id} non consigné : pause impossible")


def _may_decide(row: dict, user: str) -> bool:
    return row["owner_id"] == user or (row.get("assignee") or "") == user.lower()


def _awaiting_row(conn, run_id: str, user: str) -> dict:
    row = conn.execute(
        run_store.run_table.select().where(run_store.run_table.c.run_id == run_id)
    ).fetchone()
    # 404 pour un tiers comme pour un run inconnu : ne pas révéler qu'il existe.
    if row is None or not _may_decide(row._asdict(), user):
        raise HTTPException(status_code=404, detail=f"Unknown run: {run_id}")
    data = row._asdict()
    closed = _CLOSED_DECISIONS.get(data.get("error_message") or "")
    if data["status"] == STATUS_ERROR and closed:
        detail = {"code": closed}
        if closed == "decision_expired":
            detail["expires_at"] = _iso(data.get("expires_at"))
        raise HTTPException(status_code=409, detail=detail)
    if data["status"] != STATUS_AWAITING or not data.get("paused_state"):
        raise HTTPException(
            status_code=409,
            detail={"code": "decision_already_answered", "status": data["status"]},
        )
    return data


def list_awaiting(user: str) -> list[dict]:
    """Les runs en attente d'une décision de ``user`` (propriétaire ou assigné)."""
    t = run_store.run_table
    with run_store.engine.begin() as conn:
        _release_dead_resumptions(conn)
        _expire_due(conn)
        # Jamais ``paused_state`` ici : l'instantané peut peser des Mio.
        rows = conn.execute(
            t.select()
            .with_only_columns(t.c.run_id, t.c.owner_id, t.c.config, t.c.decision)
            .where(
                t.c.status == STATUS_AWAITING,
                (t.c.owner_id == user) | (t.c.assignee == user.lower()),
            )
            .order_by(t.c.created_at.desc())
        ).fetchall()
    out = []
    for row in rows:
        data = row._asdict()
        config = json.loads(data.get("config") or "{}")
        out.append(
            {
                "run_id": data["run_id"],
                # Interne (nom du workflow) : la route le retire, l'assigné
                # n'a pas à connaître l'adresse du propriétaire.
                "owner_id": data["owner_id"],
                "mine": data["owner_id"] == user,
                "workflow_id": config.get("workflow_id"),
                **json.loads(data.get("decision") or "{}"),
            }
        )
    return out


def claim_paused_run(run_id: str, user: str, choice, text) -> dict:
    """Prend la décision pour ``user`` : la ligne repasse ``running``.

    404 (inconnu ou pas à lui), 409 (déjà repris, abandonné, ou pris par un
    double clic concurrent : l'``UPDATE ... WHERE status='awaiting'`` ne
    réussit qu'une fois), 422 (réponse non permise). Renvoie la ligne, son
    instantané (``state``) et la réponse (``answer``).
    """
    from cryptography.fernet import InvalidToken

    from apowerb.core.workflow_graph import GraphError, check_decision_answer

    t = run_store.run_table
    with run_store.engine.begin() as conn:
        # Validées avant un refus qui annulerait la transaction.
        _release_dead_resumptions(conn)
        _expire_due(conn)
    resumed_at = _now()
    with run_store.engine.begin() as conn:
        data = _awaiting_row(conn, run_id, user)
        try:
            state = _unseal(data["paused_state"])
        except (InvalidToken, ValueError):
            # Clé changée, jeton abîmé ou ancien instantané en clair corrompu
            # (``JSONDecodeError`` est une ``ValueError``) : aucune reprise
            # possible. Le run est clos ici plutôt que laissé en attente
            # jusqu'à son échéance ; le refus suit, hors de cette transaction.
            state = None
            conn.execute(
                t.update()
                .where(t.c.run_id == run_id, t.c.status == STATUS_AWAITING)
                .values(
                    status=STATUS_ERROR,
                    error_message=UNREADABLE_MESSAGE,
                    finished_at=_now(),
                    paused_state=None,
                    decision=None,
                    end_notice=END_NOTICE_PENDING,
                )
            )
    if state is None:
        raise HTTPException(status_code=409, detail={"code": "decision_state_unreadable"})
    with run_store.engine.begin() as conn:
        try:
            choice, text = check_decision_answer(state.get("prompt") or {}, choice, text)
        except GraphError as exc:
            raise HTTPException(
                status_code=422, detail={"code": exc.code, "params": exc.params}
            ) from exc
        # L'instantané reste en place : si la reprise n'atteint pas la
        # décision (client parti, panne au démarrage), ``release_paused_run``
        # remet le run en attente. ``finish_run`` l'efface à la vraie fin.
        claimed = conn.execute(
            t.update()
            .where(t.c.run_id == run_id, t.c.status == STATUS_AWAITING)
            .values(status=STATUS_RUNNING, resumed_at=resumed_at)
        )
        if claimed.rowcount != 1:
            raise HTTPException(
                status_code=409, detail={"code": "decision_already_answered"}
            )
    answer = {
        "node_id": state.get("node_id"),
        "choice": choice,
        "text": text,
        "answered_by": user,
        "answered_at": datetime.now(timezone.utc).isoformat(),
    }
    data["config"] = json.loads(data.get("config") or "{}")
    return {"run": data, "state": state, "answer": answer, "resumed_at": resumed_at}


def mark_decided(run_id: str, resumed_at: str | None = None) -> bool:
    """La réponse est prise par le moteur : l'instantané ne sert plus.

    Sans instantané, ni ``release_paused_run`` ni la réconciliation ne
    remettent le run en attente : la suite a pu agir. ``resumed_at`` est
    celui de la prise : si la ligne a été remise en attente puis reprise
    ailleurs entre-temps, elle n'est plus la nôtre et rien n'est écrit.
    Renvoie si la décision est bien consignée pour cette reprise.
    """
    t = run_store.run_table
    where = [t.c.run_id == run_id, t.c.status == STATUS_RUNNING]
    if resumed_at is not None:
        where.append(t.c.resumed_at == resumed_at)
    with run_store.engine.begin() as conn:
        done = conn.execute(
            t.update().where(*where).values(paused_state=None, decision=None, assignee=None)
        )
    return done.rowcount == 1


def release_paused_run(run_id: str) -> None:
    """Remet en attente un run repris qui n'a pas atteint sa décision."""
    t = run_store.run_table
    with run_store.engine.begin() as conn:
        conn.execute(
            t.update()
            .where(
                t.c.run_id == run_id,
                t.c.status == STATUS_RUNNING,
                t.c.paused_state.isnot(None),
            )
            .values(status=STATUS_AWAITING, resumed_at=None)
        )


def abandon_paused_run(run_id: str, user: str) -> None:
    """Clôt un run en attente sans y répondre (``cancelled``).

    Réservé au propriétaire : l'assigné répond, il n'annule pas le run d'un
    autre.
    """
    t = run_store.run_table
    with run_store.engine.begin() as conn:
        _release_dead_resumptions(conn)
        _expire_due(conn)
    with run_store.engine.begin() as conn:
        data = _awaiting_row(conn, run_id, user)
        if data["owner_id"] != user:
            raise HTTPException(
                status_code=403, detail="Only the run's owner can abandon it"
            )
        done = conn.execute(
            t.update()
            .where(t.c.run_id == run_id, t.c.status == STATUS_AWAITING)
            .values(
                status=STATUS_CANCELLED,
                paused_state=None,
                decision=None,
                # L'assigné reste : il est prévenu du retrait, et sa réponse
                # tardive s'entend dire « déjà réglée » (409), pas « inconnue ».
                error_message=ABANDONED_MESSAGE,
                finished_at=_now(),
                end_notice=END_NOTICE_PENDING,
            )
        )
        if done.rowcount != 1:
            raise HTTPException(
                status_code=409, detail={"code": "decision_already_answered"}
            )


def stop_unanswered_resume(run_id: str, resumed_at: str | None) -> bool:
    """Clôt en ``cancelled`` une reprise arrêtée avant de prendre la réponse.

    L'arrêt est demandé par le propriétaire (``POST /{wid}/cancel``) : le
    run s'arrête vraiment, comme un abandon, au lieu de se remettre en
    attente derrière un panneau qui dit « annulé ». ``resumed_at`` est celui
    de la prise : une ligne reprise ailleurs entre-temps n'est pas touchée.
    Renvoie si la ligne a été close.
    """
    t = run_store.run_table
    where = [
        t.c.run_id == run_id,
        t.c.status == STATUS_RUNNING,
        t.c.paused_state.isnot(None),
    ]
    if resumed_at is not None:
        where.append(t.c.resumed_at == resumed_at)
    with run_store.engine.begin() as conn:
        done = conn.execute(
            t.update()
            .where(*where)
            .values(
                status=STATUS_CANCELLED,
                paused_state=None,
                decision=None,
                error_message=STOPPED_MESSAGE,
                finished_at=_now(),
                end_notice=END_NOTICE_PENDING,
            )
        )
    return done.rowcount == 1

