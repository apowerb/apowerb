"""Titres de tableaux de bord uniques a la creation par l'agent : « X », « X (2) », « X (3) »...
Le store simule la contrainte d'unicite (nom, proprietaire) de la base."""

import contextlib
import json

import pytest

import apowerb.tools_store.portfolio.bi_dashboard_tools as t
from apowerb.bi.dashboards.service import DashboardNameConflictError

ALICE = "alice@example.com"
BOB = "bob@example.com"

SPEC = {
    "sources": [{"name": "stocks", "kind": "dataset", "dataset_id": "ds-1"}],
    "filters": [],
    "components": [],
}


class UniqueViolation(Exception):
    pass


class UniqueStore:
    def __init__(self, rows, owner):
        self.rows, self.owner = rows, owner

    async def save(self, dashboard):
        if any(o == self.owner and d.title == dashboard.title for o, d in self.rows.values()):
            raise UniqueViolation("duplicate key value violates unique constraint")
        self.rows[dashboard.id] = (self.owner, dashboard)
        return dashboard


@pytest.fixture
def rows(monkeypatch):
    table = {}
    @contextlib.asynccontextmanager
    async def store(who):
        yield UniqueStore(table, who)

    async def no_preview(*a, **k):
        return {"components": [], "errors": {}, "filters": {}}

    monkeypatch.setattr(t, "_dashboard_store", store)
    monkeypatch.setattr(t, "_preview", no_preview)
    monkeypatch.setattr(t, "_validate", lambda d: [])
    monkeypatch.setattr(t, "_agent_owner", lambda: ALICE)
    return table


def _titles(rows, who=ALICE):
    return sorted(d.title for o, d in rows.values() if o == who)


def _create(title="Pilotage"):
    return t.tool_bi_create_dashboard(title, json.dumps(SPEC))


def test_successive_creations_get_numbered_titles_in_the_store(rows):
    first, second, third = _create(), _create(), _create()
    assert [r["success"] for r in (first, second, third)] == [True, True, True]
    assert [r["title"] for r in (first, second, third)] == ["Pilotage", "Pilotage (2)", "Pilotage (3)"]
    assert _titles(rows) == ["Pilotage", "Pilotage (2)", "Pilotage (3)"]
    assert "renamed_from" not in first
    assert second["renamed_from"] == "Pilotage" and "Pilotage (2)" in second["message"]
    assert rows[second["dashboard_id"]][1].title == "Pilotage (2)"


def test_other_owner_keeps_the_plain_title(rows, monkeypatch):
    _create()
    monkeypatch.setattr(t, "_agent_owner", lambda: BOB)
    res = _create()
    assert res["title"] == "Pilotage" and "renamed_from" not in res


def test_gives_up_cleanly_after_twenty_attempts(rows):
    for _ in range(20):
        assert _create()["success"] is True
    res = _create()
    assert res["success"] is False and "already exists" in res["error"]
    assert len(_titles(rows)) == 20


def test_stock_dashboard_is_deduplicated_too(rows):
    a = t.tool_bi_create_stock_dashboard("ds-1")
    b = t.tool_bi_create_stock_dashboard("ds-1")
    assert (a["title"], b["title"]) == ("Pilotage stocks", "Pilotage stocks (2)")
    assert _titles(rows) == ["Pilotage stocks", "Pilotage stocks (2)"]


def test_other_errors_are_not_retried(rows, monkeypatch):
    calls = []

    @contextlib.asynccontextmanager
    async def broken(who):
        calls.append(1)
        raise RuntimeError("connexion perdue")
        yield

    monkeypatch.setattr(t, "_dashboard_store", broken)
    res = _create()
    assert res["success"] is False and "connexion perdue" in res["error"] and len(calls) == 1


async def test_helper_truncates_base_to_the_title_limit():
    from apowerb.bi.dashboards.service import create_with_unique_title

    seen = []

    async def create(candidate):
        seen.append(candidate)
        if len(seen) < 2:
            raise DashboardNameConflictError(candidate)
        return candidate

    long_title = "x" * 120
    _, final = await create_with_unique_title(long_title, create)
    assert final.endswith(" (2)") and len(final) == 120


async def test_helper_raises_name_conflict_after_twenty():
    from apowerb.bi.dashboards.service import create_with_unique_title

    async def create(candidate):
        raise DashboardNameConflictError(candidate)

    with pytest.raises(DashboardNameConflictError):
        await create_with_unique_title("X", create)
