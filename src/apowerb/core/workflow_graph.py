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
  ``max_iterations`` fois (plafond obligatoire, au plus 100) ;
* ``extract`` — un agent lit un objet JSON typé (``fields``) hors de son
  entrée, validé champ par champ ;
* ``rag`` — interroge les bases de connaissances rattachées à un agent
  (``run_rag``), sortie normalisée en passages.
* ``try`` — exécute un sous-graphe (``body``) comme le corps d'un ``loop`` ;
  succès -> route ``ok`` (sortie du corps), échec après ``retries`` tentatives
  -> route ``error`` (sortie lisible ``code``/``detail``/``params``/``node``,
  jamais l'exception brute) ; son corps ne peut pas contenir de ``try`` ni de
  ``loop`` imbriqué ;
* ``subworkflow`` — exécute un autre workflow enregistré (``workflow_id``) par
  un callback résolu côté routeur (mêmes droits que pour le lancer), profondeur
  3 maximum, cycle détecté à l'exécution via la pile des ``workflow_id``.

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

Un chemin n'est autorisé statiquement que vers un champ garanti par le
moteur (les clés d'un ``merge``, ``iteration.output``/``iteration.index``
dans un ``until``). Un nœud à sortie simple connue d'avance (``convert``
vers texte, nombre ou booléen) refuse tout chemin, et ``router``/
``classifier`` refusent ``.route`` : aucun des deux n'est jamais stocké.
``agent`` renvoie du texte ou du JSON relu (voir ``run_agent_message``) —
sa forme n'est connue qu'à l'exécution : un chemin y est donc accepté à la
validation, mais échoue à l'exécution (``template_ref_invalid``) s'il
traverse une valeur scalaire. Seule la référence nue ``{{id}}`` est
toujours fiable pour lire la sortie entière d'un nœud.
"""

from __future__ import annotations

import asyncio
import csv
import io
import ipaddress
import json
import re
import socket
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
    "try",
    "subworkflow",
    "approval",
    "output",
    "convert",
    "set",
    "condition",
    "extract",
    "rag",
    "http",
    "notification",
]
CONVERT_TARGETS = ("text", "json", "number", "boolean", "list", "csv", "date")
EXTRACT_FIELD_TYPES = ("string", "number", "boolean", "list", "object")
_TRUE = {"true", "yes", "oui", "1", "vrai"}
_FALSE = {"false", "no", "non", "0", "faux"}
_ROUTED = {"router", "classifier", "condition", "try"}
_CSV_DELIMITERS = ",;\t"
_DATE_FORMATS = ("%d/%m/%Y",)
_NOT_YET = {"approval"}
_MAX_LOOP = 100
_MAX_RETRIES = 3
_MAX_RETRY_DELAY_MS = 5000
_MAX_SUBWORKFLOW_DEPTH = 3
# Conversion csv bornée comme la réponse du nœud http (1 Mio) : le texte
# vient d'un agent, d'un outil ou d'un payload, donc d'une source non sûre.
MAX_CSV_BYTES = 1 * 1024 * 1024
MAX_CSV_ROWS = 10_000
_MAX_EXTRACT_FIELDS = 30
_MIN_RAG_TOP_K, _MAX_RAG_TOP_K = 1, 20
_DEFAULT_RAG_TOP_K = 5
_ITERATION = "iteration"
_ATTEMPT = "attempt"
_TEMPLATE = re.compile(r"\{\{\s*([A-Za-z][A-Za-z0-9_-]*)((?:\.[A-Za-z0-9_-]+)*)\s*\}\}")
_FIELD_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_ROOT = "workflow"

# --- Nœud http ---------------------------------------------------------------
# En-têtes qui ne doivent jamais être écrits en clair dans un graphe : c'est
# exactement ce qu'on demande de faire passer par ``auth.integration_id`` (pas
# encore disponible, voir validate_graph) plutôt que par un secret recopié
# dans la configuration, visible de quiconque lit ou exporte le workflow.
_FORBIDDEN_HTTP_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "x-api-key",
}
_HTTP_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
MIN_HTTP_TIMEOUT, MAX_HTTP_TIMEOUT, DEFAULT_HTTP_TIMEOUT = 1, 30, 15
MAX_HTTP_RESPONSE_BYTES = 1 * 1024 * 1024
MAX_HTTP_REDIRECTS = 5

# --- Nœud notification --------------------------------------------------------
_NOTIF_CHANNELS = {"email", "app", "teams"}
MAX_NOTIFICATION_RECIPIENTS = 10

RunAgent = Callable[[str, str], Awaitable[Any]]
RunTool = Callable[[str, dict], Awaitable[Any]]
# (node_id, channel, destinataires rendus, sujet rendu, corps rendu) -> nombre
# d'envois effectués. Construit dans workflow_runtime.bindings_for, comme
# run_tool : les notifications "app" et "teams" doivent connaître le
# propriétaire du run pour savoir QUI notifier (app) ou quel webhook viser
# (teams, résolu depuis son intégration chiffrée), ce que ce module ignore
# volontairement.
RunNotify = Callable[[str, str, list, str, str], Awaitable[int]]
RunRag = Callable[[str, str, int], Awaitable[dict]]
# Résolu côté routeur (mêmes droits que pour lancer ce workflow directement) ;
# ``None`` si l'appelant ne trouve pas — ou ne peut pas voir — ce workflow_id.
ResolveWorkflow = Callable[[str], Awaitable[Optional["WorkflowGraph"]]]


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


def _lookup(outputs: dict, ref: str, path: str, node_id: Optional[str] = None) -> Any:
    if ref not in outputs:
        return None
    value = outputs[ref]
    for key in [k for k in path.split(".") if k]:
        if isinstance(value, dict):
            value = value.get(key)
        elif isinstance(value, list) and key.isdigit() and int(key) < len(value):
            value = value[int(key)]
        elif isinstance(value, (str, int, float, bool)):
            full = f"{ref}{path}"
            raise GraphError(
                f"{node_id} : {{{{{full}}}}} invalide à l'exécution — la "
                f"sortie de {ref} est une valeur simple, utilisez {{{{{ref}}}}}",
                code="template_ref_invalid",
                params={"node": node_id or "", "ref": full},
            )
        else:
            return None
    return value


def render(template: Any, outputs: dict, node_id: Optional[str] = None) -> Any:
    """Résout les gabarits ``{{noeud.chemin}}`` d'une valeur (récursivement).

    ``node_id`` est le nœud dont la configuration est rendue : il n'accuse
    personne d'autre si le chemin traverse un scalaire (``template_ref_invalid``,
    voir ``_lookup``).
    """
    if isinstance(template, dict):
        return {k: render(v, outputs, node_id) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, outputs, node_id) for v in template]
    if not isinstance(template, str):
        return template
    whole = _TEMPLATE.fullmatch(template.strip())
    if whole:
        return _lookup(outputs, whole.group(1), whole.group(2), node_id)

    def _text(m: re.Match) -> str:
        v = _lookup(outputs, m.group(1), m.group(2), node_id)
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


def _ref_paths(value: Any) -> set[tuple[str, str]]:
    """Comme ``_refs``, mais garde le chemin : ``{{id.a.b}}`` -> ``(id, '.a.b')``."""
    if isinstance(value, dict):
        return set().union(*(_ref_paths(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_ref_paths(v) for v in value)) if value else set()
    if not isinstance(value, str):
        return set()
    return {(m.group(1), m.group(2)) for m in _TEMPLATE.finditer(value)}


def _num(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rule_subject(rule: dict, scope: dict, node_id: str, default: Any) -> Any:
    """La valeur qu'une règle teste : son ``field`` rendu, ou ``default``
    (l'entrée du routeur, la sortie de l'itération) quand il est vide.

    Rendre un champ vide donnait ``""`` : la règle comparait une chaîne vide
    et la route par défaut était prise à chaque run, sans aucune erreur.
    """
    field = rule.get("field")
    if field is None or (isinstance(field, str) and not field.strip()):
        return default
    return render(field, scope, node_id)


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
    if node.type == "try":
        return {"ok", "error"}
    if node.type == "condition":
        return {"true", "false"}
    return {r.get("route") for r in cfg.get("routes") or [] if r.get("route")}


def _body_graph(
    node: Node, key: str = "body", *, workflow_id: Optional[str] = None
) -> WorkflowGraph:
    """Parse et valide le sous-graphe d'un corps (``loop`` ou ``try``).

    Structure valide, règles internes cohérentes, exactement un déclencheur —
    partagé par les deux nœuds qui exécutent un corps isolé. ``workflow_id``
    est transmis pour qu'un ``subworkflow`` niché dans le corps refuse aussi
    de s'appeler lui-même.
    """
    if not isinstance(node.config.get(key), dict):
        raise GraphError(f"{node.id} : {key} manquant (le sous-graphe à exécuter)")
    try:
        body = WorkflowGraph.model_validate(node.config[key])
        validate_graph(body, workflow_id=workflow_id)
    except GraphError as exc:
        raise GraphError(f"{node.id} (corps) : {exc}") from exc
    except ValueError as exc:
        raise GraphError(f"{node.id} (corps) : graphe mal formé") from exc
    if [n.type for n in body.nodes].count("trigger") != 1:
        raise GraphError(f"{node.id} (corps) : il faut exactement un déclencheur")
    return body


def _loop_body(node: Node, *, workflow_id: Optional[str] = None) -> WorkflowGraph:
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
    return _body_graph(node, workflow_id=workflow_id)


def _bounded_int(
    cfg: dict, key: str, node_id: str, lo: int, hi: int, default: int = 0
) -> int:
    value = cfg.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not lo <= value <= hi:
        raise GraphError(f"{node_id} : {key} doit être un entier de {lo} à {hi}")
    return value


def _try_config(node: Node, *, workflow_id: Optional[str] = None) -> WorkflowGraph:
    """Bornes de ``retries``/``retry_delay_ms``, puis corps sans try/loop imbriqué.

    Même restriction que le ``loop`` actuel pour son propre corps : aucune —
    ``_loop_body`` ne l'interdit pas (vérifié, aucun contrôle de type dans le
    corps). Elle est donc ajoutée ici spécifiquement pour ``try``, comme
    demandé, sans toucher au comportement existant de ``loop``.
    """
    _bounded_int(node.config, "retries", node.id, 0, _MAX_RETRIES)
    _bounded_int(node.config, "retry_delay_ms", node.id, 0, _MAX_RETRY_DELAY_MS)
    body = _body_graph(node, workflow_id=workflow_id)
    if any(n.type in ("try", "loop") for n in body.nodes):
        raise GraphError(
            f"{node.id} (corps) : un try ne peut pas contenir de try ni de loop imbriqué"
        )
    return body


def _validate_extract_fields(node: Node) -> None:
    fields = node.config.get("fields")
    if not isinstance(fields, list) or not 1 <= len(fields) <= _MAX_EXTRACT_FIELDS:
        raise GraphError(
            f"{node.id} : fields doit compter de 1 à {_MAX_EXTRACT_FIELDS} champs"
        )
    names: set[str] = set()
    for f in fields:
        name = f.get("name") if isinstance(f, dict) else None
        if not isinstance(name, str) or not _FIELD_NAME.match(name):
            raise GraphError(f"{node.id} : nom de champ invalide : {name!r}")
        if name in names:
            raise GraphError(f"{node.id} : champ dupliqué : {name}")
        names.add(name)
        if f.get("type") not in EXTRACT_FIELD_TYPES:
            raise GraphError(
                f"{node.id} : type de champ inconnu {f.get('type')!r} pour {name} "
                f"(attendu : {', '.join(EXTRACT_FIELD_TYPES)})"
            )


def _outer_refs(node: Node) -> set[str]:
    """Les nœuds du graphe englobant qu'une configuration référence."""
    if node.type == "loop":
        return _refs(node.config.get("items")) | (
            _refs(node.config.get("until")) - {_ITERATION}
        )
    if node.type == "try":
        return (
            set()
        )  # le corps est isolé ; retries/retry_delay_ms ne sont pas des gabarits
    return _refs(node.config)


def _template_paths(node: Node) -> tuple[set[tuple[str, str]], set[str]]:
    """(nœud, chemin) référencés par un nœud ; le pseudo-nœud ``iteration``
    (condition ``until`` d'une boucle) est séparé, il n'a pas d'ancêtre.
    """
    if node.type == "try":
        return set(), set()  # le corps est isolé, comme dans _outer_refs
    if node.type != "loop":
        return _ref_paths(node.config), set()
    until_paths = _ref_paths(node.config.get("until"))
    outer = _ref_paths(node.config.get("items")) | {
        (rid, path) for rid, path in until_paths if rid != _ITERATION
    }
    iteration = {path for rid, path in until_paths if rid == _ITERATION}
    return outer, iteration


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


_SCALAR = "scalar"
_KNOWN_DICT = "dict"
_UNKNOWN = "unknown"
_SCALAR_CONVERT_TARGETS = {"text", "number", "boolean", "date"}
# Sorties à clés fixes, telles que construites par le moteur.
_FIXED_OUTPUT_KEYS = {
    "rag": {"query", "passages"},
    "http": {"status", "headers", "body"},
    "notification": {"channel", "sent"},
}


def _first_segment(path: str) -> Optional[str]:
    parts = [p for p in path.split(".") if p]
    return parts[0] if parts else None


def _output_form(node: Node, graph: WorkflowGraph) -> tuple[str, Optional[set[str]]]:
    """La forme de sortie d'un nœud connue statiquement (avant exécution).

    ``merge`` est le seul nœud à clés connues : ce sont les identifiants (du
    graphe) de ses entrées. ``convert`` vers texte/nombre/booléen est un
    scalaire garanti. ``agent`` ne l'est PAS : ``run_agent_message`` relit sa
    réponse en JSON quand elle y ressemble (``try_parse_json``), donc un
    chemin dessus n'est jamais refusé ici — seule l'exécution, qui voit la
    vraie valeur, peut le faire (voir ``_lookup``). Le reste (``trigger``,
    ``tool``, passthrough d'un ``router``/``classifier``, ``convert`` vers
    json/list, ``loop``) garde aussi une forme non garantie.
    """
    if node.type == "convert" and node.config.get("to") in _SCALAR_CONVERT_TARGETS:
        return _SCALAR, None
    if node.type == "merge":
        sources = {e.source for e in graph.edges if e.target == node.id}
        return _KNOWN_DICT, sources
    if node.type in ("set", "extract"):
        name = "key" if node.type == "set" else "name"
        fields = node.config.get("fields") or []
        return _KNOWN_DICT, {f.get(name) for f in fields if isinstance(f, dict)}
    if node.type in _FIXED_OUTPUT_KEYS:
        return _KNOWN_DICT, _FIXED_OUTPUT_KEYS[node.type]
    return _UNKNOWN, None


def _check_template_path(
    node_id: str, ref: str, path: str, target: Node, graph: WorkflowGraph
) -> None:
    seg = _first_segment(path)
    if seg is None:
        return
    full = f"{ref}{path}"
    if target.type in _ROUTED and seg == "route":
        raise GraphError(
            f"{node_id} : {{{{{full}}}}} invalide — {ref} ne stocke jamais sa "
            f"route, utilisez {{{{{ref}}}}} pour sa sortie",
            code="template_ref_invalid",
            params={"node": node_id, "ref": full},
        )
    form, keys = _output_form(target, graph)
    if form == _SCALAR:
        raise GraphError(
            f"{node_id} : {{{{{full}}}}} invalide — la sortie de {ref} est une "
            f"valeur simple, utilisez {{{{{ref}}}}}",
            code="template_ref_invalid",
            params={"node": node_id, "ref": full},
        )
    if form == _KNOWN_DICT and seg not in keys:
        raise GraphError(
            f"{node_id} : {{{{{full}}}}} invalide — {ref} n'a pas de champ "
            f"{seg!r}, utilisez {{{{{ref}}}}} ou l'un de : "
            f"{', '.join(sorted(keys))}",
            code="template_ref_invalid",
            params={"node": node_id, "ref": full},
        )


def _parse_http_body(text: str) -> Any:
    """JSON si ``text`` s'y lit, texte brut sinon (peu importe le content-type
    déclaré : un serveur qui ment sur son content-type ne doit pas nous faire
    planter, et un JSON sans content-type correct doit quand même être lu)."""
    try:
        return json.loads(text)
    except ValueError:
        return text


async def _read_capped_response(resp, node_id: str) -> dict:
    """Lit ``resp`` en flux, coupe au-delà de ``MAX_HTTP_RESPONSE_BYTES``."""
    chunks = bytearray()
    async for chunk in resp.aiter_bytes():
        chunks.extend(chunk)
        if len(chunks) > MAX_HTTP_RESPONSE_BYTES:
            raise GraphError(
                f"{node_id} : réponse trop grande",
                code="http_response_too_large",
                params={"node": node_id, "max": str(MAX_HTTP_RESPONSE_BYTES)},
            )
    text = bytes(chunks).decode(resp.encoding or "utf-8", errors="replace")
    return {
        "status": resp.status_code,
        "headers": dict(resp.headers),
        "body": _parse_http_body(text),
    }


async def _http_call(node_id: str, cfg: dict, outputs: dict) -> dict:
    """Requête HTTP bornée et protégée SSRF pour le nœud ``http``.

    Réutilise la garde SSRF de ``routers/rag/validators`` (résolution DNS,
    IP privées/loopback/link-local/réservées/multicast, forme d'URL) au lieu
    de la dupliquer : c'est elle qui décide ce qui est interne, ici on ne fait
    que traduire son refus en ``GraphError`` lisible côté workflow. Les
    redirections sont suivies à la main, chaque saut revalidé (même piège que
    ``index_url.py`` : httpx ``follow_redirects=True`` ne revalide pas la
    ``Location`` qu'il suit).
    """
    import httpx
    from fastapi import HTTPException as _HTTPException

    from apowerb.routers.rag.validators import (
        _is_disallowed_ip,
        _validate_url_not_internal,
    )

    def _refused(reason: str) -> GraphError:
        return GraphError(
            f"{node_id} : url refusée ({reason})",
            code="http_url_refused",
            params={"node": node_id, "reason": reason},
        )

    def _safe_url(url: str) -> str:
        try:
            return _validate_url_not_internal(url)
        except _HTTPException as exc:
            raise _refused(str(exc.detail)) from None

    async def _pinned(url: str) -> tuple[str, str, dict]:
        """Résout l'hôte UNE fois, valide chaque adresse, et renvoie l'URL
        réécrite sur l'IP retenue, l'en-tête Host et l'extension SNI.

        Sans cela httpx re-résout le nom à la connexion : un DNS à TTL court
        répond une IP publique à la garde puis 169.254.169.254 au connect.
        """
        parsed = httpx.URL(url)
        host = parsed.host
        default_port = 443 if parsed.scheme == "https" else 80
        port = parsed.port or default_port
        host_header = host if port == default_port else f"{host}:{port}"
        try:
            ipaddress.ip_address(host)
            return url, host_header, {}
        except ValueError:
            pass
        try:
            infos = await asyncio.to_thread(
                socket.getaddrinfo, host, port, type=socket.SOCK_STREAM
            )
            ips = [ipaddress.ip_address(info[4][0]) for info in infos]
        except (OSError, ValueError):
            raise _refused("Could not resolve URL hostname") from None
        if not ips or any(_is_disallowed_ip(ip) for ip in ips):
            raise _refused("URLs pointing to private networks are not allowed")
        extensions = {"sni_hostname": host} if parsed.scheme == "https" else {}
        return str(parsed.copy_with(host=str(ips[0]))), host_header, extensions

    method = cfg["method"]
    url = render(cfg["url"], outputs)
    url = url if isinstance(url, str) else json.dumps(url, ensure_ascii=False)

    headers: dict[str, str] = {}
    for h in cfg.get("headers") or []:
        key = render(h.get("key", ""), outputs)
        value = render(h.get("value", ""), outputs)
        key = key if isinstance(key, str) else json.dumps(key, ensure_ascii=False)
        value = (
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        )
        # validate_graph ne voit que la clé brute : ``{{a}}`` qui rend
        # "Authorization" doit être refusé ici, sur la clé réellement envoyée.
        sent_key = key.strip().lower()
        if sent_key in _FORBIDDEN_HTTP_HEADERS or sent_key == "host":
            raise GraphError(
                f"{node_id} : en-tête {sent_key!r} interdit",
                code="http_header_forbidden",
                params={"node": node_id, "header": sent_key},
            )
        headers[key] = value

    raw_body = cfg.get("body")
    raw_body = render(raw_body, outputs) if raw_body is not None else None
    json_body = raw_body if isinstance(raw_body, (dict, list)) else None
    content_body = None
    if json_body is None and raw_body is not None:
        content_body = (
            raw_body
            if isinstance(raw_body, str)
            else json.dumps(raw_body, ensure_ascii=False)
        )

    timeout = cfg.get("timeout_s", DEFAULT_HTTP_TIMEOUT)
    current_url = _safe_url(url)

    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for _ in range(MAX_HTTP_REDIRECTS + 1):
                pinned_url, host_header, extensions = await _pinned(current_url)
                async with client.stream(
                    method,
                    pinned_url,
                    headers={**headers, "Host": host_header},
                    json=json_body,
                    content=content_body,
                    extensions=extensions,
                ) as resp:
                    if resp.is_redirect:
                        location = resp.headers.get("location")
                        if not location:
                            return await _read_capped_response(resp, node_id)
                        current_url = _safe_url(
                            str(httpx.URL(current_url).join(location))
                        )
                        continue
                    return await _read_capped_response(resp, node_id)
    except httpx.TimeoutException:
        raise GraphError(
            f"{node_id} : délai dépassé",
            code="http_timeout",
            params={"node": node_id, "seconds": str(timeout)},
        ) from None
    except httpx.HTTPError:
        # Erreur réseau (DNS, connexion, protocole...) : jamais le message
        # brut de la librairie, qui peut porter l'hôte ou le chemin visés.
        raise GraphError(
            f"{node_id} : échec réseau",
            code="http_failed",
            params={"node": node_id},
        ) from None
    raise GraphError(
        f"{node_id} : trop de redirections",
        code="http_failed",
        params={"node": node_id},
    )


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


def validate_graph(graph: WorkflowGraph, *, workflow_id: Optional[str] = None) -> None:
    """Valide ``graph``.

    ``workflow_id`` — l'identifiant du workflow en cours de validation, s'il
    est déjà connu (un brouillon pas encore enregistré ne l'a pas) — permet de
    refuser un ``subworkflow`` qui s'appellerait directement lui-même. Le
    cycle indirect (A -> B -> A) n'est détectable qu'à l'exécution, via la
    pile des ``workflow_id`` traversés (``_Compiler._subworkflow``).
    """
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
        if n.type in ("agent", "classifier", "extract", "rag") and not cfg.get(
            "agent_id"
        ):
            raise GraphError(f"{n.id} : agent_id manquant")
        if n.type == "tool" and not cfg.get("tool"):
            raise GraphError(f"{n.id} : outil manquant")
        if n.type == "router" and not cfg.get("rules"):
            raise GraphError(f"{n.id} : aucune règle de routage")
        if n.type == "classifier" and len(_declared_routes(n)) < 2:
            raise GraphError(f"{n.id} : un classifieur demande au moins deux routes")
        if n.type == "loop":
            _loop_body(n, workflow_id=workflow_id)
        if n.type == "try":
            _try_config(n, workflow_id=workflow_id)
        if n.type == "subworkflow":
            target = cfg.get("workflow_id")
            if not isinstance(target, str) or not target.strip():
                raise GraphError(f"{n.id} : workflow_id manquant")
            if workflow_id is not None and target == workflow_id:
                raise GraphError(
                    f"{n.id} : un workflow ne peut pas s'appeler lui-même",
                    code="subworkflow_cycle",
                    params={"node": n.id, "workflow": target},
                )
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
        if n.type == "extract":
            _validate_extract_fields(n)
        if n.type == "rag":
            if not cfg.get("query"):
                raise GraphError(f"{n.id} : query manquant")
            top_k = cfg.get("top_k", _DEFAULT_RAG_TOP_K)
            if (
                isinstance(top_k, bool)
                or not isinstance(top_k, int)
                or not _MIN_RAG_TOP_K <= top_k <= _MAX_RAG_TOP_K
            ):
                raise GraphError(
                    f"{n.id} : top_k doit être un entier de {_MIN_RAG_TOP_K} à "
                    f"{_MAX_RAG_TOP_K}"
                )
        if n.type == "http":
            if cfg.get("method") not in _HTTP_METHODS:
                raise GraphError(
                    f"{n.id} : méthode HTTP inconnue {cfg.get('method')!r} "
                    f"(attendu : {', '.join(_HTTP_METHODS)})"
                )
            if not cfg.get("url"):
                raise GraphError(f"{n.id} : url manquante")
            for h in cfg.get("headers") or []:
                key = str(h.get("key", "")).strip().lower()
                if key in _FORBIDDEN_HTTP_HEADERS:
                    raise GraphError(
                        f"{n.id} : l'en-tête {h.get('key')!r} ne peut pas être écrit en "
                        "clair dans le graphe (secret potentiel) ; l'authentification "
                        "HTTP passera par auth.integration_id"
                    )
            timeout = cfg.get("timeout_s", DEFAULT_HTTP_TIMEOUT)
            if (
                isinstance(timeout, bool)
                or not isinstance(timeout, int)
                or not MIN_HTTP_TIMEOUT <= timeout <= MAX_HTTP_TIMEOUT
            ):
                raise GraphError(
                    f"{n.id} : timeout_s doit être un entier de {MIN_HTTP_TIMEOUT} à "
                    f"{MAX_HTTP_TIMEOUT}"
                )
            if cfg.get("auth") is not None:
                # Pas de stockage de secret encore branché pour ce nœud : voir le
                # docstring du module et le rapport du lot. auth doit rester null
                # tant que ça n'existe pas, plutôt que d'inventer une résolution.
                raise GraphError(
                    f"{n.id} : authentification HTTP pas encore disponible"
                )
        if n.type == "notification":
            channel = cfg.get("channel")
            if channel not in _NOTIF_CHANNELS:
                raise GraphError(
                    f"{n.id} : canal de notification inconnu {channel!r} "
                    f"(attendu : {', '.join(sorted(_NOTIF_CHANNELS))})"
                )
            to = cfg.get("to") or []
            if channel == "email":
                if (
                    not isinstance(to, list)
                    or not 1 <= len(to) <= MAX_NOTIFICATION_RECIPIENTS
                ):
                    raise GraphError(
                        f"{n.id} : de 1 à {MAX_NOTIFICATION_RECIPIENTS} destinataires "
                        "(to) requis pour l'email"
                    )
                if not cfg.get("subject"):
                    raise GraphError(f"{n.id} : subject manquant")
            elif channel == "app" and to:
                raise GraphError(
                    f"{n.id} : to doit être vide pour une notification app "
                    "(le destinataire est le propriétaire du workflow)"
                )
            elif channel == "teams":
                if to:
                    raise GraphError(
                        f"{n.id} : to doit être vide pour une notification teams "
                        "(le destinataire est le webhook Teams du propriétaire)"
                    )
                if not cfg.get("subject"):
                    raise GraphError(f"{n.id} : subject manquant")

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
        if n.type in ("try", "condition"):
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
        outer_paths, iteration_paths = _template_paths(n)
        for ref, path in outer_paths:
            _check_template_path(n.id, ref, path, by_id[ref], graph)
        for path in iteration_paths:
            seg = _first_segment(path)
            if seg is not None and seg not in ("output", "index"):
                raise GraphError(
                    f"{n.id} : {{{{iteration{path}}}}} invalide — iteration n'a "
                    "que output et index, utilisez {{iteration.output}} ou "
                    "{{iteration.index}}",
                    code="template_ref_invalid",
                    params={"node": n.id, "ref": f"iteration{path}"},
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


def _extract_prompt(fields: list[dict], text: str) -> str:
    lines = [
        "Reply with ONLY a JSON object with exactly these fields, nothing else.",
        "",
    ]
    for f in fields:
        req = "required" if f.get("required") else "optional"
        desc = f.get("description")
        lines.append(
            f"- {f['name']} ({f['type']}, {req})" + (f": {desc}" if desc else "")
        )
    lines += ["", "<<< INPUT >>>", text, "<<< END INPUT >>>"]
    return "\n".join(lines)


def _matches_extract_type(value: Any, type_: str) -> bool:
    if type_ == "string":
        return isinstance(value, str)
    if type_ == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if type_ == "boolean":
        return isinstance(value, bool)
    if type_ == "list":
        return isinstance(value, list)
    if type_ == "object":
        return isinstance(value, dict)
    return False  # pragma: no cover - refusé par validate_graph


def _extract_result(node_id: str, fields: list[dict], answer: Any) -> dict:
    """La réponse d'un agent, relue comme JSON puis validée champ par champ.

    Le texte brut de la réponse n'apparaît jamais dans l'erreur : seuls le
    nom du champ fautif (``None`` si la réponse n'est pas du JSON) et la
    nature du problème sont exposés.
    """
    parsed = try_parse_json(answer)
    if not isinstance(parsed, dict):
        raise GraphError(
            f"{node_id} : réponse d'extraction inexploitable (pas un objet JSON)",
            code="extract_failed",
            params={"node": node_id, "field": None, "problem": "not_json"},
        )
    result: dict[str, Any] = {}
    for f in fields:
        name, type_ = f["name"], f["type"]
        value = parsed.get(name)
        if name not in parsed or value is None:
            if f.get("required"):
                raise GraphError(
                    f"{node_id} : champ requis manquant : {name}",
                    code="extract_failed",
                    params={"node": node_id, "field": name, "problem": "missing"},
                )
            result[name] = None
            continue
        if not _matches_extract_type(value, type_):
            raise GraphError(
                f"{node_id} : {name} n'est pas du type {type_}",
                code="extract_failed",
                params={"node": node_id, "field": name, "problem": "type"},
            )
        result[name] = value
    return result


class _Compiler:
    def __init__(
        self,
        graph,
        payload,
        run_agent,
        run_tool,
        emit,
        cancel_event,
        *,
        run_rag: Optional[RunRag] = None,
        run_notify: Optional[RunNotify] = None,
        run_subworkflow: Optional["ResolveWorkflow"] = None,
        workflow_stack: tuple[str, ...] = (),
        depth: int = 1,
    ):
        self.g = graph
        self.payload = payload
        self.run_agent = run_agent
        self.run_tool = run_tool
        self.run_rag = run_rag
        self.run_notify = run_notify
        self.emit = emit
        self.cancel = cancel_event
        # Sous-workflows : callback de résolution (owner-scopé, côté routeur),
        # pile des workflow_id déjà en cours d'exécution (cycle) et
        # profondeur courante (plafond _MAX_SUBWORKFLOW_DEPTH). Inchangés
        # pour un corps de loop/try (même workflow) ; avancés d'un cran pour
        # un saut de subworkflow.
        self.run_subworkflow = run_subworkflow
        self.workflow_stack = workflow_stack
        self.depth = depth
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
                    msg = render(cfg["input"], self.outputs, node.id)
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
                    args = render(cfg["args"], self.outputs, node.id)
                else:
                    args = UpstreamArgs(
                        node_input if isinstance(node_input, dict) else {}
                    )
                return await self.run_tool(cfg["tool"], args), None
        elif node.type == "router":

            async def body(node_input):
                for rule in cfg["rules"]:
                    if evaluate_rule(
                        rule, _rule_subject(rule, self.outputs, node.id, node_input)
                    ):
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
                    return render(cfg["value"], self.outputs, node.id), None
                return node_input, None
        elif node.type == "convert":

            async def body(node_input):
                value = (
                    render(cfg["input"], self.outputs, node.id)
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
                # Champ vide = l'entrée du nœud : rendu, il donnerait "" et la
                # condition sortirait toujours false, sans erreur.
                outcomes = [
                    evaluate_rule(
                        rule,
                        render(rule["field"], self.outputs)
                        if (rule.get("field") or "").strip()
                        else node_input,
                    )
                    for rule in cfg["rules"]
                ]
                matched = (
                    all(outcomes) if cfg.get("match", "all") == "all" else any(outcomes)
                )
                return node_input, "true" if matched else "false"
        elif node.type == "extract":

            async def body(node_input):
                value = (
                    render(cfg["input"], self.outputs)
                    if cfg.get("input") not in (None, "")
                    else node_input
                )
                text = (
                    value
                    if isinstance(value, str)
                    else json.dumps(value, ensure_ascii=False)
                )
                answer = await self.run_agent(
                    cfg["agent_id"], _extract_prompt(cfg["fields"], text)
                )
                return _extract_result(node.id, cfg["fields"], answer), None
        elif node.type == "rag":

            async def body(node_input):
                query = render(cfg["query"], self.outputs)
                query = (
                    query
                    if isinstance(query, str)
                    else json.dumps(query, ensure_ascii=False)
                )
                top_k = cfg.get("top_k", _DEFAULT_RAG_TOP_K)
                if self.run_rag is None:
                    raise GraphError(
                        f"{node.id} : recherche RAG indisponible",
                        code="rag_failed",
                        params={"node": node.id},
                    )
                try:
                    return await self.run_rag(cfg["agent_id"], query, top_k), None
                except GraphError as exc:
                    # agent_not_found (mauvais propriétaire / agent inconnu) se
                    # propage tel quel, comme pour le nœud agent : seules les
                    # erreurs propres au RAG portent le nœud fautif.
                    if exc.code in ("rag_no_knowledge", "rag_failed"):
                        raise GraphError(
                            str(exc),
                            code=exc.code,
                            params={**exc.params, "node": node.id},
                        ) from None
                    raise
        elif node.type == "http":

            async def body(node_input):
                return await _http_call(node.id, cfg, self.outputs), None
        elif node.type == "notification":

            async def body(node_input):
                channel = cfg["channel"]
                to = [render(t, self.outputs) for t in cfg.get("to") or []]
                to = [
                    t if isinstance(t, str) else json.dumps(t, ensure_ascii=False)
                    for t in to
                ]
                subject = render(cfg.get("subject", ""), self.outputs)
                subject = (
                    subject
                    if isinstance(subject, str)
                    else json.dumps(subject, ensure_ascii=False)
                )
                message = render(cfg.get("body", ""), self.outputs)
                message = (
                    message
                    if isinstance(message, str)
                    else json.dumps(message, ensure_ascii=False)
                )
                if self.run_notify is None:
                    raise GraphError(
                        f"{node.id} : notifications indisponibles dans ce contexte d'exécution",
                        code="notification_unavailable",
                        params={"node": node.id},
                    )
                sent = await self.run_notify(node.id, channel, to, subject, message)
                return {"channel": channel, "sent": sent}, None
        elif node.type == "loop":
            body_graph = WorkflowGraph.model_validate(cfg["body"])

            async def body(node_input):
                return await self._loop(node, body_graph, node_input), None
        elif node.type == "try":
            body_graph = WorkflowGraph.model_validate(cfg["body"])

            async def body(node_input):
                return await self._try(node, body_graph, node_input)
        elif node.type == "subworkflow":

            async def body(node_input):
                return await self._subworkflow(node, cfg, node_input), None
        else:  # pragma: no cover - refusé par validate_graph
            raise GraphError(f"type non pris en charge : {node.type}")
        return body

    async def _loop(self, node: Node, body: WorkflowGraph, node_input: Any) -> Any:
        cfg = node.config
        cap = cfg["max_iterations"]
        if cfg["mode"] == "foreach":
            items = render(cfg["items"], self.outputs, node.id)
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
            if evaluate_rule(
                cfg["until"], _rule_subject(cfg["until"], scope, node.id, previous)
            ):
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
        return await self._run_body(
            node, body, payload, index_key=_ITERATION, index=index
        )

    async def _run_body(
        self,
        node: Node,
        body: WorkflowGraph,
        payload: Any,
        *,
        index_key: Optional[str] = None,
        index: Optional[int] = None,
        workflow_stack: Optional[tuple[str, ...]] = None,
        depth: Optional[int] = None,
    ) -> Any:
        """Exécute ``body`` comme un workflow ADK à part entière, portée isolée.

        Partagé par ``loop`` (``iteration``), ``try`` (``attempt``) et
        ``subworkflow`` (ni l'un ni l'autre : ``index_key`` reste ``None``).
        Événements préfixés ``"<node.id>.<inner>"``. En cas d'échec, le nœud
        interne responsable (identifiant brut, non préfixé) est mémorisé sur
        l'exception (``_apowerb_inner_node``) pour que ``try`` puisse le
        rapporter sans avoir à relire les événements déjà émis.
        """
        if self.cancel.is_set():
            raise WorkflowCancelled()
        last_error_node: Optional[str] = None

        def emit(ev: dict) -> None:
            nonlocal last_error_node
            if ev.get("event") == "node_error":
                last_error_node = ev.get("node_id")
            if "node_id" in ev:
                ev = {**ev, "node_id": f"{node.id}.{ev['node_id']}"}
                if index_key is not None:
                    ev[index_key] = index
            self.emit(ev)

        queue: asyncio.Queue = asyncio.Queue()
        inner = _Compiler(
            body,
            payload,
            self.run_agent,
            self.run_tool,
            queue.put_nowait,
            self.cancel,
            run_rag=self.run_rag,
            run_notify=self.run_notify,
            run_subworkflow=self.run_subworkflow,
            workflow_stack=self.workflow_stack
            if workflow_stack is None
            else workflow_stack,
            depth=self.depth if depth is None else depth,
        )
        async for item in drive_workflow(inner.build(), queue, root_name=_ROOT):
            if isinstance(item, _Finished):
                if item.exc is not None:
                    try:
                        item.exc._apowerb_inner_node = last_error_node
                    except (AttributeError, TypeError):
                        pass
                    raise item.exc
                break
            emit(item)
        return _final_output(body, inner.outputs)

    async def _try(
        self, node: Node, body: WorkflowGraph, node_input: Any
    ) -> tuple[Any, str]:
        """Rejoue le corps jusqu'à ``retries`` fois ; ``ok``/``error`` en route.

        Une exception épuisée après les tentatives ne remonte JAMAIS comme un
        échec du nœud : elle devient une sortie lisible routée "error", comme
        un router sans règle par défaut mais sans jamais échouer le run.
        L'annulation, elle, n'est pas une tentative ratée : elle se propage.
        """
        cfg = node.config
        retries = cfg.get("retries", 0)
        delay_s = cfg.get("retry_delay_ms", 0) / 1000
        last_exc: Optional[BaseException] = None
        for attempt in range(retries + 1):
            if attempt and delay_s:
                await asyncio.sleep(delay_s)
            try:
                result = await self._run_body(
                    node, body, node_input, index_key=_ATTEMPT, index=attempt
                )
                return result, "ok"
            except WorkflowCancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - épuisé -> route "error", jamais une exception globale
                last_exc = exc
        fields = error_fields(last_exc)
        return {
            "code": fields.get("code"),
            "detail": fields.get("detail"),
            "params": fields.get("params") or {},
            "node": getattr(last_exc, "_apowerb_inner_node", None),
        }, "error"

    async def _subworkflow(self, node: Node, cfg: dict, node_input: Any) -> Any:
        """Exécute le workflow ``cfg['workflow_id']`` via ``self.run_subworkflow``.

        Gardes AVANT tout appel réseau/DB : cycle (pile des workflow_id déjà
        en cours) puis profondeur (``_MAX_SUBWORKFLOW_DEPTH``) — un cycle ou
        une profondeur excessive ne doit jamais déclencher la résolution du
        workflow suivant.
        """
        target = cfg["workflow_id"]
        if self.run_subworkflow is None:
            raise GraphError(
                f"{node.id} : les sous-workflows ne sont pas disponibles dans ce contexte",
                code="subworkflow_not_found",
                params={"node": node.id, "workflow": target},
            )
        if target in self.workflow_stack:
            raise GraphError(
                f"{node.id} : cycle de sous-workflow sur {target}",
                code="subworkflow_cycle",
                params={"node": node.id, "workflow": target},
            )
        if self.depth + 1 > _MAX_SUBWORKFLOW_DEPTH:
            raise GraphError(
                f"{node.id} : profondeur de sous-workflow dépassée (max {_MAX_SUBWORKFLOW_DEPTH})",
                code="subworkflow_too_deep",
                params={"node": node.id, "max": _MAX_SUBWORKFLOW_DEPTH},
            )
        body = await self.run_subworkflow(target)
        if body is None:
            raise GraphError(
                f"{node.id} : workflow introuvable : {target}",
                code="subworkflow_not_found",
                params={"node": node.id, "workflow": target},
            )
        try:
            validate_graph(body, workflow_id=target)
        except GraphError as exc:
            raise GraphError(f"{node.id} (sous-workflow {target}) : {exc}") from exc
        payload = (
            render(cfg["input"], self.outputs)
            if cfg.get("input") is not None
            else node_input
        )
        return await self._run_body(
            node,
            body,
            payload,
            workflow_stack=self.workflow_stack + (target,),
            depth=self.depth + 1,
        )

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
    run_rag: Optional[RunRag] = None,
    cancel_event: asyncio.Event,
    run_subworkflow: Optional[ResolveWorkflow] = None,
    workflow_id: Optional[str] = None,
    run_notify: Optional[RunNotify] = None,
) -> AsyncGenerator[str, None]:
    """Valide, compile et exécute un graphe ; produit ses événements SSE.

    Événements : ``node_start`` / ``node_complete`` (``output``,
    ``duration_ms``) / ``node_error`` / ``route``, puis un seul terminal parmi
    ``done`` (``output``), ``cancelled`` et ``error`` (``detail``).

    ``run_subworkflow`` — callback résolu côté routeur (même contrôle d'accès
    que pour lancer ce workflow directement), voir
    ``workflow_runtime.resolve_workflow_for``. ``workflow_id`` — l'identifiant
    du workflow en train de s'exécuter, s'il est connu : il amorce la pile de
    cycle et permet à ``validate_graph`` de refuser l'auto-référence directe.
    Sans lui, un ``subworkflow`` peut quand même s'exécuter (profondeur 1),
    seul le cas A -> A immédiat échappe alors à la détection.

    ``max_concurrency=1`` : les identifiants des agents et des outils passent
    encore par ``os.environ`` (``to_agent``, ``dict_to_envvar``) ; deux nœuds
    concurrents pourraient s'échanger leurs secrets. Le fan-out reste correct,
    il est seulement sérialisé.
    """
    try:
        validate_graph(graph, workflow_id=workflow_id)
    except GraphError as exc:
        yield _sse({"event": "error", "code": "invalid_graph", "detail": str(exc)})
        return
    queue: asyncio.Queue = asyncio.Queue()
    compiler = _Compiler(
        graph,
        payload,
        run_agent,
        run_tool,
        queue.put_nowait,
        cancel_event,
        run_rag=run_rag,
        run_notify=run_notify,
        run_subworkflow=run_subworkflow,
        workflow_stack=(workflow_id,) if workflow_id else (),
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
