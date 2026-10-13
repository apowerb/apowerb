"""Shared checks for the spec v2 template tests: grid, references and a real engine run."""

from __future__ import annotations

import asyncio
import re

import pandas as pd

from apowerb.bi.spec.models import DashboardDataRequest
from tests.helpers.bi_template_env import dashboard_from_spec

_TOKEN = re.compile(r"\{\{\s*([a-z_][a-z0-9_]*)")


def compute(monkeypatch, spec: dict, df: pd.DataFrame, filters: dict | None = None):
    import apowerb.bi.spec.sources as sources
    from apowerb.bi.spec.engine import compute_dashboard

    async def fake_load(dataset_id, owner, sheet=None):
        return df

    monkeypatch.setattr(sources, "load_dataset", fake_load)
    dash = dashboard_from_spec(spec)
    resp = asyncio.run(compute_dashboard(dash, DashboardDataRequest(filters=filters or {}), owner="alice@example.com"))
    return dash, resp


def errors(resp) -> dict:
    return {cid: r.error for cid, r in resp.components.items() if r.error}


def kpis(dash, resp) -> dict:
    return {c.visual.title: resp.components[c.id].value for c in dash.components if c.visual.type == "kpi"}


def component(dash, resp, title: str):
    comp = next(c for c in dash.components if c.visual.title == title)
    return comp.visual, resp.components[comp.id]


def titles(spec: dict) -> set[str | None]:
    return {c["visual"].get("title") for c in spec["components"]}


def assert_grid_ok(spec: dict) -> None:
    taken: dict[tuple[int, int], int] = {}
    for idx, comp in enumerate(spec["components"]):
        p = comp["position"]
        assert 0 <= p["col"] and p["col"] + p["width"] <= 12, p
        for r in range(p["row"], p["row"] + p["height"]):
            for c in range(p["col"], p["col"] + p["width"]):
                assert (r, c) not in taken, f"component {idx} overlaps {taken[(r, c)]}"
                taken[(r, c)] = idx


def assert_references_exist(spec: dict, group_keys: set[str]) -> None:
    (source,) = spec["sources"]
    measures = {m["name"] for m in source["measures"]}
    fields = {f["name"] for f in source["fields"]}
    assert not measures & fields
    for comp in spec["components"]:
        v = comp["visual"]
        if v["type"] == "note":
            continue
        assert v["source"] == source["name"]
        refs: set[str] = set()
        if v.get("kpi"):
            refs |= {v["kpi"]["measure"], *_TOKEN.findall(v["kpi"].get("secondary", ""))}
            if v["kpi"].get("sparkline"):
                refs.add(v["kpi"]["sparkline"])
        refs |= {s["key"] for s in v.get("series", [])}
        refs |= {c["key"] for c in v.get("columns", [])} - group_keys
        if v["type"] == "text":
            refs |= {r for r in _TOKEN.findall(v["text"]) if r not in ("filters", "period")}
        assert refs <= measures | fields, (v.get("title"), refs - measures - fields)
