"""Inputs for building every dashboard template module the same way in tests.

Two cases per template: the required columns only, and every column filled
(the declared default, or the slot key itself), with the default parameters.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from apowerb.bi.spec import (
    catalog,
    templates_cash,
    templates_construction,
    templates_ecommerce,
    templates_finance,
    templates_forecast,
    templates_hr,
    templates_otif,
    templates_purchasing,
    templates_quality,
    templates_receivables,
    templates_restaurant,
    templates_sales,
    templates_services,
    templates_support,
)

DATASET_ID = "11111111-2222-3333-4444-555555555555"
GOLDEN_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "bi_templates_fr"

MODULES = (
    templates_sales,
    templates_forecast,
    templates_otif,
    templates_quality,
    templates_receivables,
    templates_purchasing,
    templates_support,
    templates_finance,
    templates_cash,
    templates_hr,
    templates_services,
    templates_ecommerce,
    templates_restaurant,
    templates_construction,
)


def _module_entry(module: Any) -> tuple[str, list[dict], list[dict], Any]:
    d = module.DESCRIPTOR
    return d["id"], d["columns"], d.get("params", []), module.build


def _stock_entry() -> tuple[str, list[dict], list[dict], Any]:
    t = catalog.get_template("stock_pilotage")
    assert t is not None
    columns = [{"key": c.key, "required": c.required, "default": c.default} for c in t.columns]
    params = [{"key": p.key, "default": p.default} for p in t.params]
    return t.id, columns, params, t.build


def entries() -> list[tuple[str, list[dict], list[dict], Any]]:
    return [_stock_entry(), *(_module_entry(m) for m in MODULES)]


TEMPLATE_IDS = [e[0] for e in entries()]
CASES = ("required", "full")


def inputs(columns: list[dict], params: list[dict], case: str) -> tuple[dict, dict]:
    mapping: dict[str, str | None] = {}
    for c in columns:
        if c["required"]:
            mapping[c["key"]] = c.get("default") or c["key"]
        else:
            mapping[c["key"]] = (c.get("default") or c["key"]) if case == "full" else None
    return mapping, {p["key"]: p["default"] for p in params}


def build_case(template_id: str, case: str, **kwargs: Any) -> dict:
    for tid, columns, params, build in entries():
        if tid == template_id:
            mapping, values = inputs(columns, params, case)
            return build(DATASET_ID, mapping, values, **kwargs)
    raise KeyError(template_id)


def golden_path(template_id: str, case: str) -> Path:
    return GOLDEN_DIR / f"{template_id}.{case}.json"


def write_goldens() -> None:
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    for tid in TEMPLATE_IDS:
        for case in CASES:
            spec = build_case(tid, case)
            golden_path(tid, case).write_text(json.dumps(spec, ensure_ascii=False, indent=1, sort_keys=True) + "\n")


if __name__ == "__main__":
    write_goldens()
