"""Suivi en boucle fermée des prévisions (contrat étape 5 §3).

Fonctions pures — aucune I/O, aucune dépendance FastAPI/SQLAlchemy — pour
rester testables isolément. La route (``routers/forecast.py``) les appelle
autour du relais th2forecast et du stockage des instantanés
(``forecast_snapshot_store.py``).

Vocabulaire :
- un « instantané » (snapshot) est une réponse de prévision passée, gardée en
  base avec la date de fin de son historique d'entraînement (``history_end``)
  et le hash de sa configuration (``config_hash``) ;
- le « suivi » compare, pour chaque point réel apparu depuis, la valeur
  réellement observée à ce qu'un instantané antérieur avait prévu pour cette
  même date ;
- les actuels viennent de la RÉPONSE du moteur (``series[].history``), pas de
  ``data`` brut de la requête : th2forecast y régularise les dates (toujours
  ISO ``YYYY-MM-DD``) et stringifie ``group`` — comparer contre ``data`` brut
  ferait échouer le rapprochement sur un format de date différent
  (``2024/01/05``, avec heure...) ou un groupe numérique.
- une série est identifiée par (group, level) : avec une hiérarchie, un
  agrégat ("Total", un niveau intermédiaire) et une série du bas peuvent
  porter le même `group` sous des `level` différents, et ne doivent pas se
  mélanger.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Champs qui définissent la configuration d'une prévision : deux réponses
# avec la même valeur pour tous ces champs sont comparables terme à terme.
# `data` (les lignes brutes) n'y entre pas : seule la config qui a produit la
# prévision compte, pas l'historique fourni pour l'obtenir.
_CONFIG_HASH_FIELDS = (
    "date_var",
    "target_var",
    "group_var",
    "frequency",
    "horizon",
    "models",
    "confidence_levels",
    "hierarchy",
    "reconciliation",
    "events",
    "scenarios",
)

# Nombre de ruptures gardées dans la réponse, la plus récente d'abord.
_MAX_BREACHES = 20

SeriesKey = tuple[Any, Any]  # (group, level) — level est None hors hiérarchie.


def compute_config_hash(payload: dict[str, Any]) -> str:
    """Hash stable (sha256, hex) de la configuration d'une requête.

    Indépendant de l'ordre des clés et du type exact des valeurs (les listes
    sont sérialisées telles quelles ; `sort_keys` gère l'objet englobant).
    """
    normalized = {field: payload.get(field) for field in _CONFIG_HASH_FIELDS}
    encoded = json.dumps(normalized, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def prunable_snapshot_payload(result: dict[str, Any]) -> dict[str, Any]:
    """Ce qui est réellement stocké dans l'instantané (contrat étape 5 §3) :
    par série group/level/model/forecast seulement — ni `history`, ni
    métriques, ni avertissements. Une réponse peut porter jusqu'à 100 000
    lignes d'historique par série ; les rejouer à chaque instantané ferait
    exploser le stockage pour rien, `history` ne sert qu'à la comparaison du
    moment, jamais relue depuis un instantané passé."""
    series = []
    for s in result.get("series", []):
        series.append(
            {
                "group": s.get("group"),
                "level": s.get("level"),
                "model": s.get("model"),
                "forecast": s.get("forecast", []),
            }
        )
    return {"series": series}


def _series_key(series: dict[str, Any]) -> SeriesKey:
    return (series.get("group"), series.get("level"))


def _forecast_point(snapshot: dict[str, Any], key: SeriesKey, date: str) -> dict[str, Any] | None:
    """Le point prévu par `snapshot` pour la série `key` à `date`, ou None."""
    payload = snapshot.get("payload") or {}
    for series in payload.get("series", []):
        if _series_key(series) == key:
            for point in series.get("forecast", []):
                if point.get("date") == date:
                    return point
    return None


def _confidence_levels(comparisons: list[tuple]) -> list[str]:
    """Les suffixes de niveau ("80", "95"...) présents sur les points comparés,
    du plus étroit au plus large."""
    levels = {
        key[len("lower_"):]
        for _, _, _, point in comparisons
        for key in point
        if key.startswith("lower_") and point.get(f"upper_{key[len('lower_'):]}") is not None
    }
    return sorted(levels, key=lambda s: float(s))


def compute_tracking(
    *,
    series: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    config_hash: str,
) -> dict[str, Any]:
    """Le champ `tracking` de la réponse : compare l'historique régularisé
    renvoyé par le moteur (``series[].history``) aux instantanés antérieurs
    de même configuration.

    ``series`` : la liste `series` de la réponse th2forecast COURANTE, chaque
    entrée portant ``group``, ``level`` (optionnel), ``history`` (``[{"date":
    "YYYY-MM-DD", "value": ...}, ...]``, dates déjà régularisées en ISO par
    le moteur) et ``forecast``.
    ``snapshots`` : liste de dicts ``{"config_hash": ..., "history_end": ...,
    "payload": {...instantané déjà élagué (prunable_snapshot_payload)...}}``.
    ``history_end`` et les dates d'historique doivent être comparables comme
    des chaînes ISO (``YYYY-MM-DD``) ou des ``date`` : peu importe, tant que
    le type est homogène.
    """
    empty = {"points": 0, "since": None, "coverage": {}, "mase": None, "breaches": [], "latest_breach": False}

    relevant = [s for s in snapshots if s.get("config_hash") == config_hash]
    if not relevant:
        return empty

    # Historique complet par série (pour MASE) et points comparables.
    history_by_key: dict[SeriesKey, list[tuple[str, float]]] = {}
    comparisons: list[tuple[SeriesKey, str, float, dict[str, Any]]] = []
    for s in series:
        key = _series_key(s)
        history_points = [
            (point["date"], float(point["value"]))
            for point in (s.get("history") or [])
            if point.get("date") is not None and isinstance(point.get("value"), (int, float))
        ]
        history_by_key[key] = history_points

        for date, actual in history_points:
            candidates = [snap for snap in relevant if snap["history_end"] < date]
            if not candidates:
                continue
            snapshot = max(candidates, key=lambda snap: snap["history_end"])
            point = _forecast_point(snapshot, key, date)
            if point is None or not isinstance(point.get("value"), (int, float)):
                continue
            comparisons.append((key, date, actual, point))

    if not comparisons:
        return empty

    levels = _confidence_levels(comparisons)

    coverage: dict[str, float] = {}
    for level in levels:
        lower_key, upper_key = f"lower_{level}", f"upper_{level}"
        scoreable = [
            (actual, point) for _, _, actual, point in comparisons
            if point.get(lower_key) is not None and point.get(upper_key) is not None
        ]
        if scoreable:
            hits = sum(1 for actual, point in scoreable if point[lower_key] <= actual <= point[upper_key])
            coverage[level] = round(hits / len(scoreable), 4)

    # MASE : par série, erreur absolue moyenne / moyenne des |diff| de son
    # historique complet (renvoyé par le moteur), puis moyenne (non
    # pondérée) entre séries.
    errors_by_key: dict[SeriesKey, list[float]] = {}
    for key, _, actual, point in comparisons:
        errors_by_key.setdefault(key, []).append(abs(actual - point["value"]))

    per_series_mase: list[float] = []
    for key, errors in errors_by_key.items():
        history = sorted(history_by_key.get(key, []), key=lambda pair: pair[0])
        diffs = [abs(history[i][1] - history[i - 1][1]) for i in range(1, len(history))]
        naive_error = (sum(diffs) / len(diffs)) if diffs else 0.0
        if naive_error > 0:
            per_series_mase.append((sum(errors) / len(errors)) / naive_error)
    mase = round(sum(per_series_mase) / len(per_series_mase), 4) if per_series_mase else None

    # Ruptures : hors bande la plus large -> ce niveau ; sinon hors bande la
    # plus étroite -> ce niveau ; sinon rien. 20 plus récentes d'abord.
    breaches: list[dict[str, Any]] = []
    widest, narrowest = levels[-1], levels[0]
    for key, date, actual, point in comparisons:
        level_hit = None
        lower_w, upper_w = point.get(f"lower_{widest}"), point.get(f"upper_{widest}")
        if lower_w is not None and upper_w is not None and not (lower_w <= actual <= upper_w):
            level_hit = widest
        elif widest != narrowest:
            lower_n, upper_n = point.get(f"lower_{narrowest}"), point.get(f"upper_{narrowest}")
            if lower_n is not None and upper_n is not None and not (lower_n <= actual <= upper_n):
                level_hit = narrowest
        if level_hit is None:
            continue
        lower, upper = point[f"lower_{level_hit}"], point[f"upper_{level_hit}"]
        breaches.append(
            {
                "group": key[0],
                "date": date,
                "actual": actual,
                "value": point["value"],
                "lower": lower,
                "upper": upper,
                "level": level_hit,
                "direction": "above" if actual > upper else "below",
            }
        )

    latest_date = max(date for _, date, _, _ in comparisons)
    latest_breach = any(b["date"] == latest_date for b in breaches)

    breaches.sort(key=lambda b: b["date"], reverse=True)

    return {
        "points": len(comparisons),
        "since": min(date for _, date, _, _ in comparisons),
        "coverage": coverage,
        "mase": mase,
        "breaches": breaches[:_MAX_BREACHES],
        "latest_breach": latest_breach,
    }
