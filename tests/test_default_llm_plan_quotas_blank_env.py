"""``default_llm_plan_quotas`` is a ``dict[str, int]``, so pydantic-settings
JSON-decodes it from the raw environment string. ``.env.example`` shipped
``DEFAULT_LLM_PLAN_QUOTAS=`` -- declared, but blank -- which reads back as
``""``, not absent. ``json.loads("")`` raises, and that exception propagated
out of ``Settings()`` on the first import, before the app served a single
route: any operator who copied ``.env.example`` verbatim could not boot the
server at all.
"""

from __future__ import annotations

from apowerb.configs.settings import Settings


def test_blank_env_value_is_the_empty_dict(monkeypatch):
    monkeypatch.setenv("DEFAULT_LLM_PLAN_QUOTAS", "")

    settings = Settings(_env_file=None)

    assert settings.default_llm_plan_quotas == {}


def test_a_real_json_object_still_parses(monkeypatch):
    monkeypatch.setenv(
        "DEFAULT_LLM_PLAN_QUOTAS", '{"free": 1000000, "pro": 50000000}'
    )

    settings = Settings(_env_file=None)

    assert settings.default_llm_plan_quotas == {"free": 1000000, "pro": 50000000}
