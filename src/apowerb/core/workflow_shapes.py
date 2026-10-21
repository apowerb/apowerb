"""Forme des sorties de nœuds, connue sans exécuter tout le workflow.

Studio proactif, étape 2 : dès que deux nœuds sont liés, l'éditeur propose
les champs de la sortie amont pour composer l'entrée du nœud aval. Une forme
décrit des clés et des types, JAMAIS des valeurs :

* ``{"type": "string" | "number" | "boolean" | "null" | "any"}`` ;
* ``{"type": "object", "fields": {clé: forme}}`` (``fields`` absent si
  inconnu, ``truncated: true`` si des clés ont été écartées) ;
* ``{"type": "array", "items": forme}`` (``items`` absent si inconnu).

Deux sources : la configuration du graphe (``node_shapes``, pure) et la
réponse des outils au dernier run de l'utilisateur (``record_tool_shape``),
car le catalogue des outils décrit leurs arguments, pas leur réponse.

Stockage des formes apprises : une ligne par (utilisateur, outil) dans
``workflow_tool_shapes``, à côté des workflows (même base, même création
idempotente au démarrage). La forme seule est écrite, plafonnée à
``MAX_SHAPE_BYTES`` : rien d'une valeur de réponse n'atteint la base.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable, Optional

from sqlalchemy.dialects import postgresql, sqlite

from apowerb.configs.th2logger import setup_logging
from apowerb.core.workflow_graph import (
    _TEMPLATE,
    WorkflowGraph,
    tool_reported_error,
)
from apowerb.core.workflow_main import workflow_store

logger = setup_logging(__name__)

MAX_DEPTH = 6
MAX_KEYS = 50
MAX_SHAPE_BYTES = 8_000
MAX_TOOL_REF = 200

_ANY = {"type": "any"}
_STRING = {"type": "string"}
_NUMBER = {"type": "number"}
_BOOLEAN = {"type": "boolean"}
_OBJECT = {"type": "object"}
_ARRAY = {"type": "array"}

# Sorties à clés fixes, telles que construites par le moteur
# (``workflow_graph._FIXED_OUTPUT_KEYS``), ici avec leurs types.
_FIXED_SHAPES = {
    "http": {"status": _NUMBER, "headers": _OBJECT, "body": _ANY},
    "rag": {"query": _STRING, "passages": _ARRAY},
    "notification": {"channel": _STRING, "sent": _NUMBER},
    "document": {"documents": _ARRAY, "text": _STRING, "count": _NUMBER},
    "db_write": {"table": _STRING, "inserted": _NUMBER},
}
_EXTRACT_TYPES = {
    "string": _STRING,
    "number": _NUMBER,
    "boolean": _BOOLEAN,
    "list": _ARRAY,
    "object": _OBJECT,
}
_CONVERT_TYPES = {
    "text": _STRING,
    "number": _NUMBER,
    "boolean": _BOOLEAN,
    "date": _STRING,
    "list": _ARRAY,
}
# Ces nœuds rendent leur entrée telle quelle (une route en plus).
_PASSTHROUGH = {"router", "condition", "classifier"}


def shape_of(value: Any, *, max_depth: int = MAX_DEPTH) -> dict:
    """La forme de ``value`` : ses clés et ses types, aucune valeur."""
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return dict(_BOOLEAN)
    if isinstance(value, (int, float)):
        return dict(_NUMBER)
    if isinstance(value, str):
        return dict(_STRING)
    if isinstance(value, dict):
        if max_depth <= 0:
            return dict(_OBJECT)
        keys = [k for k in value if isinstance(k, str)]
        shape: dict = {
            "type": "object",
            "fields": {
                k: shape_of(value[k], max_depth=max_depth - 1) for k in keys[:MAX_KEYS]
            },
        }
        if len(keys) > MAX_KEYS:
            shape["truncated"] = True
        return shape
    if isinstance(value, (list, tuple)):
        if max_depth <= 0 or not value:
            return dict(_ARRAY)
        return {"type": "array", "items": shape_of(value[0], max_depth=max_depth - 1)}
    return dict(_ANY)


def capped_shape(value: Any) -> dict:
    """``shape_of`` réduite en profondeur jusqu'à tenir dans ``MAX_SHAPE_BYTES``."""
    for depth in range(MAX_DEPTH, -1, -1):
        shape = shape_of(value, max_depth=depth)
        if len(json.dumps(shape)) <= MAX_SHAPE_BYTES:
            return shape
    return dict(_ANY)  # pragma: no cover - profondeur 0 tient toujours


def _at_path(shape: Optional[dict], path: str) -> dict:
    for part in (p for p in path.split(".") if p):
        if not shape or shape.get("type") != "object" or part not in shape.get("fields", {}):
            return dict(_ANY)
        shape = shape["fields"][part]
    return shape or dict(_ANY)


def _template_shape(value: Any, known: dict[str, Optional[dict]]) -> dict:
    """Forme d'une valeur de ``set`` : un gabarit seul rend la valeur visée,
    un texte qui en contient reste un texte."""
    if isinstance(value, str):
        whole = _TEMPLATE.fullmatch(value.strip())
        if whole:
            return _at_path(known.get(whole.group(1)), whole.group(2))
        return dict(_STRING)
    return shape_of(value)


def _static_shape(node, parents: list[str], known: dict) -> Optional[dict]:
    cfg = node.config or {}
    t = node.type
    if t == "trigger":
        sample = cfg.get("sample_payload")
        return capped_shape(sample) if sample is not None else None
    if t == "set":
        fields = [f for f in cfg.get("fields") or [] if isinstance(f, dict) and f.get("key")]
        return {
            "type": "object",
            "fields": {f["key"]: _template_shape(f.get("value"), known) for f in fields},
        }
    if t == "extract":
        fields = [f for f in cfg.get("fields") or [] if isinstance(f, dict) and f.get("name")]
        return {
            "type": "object",
            "fields": {f["name"]: dict(_EXTRACT_TYPES.get(f.get("type"), _ANY)) for f in fields},
        }
    if t == "merge":
        return {
            "type": "object",
            "fields": {p: known.get(p) or dict(_ANY) for p in sorted(parents)},
        }
    if t == "convert":
        return dict(_CONVERT_TYPES[cfg["to"]]) if cfg.get("to") in _CONVERT_TYPES else None
    if t in _FIXED_SHAPES:
        return {"type": "object", "fields": {k: dict(v) for k, v in _FIXED_SHAPES[t].items()}}
    if t == "agent" and cfg.get("output_type") == "text":
        return dict(_STRING)
    if t == "loop":
        return dict(_ARRAY)
    passthrough = t in _PASSTHROUGH or (t == "output" and cfg.get("value") in (None, ""))
    if passthrough and len(parents) == 1:
        return known.get(parents[0])
    return None


def node_shapes(
    graph: WorkflowGraph, *, tool_shapes: Optional[dict[str, dict]] = None
) -> dict[str, dict]:
    """``{node_id: {"source": "static" | "learned", "shape": forme}}``.

    Seuls les nœuds de premier niveau dont la forme est connue y figurent ;
    un ``tool`` reprend la forme apprise de sa réponse (``tool_shapes``),
    un ``agent`` sans ``output_type: text`` reste inconnu.
    """
    tool_shapes = tool_shapes or {}
    parents: dict[str, list[str]] = {n.id: [] for n in graph.nodes}
    for e in graph.edges:
        if e.target in parents and e.source not in parents[e.target]:
            parents[e.target].append(e.source)
    by_id = {n.id: n for n in graph.nodes}
    known: dict[str, Optional[dict]] = {}
    out: dict[str, dict] = {}
    visiting: set[str] = set()

    def _visit(node_id: str) -> Optional[dict]:
        if node_id in known:
            return known[node_id]
        if node_id in visiting:  # cycle : refusé ailleurs, ici simplement inconnu
            return None
        visiting.add(node_id)
        for p in parents.get(node_id, []):
            if p in by_id:
                _visit(p)
        node = by_id[node_id]
        source, shape = "static", None
        if node.type == "tool":
            shape = tool_shapes.get((node.config or {}).get("tool"))
            source = "learned"
        else:
            shape = _static_shape(node, parents.get(node_id, []), known)
        known[node_id] = shape
        if shape is not None:
            out[node_id] = {"source": source, "shape": shape}
        visiting.discard(node_id)
        return shape

    for n in graph.nodes:
        _visit(n.id)
    return out


# --- Formes apprises des outils --------------------------------------------------


def _insert(dialect: str):
    return postgresql.insert if dialect == "postgresql" else sqlite.insert


def record_tool_shape(owner_id: str, tool_ref: str, result: Any) -> None:
    """Retient la forme de la réponse de ``tool_ref`` pour ``owner_id``.

    Best-effort : un échec d'écriture est consigné, jamais propagé au run.
    Une réponse d'erreur (``tool_reported_error``) n'est pas une forme.
    """
    if not tool_ref or len(tool_ref) > MAX_TOOL_REF or tool_reported_error(result):
        return
    row = {
        "owner_id": owner_id,
        "tool_ref": tool_ref,
        "shape": json.dumps(capped_shape(result)),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    t = workflow_store.tool_shape_table
    try:
        with workflow_store.engine.begin() as conn:
            stmt = _insert(conn.dialect.name)(t).values(row)
            conn.execute(
                stmt.on_conflict_do_update(
                    index_elements=[t.c.owner_id, t.c.tool_ref],
                    set_={"shape": stmt.excluded.shape, "updated_at": stmt.excluded.updated_at},
                )
            )
    except Exception:  # noqa: BLE001 - une forme ne doit jamais casser un run
        logger.exception("[workflows] forme de l'outil %s non retenue", tool_ref)


def read_tool_shapes(owner_id: str, tool_refs: Iterable[str]) -> dict[str, dict]:
    """Les formes apprises de ``tool_refs`` pour ``owner_id`` (et lui seul)."""
    refs = sorted({r for r in tool_refs if isinstance(r, str) and r})
    if not refs:
        return {}
    t = workflow_store.tool_shape_table
    query = t.select().where(t.c.owner_id == owner_id, t.c.tool_ref.in_(refs))
    with workflow_store.engine.begin() as conn:
        return {r.tool_ref: json.loads(r.shape) for r in conn.execute(query)}


RunTool = Callable[[str, dict], Awaitable[Any]]


def learning_run_tool(owner_id: str, run_tool: RunTool) -> RunTool:
    """``run_tool`` qui retient la forme de la première réponse de chaque
    outil du run (une boucle ne réécrit pas cent fois la même ligne).

    L'écriture, bloquante, passe dans un thread comme les services des nœuds.
    """
    seen: set[str] = set()

    async def _run(tool_ref: str, args: dict) -> Any:
        result = await run_tool(tool_ref, args)
        if tool_ref not in seen and not tool_reported_error(result):
            seen.add(tool_ref)
            await asyncio.to_thread(record_tool_shape, owner_id, tool_ref, result)
        return result

    return _run
