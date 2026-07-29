"""Per-org visibility for SuperAgent templates.

Templates may carry ``visible_to_orgs: list[str]`` to restrict them to
specific organisations. SCEI membership is derived from the user's email
domain (same convention as ``scei.dependencies.scei_required``).

These tests guard:

  - the new ``user`` parameter on ``list_superagent_templates`` /
    ``get_superagent_template`` filters correctly,
  - ``user=None`` keeps the legacy "all-templates" behaviour for system
    callers,
  - the SCEI template specifically is hidden from non-SCEI users,
  - templates without ``visible_to_orgs`` (the vast majority) keep
    showing for everyone.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from th2agent.configs.settings import get_settings
from th2agent.core.superagents import (
    SUPERAGENT_TEMPLATES,
    _user_org_slugs,
    get_superagent_template,
    list_superagent_templates,
)


def _user(email: str | None) -> SimpleNamespace:
    """Minimal stand-in for the User pydantic model."""
    return SimpleNamespace(email=email)


@pytest.fixture(autouse=True)
def scei_org_mapping(monkeypatch):
    """Inject the ``scei88.fr → scei`` mapping into ``settings.org_domain_slugs``.

    After PR #143 ripped the SCEI hardcode out of the codebase, the
    org→email mapping lives in the ``ORG_DOMAIN_SLUGS`` env var and is
    empty by default. The SCEI visibility tests need *some* mapping to
    exercise the SCEI / non-SCEI branches — they were written against
    the old hardcoded `settings.scei_org_domain="scei88.fr"`. We restore
    that exact mapping here so each test runs against a known-good
    config, regardless of what the deploying environment provides.
    """
    settings = get_settings()
    monkeypatch.setattr(
        settings, "org_domain_slugs", {"scei88.fr": "scei"}, raising=False
    )


# ---------------------------------------------------------------------------
# org-slug derivation
# ---------------------------------------------------------------------------


class TestUserOrgSlugs:
    def test_none_user_yields_empty_set(self):
        assert _user_org_slugs(None) == set()

    def test_user_without_email_yields_empty_set(self):
        assert _user_org_slugs(_user(None)) == set()
        assert _user_org_slugs(_user("")) == set()

    def test_scei_email_yields_scei_slug(self):
        assert "scei" in _user_org_slugs(_user("alice@scei88.fr"))

    def test_scei_email_uppercase_still_matched(self):
        assert "scei" in _user_org_slugs(_user("BOB@SCEI88.fr"))

    def test_non_scei_email_yields_empty_set(self):
        assert _user_org_slugs(_user("alice@example.com")) == set()

    def test_substring_match_does_not_count(self):
        # "evil-scei88.fr" must NOT be matched as scei88.fr.
        assert _user_org_slugs(_user("alice@evil-scei88.fr")) == set()
        # "alice-scei88.fr" with no @ is also not a SCEI user.
        assert _user_org_slugs(_user("alice-scei88.fr")) == set()


# ---------------------------------------------------------------------------
# list_superagent_templates
# ---------------------------------------------------------------------------


class TestListVisibility:
    def test_none_user_returns_full_catalog(self):
        """Internal / system callers (no auth) keep seeing everything."""
        out = list_superagent_templates(user=None)
        assert len(out) == len(SUPERAGENT_TEMPLATES)

    def test_no_arg_defaults_to_full_catalog(self):
        """Backward compatibility — pre-existing call sites pass no user."""
        assert len(list_superagent_templates()) == len(SUPERAGENT_TEMPLATES)

    def test_non_scei_user_does_not_see_scei_template(self):
        out = list_superagent_templates(user=_user("alice@example.com"))
        ids = {t["template_id"] for t in out}
        assert "scei_ar_assistant" not in ids

    def test_scei_user_sees_scei_template(self):
        out = list_superagent_templates(user=_user("ops@scei88.fr"))
        ids = {t["template_id"] for t in out}
        assert "scei_ar_assistant" in ids

    def test_unrestricted_templates_visible_to_everyone(self):
        """Every template without ``visible_to_orgs`` must appear for
        every authenticated user — we don't accidentally hide the
        existing catalog when introducing the filter."""
        unrestricted = [
            t["template_id"]
            for t in SUPERAGENT_TEMPLATES
            if t.get("visible_to_orgs") is None
        ]
        assert unrestricted, "expected at least some unrestricted templates"

        for email in ("alice@example.com", "ops@scei88.fr"):
            ids = {
                t["template_id"]
                for t in list_superagent_templates(user=_user(email))
            }
            for tid in unrestricted:
                assert tid in ids, (
                    f"{tid} must be visible to {email} (no visible_to_orgs)"
                )


# ---------------------------------------------------------------------------
# get_superagent_template
# ---------------------------------------------------------------------------


class TestGetVisibility:
    def test_non_scei_user_gets_none_for_scei_template(self):
        out = get_superagent_template(
            "scei_ar_assistant", user=_user("alice@example.com")
        )
        assert out is None

    def test_scei_user_gets_scei_template(self):
        out = get_superagent_template(
            "scei_ar_assistant", user=_user("ops@scei88.fr")
        )
        assert out is not None
        assert out["template_id"] == "scei_ar_assistant"

    def test_legacy_signature_still_returns_template(self):
        """``user=None`` MUST keep returning the template — internal
        call sites (e.g. ``audio_stream``) rely on it."""
        out = get_superagent_template("scei_ar_assistant")
        assert out is not None
        assert out["template_id"] == "scei_ar_assistant"

    def test_unknown_id_returns_none(self):
        out = get_superagent_template("nope", user=_user("alice@example.com"))
        assert out is None


# ---------------------------------------------------------------------------
# Sanity — the SCEI template is the one carrying the restriction
# ---------------------------------------------------------------------------


class TestSceiTemplateMarked:
    def test_scei_template_carries_visible_to_orgs(self):
        scei = next(
            (t for t in SUPERAGENT_TEMPLATES if t["template_id"] == "scei_ar_assistant"),
            None,
        )
        assert scei is not None
        assert scei.get("visible_to_orgs") == ["scei"], (
            f"expected ['scei'], got {scei.get('visible_to_orgs')!r} — "
            "the visibility restriction must travel with the template"
        )
