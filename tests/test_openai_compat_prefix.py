"""Un modele qui a son propre provider litellm garde son prefixe, sur les
quatre chemins qui parlent a un endpoint OpenAI-compatible.

``openai/`` et ``azure_ai/`` passent intacts, tout le reste devient
``openai/<nom>``.
"""

from __future__ import annotations

AZURE = "azure_ai/mistral-large"
BASE = "https://foundry.example/v1"


def _capture_litellm(monkeypatch, module, captured):
    """Remplace ``LiteLlm`` par un double qui garde les kwargs recus."""

    def fake_litellm(**kwargs):
        captured.update(kwargs)
        return type("Model", (), kwargs)()

    monkeypatch.setattr(module, "LiteLlm", fake_litellm)


def _capture_acompletion(captured):
    """Double de ``litellm.acompletion`` : garde les kwargs puis coupe l'appel."""

    async def fake_acompletion(**kwargs):
        captured.update(kwargs)
        raise RuntimeError("appel reseau coupe, seuls les kwargs nous interessent")

    return fake_acompletion


def test_agent_builder_keeps_the_azure_ai_prefix(monkeypatch):
    from apowerb.core.agent_helpers import llm_model_builder as builder

    monkeypatch.setattr(builder, "decrypt_value_in_dict", lambda d, **_: d)
    captured: dict = {}
    _capture_litellm(monkeypatch, builder, captured)

    builder.build_litellm_model(
        {
            "agent_model": AZURE,
            "agent_model_params": {"model_api_base": BASE, "model_api_key": "k"},
        },
        temperature=None,
    )

    assert captured["model"] == AZURE


def test_workflow_suggest_keeps_the_azure_ai_prefix(monkeypatch):
    from apowerb.core import workflow_suggest

    class _Settings:
        workflow_suggest_model = AZURE
        default_llm_model = AZURE
        default_llm_api_key = "k"
        default_llm_api_base = BASE
        workflow_suggest_timeout_s = 30

    monkeypatch.setattr("apowerb.configs.settings.get_settings", lambda: _Settings())

    kwargs = workflow_suggest._completion_kwargs([{"role": "user", "content": "x"}])

    assert kwargs["model"] == AZURE


async def test_title_generator_keeps_the_azure_ai_prefix(monkeypatch):
    import litellm

    from apowerb.helpers import title_generator

    monkeypatch.setattr(
        title_generator, "_resolve_model_creds",
        lambda _agent_id: (AZURE, {"model_api_key": "k", "model_api_base": BASE}),
    )
    captured: dict = {}
    monkeypatch.setattr(litellm, "acompletion", _capture_acompletion(captured))

    await title_generator.generate_session_title("bonjour", agent_id="agent1")

    assert captured.get("model") == AZURE


async def test_json_repair_keeps_the_azure_ai_prefix(monkeypatch):
    from pydantic import BaseModel

    from apowerb.core.agent_helpers import callbacks

    class _Schema(BaseModel):
        name: str

    captured: dict = {}
    monkeypatch.setattr(
        callbacks.litellm, "acompletion", _capture_acompletion(captured)
    )

    await callbacks._attempt_json_repair(
        _Schema,
        "pas du json",
        AZURE,
        repair_api_key="k",
        repair_api_base=BASE,
    )

    assert captured.get("model") == AZURE


def test_one_definition_of_the_rule(monkeypatch):
    """La regle vit dans ``helpers.model_names`` seulement : le builder et les
    outils SQL appellent cette fonction, pas une copie locale."""
    from apowerb.core.agent_helpers import llm_model_builder as builder
    from apowerb.helpers import model_names
    from apowerb.tools_store.portfolio import text_to_sql

    assert builder.openai_compat_model_name is model_names.openai_compat_model_name

    monkeypatch.setattr(model_names, "openai_compat_model_name", lambda m: "sentinel")
    state = type(
        "S", (), {"model": AZURE, "api_base": BASE, "api_key": "k", "api_version": None}
    )()

    assert text_to_sql._llm_endpoint_kwargs(state)["model"] == "sentinel"
