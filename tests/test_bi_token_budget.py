"""Budget de taille des requêtes au modèle sur la conversation BI de la démo.

Rejoue bench/bi_tokens (modèle scripté, vrai agent, vrais outils BI) et borne
l'historique envoyé : une réponse d'outil BI qui grossit fait échouer ce test au
lieu de renchérir chaque conversation en silence. Les déclarations d'outils,
communes à tous les agents de chat, ne sont pas bornées ici.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "bench" / "bi_tokens"


@pytest.fixture(scope="module")
def replay():
    import apowerb.tools_store.portfolio.bi_dashboard_tools as t
    from apowerb.core.agent_helpers import agent_utils, extras_loader

    saved = [(mod, name, getattr(mod, name)) for mod, name in (
        (t, "_dashboard_store"), (t, "_compute"), (t, "_agent_owner"), (t, "_run_tool"),
        (agent_utils, "get_agent_details"), (extras_loader, "_inject_tool_config_env"),
    )]
    sys.path.insert(0, str(BENCH))
    try:
        import replay as module
    finally:
        sys.path.remove(str(BENCH))
    yield module, module.install_live_tools()
    for mod, name, value in saved:
        setattr(mod, name, value)


def _history(log):
    return sum(r["history"] for r in log)


def test_demo_conversation_history_stays_under_its_budget(replay):
    module, live = replay
    creation = asyncio.run(module.run_turn("creation", live))
    question = asyncio.run(module.run_turn("question", live))
    # 15/10, caractères d'historique sur les appels du tour :
    # 98 466 et 59 135 avant, 70 041 et 43 379 après.
    assert _history(creation) <= 77_000
    assert _history(question) <= 48_000


def test_targeted_queries_still_carry_the_roas_of_every_channel(replay):
    """Ce que le modèle a répondu en démo (Email 32,8, Google Ads 3,6,
    Instagram 3,0) doit rester lisible quand il ne demande que le KPI ROAS."""
    module, _ = replay
    import apowerb.tools_store.portfolio.bi_dashboard_tools as t

    dash_id = module._query_fixture()[None]["dashboard_id"]
    seen = {}
    for canal in ("Email", "Google Ads", "Instagram"):
        res = t.tool_bi_query_dashboard(dash_id, json.dumps({"canal": canal}), component_ids="ROAS")
        assert [c["title"] for c in res["components"]] == ["ROAS"]
        seen[canal] = res["components"][0]["value"]
    assert seen == {"Email": "32.8", "Google Ads": "3.6", "Instagram": "3.0"}
