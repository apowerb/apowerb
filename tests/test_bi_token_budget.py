"""Budget de taille des requêtes au modèle sur la conversation BI de la démo.

Rejoue bench/bi_tokens (modèle scripté, vrai agent, vrais outils BI) et borne
les caractères envoyés : une réponse d'outil ou une déclaration qui grossit fait
échouer ce test au lieu de renchérir chaque conversation en silence.
"""
import asyncio
import json
import sys
from pathlib import Path

import pytest

BENCH = Path(__file__).resolve().parents[1] / "bench" / "bi_tokens"


@pytest.fixture(scope="module")
def replay():
    sys.path.insert(0, str(BENCH))
    try:
        import replay as module
    finally:
        sys.path.remove(str(BENCH))
    live = module.install_live_tools()
    return module, live


def _chars(log):
    return sum(r["system"] + r["tools"] + r["history"] for r in log)


def test_demo_conversation_stays_under_its_budget(replay):
    module, live = replay
    creation = asyncio.run(module.run_turn("creation", live))
    question = asyncio.run(module.run_turn("question", live))
    # 15/10 : 411 281 et 246 824 caractères avant, 386 066 et 232 994 après.
    assert _chars(creation) <= 390_000
    assert _chars(question) <= 236_000


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
