"""Unit tests for the extension registry (pure, no DB/network)."""

from apowerb.core.extensions.registry import CallbackSpec, ExtensionRegistry, registry


def test_callbacks_preserve_registration_order():
    r = ExtensionRegistry()
    a = CallbackSpec(trigger="S", builder=lambda **k: "a")
    b = CallbackSpec(trigger="S", builder=lambda **k: "b")
    r.register_callback(a)
    r.register_callback(b)
    assert r.callbacks("S") == [a, b]
    assert r.callbacks("unknown") == []


def test_callback_spec_carries_wiring_metadata():
    s = CallbackSpec(
        trigger="ARMatchPayload",
        builder=lambda **k: None,
        position="head",
        flag="SCEI_AR_GATE_ACTIVE",
        required_output_key="ar_match",
        db_trigger="attachment_pdf_gate",
    )
    assert s.position == "head"
    assert s.flag == "SCEI_AR_GATE_ACTIVE"
    assert s.required_output_key == "ar_match"
    assert s.db_trigger == "attachment_pdf_gate"


def test_all_callbacks_spans_triggers_and_is_hashable():
    r = ExtensionRegistry()
    s1 = CallbackSpec(trigger="A", builder=lambda **k: None)
    s2 = CallbackSpec(trigger="B", builder=lambda **k: None, db_trigger="col")
    r.register_callback(s1)
    r.register_callback(s2)
    assert set(r.all_callbacks()) == {s1, s2}


def test_gate_appliers_preserve_order_and_apply():
    r = ExtensionRegistry()
    calls = []
    def a(agent_details, output_key, kw):
        calls.append("a"); return True
    def b(agent_details, output_key, kw):
        calls.append("b"); return False
    r.register_gate_applier("a", a)
    r.register_gate_applier("b", b)
    assert [n for n, _ in r.gate_appliers()] == ["a", "b"]
    for _name, fn in r.gate_appliers():
        fn({}, "k", {})
    assert calls == ["a", "b"]


def test_gate_appliers_empty_by_default():
    assert ExtensionRegistry().gate_appliers() == []


def test_tool_rebinder_roundtrip():
    r = ExtensionRegistry()
    def reb(agent_name, tools_ids, tools_funcs, owner_id):
        return tools_funcs
    r.register_tool_rebinder("tool_persist_ar_record", reb)
    assert r.tool_rebinders()["tool_persist_ar_record"] is reb
    assert "tool_send_scei_mail" not in r.tool_rebinders()


def test_webhook_hooks_named_points():
    r = ExtensionRegistry()
    r.register_webhook_hook("fanout.should_split", lambda att: True)
    r.register_webhook_hook("initial_state_extras", lambda ctx: {"k": 1})
    assert r.webhook_hook("fanout.should_split")(None) is True
    assert r.webhook_hook("initial_state_extras")(None) == {"k": 1}
    assert r.webhook_hook("absent") is None


def test_templates_and_schemas_merge():
    r = ExtensionRegistry()
    r.register_templates([{"name": "x"}])
    r.register_schema("S", object)
    assert r.templates() == [{"name": "x"}]
    assert "S" in r.schemas()


def test_singleton_reset():
    registry.register_schema("tmp", object)
    registry.reset()
    assert registry.schemas() == {}
    assert registry.callbacks("anything") == []
