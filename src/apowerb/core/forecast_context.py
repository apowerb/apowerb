"""Contexte d'une prévision dit en français, traduit en événements datés.

L'utilisateur écrit « promo -20 % chaque premier week-end du mois, fermé du
1er au 15 août, et si on arrêtait les promos ? ». Le modèle mutualisé en tire
des événements (plages de dates) et des scénarios ; th2forecast apprend ensuite
l'effet des événements sur l'historique (voir son ``docs/API.md``).

La sortie du modèle est une donnée, pas un ordre :

- chaque date est relue ici : format, plage à l'endroit, comprise entre le début
  de l'historique et la fin de l'horizon ; un groupe doit exister ; un ajustement
  a soit un pourcentage (> -100), soit une valeur ; sinon l'élément est écarté
  avec sa raison, jamais « réparé » ;
- un scénario n'est pas demandé au modèle sous la forme de th2forecast (liste
  complète des événements futurs, facile à rater) mais en ``without`` / ``with``,
  compilés ici à partir des événements connus ;
- l'appel passe par ``apply_run_guards`` et sa consommation est consignée dans
  ``llm_usage``, comme ``workflow_suggest``.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import date
from typing import Any

from fastapi import HTTPException, status

from apowerb.core.workflow_suggest import _record_usage

logger = logging.getLogger(__name__)

USAGE_AGENT_NAME = "forecast_interpret"
# Mêmes plafonds que th2forecast : une requête qui les dépasse y serait refusée.
MAX_EVENTS = 20
MAX_RANGES = 400
MAX_SCENARIOS = 5
MAX_ADJUSTMENTS = 20
MAX_NAME = 60

SYSTEM = """Tu traduis le contexte métier d'une prévision, écrit en français, en JSON.

Entrée (JSON) : "text" (le contexte), "history_start" et "history_end" (dates de
l'historique connu), "horizon_end" (dernière date prévue), "frequency", "groups"
(séries disponibles, vide si une seule), "known_events" (événements déjà déclarés).

Réponds par un objet JSON et rien d'autre :
{
  "events": [{"name": "promo", "ranges": [{"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}], "groups": ["..."]}],
  "scenarios": [{"name": "Sans promo", "without": ["promo"], "with": [], "adjustments": []}],
  "off_topic": false,
  "notes": ["..."]
}

Règles :
- Un événement = un fait daté qui change le niveau de la série (promotion, fermeture,
  soldes, férié local, rupture, campagne). Donne TOUTES ses occurrences entre
  history_start et horizon_end, passées comme futures : l'effet s'apprend sur le passé.
- Un nom court en minuscules ; un même fait récurrent = un seul événement avec
  plusieurs plages. "groups" seulement si le texte vise certaines séries, avec
  exactement leurs noms.
- Un scénario répond à « et si… ». "without" retire des événements connus ou
  déclarés ici ; "with" ajoute des plages futures à un événement qui a des
  précédents (même nom) ; "adjustments" impose un effet chiffré sur des dates :
  {"start", "end", "percent": -20} ou {"start", "end", "add": 150}, jamais les deux.
- N'invente rien : si une date ou une ampleur n'est pas dite ou déductible, ne la
  mets pas et explique-le dans "notes". Dates hors de [history_start, horizon_end] : ignore-les.
- Si le texte ne décrit aucun contexte de prévision, renvoie "off_topic": true et des listes vides.
"""


class InterpretUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def interpret_enabled() -> bool:
    """Activée par l'opérateur ET servie par le modèle mutualisé."""
    from apowerb.configs.settings import get_settings
    from apowerb.core.agent_helpers.default_llm import default_llm_available

    return bool(getattr(get_settings(), "forecast_interpret_enabled", False)) and default_llm_available()


def _completion_kwargs(messages: list[dict]) -> dict:
    from apowerb.configs.settings import get_settings
    from apowerb.core import workflow_suggest

    settings = get_settings()
    # Le modèle énumère des dates (jusqu'à des centaines) : plus de jetons et de
    # délai que la suggestion de nœud, et le modèle mutualisé, pas celui de l'éditeur.
    return workflow_suggest._completion_kwargs(
        messages,
        model=settings.default_llm_model,
        max_tokens=4000,
        timeout=settings.forecast_interpret_timeout_s,
    )


class _Checker:
    """Relit la sortie du modèle ; ce qui ne passe pas est écarté avec sa raison."""

    def __init__(self, lo: date, hi: date, groups: set[str]):
        self.lo, self.hi, self.groups = lo, hi, groups
        self.rejected: list[dict] = []

    def reject(self, what: str, reason: str) -> None:
        self.rejected.append({"item": what, "reason": reason})

    def _date(self, value: Any) -> date | None:
        try:
            return date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
        except ValueError:
            return None

    def span(self, raw: Any, what: str) -> tuple[date, date] | None:
        if not isinstance(raw, dict):
            self.reject(what, "plage mal formée")
            return None
        start, end = self._date(raw.get("start")), self._date(raw.get("end", raw.get("start")))
        if start is None or end is None:
            self.reject(what, "date invalide : %s → %s" % (raw.get("start"), raw.get("end")))
            return None
        if end < start:
            self.reject(what, "plage à l'envers : %s → %s" % (start, end))
            return None
        if start < self.lo or end > self.hi:
            self.reject(what, "%s → %s hors de la période %s → %s" % (start, end, self.lo, self.hi))
            return None
        return start, end

    def name(self, raw: Any, what: str) -> str | None:
        if not isinstance(raw, str) or not raw.strip():
            self.reject(what, "nom manquant")
            return None
        return raw.strip()[:MAX_NAME]

    def event(self, raw: Any, what: str) -> dict | None:
        if not isinstance(raw, dict):
            self.reject(what, "événement mal formé")
            return None
        name = self.name(raw.get("name"), what)
        if name is None:
            return None
        ranges = raw.get("ranges")
        ranges = ranges if isinstance(ranges, list) else []
        if len(ranges) > MAX_RANGES:
            self.reject("%s « %s »" % (what, name), "au plus %d plages" % MAX_RANGES)
            return None
        before = len(self.rejected)
        spans = [s for i, r in enumerate(ranges) if (s := self.span(r, "%s « %s » plage %d" % (what, name, i + 1)))]
        if not spans:
            # Chaque plage écartée a déjà sa raison ; sans plage du tout, on le dit.
            if len(self.rejected) == before:
                self.reject("%s « %s »" % (what, name), "aucune plage de dates")
            return None
        out: dict = {"name": name, "ranges": [{"start": a.isoformat(), "end": b.isoformat()} for a, b in sorted(set(spans))]}
        groups = raw.get("groups")
        if isinstance(groups, list) and groups:
            unknown = sorted({str(g) for g in groups} - self.groups)
            if unknown:
                self.reject("%s « %s »" % (what, name), "série(s) inconnue(s) : %s" % ", ".join(unknown))
                return None
            out["groups"] = sorted({str(g) for g in groups})
        return out

    def adjustment(self, raw: Any, what: str) -> dict | None:
        span = self.span(raw, what)
        if span is None:
            return None
        values = {k: raw.get(k) for k in ("percent", "add") if raw.get(k) is not None}
        if len(values) != 1:
            self.reject(what, "indiquer soit un pourcentage, soit une valeur")
            return None
        (key, value), = values.items()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            self.reject(what, "%s non numérique : %r" % (key, value))
            return None
        if key == "percent" and not -100 < value <= 1000:
            self.reject(what, "pourcentage hors de ]-100, 1000] : %s" % value)
            return None
        return {"start": span[0].isoformat(), "end": span[1].isoformat(), key: float(value)}


def known_events(raw: list[dict], *, groups: list[str]) -> list[dict]:
    """Événements déjà relus par l'utilisateur (config du graphique), mis en forme.

    Leur forme est vérifiée, pas leurs dates : l'horizon a pu changer depuis, et
    th2forecast ignore ce qui tombe hors de la série.
    """
    c = _Checker(date.min, date.max, set(groups))
    out = []
    for i, r in enumerate(raw):
        if isinstance(r, dict) and r.get("dates") and not r.get("ranges"):
            r = {**r, "ranges": [{"start": d, "end": d} for d in r["dates"] if isinstance(d, str)]}
        e = c.event(r, "événement déclaré %d" % (i + 1))
        if e is not None:
            out.append(e)
    if c.rejected:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            {"code": "KNOWN_EVENTS_INVALID", "rejected": c.rejected})
    return out


def _merge(known: list[dict], found: list[dict]) -> dict[str, dict]:
    """Événements connus puis trouvés ; un nom déjà connu reçoit les plages en plus."""
    merged: dict[str, dict] = {}
    spans: dict[str, set[tuple[str, str]]] = {}
    for e in known + found:
        merged.setdefault(e["name"], e)
        spans.setdefault(e["name"], set()).update((r["start"], r["end"]) for r in e["ranges"])
    return {k: {**v, "ranges": [{"start": a, "end": b} for a, b in sorted(spans[k])]} for k, v in merged.items()}


def check(content: Any, *, history_start: date, horizon_end: date, groups: list[str],
          known_events: list[dict]) -> dict:
    """Sortie du modèle → ``{events, scenarios, rejected, notes, off_topic}`` au format th2forecast."""
    try:
        data = json.loads(content) if isinstance(content, str) else None
    except json.JSONDecodeError:
        data = None
    if not isinstance(data, dict):
        raise InterpretUnavailable("bad_output")

    c = _Checker(history_start, horizon_end, set(groups))
    raw_events = data.get("events") if isinstance(data.get("events"), list) else []
    found = [e for i, r in enumerate(raw_events[:MAX_EVENTS]) if (e := c.event(r, "événement %d" % (i + 1)))]
    for i in range(MAX_EVENTS, len(raw_events)):
        c.reject("événement %d" % (i + 1), "au plus %d événements" % MAX_EVENTS)
    base = _merge(known_events, found)
    if len(base) > MAX_EVENTS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY,
                            {"code": "CONTEXT_TOO_MANY_EVENTS", "max": MAX_EVENTS})

    scenarios = []
    raw_scenarios = data.get("scenarios") if isinstance(data.get("scenarios"), list) else []
    for i, raw in enumerate(raw_scenarios[:MAX_SCENARIOS]):
        what = "scénario %d" % (i + 1)
        if not isinstance(raw, dict):
            c.reject(what, "scénario mal formé")
            continue
        name = c.name(raw.get("name"), what)
        if name is None:
            continue
        what = "scénario « %s »" % name
        without = {str(n) for n in raw.get("without") or [] if isinstance(n, (str, int))}
        for n in sorted(without - base.keys()):
            c.reject(what, "« %s » n'est pas un événement connu" % n)
        future = {k: dict(v) for k, v in base.items() if k not in without}
        for j, w in enumerate(raw.get("with") or []):
            e = c.event(w, "%s ajout %d" % (what, j + 1))
            if e is None:
                continue
            if e["name"] not in base:
                c.reject("%s « %s »" % (what, e["name"]),
                         "aucun précédent dans l'historique : son effet ne peut pas être appris, utiliser un ajustement")
                continue
            future[e["name"]] = _merge([future.get(e["name"], {**e, "ranges": []})], [e])[e["name"]]
        adjustments = [a for j, r in enumerate((raw.get("adjustments") or [])[:MAX_ADJUSTMENTS])
                       if (a := c.adjustment(r, "%s ajustement %d" % (what, j + 1)))]
        changed = future != base
        if not changed and not adjustments:
            c.reject(what, "ne change rien à la prévision de base")
            continue
        scenario: dict = {"name": name}
        if changed:
            scenario["events"] = list(future.values())
        if adjustments:
            scenario["adjustments"] = adjustments
        scenarios.append(scenario)

    notes = [str(n)[:300] for n in data.get("notes") or [] if isinstance(n, str)][:10]
    return {
        "events": found,
        "scenarios": scenarios,
        "rejected": c.rejected,
        "notes": notes,
        "off_topic": bool(data.get("off_topic")) and not found and not scenarios,
    }


async def interpret(
    text: str,
    *,
    owner_id: str,
    history_start: date,
    history_end: date,
    horizon_end: date,
    frequency: str,
    groups: list[str],
    known_events: list[dict],
) -> dict:
    """Événements et scénarios tirés de ``text``, déjà relus.

    Lève ``HTTPException`` 404 si la fonction est éteinte, 402 si le plafond est
    atteint, 503 si le modèle ne répond pas utilement, 422 si le texte ne décrit
    aucun contexte exploitable.
    """
    if not interpret_enabled():
        raise HTTPException(status.HTTP_404_NOT_FOUND, {"code": "INTERPRET_DISABLED"})

    from apowerb.core.run_gate import apply_run_guards, resolve_owner_plan

    await apply_run_guards(
        agent_name=USAGE_AGENT_NAME,
        owner_id=owner_id,
        plan=await resolve_owner_plan(owner_id),
    )
    view = {
        "text": text,
        "history_start": history_start.isoformat(),
        "history_end": history_end.isoformat(),
        "horizon_end": horizon_end.isoformat(),
        "frequency": frequency,
        "groups": groups,
        "known_events": [{"name": e["name"], "occurrences": len(e["ranges"])} for e in known_events],
    }
    kwargs = _completion_kwargs(
        [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": json.dumps(view, ensure_ascii=False)},
        ]
    )

    import litellm

    try:
        response = await litellm.acompletion(**kwargs)
    except Exception as exc:  # noqa: BLE001 - délai, fournisseur, réseau
        reason = "timeout" if isinstance(exc, litellm.Timeout) else "error"
        logger.warning("[INTERPRET] modèle indisponible (%s) : %s", reason, exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"code": "INTERPRET_UNAVAILABLE", "reason": reason},
        ) from exc
    await _record_usage(owner_id, kwargs["model"], getattr(response, "usage", None), agent_name=USAGE_AGENT_NAME)

    try:
        content = response.choices[0].message.content
    except (AttributeError, IndexError, TypeError):
        content = None
    try:
        result = check(content, history_start=history_start, horizon_end=horizon_end,
                       groups=groups, known_events=known_events)
    except InterpretUnavailable as exc:
        logger.warning("[INTERPRET] réponse du modèle inutilisable")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            {"code": "INTERPRET_UNAVAILABLE", "reason": exc.reason},
        ) from exc
    if not result["events"] and not result["scenarios"]:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            {
                "code": "CONTEXT_OFF_TOPIC" if result["off_topic"] else "CONTEXT_NOT_UNDERSTOOD",
                "rejected": result["rejected"],
                "notes": result["notes"],
            },
        )
    return result
