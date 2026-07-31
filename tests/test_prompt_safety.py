"""Tests for ``prompt_safety`` — pre-flight check matching ADK's actual
brace resolution logic.

Reproduces ``google.adk.utils.instructions_utils.inject_session_state``:
match ``{+[^{}]*}+``, strip all surrounding braces, check
``isidentifier()`` (or ``<prefix>:<identifier>``), look up in
``session.state``, ``KeyError`` if missing.

Key learning from 2026-05-13 incident & subsequent reviewer feedback:

* ``{{xxx}}`` IS NOT an escape — ADK strips all braces. PR #172 doubled
  ``{keyword}`` to ``{{keyword}}`` thinking it would escape; it does
  not. ``{{keyword}}`` still crashes /run with the same KeyError.
* ``{4,6}``, ``{a, b, c}``, ``{"k": v}`` were ALREADY safe at runtime
  (content not isidentifier → ADK leaves literal). Doubling them is a
  no-op (also from PR #172).
* The actual runtime bug was the single ``{keyword}`` in ``scei.py``
  line 287, then re-broken as ``{{keyword}}`` by PR #172.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# find_unsafe_braces — matches ADK's actual logic
# ---------------------------------------------------------------------------


class TestFindUnsafeBraces:
    def test_empty_string_returns_no_issues(self):
        from apowerb.core.validation.prompt_safety import find_unsafe_braces

        assert find_unsafe_braces("") == []

    def test_plain_text_returns_no_issues(self):
        from apowerb.core.validation.prompt_safety import find_unsafe_braces

        assert find_unsafe_braces("Hello world, no braces here.") == []

    def test_single_braces_identifier_is_error(self):
        """The original bug: ``{keyword}`` → lookup → KeyError."""
        from apowerb.core.validation.prompt_safety import find_unsafe_braces

        issues = find_unsafe_braces("Reply: {keyword}")
        assert len(issues) == 1
        assert issues[0].level == "error"
        assert issues[0].var_name == "keyword"

    def test_double_braces_identifier_is_ALSO_error(self):
        """ADK strips all braces. PR #172's ``{{keyword}}`` is NOT escaped
        — it crashes identically to ``{keyword}``."""
        from apowerb.core.validation.prompt_safety import find_unsafe_braces

        issues = find_unsafe_braces("Reply: {{keyword}}")
        assert len(issues) == 1
        assert issues[0].level == "error"
        assert issues[0].var_name == "keyword"
        # The reason must call out the no-escape pitfall.
        assert "NOT an escape" in issues[0].reason

    def test_triple_braces_identifier_is_error_too(self):
        from apowerb.core.validation.prompt_safety import find_unsafe_braces

        issues = find_unsafe_braces("{{{my_var}}}")
        assert issues[0].var_name == "my_var"
        assert issues[0].level == "error"

    def test_non_identifier_braces_are_safe(self):
        """ADK leaves these literal — content fails ``isidentifier()``.
        These are the patterns PR #172 doubled-up, which was a no-op."""
        from apowerb.core.validation.prompt_safety import find_unsafe_braces

        safe_cases = [
            "regex `^CF[0-9]{4,6}$`",
            "LIST of `{commande_number_display, commande_number_sql, ...}`",
            "`{ligne_numero, ref_fournisseur, qty}`",
            "value in {'', NULL, 'C', 'M'}",
            'JSON like {"PainPoint": ..., "DigitalOpportunity": ...}',
            "dotted {ar_intake.status}",  # dot fails isidentifier
        ]
        for prompt in safe_cases:
            issues = find_unsafe_braces(prompt)
            errs = [i for i in issues if i.level == "error"]
            assert errs == [], f"false positive on safe pattern: {prompt!r} → {errs}"

    def test_angle_bracket_escape_is_safe(self):
        """``<keyword>`` is the recommended literal-rendering pattern —
        no braces, no ADK interpolation, no issue."""
        from apowerb.core.validation.prompt_safety import find_unsafe_braces

        assert find_unsafe_braces("Reply: '<keyword>'") == []

    def test_known_keys_bind_the_var(self):
        """When upstream sub-agent declares ``output_key='ar_intake'``,
        ``{ar_intake}`` downstream is bound — no issue."""
        from apowerb.core.validation.prompt_safety import find_unsafe_braces

        assert (
            find_unsafe_braces("Match {ar_intake}", known_keys={"ar_intake"}) == []
        )

    def test_optional_suffix_is_warning_not_error(self):
        """``{var?}`` → ADK returns empty string on miss instead of raising.
        Doesn't crash, but probably not what the operator wanted."""
        from apowerb.core.validation.prompt_safety import find_unsafe_braces

        issues = find_unsafe_braces("Reply: {keyword?}")
        assert len(issues) == 1
        assert issues[0].level == "warning"
        assert issues[0].var_name == "keyword"

    def test_state_prefix_is_recognized(self):
        """``{user:my_var}`` is a valid ADK state name."""
        from apowerb.core.validation.prompt_safety import find_unsafe_braces

        issues = find_unsafe_braces("Hello {user:my_var}")
        assert len(issues) == 1
        assert issues[0].level == "error"
        assert issues[0].var_name == "user:my_var"

    def test_invalid_prefix_is_safe(self):
        """``{nope:my_var}`` — ``nope:`` not in ADK's prefix set → not a
        valid state name → ADK leaves literal."""
        from apowerb.core.validation.prompt_safety import find_unsafe_braces

        assert find_unsafe_braces("Hello {nope:my_var}") == []

    def test_line_number_is_reported(self):
        from apowerb.core.validation.prompt_safety import find_unsafe_braces

        text = "line one\nbroken {keyword} here\nline three\n"
        issues = find_unsafe_braces(text)
        assert len(issues) == 1
        assert issues[0].line == 2


# ---------------------------------------------------------------------------
# validate_templates — applies the scanner to a list
# ---------------------------------------------------------------------------


class TestValidateTemplates:
    def test_clean_templates_pass(self):
        from apowerb.core.validation.prompt_safety import validate_templates

        templates = [
            {"name": "ok_one", "agent_instruction": "no placeholders here"},
            {"name": "ok_two", "agent_instruction": "use <name> literal"},
            {"name": "ok_three", "agent_instruction": "regex {4,6} fine"},
        ]
        assert validate_templates(templates) == []

    def test_bad_template_surfaces_with_name(self):
        from apowerb.core.validation.prompt_safety import validate_templates

        templates = [
            {"name": "bogus", "agent_instruction": "Reply with {keyword}"},
        ]
        results = validate_templates(templates)
        assert len(results) == 1
        name, issues = results[0]
        assert name == "bogus"
        assert any(i.level == "error" for i in issues)

    def test_double_brace_in_template_is_still_caught(self):
        """Specifically: a template that thought it was escaping with
        ``{{xxx}}`` (à la PR #172) must NOT pass the validator."""
        from apowerb.core.validation.prompt_safety import validate_templates

        templates = [
            {"name": "scei_like", "agent_instruction": "Reply: '({{keyword}})'"},
        ]
        results = validate_templates(templates)
        assert len(results) == 1
        _, issues = results[0]
        assert any(i.var_name == "keyword" and i.level == "error" for i in issues)

    def test_template_without_instruction_is_skipped(self):
        from apowerb.core.validation.prompt_safety import validate_templates

        assert validate_templates([
            {"name": "parent", "agent_instruction": None},
            {"name": "other", "agent_instruction": ""},
        ]) == []

    def test_shipped_templates_are_clean(self):
        """Smoke: after fixing the real ``{{keyword}}`` runtime bug in
        scei.py, no shipped template should trigger ADK at runtime.

        This test is the regression guard. It would have failed on
        2026-05-13 (when ``{keyword}`` first shipped) and would have
        failed again on 2026-05-18 after PR #172 (when ``{{keyword}}``
        replaced it but still crashes ADK).
        """
        from apowerb.core.superagents.templates import SUPERAGENT_TEMPLATES
        from apowerb.core.validation.prompt_safety import validate_templates

        results = validate_templates(SUPERAGENT_TEMPLATES)
        errors = [
            (name, i) for name, issues in results for i in issues if i.level == "error"
        ]
        assert errors == [], (
            f"Shipped templates contain unsafe placeholders that ADK will "
            f"resolve at runtime (KeyError on /run): {errors}"
        )


# ---------------------------------------------------------------------------
# assert_templates_safe — the strict boot-time gate
# ---------------------------------------------------------------------------


class TestAssertTemplatesSafe:
    def test_raises_on_error_level(self):
        from apowerb.core.validation.prompt_safety import assert_templates_safe

        templates = [{"name": "bad", "agent_instruction": "Reply: {keyword}"}]
        with pytest.raises(ValueError, match="resolve against `session.state`"):
            assert_templates_safe(templates)

    def test_raises_on_double_brace_too(self):
        """The PR #172 case: must still raise."""
        from apowerb.core.validation.prompt_safety import assert_templates_safe

        templates = [{"name": "scei_like", "agent_instruction": "({{keyword}})"}]
        with pytest.raises(ValueError):
            assert_templates_safe(templates)

    def test_warnings_do_not_raise(self):
        from apowerb.core.validation.prompt_safety import assert_templates_safe

        templates = [{"name": "warn", "agent_instruction": "Use {unknown_var?}"}]
        assert_templates_safe(templates)  # no raise
