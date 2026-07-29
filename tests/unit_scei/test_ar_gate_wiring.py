"""Câblage du gate AR : préfixé en tête, liste plate, trigger SCEIIntakePayload."""


def _named(n):
    def f(callback_context):
        return None
    f.__name__ = n
    return f


def test_maybe_wire_ar_gate_prefixes_flat():
    from th2customers.scei.gates import maybe_wire_ar_gate
    agent_kwargs = {"before_agent_callback": [_named("excluded_x"), _named("attachment_x")]}
    agent_details = {"output_schema_name": "SCEIIntakePayload"}

    assert maybe_wire_ar_gate(agent_details, "intake_out", agent_kwargs) is True
    before = agent_kwargs["before_agent_callback"]
    assert isinstance(before, list) and len(before) == 3  # liste PLATE, pas imbriquée
    assert before[0].__name__.startswith("ar_gate_writes")  # gate AR en tête
    assert [c.__name__ for c in before[1:]] == ["excluded_x", "attachment_x"]


def test_maybe_wire_ar_gate_not_triggered():
    from th2customers.scei.gates import maybe_wire_ar_gate
    agent_kwargs = {}
    assert maybe_wire_ar_gate({"output_schema_name": "ARMatchPayload"}, "k", agent_kwargs) is False
    assert "before_agent_callback" not in agent_kwargs
