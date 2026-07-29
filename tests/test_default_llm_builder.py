"""Cablage du modele « thaink2 par defaut » dans le builder LiteLlm et
dans la validation de modele a l'ecriture.

Le test qui compte vraiment : `test_agent_carried_credentials_are_ignored`.
Si un jour il tombe, un utilisateur peut faire payer la cle mutualisee
thaink2 sur son propre endpoint.
"""
import pytest

from th2agent.core.agent_helpers import default_llm as dl
from th2agent.core.agent_helpers import llm_model_builder as builder


class _FakeSettings:
    def __init__(self, model="", key="", base=""):
        self.default_llm_model = model
        self.default_llm_api_key = key
        self.default_llm_api_base = base


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(
        dl, "get_settings", lambda: _FakeSettings("gemini/gemini-2.5-flash", "sk-shared")
    )


@pytest.fixture
def configured_openai_compat(monkeypatch):
    monkeypatch.setattr(
        dl,
        "get_settings",
        lambda: _FakeSettings("mistral/small", "sk-shared", "https://ovh.example/v1"),
    )


@pytest.fixture(autouse=True)
def _no_encryption(monkeypatch):
    """Le builder dechiffre les params ; en test la valeur est deja en clair."""
    monkeypatch.setattr(builder, "decrypt_value_in_dict", lambda d, **_: d)


def test_builder_uses_env_model_and_key(configured, monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    model = builder.build_litellm_model(
        {"agent_model": "thaink2/default", "agent_model_params": {}}, temperature=None
    )
    assert model.model == "gemini/gemini-2.5-flash"
    # Gemini passe par l'env var, cf. commentaire du builder
    import os

    assert os.environ["GEMINI_API_KEY"] == "sk-shared"


def test_agent_carried_credentials_are_ignored(configured, monkeypatch):
    """Cle et endpoint plantes sur l'agent ne doivent jamais servir."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    model = builder.build_litellm_model(
        {
            "agent_model": "thaink2/default",
            "agent_model_params": {
                "model_api_key": "sk-attacker",
                "model_api_base": "https://evil.example/v1",
            },
        },
        temperature=None,
    )
    assert model.model == "gemini/gemini-2.5-flash"
    assert "evil.example" not in str(getattr(model, "api_base", "") or "")
    import os

    assert os.environ["GEMINI_API_KEY"] == "sk-shared"


def test_builder_honours_configured_api_base(configured_openai_compat):
    model = builder.build_litellm_model(
        {"agent_model": "thaink2/default", "agent_model_params": {}}, temperature=0.3
    )
    # api_base configure -> le builder force le prefixe openai/
    assert model.model.startswith("openai/")


def test_builder_leaves_a_normal_agent_alone(configured):
    model = builder.build_litellm_model(
        {
            "agent_model": "mistral/mistral-large-latest",
            "agent_model_params": {"model_api_key": "sk-user"},
        },
        temperature=None,
    )
    assert model.model.endswith("mistral-large-latest")


def test_validation_accepts_the_default_model_when_configured(configured):
    builder.validate_agent_model("thaink2/default")  # ne doit pas lever


def test_validation_rejects_the_default_model_when_not_configured(monkeypatch):
    """Sinon l'agent serait cree puis muet a chaque tour -- le bug que
    validate_agent_model existe justement pour empecher."""
    monkeypatch.setattr(dl, "get_settings", lambda: _FakeSettings())
    with pytest.raises(ValueError) as exc:
        builder.validate_agent_model("thaink2/default")
    assert "DEFAULT_LLM_MODEL" in str(exc.value)
