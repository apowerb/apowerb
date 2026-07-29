"""Tests du strip des signatures __thought__ Gemini (réparation function-calling multi-tours)."""

from th2agent.helpers.litellm_config import _strip_thought_signatures


def test_strip_tool_call_id_cote_reponse():
    k = {"messages": [{"role": "tool", "tool_call_id": "call_abc__thought__SIGN", "content": "{}"}]}
    _strip_thought_signatures(k)
    assert k["messages"][0]["tool_call_id"] == "call_abc"


def test_strip_id_cote_assistant():
    k = {"messages": [{"role": "assistant",
                       "tool_calls": [{"id": "call_xyz__thought__SIGN", "type": "function"}]}]}
    _strip_thought_signatures(k)
    assert k["messages"][0]["tool_calls"][0]["id"] == "call_xyz"


def test_noop_sans_thought():
    """Aucun id avec __thought__ -> rien ne bouge (sûr pour Mistral et autres)."""
    k = {"messages": [
        {"role": "tool", "tool_call_id": "call_clean"},
        {"role": "assistant", "tool_calls": [{"id": "call_clean2"}]},
    ]}
    _strip_thought_signatures(k)
    assert k["messages"][0]["tool_call_id"] == "call_clean"
    assert k["messages"][1]["tool_calls"][0]["id"] == "call_clean2"


def test_coherence_appel_reponse():
    """Même base, signatures différentes -> après strip, identiques -> s'apparient."""
    k = {"messages": [
        {"role": "assistant", "tool_calls": [{"id": "call_1__thought__AAA"}]},
        {"role": "tool", "tool_call_id": "call_1__thought__BBB"},
    ]}
    _strip_thought_signatures(k)
    assert k["messages"][0]["tool_calls"][0]["id"] == "call_1"
    assert k["messages"][1]["tool_call_id"] == "call_1"


def test_robuste_messages_absents_ou_invalides():
    assert _strip_thought_signatures({}) == {}
    _strip_thought_signatures({"messages": "pas une liste"})  # ne crashe pas
    _strip_thought_signatures({"messages": [None, "x", {"role": "user", "content": "hi"}]})  # ne crashe pas
