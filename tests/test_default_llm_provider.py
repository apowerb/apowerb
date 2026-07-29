"""Modele « thaink2 par defaut » : la cle mutualisee ne doit JAMAIS
etre lisible par l'utilisateur (demande Farid, 27/07/26).

Deux garanties testees ici :
1. resolution -- un agent en ``thaink2/default`` tire (modele, cle, base)
   de l'environnement serveur, et ce que l'agent porte en base est ignore ;
2. masquage -- l'API ne renvoie jamais une cle en clair, et un PUT qui
   repond avec le masque ne doit pas ecraser la cle stockee.
"""
import pytest

from th2agent.core.agent_helpers import default_llm as dl


class _FakeSettings:
    def __init__(self, model="", key="", base=""):
        self.default_llm_model = model
        self.default_llm_api_key = key
        self.default_llm_api_base = base


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setattr(
        dl,
        "get_settings",
        lambda: _FakeSettings("gemini/gemini-2.5-flash", "sk-thaink2-secret", ""),
    )


@pytest.fixture
def not_configured(monkeypatch):
    monkeypatch.setattr(dl, "get_settings", lambda: _FakeSettings())


# --------------------------------------------------------------- resolution


def test_default_model_id_is_recognized():
    assert dl.is_default_llm_model("thaink2/default")
    assert dl.is_default_llm_model("  thaink2/default  ")
    assert not dl.is_default_llm_model("gemini/gemini-2.5-flash")
    assert not dl.is_default_llm_model("")
    assert not dl.is_default_llm_model(None)


def test_available_only_when_model_and_key_are_set(monkeypatch):
    monkeypatch.setattr(dl, "get_settings", lambda: _FakeSettings("gemini/x", "k"))
    assert dl.default_llm_available()
    monkeypatch.setattr(dl, "get_settings", lambda: _FakeSettings("gemini/x", ""))
    assert not dl.default_llm_available()
    monkeypatch.setattr(dl, "get_settings", lambda: _FakeSettings("", "k"))
    assert not dl.default_llm_available()


def test_resolve_passes_through_a_normal_model(configured):
    model, params = dl.resolve_model_credentials(
        "gemini/gemini-2.5-pro", {"model_api_key": "sk-user"}
    )
    assert model == "gemini/gemini-2.5-pro"
    assert params == {"model_api_key": "sk-user"}


def test_resolve_substitutes_env_credentials(configured):
    model, params = dl.resolve_model_credentials("thaink2/default", {})
    assert model == "gemini/gemini-2.5-flash"
    assert params["model_api_key"] == "sk-thaink2-secret"


def test_resolve_ignores_anything_the_agent_carries(configured):
    """Une cle plantee en base sur un agent « default » ne doit pas etre
    utilisee : sinon un utilisateur pourrait faire payer thaink2 pour son
    propre endpoint, ou detourner le trafic vers un api_base a lui."""
    model, params = dl.resolve_model_credentials(
        "thaink2/default",
        {"model_api_key": "sk-attacker", "model_api_base": "https://evil.example/v1"},
    )
    assert model == "gemini/gemini-2.5-flash"
    assert params["model_api_key"] == "sk-thaink2-secret"
    assert "model_api_base" not in params


def test_resolve_api_base_only_when_configured(monkeypatch):
    monkeypatch.setattr(
        dl, "get_settings", lambda: _FakeSettings("openai/qwen", "k", "https://ovh/v1")
    )
    _, params = dl.resolve_model_credentials("thaink2/default", {})
    assert params["model_api_base"] == "https://ovh/v1"


def test_resolve_raises_when_not_configured(not_configured):
    with pytest.raises(ValueError) as exc:
        dl.resolve_model_credentials("thaink2/default", {})
    assert "DEFAULT_LLM_MODEL" in str(exc.value)


def test_resolve_tolerates_none_params(configured):
    model, params = dl.resolve_model_credentials("thaink2/default", None)
    assert model == "gemini/gemini-2.5-flash"
    assert params["model_api_key"] == "sk-thaink2-secret"


# ------------------------------------------------------------------ ecriture


def test_strip_removes_credentials_on_default_model():
    """Rien a persister pour un agent « default » : ni cle, ni endpoint."""
    out = dl.strip_default_llm_params(
        "thaink2/default",
        {"model_api_key": "sk-x", "model_api_base": "https://x/v1", "temperature": 0.2},
    )
    assert out == {"temperature": 0.2}


def test_strip_leaves_a_normal_agent_untouched():
    params = {"model_api_key": "sk-user", "model_api_base": "https://x/v1"}
    assert dl.strip_default_llm_params("gemini/gemini-2.5-flash", params) == params


# ------------------------------------------------------------------ masquage


def test_mask_replaces_a_stored_key_with_a_sentinel():
    masked = dl.mask_model_api_key({"model_api_key": "sk-user", "temperature": 0.2})
    assert masked["model_api_key"] == dl.MASKED_API_KEY
    assert masked["temperature"] == 0.2


def test_mask_leaves_empty_key_empty():
    """Champ vide cote UI = « aucune cle enregistree », a distinguer du masque."""
    assert dl.mask_model_api_key({"model_api_key": ""})["model_api_key"] == ""
    assert dl.mask_model_api_key({})== {}


def test_mask_does_not_mutate_its_input():
    src = {"model_api_key": "sk-user"}
    dl.mask_model_api_key(src)
    assert src["model_api_key"] == "sk-user"


def test_unmask_restores_the_stored_key():
    """Le front renvoie le masque tel quel au PUT : sans ce recollage, la
    sauvegarde d'un simple renommage effacerait la cle de l'agent."""
    out = dl.unmask_model_api_key(
        {"model_api_key": dl.MASKED_API_KEY, "temperature": 0.5},
        {"model_api_key": "sk-user"},
    )
    assert out["model_api_key"] == "sk-user"
    assert out["temperature"] == 0.5


def test_unmask_keeps_a_real_new_key():
    out = dl.unmask_model_api_key(
        {"model_api_key": "sk-new"}, {"model_api_key": "sk-old"}
    )
    assert out["model_api_key"] == "sk-new"


def test_unmask_allows_clearing_the_key():
    """Vider le champ reste une action volontaire de suppression."""
    out = dl.unmask_model_api_key({"model_api_key": ""}, {"model_api_key": "sk-old"})
    assert out["model_api_key"] == ""


def test_unmask_without_stored_key_drops_the_sentinel():
    out = dl.unmask_model_api_key({"model_api_key": dl.MASKED_API_KEY}, {})
    assert out["model_api_key"] == ""


def test_unmask_tolerates_none_and_json_strings():
    assert dl.unmask_model_api_key(None, {"model_api_key": "sk"}) == {}
    out = dl.unmask_model_api_key(
        '{"model_api_key": "__unchanged__"}', '{"model_api_key": "sk-old"}'
    )
    assert out["model_api_key"] == "sk-old"
