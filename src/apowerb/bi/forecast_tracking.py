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
  même date.
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


def compute_config_hash(payload: dict[str, Any]) -> str:
    """Hash stable (sha256, hex) de la configuration d'une requête.

    Indépendant de l'ordre des clés et du type exact des valeurs (les listes
    sont sérialisées telles quelles ; `sort_keys` gère l'objet englobant).
    """
    normalized = {field: payload.get(field) for field in _CONFIG_HASH_FIELDS}
    encoded = json.dumps(normalized, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _forecast_point(snapshot: dict[str, Any], group: Any, date: str) -> dict[str, Any] | None:
    """Le point prévu par `snapshot` pour (group, date), ou None."""
    payload = snapshot.get("payload") or {}
    for series in payload.get("series", []):
        if series.get("group") == group:
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
    data: list[dict[str, Any]],
    date_var: str,
    target_var: str,
    group_var: str | None,
    snapshots: list[dict[str, Any]],
    config_hash: str,
) -> dict[str, Any]:
    """Le champ `tracking` de la réponse : compare l'historique fourni dans la
    nouvelle requête aux instantanés antérieurs de même configuration.

    ``snapshots`` : liste de dicts ``{"config_hash": ..., "history_end": ...,
    "payload": {...réponse th2forecast...}}``. ``history_end`` et les dates de
    ``data`` doivent être comparables comme des chaînes ISO (``YYYY-MM-DD``)
    ou des ``date`` : peu importe, tant que le type est homogène.
    """
    empty = {"points": 0, "since": None, "coverage": {}, "mase": None, "breaches": [], "latest_breach": False}

    relevant = [s for s in snapshots if s.get("config_hash") == config_hash]
    if not relevant:
        return empty

    # Actuels par (group, date), et historique complet par groupe (pour MASE).
    actuals: list[tuple[Any, str, float]] = []
    for row in data:
        raw_date, raw_value = row.get(date_var), row.get(target_var)
        if raw_date is None or raw_value is None:
            continue
        group = row.get(group_var) if group_var else None
        actuals.append((group, raw_date, float(raw_value)))

    comparisons: list[tuple[Any, str, float, dict[str, Any]]] = []
    for group, date, actual in actuals:
        candidates = [s for s in relevant if s["history_end"] < date]
        if not candidates:
            continue
        snapshot = max(candidates, key=lambda s: s["history_end"])
        point = _forecast_point(snapshot, group, date)
        if point is None or not isinstance(point.get("value"), (int, float)):
            continue
        comparisons.append((group, date, actual, point))

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
    # historique complet, puis moyenne (non pondérée) entre séries.
    history_by_group: dict[Any, list[tuple[str, float]]] = {}
    for group, date, actual in actuals:
        history_by_group.setdefault(group, []).append((date, actual))
    errors_by_group: dict[Any, list[float]] = {}
    for group, _, actual, point in comparisons:
        errors_by_group.setdefault(group, []).append(abs(actual - point["value"]))

    per_series_mase: list[float] = []
    for group, errors in errors_by_group.items():
        history = sorted(history_by_group.get(group, []), key=lambda pair: pair[0])
        diffs = [abs(history[i][1] - history[i - 1][1]) for i in range(1, len(history))]
        naive_error = (sum(diffs) / len(diffs)) if diffs else 0.0
        if naive_error > 0:
            per_series_mase.append((sum(errors) / len(errors)) / naive_error)
    mase = round(sum(per_series_mase) / len(per_series_mase), 4) if per_series_mase else None

    # Ruptures : hors bande la plus large -> ce niveau ; sinon hors bande la
    # plus étroite -> ce niveau ; sinon rien. 20 plus récentes d'abord.
    breaches: list[dict[str, Any]] = []
    widest, narrowest = levels[-1], levels[0]
    for group, date, actual, point in comparisons:
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
                "group": group,
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
