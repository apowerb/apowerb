"""Offline POC runner for agent evaluation.

Not wired into any request path and not mounted as a route: this is a
script, run manually, behind `EVALUATION_ENABLED`. It prints a JSON report
to stdout and never writes to the database -- see `models.py` for why
persistence isn't part of this PR.

Usage (from the repo root, with the venv active)::

    EVALUATION_ENABLED=true PYTHONPATH=src python -m apowerb.evaluation.poc_runner \\
        --session session_1786030573591

    EVALUATION_ENABLED=true EVALUATION_JUDGE_MODEL=gemini/gemini-2.5-flash \\
    EVALUATION_JUDGE_API_KEY=... PYTHONPATH=src python -m apowerb.evaluation.poc_runner \\
        --session dashboard-chat-b59ebab5-9e8c-44b1-b113-b56ae179c6ce \\
        --app-name agent1201 --user-id elom.gnaglo@gmail.com \\
        --judged-model openai/Mistral-Small-3.2-24B-Instruct-2506 --run-judge
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from apowerb.configs.settings import get_settings
from apowerb.evaluation.evaluators.task_completion_judge import evaluate_task_completion
from apowerb.evaluation.evaluators.tool_execution_outcome import evaluate_tool_execution_outcome
from apowerb.helpers.database import sessionmanager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def _run(
    session_id: str,
    app_name: str | None,
    user_id: str | None,
    judged_model: str | None,
    run_judge: bool,
) -> dict:
    settings = get_settings()
    if not settings.evaluation_enabled:
        raise RuntimeError(
            "EVALUATION_ENABLED is false -- this is scaffolding, not a shipped feature."
        )

    report: dict = {"session_id": session_id, "results": []}
    async with sessionmanager.session() as db:
        deterministic = await evaluate_tool_execution_outcome(db, session_id)
        report["results"].append(vars(deterministic))

        if run_judge:
            if not (app_name and user_id and judged_model):
                raise ValueError(
                    "--app-name, --user-id and --judged-model are required with --run-judge"
                )
            judge = await evaluate_task_completion(
                db,
                app_name=app_name,
                user_id=user_id,
                session_id=session_id,
                judged_model=judged_model,
            )
            report["results"].append(vars(judge))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, help="ADK session_id to evaluate")
    parser.add_argument("--app-name", help="ADK app_name (agent id), required with --run-judge")
    parser.add_argument("--user-id", help="ADK user_id (owner), required with --run-judge")
    parser.add_argument(
        "--judged-model", help="litellm model string of the agent being judged"
    )
    parser.add_argument(
        "--run-judge", action="store_true", help="also run the LLM-judge evaluator"
    )
    args = parser.parse_args()

    report = asyncio.run(
        _run(args.session, args.app_name, args.user_id, args.judged_model, args.run_judge)
    )
    print(json.dumps(report, indent=2, default=str))


if __name__ == "__main__":
    main()
