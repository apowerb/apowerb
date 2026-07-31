"""Tests for SCEI v2 templates — the sub-agent pipeline.

DORMANT smoke + structural tests. Phase 3 will add E2E tests once
agent6bis is seeded on DAVE_OVH and the SequentialAgent actually runs.
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Structural checks
# ---------------------------------------------------------------------------


class TestSceiV2TemplateStructure:
    def test_all_5_templates_present(self):
        from th2customers.scei.templates.scei_v2 import (
            SCEI_V2_TEMPLATES,
        )

        names = {t["name"] for t in SCEI_V2_TEMPLATES}
        assert names == {
            "scei_ar_intake",
            "scei_ar_matcher",
            "scei_ar_recorder",
            "scei_ar_notifier",
            "scei_ar_assistant_v2",
        }

    def test_parent_is_sequential_with_4_subagents(self):
        from th2customers.scei.templates.scei_v2 import (
            SCEI_AR_ASSISTANT_V2,
        )

        assert SCEI_AR_ASSISTANT_V2["agent_type"] == "sequential"
        assert SCEI_AR_ASSISTANT_V2["sub_agents"] == [
            "scei_ar_intake",
            "scei_ar_matcher",
            "scei_ar_recorder",
            "scei_ar_notifier",
        ]

    def test_each_subagent_has_output_key_and_schema(self):
        from th2customers.scei.templates.scei_v2 import (
            SCEI_AR_INTAKE,
            SCEI_AR_MATCHER,
            SCEI_AR_RECORDER,
            SCEI_AR_NOTIFIER,
        )

        expected = {
            SCEI_AR_INTAKE["name"]: ("ar_intake", "SCEIIntakePayload"),
            SCEI_AR_MATCHER["name"]: ("ar_match", "ARMatchPayload"),
            SCEI_AR_RECORDER["name"]: ("ar_record", "ARRecordPayload"),
            SCEI_AR_NOTIFIER["name"]: ("ar_notify", "ARNotifyPayload"),
        }
        for tpl in (
            SCEI_AR_INTAKE,
            SCEI_AR_MATCHER,
            SCEI_AR_RECORDER,
            SCEI_AR_NOTIFIER,
        ):
            ok_key, ok_schema = expected[tpl["name"]]
            assert tpl["output_key"] == ok_key, tpl["name"]
            assert tpl["output_schema_name"] == ok_schema, tpl["name"]

    def test_intake_tools_outlook_and_pdf_only(self):
        """Intake doesn't touch DB — no PMI / SuiviAR tools."""
        from th2customers.scei.templates.scei_v2 import SCEI_AR_INTAKE

        tools = set(SCEI_AR_INTAKE["agent_tools"])
        assert "outlook_mail.tool_read_email" in tools
        assert "outlook_mail.tool_download_attachment" in tools
        assert "basic.tool_pdf_first_page" in tools
        # No SQL tools
        assert not any("sql" in t.lower() for t in tools)
        # No mail send
        assert "outlook_mail.tool_send_outlook_email" not in tools

    def test_matcher_has_no_portfolio_tools(self):
        """SQL tools come from operator-attached tool_config rows
        (cf v1 monolithic scei.py), NOT the portfolio. Phase 3 seed
        of agent6bis must attach a PMI tool_config to the matcher."""
        from th2customers.scei.templates.scei_v2 import SCEI_AR_MATCHER

        assert SCEI_AR_MATCHER["agent_tools"] == []
        # No mail send leak
        assert "outlook_mail.tool_send_outlook_email" not in (
            SCEI_AR_MATCHER["agent_tools"]
        )

    def test_recorder_binds_python_persist_tool(self):
        """The recorder MUST declare the schema-guaranteed Python persist
        tool (header + lines, atomic). It used to be [] — the prompt then
        referenced a tool the agent did not have, so the header went via raw
        SQL and lines were lost (orphan headers). SQL DB creds still come from
        the operator-attached tool_config* via the resync merge."""
        from th2customers.scei.templates.scei_v2 import (
            SCEI_AR_RECORDER,
        )

        assert "scei_ar_persist.tool_persist_ar_record" in (
            SCEI_AR_RECORDER["agent_tools"]
        )
        assert "outlook_mail.tool_send_outlook_email" not in (
            SCEI_AR_RECORDER["agent_tools"]
        )

    def test_notifier_uses_scei_mail_wrapper_not_generic_outlook(self):
        """Defense in depth: the notifier carries the SCEI-specific
        wrapper (Python-enforced whitelist + once-per-run + audit log +
        dry-run) and NOT the generic Outlook tool — so the LLM cannot
        bypass the guards by calling the raw tool directly."""
        from th2customers.scei.templates.scei_v2 import SCEI_AR_NOTIFIER

        tools = set(SCEI_AR_NOTIFIER["agent_tools"])
        assert "scei_mail.tool_send_scei_mail" in tools
        # Generic Outlook tool MUST be absent
        assert "outlook_mail.tool_send_outlook_email" not in tools

    def test_notifier_prompt_describes_python_guards_not_self_ban(self):
        """Prompt informs the LLM about the Python-level guards, no
        longer relies on absolutist 'BANNED' self-discipline."""
        from th2customers.scei.templates.scei_v2 import SCEI_AR_NOTIFIER

        instruction = SCEI_AR_NOTIFIER["agent_instruction"]
        # Python-level enforcement mentioned
        assert "Python" in instruction or "tool enforces" in instruction.lower()
        # The four guards listed
        for marker in ("Once per run", "Whitelist", "Audit log", "Dry-run"):
            assert marker in instruction or marker.lower() in instruction.lower(), marker
        # Guard rail: explicit tool name (the only mail tool available)
        assert "scei_mail.tool_send_scei_mail" in instruction

    def test_all_v2_templates_restricted_to_scei_org(self):
        from th2customers.scei.templates.scei_v2 import (
            SCEI_V2_TEMPLATES,
        )

        for tpl in SCEI_V2_TEMPLATES:
            assert tpl.get("visible_to_orgs") == ["scei"], tpl["name"]


# ---------------------------------------------------------------------------
# Prompt safety — the BIG one
# ---------------------------------------------------------------------------


class TestSceiV2PromptSafety:
    def test_each_subagent_prompt_passes_with_cross_template_known_keys(self):
        """The matcher prompt uses ``{ar_intake}`` — that var is bound by
        the intake's ``output_key='ar_intake'``. Same for recorder
        reading ``{ar_intake}`` + ``{ar_match}``, and notifier reading
        ``{ar_match}`` + ``{ar_record}``. The cross-template
        ``collect_known_keys`` must surface all these so the smoke gate
        doesn't false-positive."""
        from th2customers.scei.templates.scei_v2 import (
            SCEI_V2_TEMPLATES,
        )
        from apowerb.core.validation.prompt_safety import (
            validate_templates,
            collect_known_keys,
        )

        # Sanity: known_keys must include the 4 output_keys
        known = collect_known_keys(SCEI_V2_TEMPLATES)
        assert {"ar_intake", "ar_match", "ar_record", "ar_notify"} <= known

        # Validate with cross-template context: zero error-level issues
        results = validate_templates(SCEI_V2_TEMPLATES)
        errors = [
            (n, i)
            for n, issues in results
            for i in issues
            if i.level == "error"
        ]
        assert errors == [], (
            f"v2 templates trigger ADK runtime KeyError: {errors}"
        )

    def test_shipped_templates_still_clean_with_v2_appended(self):
        """When we register v2 in templates/__init__.py, the full
        SUPERAGENT_TEMPLATES list must still pass the boot gate."""
        from apowerb.core.superagents.templates import SUPERAGENT_TEMPLATES
        from apowerb.core.validation.prompt_safety import validate_templates

        results = validate_templates(SUPERAGENT_TEMPLATES)
        errors = [
            (n, i)
            for n, issues in results
            for i in issues
            if i.level == "error"
        ]
        assert errors == [], (
            f"Full template registry triggers ADK runtime KeyError "
            f"after v2 wired: {errors}"
        )


# ---------------------------------------------------------------------------
# Registry integration
# ---------------------------------------------------------------------------


class TestV2RegisteredInSuperagentTemplates:
    @staticmethod
    def _catalog_names():
        # SCEI templates live in the overlay now; importing them self-registers
        # into the extension registry, and _build_templates() merges the
        # registry into the catalog (same path as init_overlay at startup).
        import th2customers.scei.templates.scei  # noqa: F401
        import th2customers.scei.templates.scei_v2  # noqa: F401
        from apowerb.core.superagents.templates import _build_templates

        return {t["name"] for t in _build_templates()}

    def test_v2_templates_present_in_global_registry(self):
        names = self._catalog_names()
        assert "scei_ar_intake" in names
        assert "scei_ar_matcher" in names
        assert "scei_ar_recorder" in names
        assert "scei_ar_notifier" in names
        assert "scei_ar_assistant_v2" in names

    def test_v1_scei_ar_assistant_still_present(self):
        """Critical: agent6 (monolithic) must keep working — don't ship
        a regression that removes the v1 template."""
        names = self._catalog_names()
        assert "scei_ar_assistant" in names



def test_recorder_prompt_uses_tool_persist_ar_record():
    """The Commandes INSERT must go through ``tool_persist_ar_record``
    (Python guards), not the generic ``tool_run_sql_suiviar``. The
    Python tool hardcodes ``DateReceptionAR``, ``Traite=0`` and
    ``Decision=NULL`` so the row is well-formed even if the LLM
    drifts. See ``[[incident_2026-05-19_dashboard_dateReceptionAR]]``."""
    from th2customers.scei.templates.scei_v2 import SCEI_AR_RECORDER
    instr = SCEI_AR_RECORDER["agent_instruction"]
    assert "tool_persist_ar_record" in instr, (
        "_RECORDER_PROMPT must instruct the agent to call "
        "``tool_persist_ar_record`` for the Commandes header INSERT."
    )
    # The Commandes header MUST not be written via tool_run_sql_suiviar.
    # The prompt must explicitly forbid tool_run_sql_suiviar for the
    # Commandes header INSERT (LignesCommande can use it — different block).
    assert "NOT `tool_run_sql_suiviar`" in instr, (
        "Prompt must explicitly forbid tool_run_sql_suiviar for the "
        "Commandes header INSERT."
    )
    # tool_persist_ar_record must appear before the first
    # tool_run_sql_suiviar mention (Commandes header is documented first).
    persist_idx = instr.find("tool_persist_ar_record")
    raw_idx = instr.find("tool_run_sql_suiviar")
    assert persist_idx >= 0 and raw_idx >= 0
    assert persist_idx < raw_idx, (
        "tool_persist_ar_record (Commandes header) must come before "
        "tool_run_sql_suiviar (LignesCommande) in the prompt."
    )


class TestIntakeV2Redesign:
    """Step 4 — the intake switched to the deterministic-gate + native-PDF
    + classification/extraction design."""

    def test_intake_uses_new_schema_and_gate(self):
        from th2customers.scei.templates.scei_v2 import SCEI_AR_INTAKE

        assert SCEI_AR_INTAKE["output_schema_name"] == "SCEIIntakePayload"
        assert SCEI_AR_INTAKE["attachment_pdf_gate"] is True
        assert SCEI_AR_INTAKE["output_key"] == "ar_intake"

    def test_intake_swapped_to_first_page_tool(self):
        from th2customers.scei.templates.scei_v2 import SCEI_AR_INTAKE

        tools = set(SCEI_AR_INTAKE["agent_tools"])
        assert "basic.tool_pdf_first_page" in tools
        assert "basic.tool_pdf_to_images" not in tools  # replaced
        assert "outlook_mail.tool_read_email" in tools
        assert "outlook_mail.tool_download_attachment" in tools

    def test_intake_prompt_contract_and_signals(self):
        from th2customers.scei.templates.scei_v2 import _INTAKE_PROMPT

        # New output contract
        assert "email_classification" in _INTAKE_PROMPT
        assert "extraction_results" in _INTAKE_PROMPT
        assert "not_ar" in _INTAKE_PROMPT
        # Excluded-supplier check moved to the deterministic gate
        # (build_excluded_supplier_gate_callback). The broken
        # tool_run_sql_suiviar instruction MUST be gone from the prompt
        # (it caused the LlmCallsLimit/500 loop).
        assert "tool_run_sql_suiviar" not in _INTAKE_PROMPT
        assert "FournisseursExclus" not in _INTAKE_PROMPT
        assert "tool_pdf_first_page" in _INTAKE_PROMPT
        # Data-driven classification signals
        assert "ARC" in _INTAKE_PROMPT
        assert "facture" in _INTAKE_PROMPT.lower() or "invoice" in _INTAKE_PROMPT
        # Guard rails preserved
        assert "tool_send_outlook_email" in _INTAKE_PROMPT


class TestRippleV2Downstream:
    """Step 5 — matcher & recorder read the new intake format
    (email_classification / extraction_results)."""

    def test_matcher_reads_new_intake_format(self):
        from th2customers.scei.templates.scei_v2 import _MATCHER_PROMPT

        assert "email_classification" in _MATCHER_PROMPT
        assert "extraction_results" in _MATCHER_PROMPT
        # AR fields read via extraction_results, not legacy `ar.` ARData refs
        assert "extraction_results.commande_number_sql" in _MATCHER_PROMPT
        # legacy intake skip wording removed for ar_intake
        assert "status == 'skip'" not in _MATCHER_PROMPT

    def test_recorder_reads_new_intake_format(self):
        from th2customers.scei.templates.scei_v2 import _RECORDER_PROMPT

        assert "email_classification" in _RECORDER_PROMPT
        assert "extraction_results" in _RECORDER_PROMPT
        # ar_match still uses its own status field (unchanged schema)
        assert "ar_match.status" in _RECORDER_PROMPT
