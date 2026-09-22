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
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Awaitable, Callable, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
]
CONVERT_TARGETS = ("text", "json", "number", "boolean", "list")
_TRUE = {"true", "yes", "oui", "1", "vrai"}
_FALSE = {"false", "no", "non", "0", "faux"}
_ROUTED = {"router", "classifier"}
_NOT_YET = {"approval"}
_MAX_LOOP = 100

# --- Triggers (T1 : socle + validation des 8 kinds) -------------------------
#
# Un trigger automatique n'exécute que la version PUBLIÉE d'un workflow, au
# nom de son propriétaire (voir apowerb.core.workflow_triggers). Ici on ne
# valide que la FORME de ``config`` du nœud ``trigger`` — c'est l'API de
# gestion qui répond ``active:false, reason:"not_available"`` pour un kind
# pas encore branché (email, agent_tool, form, file, workflow_done : T2).
TRIGGER_KINDS = (
    "manual",
    "webhook",
    "schedule",
    "email",
    "agent_tool",
    "form",
    "file",
    "workflow_done",
)
_EMAIL_PROVIDERS = ("outlook", "gmail")
_FILE_PROVIDERS = ("onedrive", "google_drive")
_FORM_FIELD_TYPES = ("text", "textarea", "number", "boolean", "select", "date")
_FORM_ACCESS = ("authenticated", "public")
_TOOL_ARG_TYPES = ("string", "number", "boolean", "array", "object")
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
_WORKFLOW_DONE_ON = ("success", "error", "any")
_DEFAULT_TRIGGER_TIMEZONE = "Europe/Paris"
_MIN_CRON_INTERVAL_MINUTES = 5
_MIN_FILE_INTERVAL_MINUTES = 5
_MAX_FILE_INTERVAL_MINUTES = 1440
# ``weekday`` va jusqu'à 7 : 0 et 7 valent tous deux dimanche, comme cron.
_CRON_FIELD_BOUNDS = (
    ("minute", 0, 59),
    ("hour", 0, 23),
    ("day", 1, 31),
    ("month", 1, 12),
    ("weekday", 0, 7),
)
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
    raise ValueError(f"unknown conversion {to!r}")


def _parse_cron_field(part_group: str, lo: int, hi: int) -> set[int]:
    values: set[int] = set()
    for part in part_group.split(","):
        rng, step = part, 1
        if "/" in part:
            rng, step_s = part.split("/", 1)
            if not step_s.isdigit() or int(step_s) < 1:
                raise ValueError(f"pas invalide : {part!r}")
            step = int(step_s)
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, b = rng.split("-", 1)
            if not (a.isdigit() and b.isdigit()):
                raise ValueError(f"plage invalide : {part!r}")
            start, end = int(a), int(b)
        elif rng.isdigit():
            start = end = int(rng)
        else:
            raise ValueError(f"champ invalide : {part!r}")
        if not (lo <= start <= hi and lo <= end <= hi and start <= end):
            raise ValueError(f"hors bornes [{lo}-{hi}] : {part!r}")
        values.update(range(start, end + 1, step))
    return values


def parse_cron(cron: str) -> dict[str, set[int]]:
    """5 champs (minute heure jour mois jour_semaine) -> ensembles de valeurs.

    Supporte ``*``, ``A``, ``A-B``, ``A,B,C`` et ``.../N`` (pas), combinables.
    ``jour_semaine`` accepte 0-7 (0 et 7 valent dimanche) ; 7 est ramené à 0.
    """
    parts = (cron or "").split()
    if len(parts) != 5:
        raise GraphError(
            f"cron invalide (5 champs attendus, {len(parts)} reçus) : {cron!r}",
            code="invalid_cron",
        )
    parsed: dict[str, set[int]] = {}
    for (name, lo, hi), part in zip(_CRON_FIELD_BOUNDS, parts):
        try:
            parsed[name] = _parse_cron_field(part, lo, hi)
        except ValueError as exc:
            raise GraphError(
                f"cron invalide (champ {name}) : {exc}",
                code="invalid_cron",
                params={"field": name},
            ) from None
    if 7 in parsed["weekday"]:
        parsed["weekday"].discard(7)
        parsed["weekday"].add(0)
    return parsed


def _check_cron_min_interval(cron: str) -> None:
    """Refuse un cron qui peut se déclencher à moins de 5 minutes d'intervalle.

    Ne modélise que le pire cas des champs jour/mois/jour_semaine (ignorés :
    un cron qui ne correspond qu'à un jour par mois est de toute façon bien
    au-delà de l'intervalle minimal). Seuls minute et heure comptent : c'est
    la seule paire qui peut faire tomber deux déclenchements dans la même
    fenêtre de 5 minutes.
    """
    fields = parse_cron(cron)
    minutes = sorted(fields["minute"])
    if not minutes:
        raise GraphError(
            "cron invalide : aucune minute ne correspond", code="invalid_cron"
        )
    gaps = [b - a for a, b in zip(minutes, minutes[1:])]
    if len(fields["hour"]) > 1:
        # Plus d'une heure retenue : la minute la plus haute d'une heure peut
        # être suivie de la plus basse de l'heure d'après.
        gaps.append(60 - minutes[-1] + minutes[0])
    if gaps and min(gaps) < _MIN_CRON_INTERVAL_MINUTES:
        raise GraphError(
            f"cron trop fréquent : intervalle minimal {_MIN_CRON_INTERVAL_MINUTES} minutes",
            code="cron_too_frequent",
        )


def _check_timezone(tz_name: str) -> None:
    try:
        ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        raise GraphError(
            f"fuseau horaire inconnu : {tz_name!r}", code="unknown_timezone"
        ) from None


def _check_at_not_past(at: str, tz_name: str) -> None:
    try:
        when = datetime.fromisoformat(at)
    except ValueError:
        raise GraphError(f"date/heure invalide : {at!r}", code="invalid_at") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=ZoneInfo(tz_name))
    if when.astimezone(timezone.utc) <= datetime.now(timezone.utc):
        raise GraphError(f"{at!r} est déjà passé", code="at_in_past")


def _require_trigger_str(cfg: dict, field: str, *, code: str) -> str:
    value = cfg.get(field)
    if not isinstance(value, str) or not value.strip():
        raise GraphError(
            f"champ requis manquant : {field}", code=code, params={"field": field}
        )
    return value


def validate_trigger_config(
    cfg: dict,
    *,
    node_id: str = "trigger",
    workflow_id: Optional[str] = None,
    owner_id: Optional[str] = None,
) -> None:
    """Valide ``config`` d'un nœud ``trigger`` pour l'un des 8 kinds du contrat.

    Validation de FORME uniquement (plus, depuis T2, deux vérifications
    d'appartenance quand ``owner_id`` est connu — voir plus bas). Les 5 kinds
    T2 (email, agent_tool, form, file, workflow_done) sont désormais exécutés
    par le moteur (``core.workflow_triggers``) ; cette fonction ne fait
    toujours que la forme.

    ``workflow_id``, quand connu (édition d'un workflow existant), permet le
    refus d'un ``workflow_done`` qui s'écouterait lui-même.

    ``owner_id``, quand connu (appel depuis ``workflow_main``, où le
    propriétaire est toujours disponible), active deux contrôles qui ont
    besoin de la base : un ``agent_tool.tool_name`` déjà pris par un AUTRE
    workflow du même propriétaire, et un ``workflow_done.workflow_id`` qui ne
    lui appartient pas. Import différé (``workflow_triggers``/``workflow_main``
    dépendent tous deux de ce module) pour éviter un cycle au chargement.
    """
    kind = cfg.get("kind", "manual")
    if kind not in TRIGGER_KINDS:
        raise GraphError(
            f"{node_id} : kind de trigger inconnu {kind!r} "
            f"(attendu : {', '.join(TRIGGER_KINDS)})",
            code="unknown_trigger_kind",
            params={"node": node_id, "kind": str(kind)},
        )
    if kind == "manual":
        return
    if kind == "webhook":
        hmac_flag = cfg.get("hmac", False)
        if not isinstance(hmac_flag, bool):
            raise GraphError(
                f"{node_id} : hmac doit être un booléen",
                code="invalid_field",
                params={"field": "hmac"},
            )
        return
    if kind == "schedule":
        cron, at = cfg.get("cron"), cfg.get("at")
        if bool(cron) == bool(at):
            raise GraphError(
                f"{node_id} : exactement un de cron ou at est requis",
                code="schedule_needs_one_of_cron_or_at",
            )
        tz_name = cfg.get("timezone") or _DEFAULT_TRIGGER_TIMEZONE
        _check_timezone(tz_name)
        if cron:
            _check_cron_min_interval(cron)
        else:
            _check_at_not_past(at, tz_name)
        return
    if kind == "email":
        provider = cfg.get("provider")
        if provider not in _EMAIL_PROVIDERS:
            raise GraphError(
                f"{node_id} : provider email inconnu {provider!r} "
                f"(attendu : {', '.join(_EMAIL_PROVIDERS)})",
                code="unknown_email_provider",
            )
        for field in ("from_filter", "subject_filter"):
            value = cfg.get(field)
            if value is not None and not isinstance(value, str):
                raise GraphError(
                    f"{node_id} : {field} doit être une chaîne ou null",
                    code="invalid_field",
                    params={"field": field},
                )
        return
    if kind == "agent_tool":
        tool_name = cfg.get("tool_name")
        if not isinstance(tool_name, str) or not _TOOL_NAME_RE.match(tool_name):
            raise GraphError(
                f"{node_id} : tool_name invalide {tool_name!r} "
                "(attendu : ^[a-z][a-z0-9_]{2,40}$)",
                code="invalid_tool_name",
            )
        _require_trigger_str(cfg, "description", code="tool_description_required")
        schema = cfg.get("input_schema")
        if not isinstance(schema, list):
            raise GraphError(
                f"{node_id} : input_schema doit être une liste",
                code="invalid_input_schema",
            )
        names: set[str] = set()
        for field in schema:
            if not isinstance(field, dict) or not field.get("name"):
                raise GraphError(
                    f"{node_id} : champ input_schema sans nom",
                    code="invalid_input_schema",
                )
            if field["name"] in names:
                raise GraphError(
                    f"{node_id} : champ dupliqué dans input_schema : {field['name']}",
                    code="duplicate_tool_field",
                )
            names.add(field["name"])
            if field.get("type") not in _TOOL_ARG_TYPES:
                raise GraphError(
                    f"{node_id} : type de champ inconnu {field.get('type')!r} "
                    f"pour {field['name']}",
                    code="invalid_input_schema_type",
                )
        if owner_id is not None:
            from apowerb.core.workflow_triggers import tool_name_taken

            if tool_name_taken(
                tool_name, owner_id=owner_id, exclude_workflow_id=workflow_id
            ):
                raise GraphError(
                    f"{node_id} : tool_name {tool_name!r} déjà pris par un "
                    "autre workflow",
                    code="tool_name_taken",
                    params={"tool_name": tool_name},
                )
        return
    if kind == "form":
        _require_trigger_str(cfg, "title", code="form_title_required")
        if cfg.get("description") is not None and not isinstance(
            cfg.get("description"), str
        ):
            raise GraphError(
                f"{node_id} : description doit être une chaîne ou null",
                code="invalid_field",
                params={"field": "description"},
            )
        fields = cfg.get("fields")
        if not isinstance(fields, list) or not fields:
            raise GraphError(
                f"{node_id} : au moins un champ de formulaire est requis",
                code="form_fields_required",
            )
        names = set()
        for field in fields:
            if (
                not isinstance(field, dict)
                or not field.get("name")
                or not field.get("label")
            ):
                raise GraphError(
                    f"{node_id} : champ de formulaire invalide",
                    code="invalid_form_field",
                )
            if field["name"] in names:
                raise GraphError(
                    f"{node_id} : champ de formulaire dupliqué : {field['name']}",
                    code="duplicate_form_field",
                )
            names.add(field["name"])
            if field.get("type") not in _FORM_FIELD_TYPES:
                raise GraphError(
                    f"{node_id} : type de champ de formulaire inconnu {field.get('type')!r}",
                    code="invalid_form_field",
                )
        if cfg.get("access") not in _FORM_ACCESS:
            raise GraphError(
                f"{node_id} : access inconnu {cfg.get('access')!r} "
                f"(attendu : {', '.join(_FORM_ACCESS)})",
                code="invalid_form_access",
            )
        return
    if kind == "file":
        provider = cfg.get("provider")
        if provider not in _FILE_PROVIDERS:
            raise GraphError(
                f"{node_id} : provider fichier inconnu {provider!r} "
                f"(attendu : {', '.join(_FILE_PROVIDERS)})",
                code="unknown_file_provider",
            )
        _require_trigger_str(cfg, "folder_id", code="folder_id_required")
        _require_trigger_str(cfg, "folder_label", code="folder_label_required")
        interval = cfg.get("interval_min", 15)
        if (
            isinstance(interval, bool)
            or not isinstance(interval, int)
            or not (
                _MIN_FILE_INTERVAL_MINUTES <= interval <= _MAX_FILE_INTERVAL_MINUTES
            )
        ):
            raise GraphError(
                f"{node_id} : interval_min doit être un entier entre "
                f"{_MIN_FILE_INTERVAL_MINUTES} et {_MAX_FILE_INTERVAL_MINUTES}",
                code="invalid_file_interval",
            )
        return
    if kind == "workflow_done":
        source_id = _require_trigger_str(
            cfg, "workflow_id", code="workflow_done_workflow_id_required"
        )
        if cfg.get("on") not in _WORKFLOW_DONE_ON:
            raise GraphError(
                f"{node_id} : on inconnu {cfg.get('on')!r} "
                f"(attendu : {', '.join(_WORKFLOW_DONE_ON)})",
                code="invalid_workflow_done_on",
            )
        if workflow_id is not None and source_id == workflow_id:
            raise GraphError(
                f"{node_id} : un workflow ne peut pas s'écouter lui-même",
                code="workflow_done_self_listen",
            )
        if owner_id is not None:
            from apowerb.core.workflow_main import get_workflow

            if get_workflow(source_id, owner_id=owner_id) is None:
                raise GraphError(
                    f"{node_id} : workflow source introuvable {source_id!r}",
                    code="workflow_done_unknown_source",
                    params={"workflow_id": source_id},
                )
        return


def trigger_spec(graph: WorkflowGraph) -> dict:
    """``config`` du nœud ``trigger`` de tête, ``{"kind": "manual"}`` à défaut.

    Ne regarde que les nœuds de premier niveau : le déclencheur du corps
    d'une boucle (``_loop_body``) vit dans un sous-graphe séparé et n'est pas
    le trigger automatique du workflow.
    """
    for node in graph.nodes:
        if node.type == "trigger":
            return {"kind": "manual", **node.config}
    return {"kind": "manual"}


def validate_graph(
    graph: WorkflowGraph,
    *,
    workflow_id: Optional[str] = None,
    owner_id: Optional[str] = None,
) -> None:
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
        if n.type == "trigger":
            validate_trigger_config(
                cfg, node_id=n.id, workflow_id=workflow_id, owner_id=owner_id
            )

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
