"""Truncation can keep the text of the turns it drops.

The 14-message window counts tool calls and results, so a few tool-heavy turns
push the earlier answers out (Data Analyst bench: the turn-2 total is gone by
turn 5 and a real model queries it again). With ``keep_text_chars`` the
user/model text messages that fell out of the window come back before it,
newest first within the budget; their tool calls and results do not.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from apowerb.core.agent_helpers.callbacks import create_truncate_history_callback


def _text(role, text):
    return SimpleNamespace(role=role, parts=[SimpleNamespace(text=text, function_call=None, function_response=None)])


def _call(name):
    fc = SimpleNamespace(name=name, args={})
    return SimpleNamespace(role="model", parts=[SimpleNamespace(text=None, function_call=fc, function_response=None)])


def _result(name):
    fr = SimpleNamespace(name=name, response={"rows": ["x"] * 50})
    return SimpleNamespace(role="user", parts=[SimpleNamespace(text=None, function_call=None, function_response=fr)])


def _history():
    contents = []
    for turn in range(1, 6):
        contents.append(_text("user", f"question {turn}"))
        for i in range(3):
            contents += [_call(f"t{turn}_{i}"), _result(f"t{turn}_{i}")]
        contents.append(_text("model", f"answer {turn}: total {turn * 1000}"))
    return contents


def _run(contents, **kw):
    req = MagicMock()
    req.contents = list(contents)
    create_truncate_history_callback(keep_recent=14, **kw)(callback_context=MagicMock(), llm_request=req)
    return req.contents


def _texts(contents):
    return [p.text for c in contents for p in c.parts if p.text]


def test_default_is_unchanged():
    out = _run(_history())
    assert len(out) == 14  # first user message + 13 (orphan result dropped)
    assert "answer 2: total 2000" not in _texts(out)


def test_dropped_turn_texts_come_back_without_their_tools():
    contents = _history()
    base = _run(contents)
    out = _run(contents, keep_text_chars=6000)

    texts = _texts(out)
    assert texts[0] == "question 1"
    assert "answer 1: total 1000" in texts and "answer 2: total 2000" in texts
    assert out[0] is base[0] and out[-13:] == base[1:]
    kept_tools = {p.function_call.name for c in out for p in c.parts if p.function_call}
    window_tools = {p.function_call.name for c in base for p in c.parts if p.function_call}
    assert kept_tools == window_tools
    assert not any(p.function_response for c in out[:-13] for p in c.parts)
    # chronological order is kept
    assert texts.index("answer 1: total 1000") < texts.index("question 2") < texts.index("answer 2: total 2000")


def test_budget_keeps_the_newest_texts():
    # the window starts inside turn 4: "question 4" is the newest dropped text
    out = _run(_history(), keep_text_chars=len("answer 3: total 3000") + len("question 4"))
    texts = _texts(out)
    assert "answer 3: total 3000" in texts and "question 4" in texts
    assert "question 3" not in texts and "answer 2: total 2000" not in texts
    assert texts[0] == "question 1"
