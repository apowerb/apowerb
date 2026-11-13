"""Per-org visibility for SuperAgent templates.

Templates may carry ``visible_to_orgs: list[str]`` to restrict them to
specific organisations. Membership is derived from the user's email
domain, through ``settings.org_domain_slugs`` (env var ``ORG_DOMAIN_SLUGS``).

These tests guard:

  - the ``user`` parameter on ``list_superagent_templates`` /
    ``get_superagent_template`` filters correctly,
  - ``user=None`` keeps the legacy "all-templates" behaviour for system
    callers,
  - a client-restricted template is hidden from users of other orgs,
  - templates without ``visible_to_orgs`` (the vast majority) keep
    showing for everyone.

The core ships no client template: the restricted one is the
``demo_client_template`` fixture, registered the way an overlay would.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apowerb.configs.settings import get_settings
from apowerb.core.superagents import (
    SUPERAGENT_TEMPLATES,
    _build_templates,
    _user_org_slugs,
    get_superagent_template,
    list_superagent_templates,
)


def _user(email: str | None) -> SimpleNamespace:
    """Minimal stand-in for the User pydantic model."""
    return SimpleNamespace(email=email)


@pytest.fixture(autouse=True)
def client_org(monkeypatch, demo_client_template):
    """Map ``acme.example`` to the ``acme`` org and register its template.

    The org→domain mapping is empty by default; each test runs against
    this known mapping, regardless of what the environment provides.
    """
    settings = get_settings()
    monkeypatch.setattr(
        settings, "org_domain_slugs", {"acme.example": "acme"}, raising=False
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

    def test_client_email_yields_client_slug(self):
        assert "acme" in _user_org_slugs(_user("alice@acme.example"))

    def test_client_email_uppercase_still_matched(self):
        assert "acme" in _user_org_slugs(_user("BOB@ACME.example"))

    def test_other_email_yields_empty_set(self):
        assert _user_org_slugs(_user("alice@example.com")) == set()

    def test_substring_match_does_not_count(self):
        # "evil-acme.example" must NOT be matched as acme.example.
        assert _user_org_slugs(_user("alice@evil-acme.example")) == set()
        # "alice-acme.example" with no @ is not a client user either.
        assert _user_org_slugs(_user("alice-acme.example")) == set()


# ---------------------------------------------------------------------------
# list_superagent_templates
# ---------------------------------------------------------------------------


class TestListVisibility:
    def test_none_user_returns_full_catalog(self):
        """Internal / system callers (no auth) keep seeing everything."""
        out = list_superagent_templates(user=None)
        assert len(out) == len(_build_templates())

    def test_no_arg_defaults_to_full_catalog(self):
        """Backward compatibility — pre-existing call sites pass no user."""
        assert len(list_superagent_templates()) == len(_build_templates())

    def test_other_user_does_not_see_client_template(self):
        out = list_superagent_templates(user=_user("alice@example.com"))
        ids = {t["template_id"] for t in out}
        assert "demo_client_assistant" not in ids

    def test_client_user_sees_client_template(self):
        out = list_superagent_templates(user=_user("ops@acme.example"))
        ids = {t["template_id"] for t in out}
        assert "demo_client_assistant" in ids

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

        for email in ("alice@example.com", "ops@acme.example"):
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
    def test_other_user_gets_none_for_client_template(self):
        out = get_superagent_template(
            "demo_client_assistant", user=_user("alice@example.com")
        )
        assert out is None

    def test_client_user_gets_client_template(self):
        out = get_superagent_template(
            "demo_client_assistant", user=_user("ops@acme.example")
        )
        assert out is not None
        assert out["template_id"] == "demo_client_assistant"

    def test_legacy_signature_still_returns_template(self):
        """``user=None`` MUST keep returning the template — internal
        call sites (e.g. ``audio_stream``) rely on it."""
        out = get_superagent_template("demo_client_assistant")
        assert out is not None
        assert out["template_id"] == "demo_client_assistant"

    def test_unknown_id_returns_none(self):
        out = get_superagent_template("nope", user=_user("alice@example.com"))
        assert out is None


# ---------------------------------------------------------------------------
# Sanity — the client template is the one carrying the restriction
# ---------------------------------------------------------------------------


class TestClientTemplateMarked:
    def test_client_template_carries_visible_to_orgs(self):
        tpl = next(
            (t for t in _build_templates() if t["template_id"] == "demo_client_assistant"),
            None,
        )
        assert tpl is not None
        assert tpl.get("visible_to_orgs") == ["acme"], (
            f"expected ['acme'], got {tpl.get('visible_to_orgs')!r} — "
            "the visibility restriction must travel with the template"
        )
