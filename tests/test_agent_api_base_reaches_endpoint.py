"""L'API URL saisie sur un agent doit etre CELLE que LiteLLM appelle.

Demande de Farid (21/09) : Azure AI Foundry, modele heberge chez le client,
fournisseur OpenAI-compatible (LiteLLM). Stocker ``model_api_base`` ne prouve
rien ; ce test monte un faux endpoint OpenAI-compatible et verifie qu'un vrai
tour de modele y arrive, avec la cle et le nom de modele de l'agent.
"""

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from google.adk.models.llm_request import LlmRequest
from google.genai import types

from apowerb.core.agent_helpers import llm_model_builder as builder


@pytest.fixture(autouse=True)
def _no_encryption(monkeypatch):
    monkeypatch.setattr(builder, "decrypt_value_in_dict", lambda d, **_: d)


@pytest.fixture
def fake_endpoint():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(
                {
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "body": body,
                }
            )
            reply = {
                "id": "x",
                "object": "chat.completion",
                "created": 0,
                "model": body["model"],
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "pong"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
            data = json.dumps(reply).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/v1", seen
    server.shutdown()


async def _one_turn(llm):
    req = LlmRequest(
        model=llm.model,
        contents=[types.Content(role="user", parts=[types.Part(text="ping")])],
        config=types.GenerateContentConfig(),
    )
    out = []
    async for resp in llm.generate_content_async(req, stream=False):
        out.append(resp)
    return out


def test_agent_api_base_is_the_url_litellm_calls(fake_endpoint):
    url, seen = fake_endpoint
    llm = builder.build_litellm_model(
        {
            "agent_model": "openai/meta-llama-3-70b-instruct",
            "agent_model_params": {
                "model_api_key": "sk-test-agent",
                "model_api_base": url,
            },
        },
        temperature=None,
    )
    responses = asyncio.run(_one_turn(llm))

    assert len(seen) == 1
    assert seen[0]["path"] == "/v1/chat/completions"
    assert seen[0]["auth"] == "Bearer sk-test-agent"
    assert seen[0]["body"]["model"] == "meta-llama-3-70b-instruct"
    assert responses[-1].content.parts[0].text == "pong"


def test_url_overrides_the_provider_default_endpoint(fake_endpoint):
    """Une URL saisie l'emporte sur l'endpoint par defaut du fournisseur
    (``mistral/`` part sinon sur OVH) ; le modele est appele en OpenAI-compat."""
    url, seen = fake_endpoint
    llm = builder.build_litellm_model(
        {
            "agent_model": "mistral/mistral-small",
            "agent_model_params": {"model_api_key": "k", "model_api_base": url},
        },
        temperature=None,
    )
    asyncio.run(_one_turn(llm))
    assert len(seen) == 1
    assert seen[0]["body"]["model"] == "mistral-small"


def test_without_url_default_provider_keeps_its_own_endpoint():
    llm = builder.build_litellm_model(
        {
            "agent_model": "anthropic/claude-sonnet-4-5",
            "agent_model_params": {"model_api_key": "k"},
        },
        temperature=None,
    )
    assert llm.model == "anthropic/claude-sonnet-4-5"
    assert "api_base" not in llm._additional_args
