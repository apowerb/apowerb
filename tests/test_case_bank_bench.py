"""Le banc de la banque de cas tourne, et la banque change bien ce que fait l'agent.

Modèle scripté : ce test garde la tuyauterie (agent ADK réel, callback de
lecture, banque SQLite), il ne dit rien du gain d'un vrai modèle.
"""

from __future__ import annotations

import importlib.util
from argparse import Namespace
from pathlib import Path

BENCH = Path(__file__).resolve().parents[1] / "bench" / "case_bank" / "bench.py"


def _bench():
    spec = importlib.util.spec_from_file_location("case_bank_bench", BENCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_les_taches_d_evaluation_ne_partagent_rien_avec_celles_de_la_banque():
    tasks = _bench().build_tasks(7)
    a = {t["expected"]["supplier_id"] for t in tasks["A"]}
    b = {t["expected"]["supplier_id"] for t in tasks["B"]}
    assert len(tasks["A"]) == len(tasks["B"]) == 20
    assert a.isdisjoint(b)
    assert {t["message"] for t in tasks["A"]}.isdisjoint(
        t["message"] for t in tasks["B"]
    )


async def test_avec_des_cas_l_agent_scripte_n_echoue_plus_sur_l_identifiant():
    bench = _bench()
    report = await bench.main_async(Namespace(model="scripted", seed=7, repeats=1))
    k0, k4 = report["arms"]["K0"], report["arms"]["K4"]
    assert report["bank_size_after_fill"] == 20
    assert k0["tool_error_rate"] > 0
    assert k4["tool_error_rate"] == 0
    assert k4["success_rate"] > k0["success_rate"]
    assert report["verdict"]["tokens_source"].startswith("estimated")
