"""Banc de la banque de cas : relire ses runs passés aide-t-il un agent à réussir ?

Go si, avec K=4 cas, le taux de réussite gagne au moins 10 points sur K=0,
sans hausse du taux d'erreurs d'outil, et avec au plus 10 % de tokens
d'entrée en plus par tâche (seuils validés avant le premier passage).

L'agent est un agent de saisie de factures, synthétique : fournisseurs
fictifs, montants et dates tirés d'une graine fixe. Sa consigne est volontairement
courte ; deux règles maison ne s'y trouvent pas et ne s'apprennent que par
l'expérience :

- ``tool_save_invoice`` veut l'identifiant du fournisseur, que donne
  ``tool_lookup_supplier`` (un nom est refusé avec ``success: false``) ;
- une facture au-dessus de 10 000 doit passer par ``tool_flag_for_review``
  avant d'être enregistrée. Rien ne le signale : le run « réussit », seul le
  contrôle final le compte faux.

La réponse attendue de chaque tâche est fixée avant tout appel au modèle.

Séparation stricte : la banque se remplit sur les tâches A (fournisseurs 1-6),
l'évaluation porte sur les tâches B (fournisseurs 7-12), qui ne sont jamais
écrites dans la banque. Trois bras sur B : K=0, K=4, K=4 sans échecs.

Ce qui est réel : l'agent ADK, le Runner, le callback de lecture et la banque
(SQLite). Avec ``--model scripted``, le modèle est un script qui ne lit que le
bloc de cas : il prouve la tuyauterie (la banque change ce que voit le
modèle), JAMAIS un gain. Le gain se mesure avec un vrai modèle.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import logging
import random
import re
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ORG = "bench-org"
AGENT_ID = 1
OWNER = "bench@example.com"
THRESHOLD = 10_000
GO_POINTS = 10.0
GO_TOKENS_RATIO = 1.10

SUPPLIERS = [
    ("Atelier Nord", "SUP-1042"),
    ("Boréal Fournitures", "SUP-1187"),
    ("Cobalt Services", "SUP-1203"),
    ("Delta Emballages", "SUP-1319"),
    ("Erable Conseil", "SUP-1455"),
    ("Fjord Logistique", "SUP-1561"),
    ("Granit Imprimerie", "SUP-1622"),
    ("Hélios Énergie", "SUP-1748"),
    ("Iroise Nettoyage", "SUP-1874"),
    ("Jade Informatique", "SUP-1903"),
    ("Kaolin Matériaux", "SUP-2016"),
    ("Lagune Transports", "SUP-2131"),
]
MONTHS = [
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
]

INSTRUCTION = (
    "You process incoming supplier invoices received by email. "
    "Record each invoice with the tools available, then reply with one short sentence."
)


# --------------------------------------------------------------------------- #
# Tâches : la réponse attendue est fixée ici, avant tout appel au modèle
# --------------------------------------------------------------------------- #


def build_tasks(seed: int = 7) -> dict[str, list[dict]]:
    rnd = random.Random(seed)
    tasks: dict[str, list[dict]] = {"A": [], "B": []}
    for split, pool in (("A", SUPPLIERS[:6]), ("B", SUPPLIERS[6:])):
        for i in range(20):
            name, sid = pool[i % len(pool)]
            high = i % 5 in (1, 3)  # 8 sur 20 au-dessus du seuil
            amount = round(
                rnd.uniform(10_500, 48_000) if high else rnd.uniform(180, 9_400), 2
            )
            usd = i % 4 == 2
            due = dt.date(2026, 11, 1) + dt.timedelta(days=rnd.randint(0, 60))
            due_txt = (
                f"{due.day:02d}/{due.month:02d}/{due.year}"
                if i % 2
                else f"{due.day} {MONTHS[due.month - 1]} {due.year}"
            )
            number = f"F-{split}{rnd.randint(1000, 9999)}"
            amount_txt = f"{amount:,.2f}".replace(",", " ").replace(".", ",")
            cur_txt = ("$" if i % 3 else "USD") if usd else ("€" if i % 3 else "EUR")
            email = (
                f"Bonjour,\nVeuillez trouver ci-joint notre facture n° {number} "
                f"d'un montant de {amount_txt} {cur_txt}, à régler avant le {due_txt}.\n"
                f"Cordialement,\nService comptable — {name}"
            )
            tasks[split].append(
                {
                    "id": f"{split}{i:02d}",
                    "message": f"Nouvelle facture reçue :\n{email}",
                    "expected": {
                        "supplier_id": sid,
                        "invoice_number": number,
                        "amount": amount,
                        "currency": "USD" if usd else "EUR",
                        "due_date": due.isoformat(),
                        "flagged": amount > THRESHOLD,
                    },
                }
            )
    return tasks


# --------------------------------------------------------------------------- #
# Outils de l'agent (état par tâche)
# --------------------------------------------------------------------------- #


class World:
    def __init__(self) -> None:
        self.saved: list[dict] = []
        self.flagged: set[str] = set()
        self.calls: list[str] = []
        self.tool_errors = 0

    def tools(self) -> list:
        world = self
        registry = dict(SUPPLIERS)

        def tool_lookup_supplier(name: str) -> dict:
            """Find a supplier in the registry by its name. Returns its supplier_id."""
            world.calls.append("tool_lookup_supplier")
            for known, sid in registry.items():
                if (
                    known.lower() in (name or "").lower()
                    or (name or "").lower() in known.lower()
                ):
                    return {"success": True, "supplier_id": sid, "name": known}
            world.tool_errors += 1
            return {"success": False, "error": "supplier not found"}

        def tool_flag_for_review(invoice_number: str, reason: str) -> dict:
            """Flag an invoice for human review before it is paid."""
            world.calls.append("tool_flag_for_review")
            world.flagged.add(invoice_number)
            return {"success": True}

        def tool_save_invoice(
            supplier_id: str,
            invoice_number: str,
            amount: float,
            currency: str,
            due_date: str,
        ) -> dict:
            """Record an invoice. due_date is YYYY-MM-DD, currency an ISO code."""
            world.calls.append("tool_save_invoice")
            problems = []
            if supplier_id not in registry.values():
                problems.append("invalid supplier_id")
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(due_date)):
                problems.append("due_date must be YYYY-MM-DD")
            if currency not in ("EUR", "USD"):
                problems.append("currency must be an ISO code")
            if problems:
                world.tool_errors += 1
                return {"success": False, "error": "; ".join(problems)}
            world.saved.append(
                {
                    "supplier_id": supplier_id,
                    "invoice_number": invoice_number,
                    "amount": float(amount),
                    "currency": currency,
                    "due_date": due_date,
                    "flagged": invoice_number in world.flagged,
                }
            )
            return {"success": True}

        return [tool_lookup_supplier, tool_flag_for_review, tool_save_invoice]


def check(world: World, expected: dict) -> bool:
    """Exactement une facture, juste, et signalée si et seulement si elle doit l'être."""
    if len(world.saved) != 1:
        return False
    got = world.saved[0]
    return (
        got["supplier_id"] == expected["supplier_id"]
        and got["invoice_number"] == expected["invoice_number"]
        and abs(got["amount"] - expected["amount"]) < 0.01
        and got["currency"] == expected["currency"]
        and got["due_date"] == expected["due_date"]
        and got["flagged"] == expected["flagged"]
    )


# --------------------------------------------------------------------------- #
# Modèle scripté : ne connaît les règles QUE par le bloc de cas
# --------------------------------------------------------------------------- #


def _scripted_model():
    from google.adk.models.base_llm import BaseLlm
    from google.adk.models.llm_response import LlmResponse
    from google.genai import types

    from apowerb.memory.case_bank import HEADER

    def parse(message: str) -> dict:
        number = re.search(r"n° (\S+)", message).group(1)
        raw = re.search(r"montant de ([\d ]+,\d{2}) (\S+)", message)
        amount = float(raw.group(1).replace(" ", "").replace(",", "."))
        currency = "USD" if raw.group(2) in ("$", "USD") else "EUR"
        d = re.search(r"avant le (\d{2})/(\d{2})/(\d{4})", message)
        if d:
            due = f"{d.group(3)}-{d.group(2)}-{d.group(1)}"
        else:
            d = re.search(r"avant le (\d{1,2}) (\w+) (\d{4})", message)
            due = (
                f"{d.group(3)}-{MONTHS.index(d.group(2)) + 1:02d}-{int(d.group(1)):02d}"
            )
        name = message.rsplit("— ", 1)[1].strip()
        return {
            "number": number,
            "amount": amount,
            "currency": currency,
            "due": due,
            "name": name,
        }

    class Scripted(BaseLlm):
        model: str = "scripted"

        async def generate_content_async(self, llm_request, stream=False):
            texts = [
                p.text
                for c in llm_request.contents
                for p in (c.parts or [])
                if getattr(p, "text", None)
            ]
            cases = next((t for t in texts if t.startswith(HEADER)), "")
            good = cases.split("Failed:")[0]
            message = next(
                t for t in reversed(texts) if t.startswith("Nouvelle facture")
            )
            inv = parse(message)
            # Sans cas, le script tombe juste une fois sur deux sur chaque règle
            # (tirage fixe par facture), comme un modèle qui tâtonne ; avec un
            # cas réussi qui montre l'outil, il le reprend toujours.
            luck = random.Random(inv["number"])
            learned_lookup = "tool_lookup_supplier" in good or luck.random() < 0.5
            learned_flag = "tool_flag_for_review" in good or luck.random() < 0.5
            done = [
                p.function_response.name
                for c in llm_request.contents
                for p in (c.parts or [])
                if getattr(p, "function_response", None)
            ]
            responses = {
                p.function_response.name: p.function_response.response
                for c in llm_request.contents
                for p in (c.parts or [])
                if getattr(p, "function_response", None)
            }
            plan = []
            if learned_lookup:
                plan.append(("tool_lookup_supplier", {"name": inv["name"]}))
            if learned_flag and inv["amount"] > THRESHOLD:
                plan.append(
                    (
                        "tool_flag_for_review",
                        {"invoice_number": inv["number"], "reason": "amount"},
                    )
                )
            sid = (responses.get("tool_lookup_supplier") or {}).get(
                "supplier_id", inv["name"]
            )
            plan.append(
                (
                    "tool_save_invoice",
                    {
                        "supplier_id": sid,
                        "invoice_number": inv["number"],
                        "amount": inv["amount"],
                        "currency": inv["currency"],
                        "due_date": inv["due"],
                    },
                )
            )
            step = len(done)
            if step < len(plan):
                name, args = plan[step]
                part = types.Part(
                    function_call=types.FunctionCall(name=name, args=args)
                )
            else:
                part = types.Part(text="Invoice processed.")
            yield LlmResponse(content=types.Content(role="model", parts=[part]))

    return Scripted()


# --------------------------------------------------------------------------- #
# Un passage
# --------------------------------------------------------------------------- #


async def run_task(task: dict, *, model: Any, bank, k: int, max_negatives: int) -> dict:
    from google.adk.agents import LlmAgent
    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from apowerb.core.invocation_context import make_identity_before_agent_callback
    from apowerb.memory import case_bank as cb

    world = World()
    usage = {"input": 0, "estimated": 0, "calls": 0}

    def count(*, callback_context, llm_request):
        usage["calls"] += 1
        chars = sum(
            len(p.text or "") for c in llm_request.contents for p in (c.parts or [])
        )
        chars += len(str(llm_request.config.system_instruction or "")) + len(
            str(llm_request.config.tools or "")
        )
        usage["estimated"] += chars // 4
        return None

    def measure(*, callback_context, llm_response):
        meta = getattr(llm_response, "usage_metadata", None)
        if meta and meta.prompt_token_count:
            usage["input"] += meta.prompt_token_count
        return None

    before = [count]
    if k > 0:
        reading = cb.make_case_bank_before_model_callback(
            agent_id=AGENT_ID, bank=bank, k=k, max_negatives=max_negatives
        )
        before = [reading, count]  # compter APRÈS l'injection : c'est ce qui part
    agent = LlmAgent(
        name="invoice_intake",
        model=model,
        instruction=INSTRUCTION,
        tools=world.tools(),
        before_agent_callback=make_identity_before_agent_callback(
            owner=OWNER, organization_id=ORG, project_id="bench", agent_id=AGENT_ID
        ),
        before_model_callback=before,
        after_model_callback=measure,
    )
    runner = Runner(
        app_name="bench", agent=agent, session_service=InMemorySessionService()
    )
    session = await runner.session_service.create_session(
        app_name="bench", user_id=OWNER
    )
    msg = types.Content(role="user", parts=[types.Part(text=task["message"])])
    started, crashed = time.perf_counter(), False
    try:
        async for _ in runner.run_async(
            user_id=OWNER, session_id=session.id, new_message=msg
        ):
            pass
    except Exception as exc:  # noqa: BLE001 -- un run qui lève est un échec, pas une panne du banc
        crashed = True
        logging.getLogger("bench").warning("%s: %r", task["id"], exc)
    return {
        "id": task["id"],
        "success": check(world, task["expected"]),
        "status_label": not crashed,  # ce que verrait agent_runs.status
        "tools": world.calls,
        "tool_errors": world.tool_errors,
        "tool_calls": len(world.calls),
        "input_tokens": usage["input"],
        "input_tokens_estimated": usage["estimated"],
        "model_calls": usage["calls"],
        "latency_s": round(time.perf_counter() - started, 3),
    }


def _bank(path: Path):
    from sqlalchemy import create_engine

    from apowerb.memory.case_bank import CaseBank

    bank = CaseBank(engine=create_engine(f"sqlite:///{path}"), schema=None)
    bank.create_table()
    return bank


async def fill(bank, tasks: list[dict], model: Any) -> list[dict]:
    """Phase A : l'agent travaille sans cas ; chaque issue CONTRÔLÉE devient un cas."""
    rows = []
    for task in tasks:
        r = await run_task(task, model=model, bank=bank, k=0, max_negatives=0)
        bank.record(
            organization_id=ORG,
            agent_id=AGENT_ID,
            scope_user=None,
            source="schedule",
            run_id=f"fill-{task['id']}",
            task_summary=task["message"],
            tool_sequence=r["tools"],
            outcome=1 if r["success"] else 0,
            failure_kind=None if r["success"] else "wrong_result",
        )
        rows.append(r)
    return rows


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    lat = sorted(r["latency_s"] for r in rows)
    return {
        "tasks": n,
        "success_rate": round(100 * sum(r["success"] for r in rows) / n, 1),
        "tool_error_rate": round(
            100
            * sum(r["tool_errors"] for r in rows)
            / max(1, sum(r["tool_calls"] for r in rows)),
            1,
        ),
        "input_tokens_per_task": round(
            statistics.mean(r["input_tokens"] for r in rows), 1
        ),
        "input_tokens_estimated_per_task": round(
            statistics.mean(r["input_tokens_estimated"] for r in rows), 1
        ),
        "p95_latency_s": lat[min(n - 1, int(round(0.95 * (n - 1))))],
        "status_label_agrees_with_check": round(
            100 * sum(r["status_label"] == r["success"] for r in rows) / n, 1
        ),
    }


def verdict(arms: dict, measured_tokens: bool) -> dict:
    k0, k4 = arms["K0"], arms["K4"]
    key = (
        "input_tokens_per_task"
        if measured_tokens
        else "input_tokens_estimated_per_task"
    )
    ratio = k4[key] / k0[key] if k0[key] else None
    gain = round(k4["success_rate"] - k0["success_rate"], 1)
    return {
        "success_gain_points": gain,
        "tool_error_rate_delta": round(
            k4["tool_error_rate"] - k0["tool_error_rate"], 1
        ),
        "tokens_ratio": round(ratio, 3) if ratio else None,
        "tokens_source": "measured (usage_metadata)"
        if measured_tokens
        else "estimated (chars/4)",
        "go": bool(
            gain >= GO_POINTS
            and k4["tool_error_rate"] <= k0["tool_error_rate"]
            and ratio is not None
            and ratio <= GO_TOKENS_RATIO
        ),
    }


async def main_async(args) -> dict:
    tasks = build_tasks(args.seed)
    if args.model == "scripted":
        model = _scripted_model()
    else:
        from google.adk.models.lite_llm import LiteLlm

        model = LiteLlm(model=args.model)
    report: dict[str, Any] = {
        "model": args.model,
        "seed": args.seed,
        "repeats": args.repeats,
        "date": dt.date.today().isoformat(),
        "runs": [],
    }
    per_arm: dict[str, list[dict]] = {"K0": [], "K4": [], "K4_pos_only": []}
    fill_rows: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp:
        for rep in range(args.repeats):
            empty = _bank(Path(tmp) / f"empty-{rep}.db")
            bank = _bank(Path(tmp) / f"bank-{rep}.db")
            fill_rows += await fill(bank, tasks["A"], model)
            for t in tasks["B"]:
                per_arm["K0"].append(
                    await run_task(t, model=model, bank=empty, k=0, max_negatives=0)
                )
                per_arm["K4"].append(
                    await run_task(t, model=model, bank=bank, k=4, max_negatives=2)
                )
                per_arm["K4_pos_only"].append(
                    await run_task(t, model=model, bank=bank, k=4, max_negatives=0)
                )
            report["bank_size_after_fill"] = bank.count()
    arms = {name: summarize(rows) for name, rows in per_arm.items()}
    measured = all(r["input_tokens"] > 0 for rows in per_arm.values() for r in rows)
    report.update(
        fill=summarize(fill_rows),
        arms=arms,
        verdict=verdict(arms, measured),
        runs={name: rows for name, rows in per_arm.items()},
    )
    if args.model == "scripted":
        report["warning"] = "Modèle scripté : prouve la tuyauterie, pas un gain."
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--model",
        default="scripted",
        help="'scripted' ou un modèle LiteLLM (gemini/gemini-2.5-flash…)",
    )
    parser.add_argument("--repeats", type=int, default=1)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--save", help="nom du fichier de résultat dans results/")
    parser.add_argument(
        "--dump-tasks",
        action="store_true",
        help="affiche les tâches et leurs réponses attendues",
    )
    args = parser.parse_args()
    if args.dump_tasks:
        print(json.dumps(build_tasks(args.seed), ensure_ascii=False, indent=1))
        return 0
    logging.disable(logging.WARNING)
    report = asyncio.run(main_async(args))
    print(
        json.dumps(
            {
                k: report[k]
                for k in ("model", "fill", "arms", "verdict", "bank_size_after_fill")
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    if args.save:
        out = HERE / "results" / f"{args.save}.json"
        out.parent.mkdir(exist_ok=True)
        out.write_text(
            json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
