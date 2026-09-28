"""POST /api/v1/forecast/interpret : texte libre -> événements et scénarios relus.

Aucun appel réseau ni base : le modèle (``litellm.acompletion``), le portier
(``apply_run_guards``) et l'enregistrement de consommation sont remplacés.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import litellm
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from apowerb.configs.settings import get_settings
from apowerb.core import run_gate
from apowerb.core.agent_helpers import usage_recorder

ALICE = "alice@acme.fr"
BODY = {
    "text": "Promo chaque 1er samedi du mois ; fermé du 1er au 15 août 2025. Et si on arrêtait les promos ?",
    "history_start": "2025-01-01",
    "history_end": "2025-10-31",
    "horizon_end": "2025-12-31",
    "frequency": "day",
    "groups": ["Lyon", "Paris"],
}
PROMO = {"name": "promo", "ranges": [{"start": "2025-02-01", "end": "2025-02-01"}, {"start": "2025-11-01"}]}
CLOSED = {"name": "fermeture", "ranges": [{"start": "2025-08-01", "end": "2025-08-15"}], "groups": ["Lyon"]}
NO_PROMO = {"name": "Sans promo", "without": ["promo"]}


def _response(payload, usage=(300, 90, 390)):
    content = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=usage[0], completion_tokens=usage[1], total_tokens=usage[2]),
    )


@pytest.fixture()
def env(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "forecast_interpret_enabled", True)
    monkeypatch.setattr(settings, "workflow_suggest_model", "gemini/gemini-lite")
    monkeypatch.setattr(settings, "default_llm_model", "gemini/gemini-test")
    monkeypatch.setattr(settings, "default_llm_api_key", "k-test")
    monkeypatch.setattr(settings, "default_llm_api_base", "")

    state = SimpleNamespace(
        calls=[], guards=[], usage=[],
        reply=_response({"events": [PROMO, CLOSED], "scenarios": [NO_PROMO], "notes": []}),
    )

    async def fake_completion(**kwargs):
        state.calls.append(kwargs)
        if isinstance(state.reply, Exception):
            raise state.reply
        return state.reply

    async def fake_guards(**kwargs):
        state.guards.append(kwargs)

    async def fake_plan(owner_id):
        return None

    async def fake_persist(**fields):
        state.usage.append(fields)

    monkeypatch.setattr(litellm, "acompletion", fake_completion)
    monkeypatch.setattr(run_gate, "apply_run_guards", fake_guards)
    monkeypatch.setattr(run_gate, "resolve_owner_plan", fake_plan)
    monkeypatch.setattr(usage_recorder, "_persist_usage_row", fake_persist)

    from apowerb.auth.dependencies import get_current_user
    from apowerb.routers import forecast

    user = MagicMock()
    user.email, user.user_id, user.role = ALICE, 1, "USER"
    app = FastAPI()
    app.include_router(forecast.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: user
    state.client = TestClient(app)
    state.settings = settings
    return state


def _interpret(env, **extra):
    return env.client.post("/api/v1/forecast/interpret", json={**BODY, **extra})


def test_off_by_default_and_the_model_is_never_called(env, monkeypatch):
    monkeypatch.setattr(env.settings, "forecast_interpret_enabled", False)
    r = _interpret(env)
    assert r.status_code == 404
    assert r.json()["detail"] == {"code": "INTERPRET_DISABLED"}
    assert env.calls == [] and env.guards == []


def test_events_and_scenarios_come_back_in_th2forecast_form_capped_and_counted(env):
    r = _interpret(env)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["events"] == [
        {"name": "promo", "ranges": [{"start": "2025-02-01", "end": "2025-02-01"},
                                     {"start": "2025-11-01", "end": "2025-11-01"}]},
        {"name": "fermeture", "ranges": [{"start": "2025-08-01", "end": "2025-08-15"}], "groups": ["Lyon"]},
    ]
    # « sans promo » = les événements futurs moins la promo, compilés ici.
    assert out["scenarios"] == [{"name": "Sans promo", "events": [out["events"][1]]}]
    assert out["rejected"] == [] and out["off_topic"] is False
    assert [g["agent_name"] for g in env.guards] == ["forecast_interpret"]
    assert [(u["agent_name"], u["total_tokens"], u["billed_to_thaink2"]) for u in env.usage] == [
        ("forecast_interpret", 390, True)
    ]
    call = env.calls[0]
    # Le modèle mutualisé, pas le modèle rapide de l'éditeur ; un délai et des jetons pour énumérer.
    assert call["model"] == "gemini/gemini-test"
    assert call["max_tokens"] == 4000 and call["timeout"] == env.settings.forecast_interpret_timeout_s
    view = json.loads(call["messages"][1]["content"])
    assert view["history_start"] == "2025-01-01" and view["horizon_end"] == "2025-12-31"
    assert view["groups"] == ["Lyon", "Paris"]


@pytest.mark.parametrize("bad, reason", [
    ({"name": "soldes", "ranges": [{"start": "2024-12-20", "end": "2024-12-31"}]}, "hors de la période"),
    ({"name": "soldes", "ranges": [{"start": "2026-01-10", "end": "2026-01-20"}]}, "hors de la période"),
    ({"name": "soldes", "ranges": [{"start": "2025-06-30", "end": "2025-06-01"}]}, "à l'envers"),
    ({"name": "soldes", "ranges": [{"start": "30/06/2025"}]}, "date invalide"),
    ({"name": "soldes", "ranges": [{"start": "2025-06-01"}], "groups": ["Marseille"]}, "série(s) inconnue(s) : Marseille"),
    ({"ranges": [{"start": "2025-06-01"}]}, "nom manquant"),
])
def test_an_invalid_event_is_dropped_with_its_reason_not_repaired(env, bad, reason):
    env.reply = _response({"events": [PROMO, bad], "scenarios": []})
    out = _interpret(env).json()
    assert [e["name"] for e in out["events"]] == ["promo"]
    assert any(reason in r["reason"] for r in out["rejected"]), out["rejected"]


@pytest.mark.parametrize("adjustment, reason", [
    ({"start": "2025-12-01", "end": "2025-12-07", "percent": -100}, "pourcentage hors de"),
    ({"start": "2025-12-01", "end": "2025-12-07", "percent": 10, "add": 5}, "soit un pourcentage"),
    ({"start": "2025-12-01", "end": "2025-12-07", "percent": "beaucoup"}, "non numérique"),
    ({"start": "2026-02-01", "end": "2026-02-07", "add": 100}, "hors de la période"),
])
def test_an_invalid_adjustment_is_dropped(env, adjustment, reason):
    env.reply = _response({"events": [PROMO], "scenarios": [{"name": "Noël", "adjustments": [adjustment]}]})
    out = _interpret(env).json()
    assert out["scenarios"] == []
    assert any(reason in r["reason"] for r in out["rejected"]), out["rejected"]


def test_a_valid_adjustment_and_a_future_occurrence_are_kept(env):
    env.reply = _response({"events": [PROMO], "scenarios": [
        {"name": "Promo en décembre", "with": [{"name": "promo", "ranges": [{"start": "2025-12-06"}]}]},
        {"name": "Noël +20 %", "adjustments": [{"start": "2025-12-15", "end": "2025-12-24", "percent": 20}]},
    ]})
    out = _interpret(env).json()
    first, second = out["scenarios"]
    assert first["events"][0]["ranges"][-1] == {"start": "2025-12-06", "end": "2025-12-06"}
    assert second == {"name": "Noël +20 %", "adjustments": [{"start": "2025-12-15", "end": "2025-12-24", "percent": 20.0}]}


def test_a_scenario_cannot_learn_an_event_without_history(env):
    env.reply = _response({"events": [PROMO], "scenarios": [
        {"name": "Ouverture", "with": [{"name": "ouverture", "ranges": [{"start": "2025-12-01"}]}]},
    ]})
    out = _interpret(env).json()
    assert out["scenarios"] == []
    assert any("aucun précédent" in r["reason"] for r in out["rejected"])


def test_known_events_are_merged_and_can_be_removed_by_a_scenario(env):
    known = [{"name": "soldes", "dates": ["2025-01-08", "2025-07-02"]}]
    env.reply = _response({"events": [], "scenarios": [{"name": "Sans soldes", "without": ["soldes"]}]})
    out = _interpret(env, events=known).json()
    assert out["events"] == []
    assert out["scenarios"] == [{"name": "Sans soldes", "events": []}]
    view = json.loads(env.calls[0]["messages"][1]["content"])
    assert view["known_events"] == [{"name": "soldes", "occurrences": 2}]


def test_off_topic_text_is_a_clear_422(env):
    env.reply = _response({"events": [], "scenarios": [], "off_topic": True, "notes": ["Recette de crêpes."]})
    r = _interpret(env, text="Donne-moi une recette de crêpes")
    assert r.status_code == 422
    assert r.json()["detail"] == {"code": "CONTEXT_OFF_TOPIC", "rejected": [], "notes": ["Recette de crêpes."]}
    assert len(env.usage) == 1  # la consommation reste comptée


def test_only_invalid_dates_is_a_422_with_the_reasons(env):
    env.reply = _response({"events": [{"name": "salon", "ranges": [{"start": "2023-03-01"}]}]})
    r = _interpret(env)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert detail["code"] == "CONTEXT_NOT_UNDERSTOOD"
    assert [r["reason"] for r in detail["rejected"]] == ["2023-03-01 → 2023-03-01 hors de la période 2025-01-01 → 2025-12-31"]


def test_request_is_checked_before_any_call(env):
    assert _interpret(env, horizon_end="2025-10-01").status_code == 422
    assert _interpret(env, frequency="hour").status_code == 422
    assert _interpret(env, text="x" * 2001).status_code == 422
    assert _interpret(env, events=[{"name": "", "dates": ["2025-01-01"]}]).status_code == 422
    assert env.calls == [] and env.guards == []


def test_cap_reached_refuses_before_the_model_is_called(env, monkeypatch):
    async def refuse(**kwargs):
        raise HTTPException(402, {"code": "TOKEN_QUOTA_EXCEEDED"})

    monkeypatch.setattr(run_gate, "apply_run_guards", refuse)
    r = _interpret(env)
    assert r.status_code == 402
    assert env.calls == [] and env.usage == []


def test_slow_model_and_unusable_output_are_503(env):
    env.reply = litellm.Timeout("trop lent", model="gemini/gemini-test", llm_provider="gemini")
    r = _interpret(env)
    assert r.status_code == 503 and r.json()["detail"] == {"code": "INTERPRET_UNAVAILABLE", "reason": "timeout"}
    env.reply = _response("désolé")
    r = _interpret(env)
    assert r.status_code == 503 and r.json()["detail"]["reason"] == "bad_output"
    assert [u["total_tokens"] for u in env.usage] == [390]


def test_public_config_announces_the_feature_only_when_it_is_served(env, monkeypatch):
    from apowerb.routers import config

    app = FastAPI()
    app.include_router(config.router, prefix="/api")
    client = TestClient(app)
    assert client.get("/api/config").json()["forecast_interpret_enabled"] is True
    monkeypatch.setattr(env.settings, "forecast_interpret_enabled", False)
    assert client.get("/api/config").json()["forecast_interpret_enabled"] is False


def test_forecast_relays_events_and_scenarios_and_omits_them_when_absent(env):
    base = {"data": [{"date": "2025-01-01", "sales": 1}], "date_var": "date", "target_var": "sales", "horizon": 3}
    with patch("apowerb.routers.forecast.Th2forecastClient") as ctor:
        ctor.return_value.forecast.return_value = {"status": "success", "series": []}
        env.client.post("/api/v1/forecast", json=base)
        env.client.post("/api/v1/forecast", json={**base, "events": [PROMO], "scenarios": [{"name": "Sans promo", "events": []}]})
    plain, rich = (c.args[0] for c in ctor.return_value.forecast.call_args_list)
    assert "events" not in plain and "scenarios" not in plain
    assert rich["events"] == [PROMO] and rich["scenarios"] == [{"name": "Sans promo", "events": []}]
