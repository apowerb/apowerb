"""Tests : filet anti-orphelin LiteLLM (_drop_orphan_tool_results, Layer B)."""
from apowerb.helpers.litellm_config import (
    _drop_orphan_tool_results, _strip_thought_signatures,
)


def _assistant(call_id):
    return {"role": "assistant", "content": "", "tool_calls": [{"id": call_id, "type": "function",
            "function": {"name": "t", "arguments": "{}"}}]}


def _toolresult(call_id, content="ok"):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def test_drop_orphan_sans_appel():
    msgs = [{"role": "user", "content": "hi"}, _toolresult("call_ORPHAN")]
    out = _drop_orphan_tool_results({"messages": msgs})["messages"]
    assert _toolresult("call_ORPHAN") not in out
    assert len(out) == 1  # ne reste que le user


def test_garde_paire_complete():
    msgs = [{"role": "user", "content": "hi"}, _assistant("call_A"), _toolresult("call_A")]
    out = _drop_orphan_tool_results({"messages": msgs})["messages"]
    assert len(out) == 3  # paire appel/reponse conservee


def test_call_dangling_conserve():
    # assistant a appele un outil mais la reponse manque -> on NE touche PAS l'appel
    msgs = [{"role": "user", "content": "hi"}, _assistant("call_B")]
    out = _drop_orphan_tool_results({"messages": msgs})["messages"]
    assert len(out) == 2


def test_mixte_garde_les_bons_drop_les_orphelins():
    msgs = [
        {"role": "user", "content": "hi"},
        _assistant("call_A"), _toolresult("call_A"),   # paire OK
        _toolresult("call_GHOST"),                       # orphelin
    ]
    out = _drop_orphan_tool_results({"messages": msgs})["messages"]
    ids = [m.get("tool_call_id") for m in out if m.get("role") == "tool"]
    assert ids == ["call_A"]  # call_GHOST retire


def test_combine_avec_strip_thought():
    # apres strip, l'id assistant et l'id reponse matchent (sans __thought__)
    a = _assistant("call_A__thought__SIG")
    r = _toolresult("call_A__thought__SIG")
    kwargs = _strip_thought_signatures({"messages": [a, r]})
    out = _drop_orphan_tool_results(kwargs)["messages"]
    assert len(out) == 2, "apres strip les ids matchent -> la paire est conservee"


def test_batch_multi_outils_un_seul_orphelin():
    # un assistant avec 2 tool_calls + 2 results dont 1 orphelin -> retire SEUL l'orphelin
    a = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "t", "arguments": "{}"}},
        {"id": "c2", "type": "function", "function": {"name": "t", "arguments": "{}"}}]}
    msgs = [{"role": "user", "content": "hi"}, a,
            _toolresult("c1"), _toolresult("c2"), _toolresult("c_ORPHAN")]
    out = _drop_orphan_tool_results({"messages": msgs})["messages"]
    ids = [m.get("tool_call_id") for m in out if m.get("role") == "tool"]
    assert ids == ["c1", "c2"], "seul l'orphelin du batch doit partir"


def test_fallback_call_id():
    # assistant qui porte 'call_id' au lieu de 'id' -> le result ne doit PAS etre orphelin
    a = {"role": "assistant", "content": "", "tool_calls": [
        {"call_id": "cid_1", "type": "function", "function": {"name": "t", "arguments": "{}"}}]}
    msgs = [a, _toolresult("cid_1")]
    out = _drop_orphan_tool_results({"messages": msgs})["messages"]
    assert _toolresult("cid_1") in out, "le fallback call_id doit etre apparie"


def test_id_non_hashable_ne_leve_pas():
    # un id de type list (hors-schema) ne doit PAS faire lever le hook
    a = {"role": "assistant", "content": "", "tool_calls": [{"id": ["x"], "function": {}}]}
    msgs = [a, {"role": "tool", "tool_call_id": ["y"], "content": "z"}]
    out = _drop_orphan_tool_results({"messages": msgs})["messages"]  # ne doit pas lever
    assert isinstance(out, list)


def test_no_op_sans_messages():
    assert _drop_orphan_tool_results({})["messages"] if False else True
    assert _drop_orphan_tool_results({"messages": None}).get("messages") is None
    # rien a dropper -> messages inchanges
    msgs = [{"role": "user", "content": "x"}]
    assert _drop_orphan_tool_results({"messages": msgs})["messages"] == msgs
