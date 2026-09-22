"""Graphe de workflow typé, compilé en ``google.adk.workflow.Workflow``.

Le canvas du builder était une liste ordonnée d'agents (roadmap#54) : aucune
branche dessinable, aucun outil sans agent autour. Ici le workflow est un
graphe persistable — nœuds typés, arêtes éventuellement routées — que l'on
compile en ``Workflow`` ADK pour l'exécution.

Types de nœuds :

* ``trigger`` — point d'entrée ; sa sortie est la charge utile du run ;
* ``agent`` — un agent existant (``agent_id``), entrée optionnelle en gabarit ;
* ``tool`` — un outil du tools_store (``tool``), arguments en gabarits ;
* ``router`` — règles déclaratives (champ, opérateur, valeur) -> une route ;
* ``classifier`` — un agent choisit la route parmi celles déclarées ;
* ``merge`` — attend toutes ses entrées, sortie indexée par nœud source ;
* ``loop`` — exécute un sous-graphe (``body``) pour chaque élément d'une liste
  (``foreach``) ou jusqu'à une condition (``until``), jamais plus de
  ``max_iterations`` fois (plafond obligatoire, au plus 100).

``approval`` est reconnu mais refusé à la validation : la validation humaine
arrive avec l'exécution durable.

Le déclencheur du corps d'une boucle reçoit ``{item, index, previous}`` —
``previous`` est la sortie de l'itération précédente, ou l'entrée de la
boucle au premier tour. Le corps ne lit que son propre déclencheur ; la
condition ``until`` lit ``{{iteration.output...}}`` et ``{{iteration.index}}``.
La sortie d'un ``foreach`` est la liste des sorties du corps, celle d'un
``until`` la sortie du dernier tour.

Aucun code utilisateur n'est exécuté : routes et gabarits sont évalués par ce
module. Un gabarit ``{{noeud.chemin.vers.valeur}}`` lit la sortie d'un nœud
amont ; seul, il garde le type de la valeur, inclus dans du texte il est
converti en texte.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
import time
from datetime import date, datetime, timezone
from typing import Any, AsyncGenerator, Awaitable, Callable, Literal, Optional

from google.adk.events import Event
from google.adk.workflow import Edge as AdkEdge
from google.adk.workflow import FunctionNode, JoinNode, Workflow
from google.genai import types
from pydantic import BaseModel, Field

from apowerb.core.workflow_engine import (
    _Finished,
    WorkflowCancelled,
    WorkflowUserError,
    _sse,
    error_fields,
    drive_workflow,
    leaf_message,
    try_parse_json,
)

NodeType = Literal[
    "trigger",
    "agent",
    "tool",
    "router",
    "classifier",
    "merge",
    "loop",
    "approval",
    "output",
    "convert",
    "set",
    "condition",
]
CONVERT_TARGETS = ("text", "json", "number", "boolean", "list", "csv", "date")
_TRUE = {"true", "yes", "oui", "1", "vrai"}
_FALSE = {"false", "no", "non", "0", "faux"}
_ROUTED = {"router", "classifier", "condition"}
_CSV_DELIMITERS = ",;\t"
_DATE_FORMATS = ("%d/%m/%Y",)
_NOT_YET = {"approval"}
_MAX_LOOP = 100
# Conversion csv bornée comme la réponse du nœud http (1 Mio) : le texte
# vient d'un agent, d'un outil ou d'un payload, donc d'une source non sûre.
MAX_CSV_BYTES = 1 * 1024 * 1024
MAX_CSV_ROWS = 10_000
_ITERATION = "iteration"
_TEMPLATE = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_-]*)((?:\.[A-Za-z0-9_-]+)*)\s*\}\}")
_ROOT = "workflow"

RunAgent = Callable[[str, str], Awaitable[Any]]
RunTool = Callable[[str, dict], Awaitable[Any]]


class UpstreamArgs(dict):
    """Entrée amont passée à un outil faute d arguments déclarés.

    Le runtime n en garde que les paramètres que l outil accepte : le
    payload d un trigger n a aucune raison de coïncider avec la signature
    de l outil. Des arguments déclarés restent un ``dict`` et passent tels
    quels, pour qu une faute de frappe de l utilisateur se voie.
    """


class GraphError(WorkflowUserError):
    """Le graphe est invalide ; rien n'a été exécuté."""


class Node(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    type: NodeType
    label: Optional[str] = None
    config: dict = Field(default_factory=dict)
    position: Optional[dict] = None


class GraphEdge(BaseModel):
    id: Optional[str] = None
    source: str
    target: str
    route: Optional[str] = None


class WorkflowGraph(BaseModel):
    version: Literal[1] = 1
    nodes: list[Node] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)


# --- Gabarits et règles -----------------------------------------------------


def _lookup(outputs: dict, node_id: str, path: str) -> Any:
    value = outputs.get(node_id)
    for key in [k for k in path.split(".") if k]:
        if isinstance(value, dict):
            value = value.get(key)
        elif isinstance(value, list) and key.isdigit() and int(key) < len(value):
            value = value[int(key)]
        else:
            return None
    return value


def render(template: Any, outputs: dict) -> Any:
    """Résout les gabarits ``{{noeud.chemin}}`` d'une valeur (récursivement)."""
    if isinstance(template, dict):
        return {k: render(v, outputs) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, outputs) for v in template]
    if not isinstance(template, str):
        return template
    whole = _TEMPLATE.fullmatch(template.strip())
    if whole:
        return _lookup(outputs, whole.group(1), whole.group(2))

    def _text(m: re.Match) -> str:
        v = _lookup(outputs, m.group(1), m.group(2))
        if v is None:
            return ""
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)

    return _TEMPLATE.sub(_text, template)


def _refs(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set().union(*(_refs(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_refs(v) for v in value)) if value else set()
    return (
        {m.group(1) for m in _TEMPLATE.finditer(value)}
        if isinstance(value, str)
        else set()
    )


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def evaluate_rule(rule: dict, value: Any) -> bool:
    op, expected = rule.get("op", "eq"), rule.get("value")
    if op == "exists":
        return value is not None
    if op == "eq":
        return value == expected
    if op == "ne":
        return value != expected
    if op in ("gt", "gte", "lt", "lte"):
        a, b = _num(value), _num(expected)
        if a is None or b is None:
            return False
        return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
    if op == "contains":
        return value is not None and str(expected).lower() in str(value).lower()
    if op == "in":
        return isinstance(expected, list) and value in expected
    raise GraphError(f"opérateur inconnu : {op}")


# --- Validation ---------------------------------------------------------------


def _declared_routes(node: Node) -> set[str]:
    cfg = node.config
    if node.type == "router":
        routes = {r.get("route") for r in cfg.get("rules") or []}
        if cfg.get("default_route"):
            routes.add(cfg["default_route"])
        return {r for r in routes if r}
    if node.type == "condition":
        return {"true", "false"}
    return {r.get("route") for r in cfg.get("routes") or [] if r.get("route")}


def _loop_body(node: Node) -> WorkflowGraph:
    cfg = node.config
    mode, cap = cfg.get("mode"), cfg.get("max_iterations")
    if mode not in ("foreach", "until"):
        raise GraphError(
            f"{node.id} : mode de boucle inconnu {mode!r} (foreach ou until)"
        )
    if isinstance(cap, bool) or not isinstance(cap, int) or not 1 <= cap <= _MAX_LOOP:
        raise GraphError(
            f"{node.id} : max_iterations doit être un entier de 1 à {_MAX_LOOP}"
        )
    if mode == "foreach" and not cfg.get("items"):
        raise GraphError(f"{node.id} : items manquant (la liste à parcourir)")
    if mode == "until" and not isinstance(cfg.get("until"), dict):
        raise GraphError(f"{node.id} : condition until manquante")
    if not isinstance(cfg.get("body"), dict):
        raise GraphError(f"{node.id} : body manquant (le sous-graphe à répéter)")
    try:
        body = WorkflowGraph.model_validate(cfg["body"])
        validate_graph(body)
    except GraphError as exc:
        raise GraphError(f"{node.id} (corps) : {exc}") from exc
    except ValueError as exc:
        raise GraphError(f"{node.id} (corps) : graphe mal formé") from exc
    if [n.type for n in body.nodes].count("trigger") != 1:
        raise GraphError(f"{node.id} (corps) : il faut exactement un déclencheur")
    return body


def _outer_refs(node: Node) -> set[str]:
    """Les nœuds du graphe englobant qu'une configuration référence."""
    if node.type != "loop":
        return _refs(node.config)
    return _refs(node.config.get("items")) | (
        _refs(node.config.get("until")) - {_ITERATION}
    )


def convert_value(value: Any, to: str) -> Any:
    """``value`` sous la forme ``to`` ; ``ValueError`` si elle ne s'y lit pas."""
    if to == "text":
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False)
    if to == "json":
        if not isinstance(value, str):
            return value
        parsed = try_parse_json(value)
        if isinstance(parsed, str):
            raise ValueError("not JSON")
        return parsed
    if to == "number":
        if isinstance(value, bool):
            raise ValueError("a boolean is not a number")
        if isinstance(value, (int, float)):
            return value
        number = float(str(value).strip().replace(",", "."))
        return int(number) if number.is_integer() else number
    if to == "boolean":
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        word = str(value).strip().lower()
        if word in _TRUE:
            return True
        if word in _FALSE:
            return False
        raise ValueError(f"{word!r} is not a boolean")
    if to == "list":
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            parsed = try_parse_json(value)
            if isinstance(parsed, list):
                return parsed
            return [line.strip() for line in value.splitlines() if line.strip()]
        return [value]
    if to == "csv":
        if isinstance(value, str):
            return _csv_to_rows(value)
        if isinstance(value, list) and all(isinstance(r, dict) for r in value):
            return _rows_to_csv(value)
        raise ValueError(f"cannot read csv from {type(value).__name__}")
    if to == "date":
        return _parse_date(value).isoformat()
    raise ValueError(f"unknown conversion {to!r}")


def _rows_to_csv(rows: list[dict]) -> str:
    """Liste de dicts -> texte CSV, en-tête = clés en ordre de 1re apparition."""
    if len(rows) > MAX_CSV_ROWS:
        raise ValueError(f"csv over {MAX_CSV_ROWS} rows")
    fieldnames: list[str] = []
    seen: set[str] = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                fieldnames.append(key)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    text = buf.getvalue()
    if len(text.encode("utf-8")) > MAX_CSV_BYTES:
        raise ValueError(f"csv over {MAX_CSV_BYTES} bytes")
    return text


def _csv_to_rows(text: str) -> list[dict]:
    """Texte CSV -> liste de dicts ; séparateur détecté, repli ``,``."""
    if len(text.encode("utf-8")) > MAX_CSV_BYTES:
        raise ValueError(f"csv over {MAX_CSV_BYTES} bytes")
    try:
        delimiter = (
            csv.Sniffer().sniff(text[:4096], delimiters=_CSV_DELIMITERS).delimiter
        )
    except csv.Error:
        delimiter = ","
    rows = []
    for row in csv.DictReader(io.StringIO(text), delimiter=delimiter):
        if len(rows) == MAX_CSV_ROWS:
            raise ValueError(f"csv over {MAX_CSV_ROWS} rows")
        rows.append(dict(row))
    return rows


def _parse_date(value: Any):
    """``value`` en ``date`` ou ``datetime`` (naïf, UTC) ; ``ValueError`` sinon."""
    if isinstance(value, bool):
        raise ValueError("a boolean is not a date")
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc).replace(
            tzinfo=None
        )
    if not isinstance(value, str):
        raise ValueError(f"cannot read a date from {type(value).__name__}")
    text = value.strip()
    if not text:
        raise ValueError("empty date")
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return date.fromisoformat(iso_text)
    except ValueError:
        pass
    try:
        parsed = datetime.fromisoformat(iso_text)
        return parsed.replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        pass
    raise ValueError(f"{value!r} is not a recognizable date")


def validate_graph(graph: WorkflowGraph) -> None:
    if not graph.nodes:
        raise GraphError("graphe vide")
    ids = [n.id for n in graph.nodes]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    if dupes:
        raise GraphError(f"identifiant de nœud dupliqué : {', '.join(dupes)}")
    by_id = {n.id: n for n in graph.nodes}
    for e in graph.edges:
        for end in (e.source, e.target):
            if end not in by_id:
                raise GraphError(f"arête vers un nœud inconnu : {end}")

    for n in graph.nodes:
        cfg = n.config
        if n.type in _NOT_YET:
            raise GraphError(f"{n.id} : le type {n.type} n'est pas encore exécutable")
        if n.type in ("agent", "classifier") and not cfg.get("agent_id"):
            raise GraphError(f"{n.id} : agent_id manquant")
        if n.type == "tool" and not cfg.get("tool"):
            raise GraphError(f"{n.id} : outil manquant")
        if n.type == "router" and not cfg.get("rules"):
            raise GraphError(f"{n.id} : aucune règle de routage")
        if n.type == "classifier" and len(_declared_routes(n)) < 2:
            raise GraphError(f"{n.id} : un classifieur demande au moins deux routes")
        if n.type == "loop":
            _loop_body(n)
        if n.type == "convert" and cfg.get("to") not in CONVERT_TARGETS:
            raise GraphError(
                f"{n.id} : conversion inconnue {cfg.get('to')!r} "
                f"(attendu : {', '.join(CONVERT_TARGETS)})"
            )
        if n.type == "set":
            fields = cfg.get("fields") or []
            if not fields:
                raise GraphError(f"{n.id} : aucun champ à définir")
            keys = [f.get("key") if isinstance(f, dict) else None for f in fields]
            for key in keys:
                if not isinstance(key, str) or not key.strip():
                    raise GraphError(f"{n.id} : clé de champ vide")
            dupes = sorted({k for k in keys if keys.count(k) > 1})
            if dupes:
                raise GraphError(
                    f"{n.id} : clé de champ dupliquée : {', '.join(dupes)}"
                )
        if n.type == "condition":
            if not cfg.get("rules"):
                raise GraphError(f"{n.id} : aucune règle de condition")
            match = cfg.get("match", "all")
            if match not in ("all", "any"):
                raise GraphError(f"{n.id} : match inconnu {match!r} (all ou any)")

    for e in graph.edges:
        src = by_id[e.source]
        if src.type == "output":
            raise GraphError(
                f"arête {e.source} -> {e.target} : un nœud output termine le flux"
            )
        if src.type in _ROUTED:
            if e.route not in _declared_routes(src):
                raise GraphError(
                    f"arête {e.source} -> {e.target} : route {e.route!r} non déclarée par {e.source}"
                )
        elif e.route is not None:
            raise GraphError(
                f"arête {e.source} -> {e.target} : route sur un nœud qui ne route pas"
            )

    for n in graph.nodes:
        if n.type == "condition":
            used = [e.route for e in graph.edges if e.source == n.id]
            dupes = sorted({r for r in used if r and used.count(r) > 1})
            if dupes:
                raise GraphError(
                    f"{n.id} : au plus une arête par route ({', '.join(dupes)})"
                )

    parents: dict[str, set[str]] = {i: set() for i in by_id}
    for e in graph.edges:
        parents[e.target].add(e.source)
    order: list[str] = []
    state: dict[str, int] = {}

    def _visit(i: str) -> None:
        if state.get(i) == 1:
            raise GraphError(f"cycle détecté autour de {i}")
        if state.get(i) == 2:
            return
        state[i] = 1
        for p in parents[i]:
            _visit(p)
        state[i] = 2
        order.append(i)

    for i in by_id:
        _visit(i)

    ancestors: dict[str, set[str]] = {}
    for i in order:
        ancestors[i] = (
            set().union(*[{p} | ancestors[p] for p in parents[i]])
            if parents[i]
            else set()
        )
    for n in graph.nodes:
        for ref in _outer_refs(n):
            if ref not in by_id:
                raise GraphError(f"{n.id} : référence inconnue {{{{{ref}}}}}")
            if ref not in ancestors[n.id]:
                raise GraphError(
                    f"{n.id} : {ref} n'est pas en amont, sa sortie n'existe pas encore"
                )


# --- Compilation ------------------------------------------------------------


def _adk_name(node_id: str) -> str:
    return "n_" + node_id.replace("-", "_")


def _classifier_prompt(routes: list[dict], node_input: Any) -> str:
    lines = [
        "Classify the input below into exactly one of these routes.",
        "Answer with the route name only, nothing else.",
        "",
    ]
    for r in routes:
        desc = r.get("description")
        lines.append(f"- {r['route']}" + (f": {desc}" if desc else ""))
    lines += ["", "<<< INPUT >>>", leaf_message(node_input), "<<< END INPUT >>>"]
    return "\n".join(lines)


def _pick_route(answer: Any, routes: list[str]) -> Optional[str]:
    text = re.sub(r"[^\w-]+", " ", str(answer)).strip().lower()
    for r in routes:
        if text == r.lower():
            return r
    hits = [r for r in routes if re.search(rf"\b{re.escape(r.lower())}\b", text)]
    return hits[0] if len(hits) == 1 else None


class _Compiler:
    def __init__(self, graph, payload, run_agent, run_tool, emit, cancel_event):
        self.g = graph
        self.payload = payload
        self.run_agent = run_agent
        self.run_tool = run_tool
        self.emit = emit
        self.cancel = cancel_event
        self.outputs: dict[str, Any] = {}

    def _wrap(self, node: Node, body):
        async def _fn(node_input: Any) -> Any:
            if self.cancel.is_set():
                raise WorkflowCancelled()
            if isinstance(node_input, types.Content):
                node_input = None
            self.emit({"event": "node_start", "node_id": node.id, "type": node.type})
            t0 = time.monotonic()
            try:
                result, route = await body(node_input)
            except Exception as exc:
                self.emit(
                    {
                        "event": "node_error",
                        "node_id": node.id,
                        **error_fields(exc),
                    }
                )
                raise
            self.outputs[node.id] = result
            self.emit(
                {
                    "event": "node_complete",
                    "node_id": node.id,
                    "output": result,
                    "duration_ms": round((time.monotonic() - t0) * 1000),
                }
            )
            if route is None:
                return result
            self.emit({"event": "route", "node_id": node.id, "route": route})
            return Event(output=result, route=route)

        return FunctionNode(func=_fn, name=_adk_name(node.id))

    def _body(self, node: Node):
        cfg = node.config

        if node.type == "trigger":

            async def body(_):
                return self.payload, None
        elif node.type == "agent":

            async def body(node_input):
                if cfg.get("input") is not None:
                    msg = render(cfg["input"], self.outputs)
                    msg = (
                        msg
                        if isinstance(msg, str)
                        else json.dumps(msg, ensure_ascii=False)
                    )
                else:
                    msg = leaf_message(node_input)
                return await self.run_agent(cfg["agent_id"], msg), None
        elif node.type == "tool":

            async def body(node_input):
                if cfg.get("args") is not None:
                    args = render(cfg["args"], self.outputs)
                else:
                    args = UpstreamArgs(
                        node_input if isinstance(node_input, dict) else {}
                    )
                return await self.run_tool(cfg["tool"], args), None
        elif node.type == "router":

            async def body(node_input):
                for rule in cfg["rules"]:
                    if evaluate_rule(rule, render(rule.get("field"), self.outputs)):
                        return node_input, rule["route"]
                if cfg.get("default_route"):
                    return node_input, cfg["default_route"]
                raise GraphError(
                    f"{node.id} : aucune règle ne correspond et pas de route par défaut",
                    code="no_route",
                    params={"node": node.id},
                )
        elif node.type == "classifier":

            async def body(node_input):
                routes = cfg["routes"]
                answer = await self.run_agent(
                    cfg["agent_id"], _classifier_prompt(routes, node_input)
                )
                route = _pick_route(answer, [r["route"] for r in routes])
                if route is None:
                    raise GraphError(
                        f"{node.id} : réponse du classifieur hors routes : {str(answer)[:80]!r}",
                        code="classifier_no_route",
                        params={
                            "node": node.id,
                            "routes": ", ".join(r["route"] for r in routes),
                        },
                    )
                return node_input, route
        elif node.type == "merge":

            async def body(node_input):
                # JoinNode indexe ses entrées par nom de nœud ADK : on revient
                # aux identifiants du graphe, seuls connus de l'utilisateur.
                sources = sorted(
                    {e.source for e in self.g.edges if e.target == node.id}
                )
                return {
                    src: (node_input or {}).get(_adk_name(src)) for src in sources
                }, None
        elif node.type == "output":

            async def body(node_input):
                # Valeur vide = non configurée : la sortie reprend son entrée.
                # Une sortie volontairement vide n'est donc pas exprimable.
                if cfg.get("value") not in (None, ""):
                    return render(cfg["value"], self.outputs), None
                return node_input, None
        elif node.type == "convert":

            async def body(node_input):
                value = (
                    render(cfg["input"], self.outputs)
                    if cfg.get("input") not in (None, "")
                    else node_input
                )
                try:
                    return convert_value(value, cfg["to"]), None
                except (ValueError, TypeError) as exc:
                    raise GraphError(
                        f"{node.id} : conversion en {cfg['to']} impossible ({exc})",
                        code="convert_failed",
                        params={"node": node.id, "to": cfg["to"]},
                    ) from None
        elif node.type == "set":

            async def body(node_input):
                return {
                    f["key"]: render(f.get("value"), self.outputs)
                    for f in cfg["fields"]
                }, None
        elif node.type == "condition":

            async def body(node_input):
                outcomes = [
                    evaluate_rule(rule, render(rule.get("field"), self.outputs))
                    for rule in cfg["rules"]
                ]
                matched = (
                    all(outcomes) if cfg.get("match", "all") == "all" else any(outcomes)
                )
                return node_input, "true" if matched else "false"
        elif node.type == "loop":
            body_graph = WorkflowGraph.model_validate(cfg["body"])

            async def body(node_input):
                return await self._loop(node, body_graph, node_input), None
        else:  # pragma: no cover - refusé par validate_graph
            raise GraphError(f"type non pris en charge : {node.type}")
        return body

    async def _loop(self, node: Node, body: WorkflowGraph, node_input: Any) -> Any:
        cfg = node.config
        cap = cfg["max_iterations"]
        if cfg["mode"] == "foreach":
            items = render(cfg["items"], self.outputs)
            if not isinstance(items, list):
                raise GraphError(
                    f"{node.id} : items n'est pas une liste ({type(items).__name__})",
                    code="loop_items_not_list",
                    params={"node": node.id, "type": type(items).__name__},
                )
            if len(items) > cap:
                self.emit(
                    {
                        "event": "loop_capped",
                        "node_id": node.id,
                        "max_iterations": cap,
                        "remaining": len(items) - cap,
                    }
                )
            results, previous = [], node_input
            for i, item in enumerate(items[:cap]):
                previous = await self._iteration(
                    node, body, i, {"item": item, "index": i, "previous": previous}
                )
                results.append(previous)
            return results
        previous = node_input
        for i in range(cap):
            previous = await self._iteration(
                node, body, i, {"item": None, "index": i, "previous": previous}
            )
            scope = {**self.outputs, _ITERATION: {"output": previous, "index": i}}
            if evaluate_rule(cfg["until"], render(cfg["until"].get("field"), scope)):
                return previous
        self.emit(
            {
                "event": "loop_capped",
                "node_id": node.id,
                "max_iterations": cap,
                "remaining": None,
            }
        )
        return previous

    async def _iteration(
        self, node: Node, body: WorkflowGraph, index: int, payload: dict
    ) -> Any:
        """Un tour de boucle : le corps est un workflow ADK à part entière."""
        if self.cancel.is_set():
            raise WorkflowCancelled()

        def emit(ev: dict) -> None:
            if "node_id" in ev:
                ev = {**ev, "node_id": f"{node.id}.{ev['node_id']}", "iteration": index}
            self.emit(ev)

        queue: asyncio.Queue = asyncio.Queue()
        inner = _Compiler(
            body, payload, self.run_agent, self.run_tool, queue.put_nowait, self.cancel
        )
        async for item in drive_workflow(inner.build(), queue, root_name=_ROOT):
            if isinstance(item, _Finished):
                if item.exc is not None:
                    raise item.exc
                break
            emit(item)
        return _final_output(body, inner.outputs)

    def build(self) -> Workflow:
        entry: dict[str, Any] = {}
        exit_: dict[str, Any] = {}
        for n in self.g.nodes:
            fn = self._wrap(n, self._body(n))
            if n.type == "merge":
                join = JoinNode(name=_adk_name(n.id) + "_in")
                entry[n.id], exit_[n.id] = join, fn
            else:
                entry[n.id] = exit_[n.id] = fn
        edges: list = []
        targets = {e.target for e in self.g.edges}
        for n in self.g.nodes:
            if n.id not in targets:
                edges.append(("START", entry[n.id]))
            if n.type == "merge":
                edges.append((entry[n.id], exit_[n.id]))
        for e in self.g.edges:
            if e.route is None:
                edges.append((exit_[e.source], entry[e.target]))
            else:
                edges.append(
                    AdkEdge(
                        from_node=exit_[e.source],
                        to_node=entry[e.target],
                        route=e.route,
                    )
                )
        return Workflow(name=_ROOT, edges=edges, max_concurrency=1)


def _final_output(graph: WorkflowGraph, outputs: dict) -> Any:
    # Un nœud output désigne la sortie ; à défaut, les feuilles atteintes.
    explicit = [n.id for n in graph.nodes if n.type == "output" and n.id in outputs]
    if len(explicit) == 1:
        return outputs[explicit[0]]
    if explicit:
        return {i: outputs[i] for i in explicit}
    sources = {e.source for e in graph.edges}
    leaves = [n.id for n in graph.nodes if n.id not in sources and n.id in outputs]
    if len(leaves) == 1:
        return outputs[leaves[0]]
    return {i: outputs[i] for i in leaves}


async def run_graph(
    graph: WorkflowGraph,
    *,
    payload: Any,
    run_agent: RunAgent,
    run_tool: RunTool,
    cancel_event: asyncio.Event,
) -> AsyncGenerator[str, None]:
    """Valide, compile et exécute un graphe ; produit ses événements SSE.

    Événements : ``node_start`` / ``node_complete`` (``output``,
    ``duration_ms``) / ``node_error`` / ``route``, puis un seul terminal parmi
    ``done`` (``output``), ``cancelled`` et ``error`` (``detail``).

    ``max_concurrency=1`` : les identifiants des agents et des outils passent
    encore par ``os.environ`` (``to_agent``, ``dict_to_envvar``) ; deux nœuds
    concurrents pourraient s'échanger leurs secrets. Le fan-out reste correct,
    il est seulement sérialisé.
    """
    try:
        validate_graph(graph)
    except GraphError as exc:
        yield _sse({"event": "error", "code": "invalid_graph", "detail": str(exc)})
        return
    queue: asyncio.Queue = asyncio.Queue()
    compiler = _Compiler(
        graph, payload, run_agent, run_tool, queue.put_nowait, cancel_event
    )
    workflow = compiler.build()
    exc: Optional[BaseException] = None
    async for item in drive_workflow(workflow, queue, root_name=_ROOT):
        if isinstance(item, _Finished):
            exc = item.exc
            break
        yield _sse(item)
    if cancel_event.is_set() or isinstance(exc, WorkflowCancelled):
        yield _sse({"event": "cancelled"})
    elif exc is not None:
        yield _sse({"event": "error", **error_fields(exc)})
    else:
        yield _sse({"event": "done", "output": _final_output(graph, compiler.outputs)})
