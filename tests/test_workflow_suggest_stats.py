"""Adoption des pastilles « Étape suivante », règles contre IA (roadmap#88).

Sur une vraie base SQLite, comme ``test_workflow_defs_api.py`` : ce qu'on
protège, c'est l'addition des totaux (l'upsert), le refus des événements
incohérents, et qu'aucune colonne ne permette de retrouver un utilisateur
ou son graphe.
"""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine, event, select
from sqlalchemy.pool import StaticPool

from apowerb.core import workflow_main as wm
from apowerb.core import workflow_suggest_stats as stats

TODAY = date(2026, 9, 25)


@pytest.fixture()
def engine(monkeypatch):
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def _attach(dbapi_connection, _record):  # pragma: no cover
        dbapi_connection.execute("ATTACH DATABASE ':memory:' AS public")

    monkeypatch.setattr(wm.workflow_store, "engine", engine)
    wm.workflow_store.metadata.create_all(engine)
    return engine


def _rows(engine):
    t = wm.workflow_store.suggest_table
    with engine.begin() as conn:
        return {
            (r.day, r.source, r.node_type): (r.steps, r.shown, r.accepted)
            for r in conn.execute(select(t))
        }


def _record(**kw):
    stats.record_event(stats.SuggestEvent(**kw), today=TODAY)


# --- Les totaux s'additionnent ------------------------------------------------


def test_events_add_up_per_day_source_and_type(engine):
    _record(rules_shown=3, accepted={"source": "rules", "type": "agent"})
    _record(rules_shown=2, ai_shown=2, accepted={"source": "ai", "type": "convert"})
    _record(rules_shown=3)
    _record(rules_shown=1, ai_shown=1, accepted={"source": "rules", "type": "agent"})

    assert _rows(engine) == {
        (TODAY.isoformat(), "rules", ""): (4, 9, 2),
        (TODAY.isoformat(), "ai", ""): (2, 3, 1),
        (TODAY.isoformat(), "rules", "agent"): (0, 0, 2),
        (TODAY.isoformat(), "ai", "convert"): (0, 0, 1),
    }


def test_the_totals_give_rates_and_most_accepted_types(engine):
    _record(rules_shown=3, accepted={"source": "rules", "type": "output"})
    _record(rules_shown=3, accepted={"source": "rules", "type": "agent"})
    _record(rules_shown=3, accepted={"source": "rules", "type": "agent"})
    _record(rules_shown=3)
    _record(rules_shown=1, ai_shown=2)

    view = stats.read_totals(days=7, today=TODAY)

    assert (view["days"], view["since"]) == (7, "2026-09-19")
    rules, ai = view["sources"]["rules"], view["sources"]["ai"]
    assert (rules["steps"], rules["shown"], rules["accepted"]) == (5, 13, 3)
    assert rules["acceptance_rate"] == 0.6
    assert rules["top_types"] == [
        {"type": "agent", "accepted": 2},
        {"type": "output", "accepted": 1},
    ]
    assert (ai["steps"], ai["shown"], ai["accepted"], ai["acceptance_rate"]) == (1, 2, 0, 0.0)
    assert ai["top_types"] == []


def test_the_totals_only_count_the_requested_days(engine):
    stats.record_event(stats.SuggestEvent(rules_shown=2), today=TODAY - timedelta(days=7))
    _record(rules_shown=3)

    assert stats.read_totals(days=7, today=TODAY)["sources"]["rules"]["shown"] == 3
    assert stats.read_totals(days=8, today=TODAY)["sources"]["rules"]["shown"] == 5
    assert stats.read_totals(days=7, today=TODAY)["sources"]["ai"] == {
        "steps": 0, "shown": 0, "accepted": 0, "acceptance_rate": None, "top_types": []
    }


# --- Un événement incohérent est refusé ---------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {},  # rien d'affiché : pas une étape
        {"rules_shown": 0, "ai_shown": 0},
        {"rules_shown": 4},  # l'éditeur n'en montre jamais plus de 3
        {"ai_shown": 3},  # ni plus de 2 venant du modèle
        {"rules_shown": -1, "ai_shown": 1},
        {"rules_shown": 2, "accepted": {"source": "ai", "type": "agent"}},  # aucune IA montrée
        {"rules_shown": 2, "accepted": {"source": "rules", "type": "not_a_node"}},
        {"rules_shown": 2, "accepted": {"source": "rules", "type": "trigger"}},
        {"rules_shown": 2, "accepted": {"source": "model", "type": "agent"}},
        {"rules_shown": 2, "owner_id": "alice@acme.fr"},  # aucun champ en plus
        {"rules_shown": 2, "accepted": {"source": "rules", "type": "agent", "label": "Tri"}},
    ],
)
def test_an_incoherent_event_is_refused(bad):
    with pytest.raises(ValidationError):
        stats.SuggestEvent(**bad)


# --- Rien ne permet de retrouver l'utilisateur ni son graphe ------------------


def test_the_table_holds_counts_only():
    assert set(wm.workflow_store.suggest_table.columns.keys()) == {
        "day", "source", "node_type", "steps", "shown", "accepted"
    }


# --- Routes ---------------------------------------------------------------------


@pytest.fixture()
def api(engine):
    from apowerb.auth.dependencies import get_current_user
    from apowerb.routers import workflow_defs

    user = MagicMock()
    user.email, user.user_id, user.role = "alice@acme.fr", 1, "USER"
    app = FastAPI()
    app.include_router(workflow_defs.router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: user
    return TestClient(app), user, app


def test_the_editor_posts_one_event_per_step(api, engine):
    client, _, _ = api
    r = client.post(
        "/api/workflows/defs/suggest-events",
        json={"rules_shown": 3, "ai_shown": 2, "accepted": {"source": "ai", "type": "notification"}},
    )
    assert r.status_code == 204
    today = datetime.now(timezone.utc).date().isoformat()
    assert _rows(engine) == {
        (today, "rules", ""): (1, 3, 0),
        (today, "ai", ""): (1, 2, 1),
        (today, "ai", "notification"): (0, 0, 1),
    }

    assert client.post("/api/workflows/defs/suggest-events", json={"rules_shown": 0}).status_code == 422
    assert len(_rows(engine)) == 3


def test_posting_requires_a_signed_in_user(api, engine):
    from apowerb.auth.dependencies import get_current_user

    client, _, app = api
    del app.dependency_overrides[get_current_user]
    r = client.post("/api/workflows/defs/suggest-events", json={"rules_shown": 3})
    assert r.status_code in (401, 403)
    assert _rows(engine) == {}


def test_the_totals_are_for_administrators_only(api):
    client, user, _ = api
    client.post("/api/workflows/defs/suggest-events", json={"rules_shown": 3})
    assert client.get("/api/workflows/defs/suggest-stats").status_code == 403

    user.role = "ADMIN"
    r = client.get("/api/workflows/defs/suggest-stats?days=1")
    assert r.status_code == 200
    body = r.json()
    assert body["days"] == 1
    assert (body["sources"]["rules"]["steps"], body["sources"]["rules"]["shown"]) == (1, 3)
    assert client.get("/api/workflows/defs/suggest-stats?days=0").status_code == 422
    assert client.get("/api/workflows/defs/suggest-stats?days=366").status_code == 422
