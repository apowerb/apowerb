"""Petits correctifs BI : instruction de l'agent de prévision, reconnaissance du doublon de nom."""
import pytest
from sqlalchemy.exc import IntegrityError

import apowerb.tools_store.portfolio.bi_dashboard_tools as t
from apowerb.bi.dashboards.service import DashboardNameConflictError
from apowerb.core.superagents.templates import SUPERAGENT_TEMPLATES


def _instruction(template_id: str) -> str:
    return next(x for x in SUPERAGENT_TEMPLATES if x["template_id"] == template_id)["agent_instruction"]


@pytest.mark.parametrize("tool", ["tool_bi_create_stock_dashboard", "tool_bi_create_dashboard"])
def test_forecasting_agent_instruction_lists_dashboard_tools_in_the_table(tool):
    rows = [ln for ln in _instruction("forecasting_agent").splitlines() if ln.startswith(f"| `{tool}`")]
    assert len(rows) == 1 and rows[0].count("|") == 4


def test_name_conflict_error_is_a_duplicate():
    assert t._is_duplicate(DashboardNameConflictError("X"))


def test_integrity_error_on_the_name_constraint_is_a_duplicate():
    exc = IntegrityError("INSERT", {}, Exception('duplicate key value violates unique constraint "uq_bi_name_type_org_project"'))
    assert t._is_duplicate(exc)


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("unique index scan failed"),
        ValueError("already exists in the spec"),
        IntegrityError("INSERT", {}, Exception('duplicate key value violates unique constraint "uq_other"')),
    ],
)
def test_other_errors_are_not_duplicates(exc):
    assert not t._is_duplicate(exc)
