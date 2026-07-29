"""Tests for the ``scei_ar_assistant`` SuperAgent template.

The template encodes the SCEI88 purchase-order acknowledgement business
rules in its system prompt. These tests guard:

  - the template is registered in ``SUPERAGENT_TEMPLATES``,
  - the schema matches what the rest of the platform expects (same shape
    as ``database_assistant``),
  - the Outlook tools the agent needs are wired,
  - the system prompt actually contains the SCEI-specific rules so a
    silent regression on the prompt is caught.
"""

from __future__ import annotations

import pytest

from th2agent.core.superagents import SUPERAGENT_TEMPLATES


REQUIRED_FIELDS = [
    "template_id",
    "name",
    "display_name",
    "description",
    "icon",
    "category",
    "agent_model",
    "agent_instruction",
    "agent_description",
    "agent_type",
    "agent_tools",
    "memory_enabled",
    "artifacts_enabled",
    "tags",
    "readme",
]


def _get_template() -> dict | None:
    for t in SUPERAGENT_TEMPLATES:
        if t["template_id"] == "scei_ar_assistant":
            return t
    return None


# ---------------------------------------------------------------------------
# Registration + schema
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_template_is_registered(self):
        assert _get_template() is not None, (
            "scei_ar_assistant must be present in SUPERAGENT_TEMPLATES"
        )

    def test_required_fields_present(self):
        template = _get_template()
        for field in REQUIRED_FIELDS:
            assert field in template, f"missing required field: {field}"

    def test_template_id_matches_name(self):
        template = _get_template()
        assert template["template_id"] == template["name"]

    def test_no_duplicate_template_id(self):
        ids = [t["template_id"] for t in SUPERAGENT_TEMPLATES]
        assert ids.count("scei_ar_assistant") == 1


# ---------------------------------------------------------------------------
# Tool wiring
# ---------------------------------------------------------------------------


class TestToolWiring:
    def test_outlook_tools_present(self):
        template = _get_template()
        tools = template["agent_tools"]
        # Every Outlook entry the system prompt names must actually be
        # listed so the runtime can resolve and dispatch them.
        for fn in (
            "outlook_mail.tool_list_emails",
            "outlook_mail.tool_read_email",
            "outlook_mail.tool_search_emails",
            "outlook_mail.tool_download_attachment",
            "outlook_mail.tool_send_outlook_email",
        ):
            assert fn in tools, f"missing tool: {fn}"

    def test_safety_tools_present(self):
        template = _get_template()
        # confirm_destructive is required because the agent writes to SuiviAR.
        assert "basic.confirm_destructive" in template["agent_tools"]

    def test_backlog_status_tool_wired(self):
        """The agent must (a) have the backlog-status tool listed in
        agent_tools so to_agent() can bind the agent_id closure, and
        (b) the prompt must instruct it to call the tool before
        concluding so the operator gets a "still N to go" line. Live
        test 2026-05-07 showed several ARs queueing up while one was
        being handled — without backlog awareness the agent's reply
        would suggest the run is over."""
        template = _get_template()
        assert "basic.tool_get_webhook_backlog_status" in template["agent_tools"], (
            "backlog-status tool must be declared so the to_agent binder "
            "can swap in the agent_id-bound closure; without it the "
            "placeholder returns 'missing agent context' at runtime."
        )
        prompt = template["agent_instruction"]
        assert "tool_get_webhook_backlog_status" in prompt, (
            "system prompt must reference the tool by name so the LLM "
            "knows to call it before concluding."
        )
        # Output marker the operator scans the chat for. Drift here
        # would silently break the dashboard hook.
        assert "Backlog:" in prompt or "Backlog cleared" in prompt, (
            "prompt must instruct the agent to render a 'Backlog:' line "
            "so the operator can read pending count at a glance."
        )

    def test_pdf_to_images_wired_for_vision(self):
        """ARs arrive as PDFs. The agent must (a) have the vision tool
        listed in agent_tools, (b) have the workflow step in the system
        prompt, otherwise it falls back to PyPDF2 text extraction and
        silently mangles scanned ARs."""
        template = _get_template()
        assert "basic.tool_pdf_to_images" in template["agent_tools"], (
            "tool_pdf_to_images must be in agent_tools — "
            "without it the agent cannot 'see' scanned PDFs."
        )
        prompt = template["agent_instruction"]
        assert "tool_pdf_to_images" in prompt, (
            "system prompt must reference tool_pdf_to_images so the LLM "
            "knows to call it before extracting fields."
        )
        # Workflow step that triggers the tool — keyword 'Render the PDF'
        # protects against later edits that drop the explicit step.
        assert "Render the PDF" in prompt or "render the PDF" in prompt


class TestSuiviARSchemaWiring:
    """The SuiviAR side has a normalised schema (Commandes / LignesCommande /
    Commanditaires) — not a single 'SuiviAR' table. The template was
    originally written assuming a monolithic table; this guard ensures the
    real schema is referenced so the LLM doesn't INSERT into a phantom
    'SuiviAR' table again (live regression: 2026-05-07 14:37 UTC)."""

    def test_real_tables_referenced(self):
        prompt = _get_template()["agent_instruction"]
        for table in ("Commandes", "LignesCommande", "Commanditaires"):
            assert table in prompt, (
                f"system prompt must reference dbo.{table} — the real "
                f"SuiviAR schema is normalised across these three tables."
            )

    def test_canonical_status_values_documented(self):
        prompt = _get_template()["agent_instruction"]
        for status in (
            "conforme",
            "non_conforme",
            # Short code: fits StatutGlobal VARCHAR(20). A 22-char code
            # once truncated/failed the INSERT (live regression 14:37 UTC).
            "non_rapproche",
        ):
            assert status in prompt, (
                f"StatutGlobal value {status!r} must be documented in the "
                "prompt — otherwise the LLM invents free-form labels and "
                "the BI dashboard misses rows."
            )

    def test_typeecart_values_documented(self):
        prompt = _get_template()["agent_instruction"]
        for code in (
            "ecart_prix",
            "ecart_qte",
            "ecart_date",
            "ligne_absente_ar",
            "ligne_absente_erp",
        ):
            assert code in prompt, f"TypeEcart code {code!r} missing from prompt"

    def test_status_codes_fit_varchar_20(self):
        """`Commandes.StatutGlobal` is `VARCHAR(20)`. Documenting a code
        longer than 20 characters means every INSERT for that status
        truncates or fails. We learned this the hard way at 14:37 UTC
        with a 22-char status code."""
        canonical = (
            "conforme",
            "non_conforme",
            "non_rapproche",
            "en_attente",
        )
        for code in canonical:
            assert len(code) <= 20, (
                f"StatutGlobal canonical code {code!r} is {len(code)} chars "
                f"— exceeds the VARCHAR(20) of dbo.Commandes.StatutGlobal."
            )

    def test_typeecart_codes_fit_varchar_20(self):
        """`LignesCommande.TypeEcart` is also `VARCHAR(20)`."""
        for code in (
            "ecart_prix", "ecart_qte", "ecart_date",
            "ligne_absente_ar", "ligne_absente_erp",
        ):
            assert len(code) <= 20, (
                f"TypeEcart code {code!r} ({len(code)}) > VARCHAR(20)"
            )

    def test_no_obsolete_status_code_documented(self):
        """Guard against re-introducing the banned ORDER_NOT_FOUND vocabulary
        in any form — merged into `non_rapproche` on 2026-05-20."""
        prompt = _get_template()["agent_instruction"]
        for banned in ("ORDER_NOT_FOUND", "order_not_found"):
            assert banned not in prompt, (
                f"Banned status token {banned!r} re-introduced — the "
                "PO-absent-from-PMI case is `non_rapproche`."
            )

    def test_db_tools_named_explicitly(self):
        """After PR #115 (multi-DB) the SCEI agent uses tool_run_sql_pmi /
        tool_run_sql_suiviar — not the bare tool_run_sql. The prompt must
        reflect the new names so the LLM picks the right database."""
        prompt = _get_template()["agent_instruction"]
        assert "tool_run_sql_pmi" in prompt
        assert "tool_run_sql_suiviar" in prompt


class TestToolExecutionContract:
    """The LLM (Gemini-3-flash-preview observed 2026-05-07 17:17 UTC)
    sometimes prints an INSERT in plain text without invoking the tool —
    the row never makes it to SuiviAR. The prompt now opens with a
    'TOOL EXECUTION CONTRACT' that explicitly forbids that pattern.
    These tests guard the contract against silent edits."""

    def test_prompt_opens_with_tool_execution_contract(self):
        prompt = _get_template()["agent_instruction"]
        # The contract block must come before the role description so the
        # LLM reads it first.
        contract_idx = prompt.find("TOOL EXECUTION CONTRACT")
        role_idx = prompt.find("SCEI88 Purchase-Order Acknowledgement")
        assert contract_idx >= 0, "TOOL EXECUTION CONTRACT block missing"
        assert role_idx >= 0
        assert contract_idx < role_idx, (
            "TOOL EXECUTION CONTRACT must precede the role description"
        )

    def test_prompt_forbids_writing_sql_in_text(self):
        """The exact phrase the LLM kept doing wrong must be called out."""
        prompt = _get_template()["agent_instruction"]
        assert "does NOT persist anything" in prompt
        # 'tool is actually invoked' — explicit anti-hallucination wording
        assert "tool is actually invoked" in prompt

    def test_prompt_requires_checking_success_field(self):
        prompt = _get_template()["agent_instruction"]
        assert "`success`" in prompt
        # Hallucination prevention — must claim success only after a real
        # successful return.
        assert "without a successful tool return" in prompt

    def test_persist_step_repeats_tool_invocation_requirement(self):
        """Step 7 should re-state the contract since LLMs lose attention
        across long prompts."""
        prompt = _get_template()["agent_instruction"]
        # The persist step references both INSERT targets and reminds to
        # actually call the tool.
        persist_section = prompt[prompt.find("7. **Persist"):]
        assert "Call the tool" in persist_section
        assert "hallucination" in persist_section.lower()

    def test_persist_step_forbids_multirow_inserts(self):
        """Live regression 2026-05-07 17:42 UTC: agent6 batched both
        LignesCommande rows into a single multi-row INSERT, dropped a
        comma on row 2, and lost every line in the batch (the Commandes
        header persisted but LignesCommande stayed empty). Force one
        INSERT per row so a typo loses one line, not all of them."""
        prompt = _get_template()["agent_instruction"]
        persist_section = prompt[prompt.find("7. **Persist"):]
        # Mention the 1+N invocation count explicitly so the LLM doesn't
        # try to be clever and batch.
        assert "1 + N" in persist_section or "one per row" in persist_section
        # The prompt must literally forbid the multi-row form.
        assert "multi-row" in persist_section.lower()
        assert "Do NOT batch" in persist_section or "do not batch" in persist_section.lower()

    def test_persist_step_describes_success_and_failure_branches(self):
        """The LLM must know how to interpret the tool's return shape:
        ``success: True, rows_affected: 1`` → continue;
        ``success: False`` → stop + surface error."""
        prompt = _get_template()["agent_instruction"]
        persist_section = prompt[prompt.find("7. **Persist"):]
        assert "rows_affected" in persist_section
        assert "success: True" in persist_section
        assert "success: False" in persist_section
        # No optimistic continuation on failure
        assert (
            "stop" in persist_section.lower()
            or "DO NOT claim" in persist_section
        )


# ---------------------------------------------------------------------------
# System prompt — guard against silent rule drift
# ---------------------------------------------------------------------------


class TestSystemPromptEncodesRules:
    def test_prompt_documents_tolerances(self):
        prompt = _get_template()["agent_instruction"]
        # 0/0/±1 day rule — drift here would change every reconciliation.
        assert "0 tolerance" in prompt or "exact match (0" in prompt
        assert "±1 day" in prompt or "1 day tolerance" in prompt.lower()

    def test_prompt_documents_cf_strip(self):
        prompt = _get_template()["agent_instruction"]
        assert "CF" in prompt
        assert "strip" in prompt.lower()
        # ECKTNUMERO / LCKTNUMERO are 6-char zero-padded numerics
        assert "ECKTNUMERO" in prompt
        assert "LCKTNUMERO" in prompt

    def test_prompt_forbids_fuzzy_po_matching(self):
        """Live regression 2026-05-07 17:52 UTC: agent6 was asked to
        register Tilco CF0916. Tilco's PO is absent from PMI. The LLM
        substring-matched and rebound the AR to AMS' CF091600 — wrong
        supplier, wrong amount, persisted into SuiviAR as 'conforme'. The
        prompt must explicitly forbid this rebinding."""
        prompt = _get_template()["agent_instruction"]
        # Must mention exact match.
        assert "EXACT MATCH" in prompt or "exact match" in prompt
        # Must call out the specific failure mode.
        assert "fuzzy" in prompt.lower() or "fuzzy-match" in prompt.lower()
        # Must mandate non_rapproche on no exact hit (no order found = non_rapproche).
        assert "non_rapproche" in prompt
        # Must explicitly forbid LIKE / substring matching.
        assert (
            "substring-search" in prompt
            or "LIKE '%" in prompt
            or "do NOT" in prompt.lower() and "substring" in prompt.lower()
        )

    def test_prompt_documents_date_format(self):
        prompt = _get_template()["agent_instruction"]
        assert "YYYYMMDD" in prompt or "AAAAMMJJ" in prompt
        assert "LCCJDELEXP" in prompt

    def test_prompt_documents_societes(self):
        """The mapping ECKTSOC=100 → SCEI / 120 → PROCEED must travel
        verbatim with each société, otherwise the LLM may swap them.

        Bare ``"100" in prompt`` would pass even after a typo that
        replaces ``ECKTSOC = 100`` with ``ECKTSOC = 1`` (because the
        substring "100" reappears in the literal "1000" of any other
        number). So we anchor on the full ``ECKTSOC = NNN`` form and on
        the label that follows it.
        """
        prompt = _get_template()["agent_instruction"]
        # Both sociétés must appear in their full ``ECKTSOC|LCKTSOC = NNN``
        # form, not just as bare integers.
        scei_anchored = (
            "ECKTSOC = 100" in prompt
            or "LCKTSOC = 100" in prompt
            or "`100`" in prompt
        )
        proceed_anchored = (
            "ECKTSOC = 120" in prompt
            or "LCKTSOC = 120" in prompt
            or "`120`" in prompt
        )
        assert scei_anchored, "société 100 must appear with its column anchor"
        assert proceed_anchored, "société 120 must appear with its column anchor"
        # Labels must travel near the values.
        assert "SCEI" in prompt
        assert "PROCEED" in prompt

    def test_prompt_documents_composite_key(self):
        prompt = _get_template()["agent_instruction"]
        # Multi-line orders must be reconciled per-line on the composite key.
        assert "LCKTSOC" in prompt
        assert "LCKTLIGNE" in prompt

    def test_prompt_marks_pmi_read_only(self):
        prompt = _get_template()["agent_instruction"]
        # ECOMFOU / LCOMFOU must never be written to.
        # Allow any equivalent phrasing as long as both intent words are there.
        assert "READ ONLY" in prompt or "read only" in prompt.lower()
        assert "SuiviAR" in prompt


class TestLcctexpuUnitConversion:
    """LCOMFOU stores the price of a *lot* in ``LCCNPUNET``, with the lot
    size carried in ``LCCTEXPU`` (``''`` = unit, ``C`` = cent ×100,
    ``M`` = mille ×1000). The supplier AR is always per-unit, so we MUST
    convert ERP price → per-unit before comparing.

    Live regression 2026-05-11 on CF100898 line TRANE01051: ERP
    ``LCCNPUNET=494.70`` with ``LCCTEXPU='C'`` is **4.947 €/unit**,
    which matched the AR. The agent flagged it ``NON_CONFORME`` because
    it compared 494.70 (raw lot price) vs 4.947 (AR per-unit), off by
    100x. These guards make sure the prompt never drifts back to that
    state."""

    def test_prompt_documents_lcctexpu_block(self):
        prompt = _get_template()["agent_instruction"]
        # Block header must be unambiguous.
        assert "LCCTEXPU" in prompt
        assert "Unit-price conversion" in prompt or "unit-price conversion" in prompt.lower()
        # All three unit codes documented.
        assert "`C`" in prompt and "`M`" in prompt

    def test_prompt_documents_all_three_multipliers(self):
        """The mapping table must contain the three real-world cases."""
        prompt = _get_template()["agent_instruction"]
        # ×1 / ×100 / ×1000 wording (any form: 100, /100, ×100, x100, cent, mille)
        assert "/ 100" in prompt or "/100" in prompt, (
            "C multiplier (÷100) must be in the prompt"
        )
        assert "/ 1000" in prompt or "/1000" in prompt, (
            "M multiplier (÷1000) must be in the prompt"
        )
        assert "/ 1" in prompt or "/1" in prompt, (
            "empty multiplier (÷1) must be in the prompt"
        )
        # Domain words make the conversion concrete.
        assert "cent" in prompt.lower()
        assert "mille" in prompt.lower()

    def test_prompt_says_convert_before_compare(self):
        """The LLM must understand the conversion happens BEFORE the
        comparison, not after."""
        prompt = _get_template()["agent_instruction"]
        # Either explicit "before comparing" or "after unit conversion"
        # wording works; both encode the same ordering.
        assert (
            "before comparing" in prompt.lower()
            or "after unit conversion" in prompt.lower()
        ), "prompt must say the per-unit conversion happens before the price comparison"
        # And the AR side must be flagged as always per-unit.
        assert "per-unit" in prompt.lower() or "always per-unit" in prompt.lower()

    def test_select_lcomfou_includes_lcctexpu(self):
        """The example LCOMFOU SELECT in the workflow must pull LCCTEXPU.
        Without it the agent cannot do the conversion at runtime — the
        ``LCCTEXPU is mandatory`` reminder is wasted if the SELECT is
        missing the column."""
        prompt = _get_template()["agent_instruction"]
        # Locate the LCOMFOU SELECT example
        select_idx = prompt.find("SELECT LCKTLIGNE")
        assert select_idx >= 0, "expected example SELECT LCKTLIGNE ... FROM LCOMFOU"
        # The SELECT statement (next ~300 chars) must include LCCTEXPU
        select_block = prompt[select_idx:select_idx + 400]
        assert "LCCTEXPU" in select_block, (
            "LCCTEXPU must be in the SELECT LCOMFOU example — without it "
            "the agent cannot compute the per-unit price at runtime"
        )

    def test_prompt_defaults_to_divisor_1_on_unknown_unit(self):
        """SCEI has no manual review process — if LCCTEXPU is not '' / C / M,
        the agent defaults to divisor 1 (treat as empty) and continues."""
        prompt = _get_template()["agent_instruction"]
        lcctexpu_idx = prompt.find("LCCTEXPU")
        assert lcctexpu_idx >= 0
        scope = prompt[lcctexpu_idx:lcctexpu_idx + 2500]
        # Auto-decision: default to divisor 1, no human escalation
        assert "divisor 1" in scope or "divide by 1" in scope or "default" in scope.lower(), (
            "prompt must instruct the agent to default to divisor 1 on "
            "unknown LCCTEXPU values (no manual review process)"
        )
        # Anti-regression: never re-introduce NEEDS_HUMAN_REVIEW
        assert "NEEDS_HUMAN_REVIEW" not in scope, (
            "prompt must NOT escalate to NEEDS_HUMAN_REVIEW — SCEI has no "
            "manual review process; this status was banned 2026-05-13"
        )

    def test_prompt_references_cf100898_regression(self):
        """The incident that triggered this rule should be named in the
        prompt so a future edit doesn't drop the unit conversion. If the
        live example disappears, the rule loses its 'why' and is
        candidate for a well-intentioned but incorrect refactor."""
        prompt = _get_template()["agent_instruction"]
        assert "CF100898" in prompt or "TRANE01051" in prompt or "494.70" in prompt, (
            "prompt must name the 2026-05-11 CF100898/TRANE01051 incident "
            "so the rule's why doesn't decay into folklore"
        )


class TestEmailSendingPolicy:
    """Hard ban on outgoing email — the agent must NEVER call
    ``outlook_mail.tool_send_outlook_email``, not even on explicit
    operator request. Live regression 2026-05-11 17:50 on SCEI_PROD:
    the agent auto-sent a mismatch alert from ``com@scei88.fr`` to
    ``j.fruchart@scei88.fr`` — and the alert itself was a false positive
    (LCOMFOU unit-price issue, see Johann's reply). The fix: tighten
    the policy from "no auto-send unless operator explicit" to
    "unconditional ban — draft only, operator copy-pastes into Outlook".
    The policy must sit at the very top of the prompt so the LLM reads
    it before the workflow."""

    def test_email_policy_block_present(self):
        prompt = _get_template()["agent_instruction"]
        # Header rewritten to make the ban unmissable.
        assert "EMAIL SENDING IS STRICTLY FORBIDDEN" in prompt, (
            "system prompt must open with the strict EMAIL SENDING block"
        )

    def test_email_policy_comes_before_workflow(self):
        prompt = _get_template()["agent_instruction"]
        policy_idx = prompt.find("EMAIL SENDING IS STRICTLY FORBIDDEN")
        workflow_idx = prompt.find("## Workflow")
        assert policy_idx >= 0
        assert workflow_idx >= 0
        assert policy_idx < workflow_idx, (
            "EMAIL SENDING block must precede the workflow so the LLM "
            "reads it before the notify step"
        )

    def test_email_policy_is_unconditional(self):
        """The ban has no exception. Operator override is removed."""
        prompt = _get_template()["agent_instruction"]
        # Must name the banned tool.
        assert "tool_send_outlook_email" in prompt
        # Must use absolutist wording (no soft "automatically", no "unless").
        assert "STRICTLY FORBIDDEN" in prompt
        assert "MUST NEVER send any email" in prompt
        # Must explicitly call out that operator requests do NOT unlock it.
        assert "operator-explicit override" in prompt and "removed" in prompt, (
            "policy must explicitly say the operator override is removed — "
            "otherwise the LLM may rely on a half-remembered earlier version "
            "of the rule"
        )
        # Must instruct refuse + draft pattern on operator request.
        assert "refuse" in prompt.lower(), (
            "policy must instruct the agent to refuse operator send-requests"
        )

    def test_email_policy_drops_old_escape_hatch(self):
        """The old wording allowed the tool 'unless the operator
        explicitly asked'. That escape hatch is the root cause of the
        2026-05-11 incident — it must be GONE from the prompt so the
        LLM cannot anchor on it."""
        prompt = _get_template()["agent_instruction"]
        # Legacy phrases must no longer appear.
        assert "forbidden unless" not in prompt, (
            "legacy escape hatch 'forbidden unless …' is back in the prompt"
        )
        assert "MUST NOT send any email automatically" not in prompt, (
            "softened wording 'automatically' is back — current rule is "
            "unconditional, not just 'not automatic'"
        )
        assert "Only call `outlook_mail.tool_send_outlook_email`" not in prompt, (
            "step 8 still has a conditional 'Only call' instruction — the "
            "tool must NEVER be called"
        )

    def test_notify_step_forbids_send_and_mandates_draft(self):
        """Step 8 must (a) reaffirm the hard ban, (b) instruct the agent
        to draft the message in the reply, (c) tell the operator to copy
        into Outlook themselves. No conditional 'call the send tool'."""
        prompt = _get_template()["agent_instruction"]
        notify_start = prompt.find("8. **Notify")
        end_candidates = [
            i for i in (
                prompt.find("9.", notify_start + 1) if notify_start >= 0 else -1,
                prompt.find("\n## ", notify_start + 1) if notify_start >= 0 else -1,
            )
            if i > 0
        ]
        notify_end = min(end_candidates) if end_candidates else len(prompt)
        notify_section = prompt[notify_start:notify_end]
        assert notify_start >= 0, "step 8 (Notify) missing"
        # Step 8 must explicitly reference the ban / EMAIL SENDING POLICY.
        assert (
            "EMAIL SENDING POLICY" in notify_section
            or "hard-banned" in notify_section
        ), "step 8 must reference the ban so it is not read in isolation"
        # Must mandate drafting in the reply.
        assert "draft" in notify_section.lower(), (
            "step 8 must instruct the agent to surface a draft in its reply"
        )
        assert (
            "copies" in notify_section.lower()
            or "copy-paste" in notify_section.lower()
        ), (
            "step 8 must tell the agent that the operator copies the draft "
            "into Outlook themselves"
        )
        # No conditional 'call the send tool' wording.
        assert (
            "call `outlook_mail.tool_send_outlook_email`" not in notify_section
        ), "step 8 must not instruct calling the send tool under any wording"
        # Refuse-on-request wording — last-mile guard against operator pressure.
        assert "refuse" in notify_section.lower(), (
            "step 8 must instruct the agent to refuse if the operator asks "
            "to send anyway"
        )


# ---------------------------------------------------------------------------
# Tags / category — used by the UI gallery filters
# ---------------------------------------------------------------------------


class TestOneMailPerRun:
    """The webhook fans out one Microsoft Graph notification = one
    agent invocation = one email. The prompt must wire this contract
    so the LLM does not pull additional ARs via list/search inside a
    single run. Interactive chat is allowed to discover several mails
    but must still process them one at a time with operator confirmation
    between each."""

    def test_one_mail_per_run_block_present(self):
        prompt = _get_template()["agent_instruction"]
        assert "ONE MAIL PER RUN" in prompt, (
            "prompt must open with an explicit ONE MAIL PER RUN block — "
            "otherwise the LLM is free to list/search and batch several "
            "ARs into one agent run, defeating the backlog worker's "
            "one-notification-one-run guarantee"
        )

    def test_one_mail_per_run_documents_both_modes(self):
        prompt = _get_template()["agent_instruction"]
        # Webhook mode: strict single message_id, no list/search
        assert "Webhook mode" in prompt or "webhook mode" in prompt
        assert (
            "do NOT call `outlook_mail.tool_list_emails`" in prompt
            or "Do NOT call `outlook_mail.tool_list_emails`" in prompt
        ), "webhook section must explicitly forbid tool_list_emails"
        assert (
            "tool_search_emails" in prompt
        ), "webhook section must reference tool_search_emails to forbid it"
        # Chat mode: list/search allowed but sequential with ask
        assert "Chat mode" in prompt or "chat mode" in prompt
        assert (
            "one at a time" in prompt
            or "strictly one at a time" in prompt.lower()
        ), "chat section must mandate one-at-a-time processing"

    def test_one_mail_block_precedes_workflow(self):
        prompt = _get_template()["agent_instruction"]
        one_mail_idx = prompt.find("ONE MAIL PER RUN")
        workflow_idx = prompt.find("## Workflow")
        assert one_mail_idx >= 0 and workflow_idx >= 0
        assert one_mail_idx < workflow_idx, (
            "ONE MAIL PER RUN must come before the Workflow section so "
            "the LLM internalises the contract before reading the step list"
        )

    def test_workflow_step1_uses_message_id_from_trigger(self):
        """Step 1 ('Read the email') must point at the message_id passed
        in the webhook trigger, not at a search/list operation. This
        guards against a future edit that re-introduces 'find ARs via
        tool_list_emails' in the webhook workflow."""
        prompt = _get_template()["agent_instruction"]
        # Locate workflow section
        wf_start = prompt.find("## Workflow")
        wf_end = prompt.find("## Safety")
        assert wf_start >= 0 and wf_end > wf_start
        workflow = prompt[wf_start:wf_end]
        # Step 1 must mention message_id and tool_read_email
        assert "tool_read_email(message_id=" in workflow
        # No list/search instruction inside the workflow itself
        assert "tool_list_emails(" not in workflow, (
            "workflow must not instruct the agent to list emails — that "
            "would let one run process several ARs in batch"
        )
        assert "tool_search_emails(" not in workflow, (
            "workflow must not instruct the agent to search emails — same "
            "single-run violation as list_emails"
        )


# ---------------------------------------------------------------------------
# Attachment handling — internal sandbox only, no file push to operator
# ---------------------------------------------------------------------------


class TestAttachmentPolicy:
    """`tool_download_attachment` saves the file in the agent sandbox so
    the LLM can render/analyse it. There is no transfer channel back to
    the operator. The prompt must (1) forbid claiming the operator now
    has the files, (2) anchor PJ requests to the AR currently in the
    conversation rather than the latest mailbox message."""

    def test_attachment_policy_block_present(self):
        prompt = _get_template()["agent_instruction"]
        assert "ATTACHMENTS ARE INTERNAL TO YOU" in prompt, (
            "prompt must carry an explicit ATTACHMENTS block — without it "
            "the LLM defaults to claiming téléchargées avec succès and "
            "leaves the operator hunting for files that never reached them"
        )

    def test_attachment_policy_bans_success_claim(self):
        prompt = _get_template()["agent_instruction"]
        assert "fichiers téléchargés avec succès" in prompt, (
            "prompt must quote the forbidden phrase verbatim so the LLM "
            "recognises it and substitutes the approved wording"
        )
        assert "PJ récupérée pour analyse" in prompt
        assert "Curiosity downloads are forbidden" in prompt

    def test_attachment_policy_redirects_user_to_outlook(self):
        prompt = _get_template()["agent_instruction"]
        # When the operator wants the files for themselves, the agent
        # must NOT call download_attachment — it must redirect to Outlook.
        assert "grab the attachment from Outlook directly" in prompt

    def test_which_email_anchor_present(self):
        prompt = _get_template()["agent_instruction"]
        assert "WHICH EMAIL FOR ATTACHMENTS" in prompt
        assert "the AR currently in the conversation" in prompt, (
            "prompt must explicitly anchor PJ requests to the AR under "
            "discussion, not the latest mailbox message — the live regression "
            "on 2026-05-12 (asked for AR PJ, agent downloaded NOVAPLEST "
            "invoices instead) is what this rule prevents"
        )

    def test_which_email_anchor_asks_when_ambiguous(self):
        prompt = _get_template()["agent_instruction"]
        # The escape hatch when there is no clear conversational anchor:
        # the agent must STOP and ask, not guess.
        assert "Never guess." in prompt or "never guess" in prompt.lower()
        # The exact French question form is part of the prompt so the LLM
        # has a ready-made line to use.
        assert "Quelle AR voulez-vous que je récupère" in prompt


# ---------------------------------------------------------------------------
# Tags / category — used by the UI gallery filters
# ---------------------------------------------------------------------------


class TestTagsCategory:
    def test_scei_tag_present(self):
        template = _get_template()
        assert "scei" in template["tags"], (
            "the 'scei' tag is what visibility-restriction work will key on"
        )

    def test_category_is_automation(self):
        assert _get_template()["category"] == "automation"




class TestCommandesPersistTool:
    """The Commandes INSERT in the monolithic prompt must redirect to
    ``tool_persist_ar_record`` (Python guards) — same fix as PR for
    the recorder v2 sub-agent. See
    ``[[incident_2026-05-19_dashboard_dateReceptionAR]]``."""

    def test_prompt_mentions_tool_persist_ar_record(self):
        prompt = _get_template()["agent_instruction"]
        assert "tool_persist_ar_record" in prompt, (
            "The Commandes header INSERT must go through the Python "
            "tool ``tool_persist_ar_record`` (forces DateReceptionAR, "
            "Traite=0, Decision=NULL). Otherwise the dashboard KPIs "
            "regress to ``DateReceptionAR IS NULL`` and 78 stable."
        )

    def test_commandes_block_does_not_use_raw_sql_tool(self):
        prompt = _get_template()["agent_instruction"]
        cmd_idx = prompt.find("INSERT** into `dbo.Commandes`")
        assert cmd_idx >= 0, "Commandes INSERT block missing"
        block = prompt[cmd_idx:cmd_idx + 1500]
        assert "tool_persist_ar_record" in block, (
            "Commandes block must call tool_persist_ar_record."
        )
        # The block must explicitly forbid tool_run_sql_suiviar here
        # (LignesCommande uses it elsewhere — different block).
        assert "NOT `tool_run_sql_suiviar`" in block, (
            "Block must explicitly tell the agent NOT to use "
            "tool_run_sql_suiviar for this INSERT."
        )
