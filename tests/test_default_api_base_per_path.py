"""L'endpoint implique par le prefixe du modele est pose sur TOUS les chemins.

``edenai/`` et le catalogue OVH Mistral portent chacun un endpoint par defaut.
Un agent qui ne renseigne pas ``model_api_base`` doit le recevoir partout, pas
seulement dans ``build_litellm_model`` : les outils SQL, le generateur de titre
et la reparation JSON appellent litellm de leur cote.
"""

from __future__ import annotations

from apowerb.helpers.model_names import (
    EDENAI_API_BASE,
    OVH_MISTRAL_API_BASE,
    default_api_base,
)

EDENAI_MODEL = "edenai/anthropic/claude-haiku-5-5"
OVH_MODEL = "mistral/Mistral-Small-3.2-24B-Instruct-2506"


def test_the_rule_itself():
    assert default_api_base(EDENAI_MODEL) == EDENAI_API_BASE
    assert default_api_base(OVH_MODEL) == OVH_MISTRAL_API_BASE
    # Un id de l'API Mistral est en minuscules et va chez Mistral, pas chez OVH.
    assert default_api_base("mistral/mistral-large-latest") is None
    assert default_api_base("anthropic/claude-haiku-5-5") is None
    assert default_api_base("") is None


def test_path_agent_builder(monkeypatch):
    from apowerb.core.agent_helpers import llm_model_builder as builder

    monkeypatch.setattr(builder, "decrypt_value_in_dict", lambda d, **_: d)
    captured: dict = {}
    monkeypatch.setattr(
        builder,
        "LiteLlm",
        lambda **kwargs: (captured.update(kwargs), type("M", (), kwargs)())[1],
    )

    builder.build_litellm_model(
        {
            "agent_model": EDENAI_MODEL,
            "agent_model_params": {"model_api_key": "sk-eden"},
        },
        temperature=None,
    )

    assert captured["api_base"] == EDENAI_API_BASE


def test_path_sql_tools(monkeypatch):
    from apowerb.tools_store.portfolio import text_to_sql

    import apowerb.core.agent_helpers as helpers

    monkeypatch.setattr(
        helpers,
        "load_agent_model_params",
        lambda _id: (EDENAI_MODEL, {"model_api_key": "sk-eden"}),
        raising=False,
    )

    model, api_base, api_key, _version = text_to_sql._agent_llm_endpoint(1)

    assert model == EDENAI_MODEL
    assert api_base == EDENAI_API_BASE
    assert api_key == "sk-eden"


async def test_path_title_generator(monkeypatch):
    import litellm

    from apowerb.helpers import title_generator

    monkeypatch.setattr(
        title_generator,
        "_resolve_model_creds",
        lambda _agent_id: (EDENAI_MODEL, {"model_api_key": "sk-eden"}),
    )
    captured: dict = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("appel reseau coupe")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    await title_generator.generate_session_title("bonjour", agent_id="agent1")

    assert captured.get("api_base") == EDENAI_API_BASE
    assert captured.get("model") == "openai/anthropic/claude-haiku-5-5"


async def test_path_json_repair(monkeypatch):
    """La resolution se fait dans agent_utils, qui alimente le callback."""
    from pydantic import BaseModel

    from apowerb.core.agent_helpers import callbacks

    class _Schema(BaseModel):
        name: str

    captured: dict = {}

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("appel reseau coupe")

    monkeypatch.setattr(callbacks.litellm, "acompletion", fake_acompletion)

    await callbacks._attempt_json_repair(
        _Schema,
        "pas du json",
        EDENAI_MODEL,
        repair_api_key="sk-eden",
        repair_api_base=default_api_base(EDENAI_MODEL),
    )

    assert captured.get("api_base") == EDENAI_API_BASE
    assert captured.get("model") == "openai/anthropic/claude-haiku-5-5"
