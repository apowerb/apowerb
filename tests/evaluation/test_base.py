"""Unit tests for evaluators/base.py."""

from apowerb.evaluation.evaluators.base import rationale_language


def test_known_locale_resolves_to_its_language_name():
    assert rationale_language("fr") == "French"


def test_default_locale_is_english():
    assert rationale_language(None) == "English"
    assert rationale_language("") == "English"


def test_locale_is_case_and_whitespace_insensitive():
    assert rationale_language(" FR ") == "French"


def test_unknown_locale_falls_back_to_the_raw_code():
    """Still a usable instruction for the model even without a name in the map."""
    assert rationale_language("nl") == "nl"
