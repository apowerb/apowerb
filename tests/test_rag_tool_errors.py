"""RAG tool errors: 'do not retry' only for configuration problems."""

from __future__ import annotations

import httpx

from apowerb.tools_store.portfolio.rag import _failure


def _resp(status, text="x" * 1000):
    return httpx.Response(status, text=text, request=httpx.Request("GET", "http://rag/x"))


def test_404_names_the_faulty_parameter_and_truncates_body():
    out = _failure("get knowledge base", resp=_resp(404), param="knowledge_id")
    assert "`knowledge_id`" in out["message"]
    assert "Do not retry" not in out["message"]
    assert len(out["message"]) < 500


def test_422_is_correctable():
    out = _failure("create knowledge", resp=_resp(422), param="files")
    assert out["retry"] is True and "`files`" in out["message"]


def test_401_is_a_configuration_issue():
    out = _failure("list knowledge bases", resp=_resp(401))
    assert out["retry"] is False and "Do not retry" in out["message"]


def test_5xx_is_retryable():
    exc = httpx.HTTPStatusError("e", request=_resp(503).request, response=_resp(503))
    assert _failure("x", exc)["retry"] is True
