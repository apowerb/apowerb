"""A model provider that refuses the credentials must reach the caller of
``/run`` as a clear, safe category -- not as an anonymous 500.

The whole ADK server is exercised (``get_fast_api_app``): the agent calls a
local OpenAI-compatible stub through LiteLLM, and the stub answers with the
provider's refusal. What ``/run`` returns is what the workflow engine, the chat
and any API client will see.
"""

from __future__ import annotations

import json
import textwrap
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from apowerb.core.provider_errors import register_provider_error_handlers

_LEAK = "https://provider.example/regenerate?secret=abc123"


def _stub(status: int):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = json.dumps({"message": f"Forbidden: see {_LEAK}"}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):  # silence
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _app(tmp_path, port: int, *, handlers: bool):
    from google.adk.cli.fast_api import get_fast_api_app

    # A fresh name per test: ADK caches agent modules by name for the process.
    name = f"stub{uuid.uuid4().hex[:8]}"
    agent_dir = tmp_path / "agents" / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "__init__.py").write_text("from . import agent\n")
    (agent_dir / "agent.py").write_text(
        textwrap.dedent(
            f"""
            from google.adk.agents import LlmAgent
            from google.adk.models.lite_llm import LiteLlm

            root_agent = LlmAgent(
                name="{name}",
                model=LiteLlm(
                    model="openai/stub-model",
                    api_base="http://127.0.0.1:{port}/v1",
                    api_key="not-a-real-key",
                    num_retries=0,
                ),
                instruction="Answer.",
            )
            """
        )
    )
    app = get_fast_api_app(agents_dir=str(tmp_path / "agents"), web=False)
    if handlers:
        register_provider_error_handlers(app)
    return app, name


def _run(app_and_name):
    app, name = app_and_name
    client = TestClient(app, raise_server_exceptions=False)
    r = client.post(f"/apps/{name}/users/u1/sessions/s1", json={})
    assert r.status_code == 200, r.text
    return client.post(
        "/run",
        json={
            "appName": name,
            "userId": "u1",
            "sessionId": "s1",
            "newMessage": {"role": "user", "parts": [{"text": "hello"}]},
        },
    )


@pytest.mark.parametrize(
    "status, code, http",
    [
        (401, "model_provider_auth", 502),
        (403, "model_provider_auth", 502),
        (429, "model_provider_rate_limit", 429),
    ],
)
def test_provider_refusal_reaches_run_as_a_category(tmp_path, status, code, http):
    server = _stub(status)
    try:
        r = _run(_app(tmp_path, server.server_port, handlers=True))
    finally:
        server.shutdown()
    assert r.status_code == http, r.text
    body = r.json()
    assert body["code"] == code
    assert body["ref"]
    # Nothing the provider said travels back: its text can carry URLs or keys.
    assert "provider.example" not in r.text and "abc123" not in r.text


def test_without_the_handlers_run_is_an_anonymous_500(tmp_path):
    """Documents the defect this module fixes (and that the stub really fails)."""
    server = _stub(403)
    try:
        r = _run(_app(tmp_path, server.server_port, handlers=False))
    finally:
        server.shutdown()
    assert r.status_code == 500


def test_the_application_registers_the_handlers():
    import litellm

    from apowerb.main import app

    assert litellm.exceptions.AuthenticationError in app.exception_handlers
    assert litellm.exceptions.PermissionDeniedError in app.exception_handlers
    assert litellm.exceptions.RateLimitError in app.exception_handlers
