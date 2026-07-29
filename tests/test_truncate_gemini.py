"""Tests : truncate_history retire les orphelins function_response (Gemini/ADK).

IMPORTANT : avec des paires call/resp propres et keep_recent pair, la fenetre tombe
toujours sur un call -> la garde n'est pas exercee. On construit donc explicitement
des historiques OU la fenetre COMMENCE sur un function_response orphelin.
"""
from types import SimpleNamespace as NS
from th2agent.core.agent_helpers.callbacks import (
    create_truncate_history_callback, _content_has_function_response,
)


def _text(role, t):
    return NS(role=role, parts=[NS(text=t, function_call=None, function_response=None)])


def _call(name):
    return NS(role="model", parts=[NS(text=None, function_call=NS(name=name), function_response=None)])


def _resp(name):
    # format Gemini/ADK : function_response dans un message role='user'
    return NS(role="user", parts=[NS(text=None, function_call=None, function_response=NS(name=name))])


def _run(contents, keep_recent):
    cb = create_truncate_history_callback(keep_recent=keep_recent)
    req = NS(contents=list(contents))
    cb(callback_context=NS(), llm_request=req)
    return req.contents


def test_helper_detecte_function_response():
    assert _content_has_function_response(_resp("x")) is True
    assert _content_has_function_response(_call("x")) is False
    assert _content_has_function_response(_text("user", "bonjour")) is False
    # exception-safety : parts None / vide / part None
    assert _content_has_function_response(NS(parts=None)) is False
    assert _content_has_function_response(NS(parts=[])) is False
    assert _content_has_function_response(NS(role="user")) is False  # pas d'attr parts


def test_pop_orphelin_en_tete():
    # keep_recent=3, 5 messages -> window = [resp_a, call_b, resp_b] : resp_a ORPHELIN
    # (son call_a est a l'index 1, HORS fenetre) -> doit etre retire.
    u0 = _text("user", "initial")
    ca, ra = _call("A"), _resp("A")
    cb, rb = _call("B"), _resp("B")
    out = _run([u0, ca, ra, cb, rb], keep_recent=3)
    assert ra not in out, "le function_response orphelin en tete doit etre retire"
    assert out[0] is u0
    # apres le user_initial, la fenetre commence par un call (pas un orphelin)
    assert not _content_has_function_response(out[1])


def test_pop_multiple_orphelins_en_tete():
    # window = [resp_x, resp_y, call_z, resp_z] -> retire les DEUX resp de tete
    u0 = _text("user", "initial")
    rx, ry, cz, rz = _resp("X"), _resp("Y"), _call("Z"), _resp("Z")
    out = _run([u0, _call("pre"), rx, ry, cz, rz], keep_recent=4)
    assert rx not in out and ry not in out, "les orphelins multiples en tete doivent partir"
    body = out[1:] if out[0] is u0 else out
    assert not _content_has_function_response(body[0])


def test_call_dangling_conserve():
    # window = [resp_a, call_b] -> pop resp_a -> reste [call_b] (dangling = tour courant, OK)
    u0 = _text("user", "initial")
    ca, ra, cb = _call("A"), _resp("A"), _call("B")
    out = _run([u0, ca, ra, cb], keep_recent=2)
    assert cb in out, "le call dangling (dernier tour) doit etre conserve"
    assert ra not in out


def test_vrai_message_user_texte_conserve():
    # un message user TEXTE en tete de fenetre n'est PAS un orphelin -> garde
    u0 = _text("user", "initial")
    tail = [_text("model", "ok"), _text("user", "et la liste ?"), _call("A"), _resp("A")]
    out = _run([u0] + tail, keep_recent=4)
    assert any(getattr(p, "text", None) == "et la liste ?" for m in out for p in (m.parts or [])), \
        "le vrai message user texte doit etre conserve"


def test_no_op_sans_orphelin():
    # historique court -> aucune troncature
    out = _run([_text("user", "a"), _call("t"), _resp("t")], keep_recent=14)
    assert len(out) == 3
    # historique long SANS orphelin en tete de fenetre -> comportement inchange (que des textes)
    contents = [_text("user", "init")] + [_text("user" if i % 2 else "model", f"m{i}") for i in range(20)]
    out2 = _run(contents, keep_recent=14)
    assert len(out2) <= 15 and out2[0].parts[0].text == "init"
