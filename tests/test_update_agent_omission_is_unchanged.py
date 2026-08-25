"""update_agent: an omitted field means "unchanged", an explicit one means what it says.

Bug: update_agent writes every column unconditionally, so a field the client
omitted arrived as None and wiped the stored value. The edit form does not
expose the sub-agent pipeline fields, so saving an agent from the UI to change
something unrelated silently cleared output_key / output_schema_name, which
breaks ADK state hand-off between sub-agents while the pipeline keeps
returning 200.

These tests exercise the real update_agent and capture what it hands to
.values(...), rather than inspecting its source text -- the distinction matters
here because the defect is about *which value* is written, not about whether a
column name appears.
"""

from __future__ import annotations

import pytest

from apowerb.core import agent_main
from apowerb.schema.agent_schema import AgentCreateSchema

STORED = {
    "output_key": "intake_result",
    "output_schema_name": "IntakeOut",
    "skip_when_upstream": "empty",
    "superagent_template_id": "42",
    "loop_exit_instruction": "stop when done",
    "code_executor": "builtin",
    "agent_model_params": {},
}

BASE = {
    "agent_name": "a1",
    "agent_model": "gemini-2.5-flash",
    "agent_instruction": "do the thing",
    "agent_type": "llm",
    "agent_description": "an agent",
}


@pytest.fixture
def written(monkeypatch):
    """Run update_agent against fakes and return the values it tried to persist."""
    captured: dict = {}

    monkeypatch.setattr(agent_main, "get_agent", lambda *a, **k: dict(STORED))
    monkeypatch.setattr(agent_main, "create_agent_module", lambda **k: None)

    class _Query:
        def where(self, *a, **k):
            return self

        def values(self, **kw):
            captured.update(kw)
            return self

    class _Result:
        rowcount = 1

    class _Conn:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def execute(self, *a, **k):
            return _Result()

    monkeypatch.setattr(agent_main.agent_store.agent_table, "update", lambda: _Query())
    monkeypatch.setattr(agent_main.agent_store.engine, "begin", lambda: _Conn())
    return captured


def _payload(**extra):
    """Build the object exactly as the router does.

    routers/agents.py parses the body into AgentCreateSchema then calls
    model_copy(update={owner_id, organization_id}) -- those two columns are
    commented out of the schema and only exist as extras. Going through the
    same path is what makes this bench faithful: model_copy rebuilds
    model_fields_set, which is precisely what the fix reads.
    """
    parsed = AgentCreateSchema(**dict(BASE, **extra))
    return parsed.model_copy(
        update={"owner_id": "u@example.com", "organization_id": "example.com"}
    )


def test_omitted_field_keeps_the_stored_value(written):
    agent_main.update_agent(1, _payload(), user_id="u")
    assert written["output_key"] == "intake_result"
    assert written["output_schema_name"] == "IntakeOut"


def test_all_six_protected_fields_survive_an_unrelated_save(written):
    agent_main.update_agent(1, _payload(agent_description="x2"), user_id="u")
    for field in STORED:
        if field == "agent_model_params":
            continue
        assert written[field] == STORED[field], field


def test_an_explicit_null_still_clears_the_field(written):
    """Naming the field is intentional -- only silence means "unchanged"."""
    agent_main.update_agent(1, _payload(output_key=None), user_id="u")
    assert written["output_key"] is None


def test_an_explicit_value_is_honoured(written):
    agent_main.update_agent(1, _payload(output_key="nouveau"), user_id="u")
    assert written["output_key"] == "nouveau"


def test_a_stored_empty_string_is_not_turned_into_null(written, monkeypatch):
    """Regression: guarding on `not in (None, "")` silently converted a stored
    empty string into NULL on any save that omitted the field."""
    monkeypatch.setattr(
        agent_main, "get_agent", lambda *a, **k: dict(STORED, output_key="")
    )
    agent_main.update_agent(1, _payload(), user_id="u")
    assert written["output_key"] == ""


def test_unrelated_fields_are_not_frozen(written):
    """Negative control: the loop must not preserve fields outside its tuple."""
    agent_main.update_agent(1, _payload(agent_description="neuf"), user_id="u")
    assert written["agent_description"] == "neuf"


def test_the_protected_tuple_still_lists_six_fields():
    """Guard: if a field is added to the pipeline, decide explicitly whether it
    belongs here -- do not let this test drift silently."""
    import inspect

    src = inspect.getsource(agent_main.update_agent)
    block = src.split("_PRESERVE_WHEN_OMITTED = (")[1].split(")")[0]
    names = [line.strip().strip('",') for line in block.splitlines() if line.strip()]
    assert sorted(names) == sorted(
        [
            "output_key",
            "output_schema_name",
            "skip_when_upstream",
            "superagent_template_id",
            "loop_exit_instruction",
            "code_executor",
        ]
    ), names
