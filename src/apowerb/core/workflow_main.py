"""Service des workflows persistés : CRUD, verrou optimiste, historique.

Isolation : tout accès est filtré par ``owner_id``, comme ``get_agent``. Un
workflow d'autrui est « introuvable », jamais « interdit » — on ne confirme pas
son existence.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from nanoid import generate as nanoid_generate
from pydantic import ValidationError

from apowerb.agent_store.workflow_store import WorkflowStore
from apowerb.core.workflow_graph import GraphError, WorkflowGraph, validate_graph
from apowerb.helpers.emails import get_domain_from_email

workflow_store = WorkflowStore()

STATUSES = ("draft", "published")
_LIST_FIELDS = (
    "workflow_id",
    "name",
    "description",
    "status",
    "version",
    "created_at",
    "updated_at",
)


class WorkflowNotFound(LookupError):
    pass


class InvalidWorkflow(ValueError):
    pass


class VersionConflict(RuntimeError):
    def __init__(self, current_version: int):
        super().__init__(f"version courante : {current_version}")
        self.current_version = current_version


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def parse_graph(graph: Any) -> WorkflowGraph:
    """Structure du graphe (types, identifiants) ; lève ``InvalidWorkflow``."""
    try:
        return WorkflowGraph.model_validate(graph)
    except ValidationError as exc:
        raise InvalidWorkflow(str(exc)) from exc


def check_workflow(
    graph: Any, *, workflow_id: Optional[str] = None, owner_id: Optional[str] = None
) -> dict:
    """Validation complète, pour l'éditeur : jamais d'exception.

    ``workflow_id`` — l'identifiant du workflow validé, quand il est déjà
    enregistré : un ``subworkflow`` qui se cible lui-même est alors refusé
    (voir ``workflow_graph.validate_graph``), de même qu'un trigger
    ``workflow_done`` qui s'écouterait lui-même.
    ``owner_id``, quand connu, active les deux contrôles T2 qui ont besoin de
    la base (unicité de ``agent_tool.tool_name``, appartenance du
    ``workflow_done.workflow_id`` source).
    """
    try:
        validate_graph(parse_graph(graph), workflow_id=workflow_id, owner_id=owner_id)
    except (InvalidWorkflow, GraphError) as exc:
        report = {"valid": False, "errors": [str(exc)]}
        # Un code rend l'erreur traduisible par l'interface ; le texte reste
        # le repli pour les erreurs qui n'en ont pas encore.
        if getattr(exc, "code", None):
            report["codes"] = [{"code": exc.code, "params": exc.params}]
        return report
    return {"valid": True, "errors": []}


def _sync_trigger(workflow_id: str, owner_id: str, graph: dict, status: str) -> None:
    """Fait suivre l'état du trigger (kind du nœud ``trigger``) au store dédié.

    Import différé : ``workflow_triggers`` a besoin de ``workflow_main`` pour
    lancer un run déclenché (``launch_triggered_run``), donc l'import au
    niveau module créerait un cycle. Cet appel n'est PAS enveloppé dans un
    ``try/except`` : une écriture de workflow qui réussit mais dont le
    trigger ne se synchronise pas doit être visible, pas avalée en silence.
    """
    from apowerb.core import workflow_triggers

    workflow_triggers.sync_trigger_for_workflow(
        workflow_id=workflow_id, owner_id=owner_id, graph=graph, status=status
    )


def _dump(graph: WorkflowGraph) -> str:
    return json.dumps(graph.model_dump(exclude_none=True), ensure_ascii=False)


def _row(row, with_graph: bool = True) -> dict:
    d = dict(row._mapping)
    graph = json.loads(d.pop("graph"))
    if with_graph:
        d["graph"] = graph
    else:
        d = {k: d[k] for k in _LIST_FIELDS}
        d["node_count"] = len(graph.get("nodes") or [])
    return d


def _fetch(conn, workflow_id: str, owner_id: str):
    t = workflow_store.workflow_table
    return conn.execute(
        t.select().where(t.c.workflow_id == workflow_id, t.c.owner_id == owner_id)
    ).fetchone()


def create_workflow(
    *, owner_id: str, name: str, graph: Any, description: Optional[str] = None
) -> dict:
    parsed = parse_graph(graph)
    if not (name or "").strip():
        raise InvalidWorkflow("nom manquant")
    now = _now()
    values = dict(
        workflow_id=nanoid_generate(size=16),
        name=name.strip(),
        description=description,
        owner_id=owner_id,
        organization_id=get_domain_from_email(owner_id),
        graph=_dump(parsed),
        status="draft",
        version=1,
        created_at=now,
        updated_at=now,
    )
    with workflow_store.engine.begin() as conn:
        conn.execute(workflow_store.workflow_table.insert().values(**values))
    # Provisionne le trigger dès la création (jeton webhook inclus) : un
    # brouillon reste inactif (``status="draft"``), mais l'utilisateur peut
    # copier son URL avant même de publier.
    _sync_trigger(
        values["workflow_id"], owner_id, parsed.model_dump(exclude_none=True), "draft"
    )
    return get_workflow(values["workflow_id"], owner_id=owner_id)


def get_workflow(workflow_id: str, *, owner_id: str) -> Optional[dict]:
    with workflow_store.engine.begin() as conn:
        row = _fetch(conn, workflow_id, owner_id)
    return _row(row) if row else None


def get_workflow_graph_at(
    workflow_id: str, version: int, *, owner_id: str
) -> dict | None:
    """Le graphe du workflow tel qu'il était à ``version``, ou ``None``.

    La version courante si elle correspond, sinon l'état archivé à cette
    version. C'est ce qu'un rejeu doit exécuter : le graphe que le run
    d'origine a exécuté, pas celui qui a pu être édité depuis.
    """
    r = workflow_store.revision_table
    with workflow_store.engine.begin() as conn:
        row = _fetch(conn, workflow_id, owner_id)
        if row is None:
            return None
        if row._mapping["version"] == version:
            return json.loads(row._mapping["graph"])
        rev = conn.execute(
            r.select()
            .where(
                r.c.workflow_id == workflow_id,
                r.c.owner_id == owner_id,
                r.c.version == version,
            )
            .order_by(r.c.revision_id.desc())
        ).first()
    return json.loads(rev._mapping["graph"]) if rev is not None else None


def list_workflows(*, owner_id: str) -> list[dict]:
    t = workflow_store.workflow_table
    with workflow_store.engine.begin() as conn:
        rows = conn.execute(
            t.select().where(t.c.owner_id == owner_id).order_by(t.c.updated_at.desc())
        ).fetchall()
    return [_row(r, with_graph=False) for r in rows]


def _archive(conn, row, reason: str) -> None:
    d = dict(row._mapping)
    conn.execute(
        workflow_store.revision_table.insert().values(
            workflow_id=d["workflow_id"],
            version=d["version"],
            name=d["name"],
            description=d["description"],
            graph=d["graph"],
            status=d["status"],
            owner_id=d["owner_id"],
            saved_at=_now(),
            reason=reason,
        )
    )


def _update_reason(previous_status: str, changes: dict) -> str:
    """Why the archived state was replaced: shown as is in the history."""
    status = changes.get("status")
    if status == "published" and previous_status != "published":
        return "publish"
    if status is not None and status != "published" and previous_status == "published":
        return "unpublish"
    return "edit"


# Rows archived by 0.2.29 carry this reason; they were all edits.
_LEGACY_REASONS = {"update": "edit"}


def update_workflow(
    workflow_id: str,
    *,
    owner_id: str,
    expected_version: int,
    name: Optional[str] = None,
    description: Optional[str] = None,
    graph: Any = None,
    status: Optional[str] = None,
) -> dict:
    with workflow_store.engine.begin() as conn:
        row = _fetch(conn, workflow_id, owner_id)
        if row is None:
            raise WorkflowNotFound(workflow_id)
        current = row._mapping
        if current["version"] != expected_version:
            raise VersionConflict(current["version"])
        changes: dict[str, Any] = {}
        if name is not None:
            if not name.strip():
                raise InvalidWorkflow("nom manquant")
            changes["name"] = name.strip()
        if description is not None:
            changes["description"] = description
        if graph is not None:
            changes["graph"] = _dump(parse_graph(graph))
        if status is not None:
            if status not in STATUSES:
                raise InvalidWorkflow(f"statut inconnu : {status}")
            changes["status"] = status
        # Valider dès que l'état FINAL est publié et que cet appel publie ou
        # change le graphe : sinon un PUT du graphe seul sur un workflow déjà
        # publié réarmerait son trigger sans aucun contrôle (cron sous le
        # plancher, config invalide...).
        final_status = changes.get("status", current["status"])
        if final_status == "published" and ("graph" in changes or status is not None):
            report = check_workflow(
                json.loads(changes.get("graph", current["graph"])),
                workflow_id=workflow_id,
                owner_id=owner_id,
            )
            if not report["valid"]:
                prefix = (
                    "publication refusée : "
                    if status == "published"
                    else "modification refusée (workflow publié) : "
                )
                raise InvalidWorkflow(prefix + report["errors"][0])
        if changes:
            _archive(conn, row, _update_reason(current["status"], changes))
            _write_if_unchanged(conn, workflow_id, owner_id, expected_version, changes)
    if changes:
        # Synchronisé sur l'état FINAL (graphe et statut retenus, modifiés ou
        # non par cet appel) : la publication comme la dépublication passent
        # ici, ainsi qu'une simple édition de graphe qui change le kind.
        _sync_trigger(
            workflow_id,
            owner_id,
            json.loads(changes.get("graph", current["graph"])),
            changes.get("status", current["status"]),
        )
    return get_workflow(workflow_id, owner_id=owner_id)


def _write_if_unchanged(
    conn, workflow_id: str, owner_id: str, version: int, changes: dict
) -> None:
    """Écrit seulement si la version en base est toujours ``version``.

    Le contrôle sur la lecture ne suffit pas : un écrivain concurrent peut
    passer entre la lecture et l'écriture. L'``UPDATE`` porte donc la
    condition lui-même ; zéro ligne touchée = conflit (la transaction, et
    l'archive qu'elle contenait, sont annulées).
    """
    t = workflow_store.workflow_table
    result = conn.execute(
        t.update()
        .where(
            t.c.workflow_id == workflow_id,
            t.c.owner_id == owner_id,
            t.c.version == version,
        )
        .values(**changes, version=version + 1, updated_at=_now())
    )
    if result.rowcount != 1:
        now = _fetch(conn, workflow_id, owner_id)
        raise VersionConflict(now._mapping["version"] if now is not None else version)


def delete_workflow(workflow_id: str, *, owner_id: str) -> None:
    t, r = workflow_store.workflow_table, workflow_store.revision_table
    with workflow_store.engine.begin() as conn:
        if _fetch(conn, workflow_id, owner_id) is None:
            raise WorkflowNotFound(workflow_id)
        conn.execute(
            r.delete().where(r.c.workflow_id == workflow_id, r.c.owner_id == owner_id)
        )
        conn.execute(
            t.delete().where(t.c.workflow_id == workflow_id, t.c.owner_id == owner_id)
        )
    from apowerb.core import workflow_triggers

    workflow_triggers.remove_trigger_for_workflow(workflow_id)


def list_revisions(workflow_id: str, *, owner_id: str) -> list[dict]:
    r = workflow_store.revision_table
    with workflow_store.engine.begin() as conn:
        if _fetch(conn, workflow_id, owner_id) is None:
            raise WorkflowNotFound(workflow_id)
        rows = conn.execute(
            r.select()
            .where(r.c.workflow_id == workflow_id, r.c.owner_id == owner_id)
            .order_by(r.c.revision_id.desc())
        ).fetchall()
    out = []
    for x in rows:
        d = {k: v for k, v in dict(x._mapping).items() if k != "graph"}
        d["reason"] = _LEGACY_REASONS.get(d.get("reason"), d.get("reason"))
        out.append(d)
    return out


def restore_revision(workflow_id: str, revision_id: int, *, owner_id: str) -> dict:
    """Restaure une révision ; l'état remplacé est archivé, rien n'est perdu."""
    r = workflow_store.revision_table
    with workflow_store.engine.begin() as conn:
        row = _fetch(conn, workflow_id, owner_id)
        if row is None:
            raise WorkflowNotFound(workflow_id)
        rev = conn.execute(
            r.select().where(
                r.c.revision_id == revision_id,
                r.c.workflow_id == workflow_id,
                r.c.owner_id == owner_id,
            )
        ).fetchone()
        if rev is None:
            raise WorkflowNotFound(f"révision {revision_id}")
        _archive(conn, row, "restore")
        rv = rev._mapping
        _write_if_unchanged(
            conn,
            workflow_id,
            owner_id,
            row._mapping["version"],
            dict(
                name=rv["name"],
                description=rv["description"],
                graph=rv["graph"],
                status="draft",
            ),
        )
    # Une restauration repasse toujours en "draft" (voir ci-dessus) : le
    # trigger, s'il était actif, se désarme comme à une dépublication.
    _sync_trigger(workflow_id, owner_id, json.loads(rv["graph"]), "draft")
    return get_workflow(workflow_id, owner_id=owner_id)


def duplicate_workflow(workflow_id: str, *, owner_id: str) -> dict:
    src = get_workflow(workflow_id, owner_id=owner_id)
    if src is None:
        raise WorkflowNotFound(workflow_id)
    return create_workflow(
        owner_id=owner_id,
        name=f"{src['name']} (copie)",
        graph=src["graph"],
        description=src["description"],
    )
