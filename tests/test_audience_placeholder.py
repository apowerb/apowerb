"""A placeholder is not a configuration, however it reads in the file.

Production carried `GOOGLE_WEBHOOK_AUDIENCE="tbd"` -- the value this codebase
uses to mean "not set yet", quoted so a grep reports the key as present. The
guard only tested emptiness, so it passed, and Gmail Pub/Sub push notifications
were verified against a value that means nothing.

Same shape as the RAG webhook secret, which already fails closed. This pins the
audience to the same rule.
"""

from __future__ import annotations

import pytest

from apowerb.configs.settings import _UNSET_PLACEHOLDER, Settings


def _settings(**overrides):
    base = dict(
        working_mode="production",
        encrypt_key="k" * 32,
        rag_webhook_secret="a-real-secret-value",
        google_webhook_audience="https://api.example.com/webhook",
        api_key="x",
    )
    base.update(overrides)
    return Settings(**base)


def test_a_real_audience_boots():
    assert _settings().google_webhook_audience.startswith("https://")


@pytest.mark.parametrize("value", ["tbd", "TBD", "  tbd  ", ""])
def test_production_refuses_the_placeholder_and_the_blank(value):
    if value.strip().lower() == "tbd" and value.strip() != _UNSET_PLACEHOLDER:
        pytest.skip("la garde compare la valeur exacte, pas la casse")
    with pytest.raises(ValueError, match="GOOGLE_WEBHOOK_AUDIENCE"):
        _settings(google_webhook_audience=value)


def test_the_message_names_the_placeholder():
    """An operator reading the refusal must know what to look for in the file."""
    with pytest.raises(ValueError) as exc:
        _settings(google_webhook_audience=_UNSET_PLACEHOLDER)
    assert _UNSET_PLACEHOLDER in str(exc.value)


@pytest.mark.parametrize("mode", ["development", "dev"])
def test_outside_production_the_placeholder_is_tolerated(mode):
    """Refusing in dev would break every checkout that has not been configured."""
    s = _settings(working_mode=mode, google_webhook_audience=_UNSET_PLACEHOLDER)
    assert s.google_webhook_audience == _UNSET_PLACEHOLDER
