"""One URL declared, four deduced -- instead of five chances to forget one.

Five public URLs each shipped its own localhost default, so an operator had
five separate opportunities to leave one behind, and no way to notice: a
default is indistinguishable from a choice once the settings object is built.

Four of them are not independent facts. A callback path is fixed by the page
that serves it; only the origin varies. They are deduced from
`APP_PUBLIC_URL`.

Explicit always wins: deducing fills a blank, and only from a base that is a
single absolute URL. An installation that configures nothing stays exactly as
it was.

The guarded list holds only settings something reads. `github_redirect_uri`
and `google_redirect_uri` are absent although their names fit -- nothing
consults them, the front computing its own callback and sending it with the
code exchange.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import pytest

from apowerb.configs.settings import _DERIVED_FROM_FRONT, Settings

FRONT = "https://app.example.com"


def _settings(**overrides) -> Settings:
    base = dict(
        working_mode="development",
        encrypt_key="k" * 32,
        rag_webhook_secret="a-real-secret-value",
    )
    base.update(overrides)
    return Settings(**base)



# The values this build ships when the environment says nothing, written out
# rather than read from the class. Reading them from the class would compare
# it to itself and could not notice a default moving underneath.
_SHIPPED_WHEN_NOTHING_IS_SET = {
    "github_integration_redirect_uri": "http://localhost:3000/integrations/github/callback",
    "google_integration_redirect_uri": "http://localhost:3000/integrations/google/callback",
    "frontend_urls": "http://localhost:3000",
    "cors_allowed_origins": "http://localhost:3000",
}


def test_nothing_configured_lands_on_the_documented_defaults():
    """Somebody's laptop, pinned to values rather than to itself.

    Written out as literals on purpose. Comparing a freshly built `Settings`
    to another freshly built `Settings` proves only that construction is
    deterministic; it cannot see a shipped default move, which is exactly what
    this is here to notice.
    """
    cfg = _settings()
    for name, expected in _SHIPPED_WHEN_NOTHING_IS_SET.items():
        assert getattr(cfg, name) == expected, name


def test_the_pinned_defaults_cover_everything_deduced():
    """Positive control on the pin itself: a seventh deduced setting added
    tomorrow must not slip past unpinned."""
    assert set(_SHIPPED_WHEN_NOTHING_IS_SET) == set(_DERIVED_FROM_FRONT)


@pytest.mark.parametrize("name", sorted(_DERIVED_FROM_FRONT))
def test_the_front_url_carries_its_family(name):
    cfg = _settings(app_public_url=FRONT)
    assert getattr(cfg, name).startswith(FRONT), getattr(cfg, name)



def _documented_paths() -> dict[str, str]:
    """The paths `.env.example` documents, for whichever deduced settings it
    happens to mention.

    An anchor outside the code under test: asserting the deduced values
    against the tables that produce them would only prove the tables equal
    themselves.

    Reads whatever that file happens to document rather than a fixed list, so
    documenting one more tomorrow widens the anchor by itself.
    """
    example = Path(__file__).resolve().parents[1] / ".env.example"
    # Only those deduced WITH a path. Two of them -- `frontend_urls` and
    # `cors_allowed_origins` -- are bare origins, and `.env.example` writes
    # the first as a JSON array (`["http://localhost:3000"]`) while its reader
    # in `routers/emailing.py` splits on commas. That disagreement is real and
    # older than this change; anchoring against it would only pin the
    # confusion. `test_frontend_urls_is_deduced_in_the_shape_its_reader_expects`
    # covers that one against the reader instead.
    tables = dict(_DERIVED_FROM_FRONT)
    deduced = {name for name, path in tables.items() if path}
    out: dict[str, str] = {}
    for line in example.read_text(encoding="utf-8").splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        name = key.strip().lower()
        if name in deduced and (path := urlparse(value.strip()).path):
            out[name] = path
    return out


def test_the_anchor_covers_something():
    """Positive control on the anchor itself: if `.env.example` stops naming
    any of them, the test below silently stops checking anything.

    ⚠️ Only one of the deduced settings is documented there today --
    `GITHUB_INTEGRATION_REDIRECT_URI` has no line at all. That is a gap in the
    documentation rather than in this test, and documenting it would widen the
    anchor by itself.
    """
    paths = _documented_paths()
    assert paths, "no deduced setting is documented in .env.example any more"
    assert all(p.startswith("/") for p in paths.values()), paths


def test_every_documented_path_matches_what_is_deduced():
    """The deduced paths, against a source outside the code that builds them.

    Comparing them to the table they come from would prove only that the table
    equals itself. `.env.example` is maintained separately, so a path that
    drifts from what the front serves shows up as a disagreement between two
    files rather than as silence.
    """
    cfg = _settings(app_public_url=FRONT)
    for name, path in _documented_paths().items():
        assert getattr(cfg, name) == f"{FRONT}{path}", name


def test_the_other_deductions_still_hold():
    cfg = _settings(app_public_url=FRONT)
    assert cfg.github_integration_redirect_uri == f"{FRONT}/integrations/github/callback"
    assert cfg.google_integration_redirect_uri == f"{FRONT}/integrations/google/callback"
    assert cfg.cors_allowed_origins == FRONT
    assert cfg.frontend_urls == FRONT


@pytest.mark.parametrize(
    "unusable",
    [
        "",
        "   ",
        "https://a.example,https://b.example",
        # URL-shaped enough to pass a careless check, and both yield a
        # redirect_uri no provider accepts -- silently, a deduced name
        # counting as provided so the guard says nothing.
        "//host.example.com",
        "host.example.com",
        "https://ho st.example.com",
        "https://host.example.com/a b",
    ],
)
def test_a_base_that_is_present_but_unusable_deduces_nothing(unusable, caplog):
    """The oldest trap in this file: a variable declared but empty reads as
    configured and behaves as nothing. Deducing from it built a schemeless
    `/auth/callback`, and the guard stayed quiet because the name WAS in
    `model_fields_set`. A comma is refused for the same reason -- pasting a
    list into a base would put one in the middle of a URI.
    """
    import logging

    with caplog.at_level(logging.WARNING):
        cfg = _settings(app_public_url=unusable)
    nom = "github_integration_redirect_uri"
    assert getattr(cfg, nom) == Settings.model_fields[nom].default
    assert nom in caplog.text


@pytest.mark.parametrize("name", sorted(set(_DERIVED_FROM_FRONT)))
def test_a_value_somebody_typed_is_never_overridden(name):
    """The rule that makes this safe to ship: deducing fills a blank, it does
    not correct anybody."""
    mine = "https://typed-by-hand.example/whatever"
    cfg = _settings(app_public_url=FRONT, **{name: mine})
    assert getattr(cfg, name) == mine


def test_a_trailing_slash_on_the_base_does_not_double_up():
    cfg = _settings(app_public_url=FRONT + "/")
    assert "//integrations" not in cfg.github_integration_redirect_uri


def test_deducing_does_not_make_the_guard_cry_wolf(caplog):
    """The interaction with the warning added earlier.

    That guard reports a public URL left at its localhost default while its
    neighbours were configured, and it reads `model_fields_set` -- which a
    deduced value never enters. Without care it would name six settings that
    are now perfectly correct, which is exactly the noise it exists to avoid.

    It may still name `root_path`, and that is not wolf-crying: nothing
    deduces it, every deployment measured sets it by hand, and forgetting it
    leaves this server calling itself on a port it may not answer.
    """
    import logging

    with caplog.at_level(logging.WARNING):
        _settings(app_public_url=FRONT)
    for deduced in set(_DERIVED_FROM_FRONT):
        assert deduced not in caplog.text, f"{deduced} named though it was deduced"


def test_the_guard_still_speaks_when_a_base_itself_is_missing(caplog):
    """The case that made the guard worth having: `app_public_url` absent
    while the rest is configured. Deducing must not swallow it -- nothing can
    deduce a base."""
    import logging

    with caplog.at_level(logging.WARNING):
        _settings(cors_allowed_origins=FRONT, frontend_urls=FRONT,
              github_integration_redirect_uri=FRONT + '/integrations/github/callback')
    assert "app_public_url" in caplog.text


def test_frontend_urls_is_deduced_in_the_shape_its_reader_expects():
    """`routers/emailing.py` does `frontend_urls.split(",")` -- a
    comma-separated list, not the JSON array `.env.example` shows. A bare URL
    survives that split; a JSON array would come back out with its brackets.
    """
    cfg = _settings(app_public_url=FRONT)
    assert cfg.frontend_urls.split(",")[0].strip().rstrip("/") == FRONT


@pytest.mark.parametrize(
    "usable",
    [
        "https://host.example.com",
        "http://localhost:3000",
        "https://host.example.com:8443",
        "https://host.example.com/app",
        "https://host.example.com////",
        "  https://host.example.com  ",
    ],
)
def test_a_well_formed_base_is_accepted(usable):
    """The negative controls above prove the check refuses things. This one
    proves it still accepts the shapes deployments actually use -- a check
    that refused everything would pass them all for the wrong reason."""
    cfg = _settings(app_public_url=usable)
    deduit = cfg.github_integration_redirect_uri
    assert deduit.endswith("/integrations/github/callback")
    assert deduit.startswith(usable.strip().rstrip("/"))
