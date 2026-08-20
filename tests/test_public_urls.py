"""A public URL left at its localhost default, in a deployment that has some.

Found by inspecting deployed environments rather than reading the settings
file. One of them ran `WORKING_MODE=prod` with its public URLs on a real
domain -- and `APP_PUBLIC_URL` simply absent, so it fell back to
`http://localhost:3000`. That setting is what `auth/service.py` builds
password-reset and e-mail-verification links from: every such mail sent from
that installation carried a link to the recipient's own machine.

Nothing failed. Nothing logged. The value reads as configured because a
default is indistinguishable from a choice once the object is built.

The guard therefore does not ask "is this localhost". A localhost URL is the
right answer for a developer, and the codebase already learned that a warning
firing on every boot becomes part of the noise (cf. the RAG webhook secret).
It asks a narrower question: **was this one forgotten while its neighbours
were configured?** An installation that set none of them is somebody's
laptop; one that set six out of nine has a hole.
"""

from __future__ import annotations

import logging

import pytest

from apowerb.configs.settings import (
    _PUBLIC_URL_SETTINGS,
    Settings,
    _public_urls_left_behind,
)

A_DOMAIN = "https://api.example.com"
A_FRONT = "https://app.example.com"


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


def _all_configured() -> dict[str, str]:
    """Every public URL set to something that is not localhost."""
    return {
        name: (A_FRONT if "3000" in str(Settings.model_fields[name].default) else A_DOMAIN)
        for name in _PUBLIC_URL_SETTINGS
    }


def test_the_guarded_list_is_not_empty_and_ships_localhost_defaults():
    """A positive control on the list itself. Were every default to move off
    localhost one day, this test says so instead of the guard quietly
    protecting nothing."""
    assert len(_PUBLIC_URL_SETTINGS) >= 5
    shipped = [
        str(Settings.model_fields[n].default) for n in _PUBLIC_URL_SETTINGS
    ]
    assert all("localhost" in d for d in shipped), shipped


def test_a_laptop_says_nothing(caplog):
    """Nothing configured: that is a developer, not a hole. Silence."""
    with caplog.at_level(logging.WARNING):
        _settings(working_mode="development")
    assert "PUBLIC URL" not in caplog.text


def test_a_fully_configured_deployment_says_nothing(caplog):
    with caplog.at_level(logging.WARNING):
        _settings(**_all_configured())
    assert "PUBLIC URL" not in caplog.text


def test_one_forgotten_among_configured_neighbours_is_named(caplog):
    """The case found in the wild, reduced to its bones."""
    cfg = _all_configured()
    del cfg["app_public_url"]
    with caplog.at_level(logging.WARNING):
        _settings(**cfg)
    assert "app_public_url" in caplog.text
    assert "PUBLIC URL" in caplog.text


def test_the_warning_names_only_what_was_forgotten(caplog):
    cfg = _all_configured()
    del cfg["app_public_url"]
    with caplog.at_level(logging.WARNING):
        _settings(**cfg)
    for still_set in cfg:
        assert still_set not in caplog.text, still_set


def test_setting_localhost_on_purpose_is_not_forgotten(caplog):
    """Explicit beats default. All three deployed environments carry
    `CORS_ALLOWED_ORIGINS=http://localhost:3000` on purpose; flagging a value
    somebody typed would teach the operator to ignore this line."""
    cfg = _all_configured()
    cfg["cors_allowed_origins"] = "http://localhost:3000"
    del cfg["app_public_url"]
    with caplog.at_level(logging.WARNING):
        _settings(**cfg)
    assert "app_public_url" in caplog.text
    assert "cors_allowed_origins" not in caplog.text


@pytest.mark.parametrize("mode", ["production", "prod", "development", "dev"])
def test_it_never_refuses_to_boot(mode):
    """Deliberately a warning, not a refusal — and this test is the record of
    why. A running deployment is in exactly this state; a refusal would take
    it down at its next restart, to punish a link that has been wrong for a
    while. Fail-closed becomes right once deployments carry the value, and this
    test is what must be changed then, on purpose."""
    cfg = _all_configured()
    del cfg["app_public_url"]
    assert _settings(working_mode=mode, **cfg) is not None


# ---------------------------------------------------------------------------
# The decision itself, fed with data.
#
# Both tests below were written because a mutation survived the first version
# of this file: deleting the "only settings whose default mentions localhost"
# filter left all ten tests green, while the docstring in settings.py claimed
# they protected exactly that. A guard nobody can see fail is a guard that
# proves nothing.
# ---------------------------------------------------------------------------


def test_a_guarded_setting_whose_default_is_not_localhost_is_never_reported():
    """The rule is "left on its shipped localhost default", not "unset".

    A setting that ships a real default, or none, is not a hole when the
    environment stays quiet about it. Drop that filter and this test says so.
    """
    a_name = _PUBLIC_URL_SETTINGS[0]
    reported = _public_urls_left_behind(
        fields_set=set(),
        defaults={a_name: "https://api.example.com"},
    )
    assert reported == []

    # Positive control on the same call shape: with a localhost default, the
    # very same input IS reported — so the empty list above means "filtered",
    # not "this function never returns anything".
    assert _public_urls_left_behind(
        fields_set=set(),
        defaults={a_name: "http://localhost:3000"},
    ) == [a_name]


def test_a_renamed_setting_does_not_take_the_service_down():
    """The list is a hard-coded tuple and this runs before anything else.

    Renaming one of those fields in an unrelated change must degrade the
    guard, never stop the process from starting — so a missing name is
    absent from the defaults it is handed.
    """
    assert _public_urls_left_behind(fields_set=set(), defaults={}) == []
