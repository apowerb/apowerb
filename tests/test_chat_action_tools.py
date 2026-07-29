"""Unit tests for chat action card tools.

These tools each return a dict shaped like ``{"_action_card": True, ...}``
which the frontend intercepts in ``useChat.js onToolCall`` to render
interactive cards.

Contract reference:
``scratchpad/action-cards-contract.md``
"""

from __future__ import annotations

import pytest

from th2agent.core.agent_helpers.chat_action_tools import (
    confirm_destructive,
    embed_chart,
    propose_agent_upgrade,
    propose_artifact_edit,
    request_file_from_user,
    request_location,
    request_payment,
    request_user_input,
    schedule_followup,
)


# --------------------------------------------------------------------------- #
# request_user_input
# --------------------------------------------------------------------------- #


class TestRequestUserInput:
    def test_text_input_returns_action_card(self) -> None:
        result = request_user_input(
            question="What's your name?",
            input_type="text",
            placeholder="Jane Doe",
        )
        assert result["_action_card"] is True
        assert result["kind"] == "user_input"
        assert result["status"] == "user_input_pending"
        assert result["question"] == "What's your name?"
        assert result["input_type"] == "text"
        assert result["placeholder"] == "Jane Doe"
        assert result["choices"] is None

    def test_select_input_with_choices(self) -> None:
        choices = ["Red", "Green", "Blue"]
        result = request_user_input(
            question="Pick a color",
            input_type="select",
            choices=choices,
        )
        assert result["_action_card"] is True
        assert result["kind"] == "user_input"
        assert result["status"] == "user_input_pending"
        assert result["input_type"] == "select"
        assert result["choices"] == choices

    @pytest.mark.parametrize(
        "valid_type",
        ["text", "number", "select", "multiline", "date"],
    )
    def test_all_valid_input_types_accepted(self, valid_type: str) -> None:
        result = request_user_input(question="Q?", input_type=valid_type)
        assert result["_action_card"] is True
        assert result["status"] == "user_input_pending"
        assert result["input_type"] == valid_type

    def test_invalid_input_type_returns_error(self) -> None:
        result = request_user_input(question="Q?", input_type="bogus")
        assert result.get("_action_card") is not True
        assert result["status"] == "error"
        assert "bogus" in result["message"]


# --------------------------------------------------------------------------- #
# confirm_destructive
# --------------------------------------------------------------------------- #


class TestConfirmDestructive:
    def test_returns_action_card(self) -> None:
        result = confirm_destructive(
            action="delete_file",
            impact="Permanent loss of data",
            item="report.pdf",
        )
        assert result["_action_card"] is True
        assert result["kind"] == "confirm_destructive"
        assert result["status"] == "confirm_destructive_pending"
        assert result["action"] == "delete_file"
        assert result["impact"] == "Permanent loss of data"
        assert result["item"] == "report.pdf"

    def test_item_defaults_to_none(self) -> None:
        result = confirm_destructive(
            action="drop_table",
            impact="All rows lost",
        )
        assert result["item"] is None
        assert result["status"] == "confirm_destructive_pending"


# --------------------------------------------------------------------------- #
# request_payment
# --------------------------------------------------------------------------- #


class TestRequestPayment:
    def test_returns_action_card(self) -> None:
        result = request_payment(
            amount=19.99,
            currency="USD",
            reason="Monthly subscription",
            checkout_url="https://pay.example.com/x",
        )
        assert result["_action_card"] is True
        assert result["kind"] == "payment"
        assert result["status"] == "payment_pending"
        assert result["amount"] == 19.99
        assert result["currency"] == "USD"
        assert result["reason"] == "Monthly subscription"
        assert result["checkout_url"] == "https://pay.example.com/x"

    def test_checkout_url_optional(self) -> None:
        result = request_payment(amount=5.0, currency="EUR", reason="Tip")
        assert result["checkout_url"] is None
        assert result["status"] == "payment_pending"


# --------------------------------------------------------------------------- #
# schedule_followup
# --------------------------------------------------------------------------- #


class TestScheduleFollowup:
    def test_returns_action_card(self) -> None:
        result = schedule_followup(
            when_iso="2026-05-01T10:00:00Z",
            recap="Review onboarding progress",
            calendar_link="https://cal.example.com/abc",
        )
        assert result["_action_card"] is True
        assert result["kind"] == "followup"
        assert result["status"] == "followup_pending"
        assert result["when_iso"] == "2026-05-01T10:00:00Z"
        assert result["recap"] == "Review onboarding progress"
        assert result["calendar_link"] == "https://cal.example.com/abc"

    def test_calendar_link_optional(self) -> None:
        result = schedule_followup(
            when_iso="2026-05-01T10:00:00Z", recap="Check in"
        )
        assert result["calendar_link"] is None


# --------------------------------------------------------------------------- #
# propose_artifact_edit
# --------------------------------------------------------------------------- #


class TestProposeArtifactEdit:
    def test_returns_action_card(self) -> None:
        result = propose_artifact_edit(
            filename="main.py",
            diff="--- a/main.py\n+++ b/main.py\n@@ ...",
            summary="Rename variable",
        )
        assert result["_action_card"] is True
        assert result["kind"] == "artifact_edit"
        assert result["status"] == "artifact_edit_pending"
        assert result["filename"] == "main.py"
        assert result["diff"].startswith("--- a/main.py")
        assert result["summary"] == "Rename variable"

    def test_summary_optional(self) -> None:
        result = propose_artifact_edit(filename="x.md", diff="@@ ...")
        assert result["summary"] is None


# --------------------------------------------------------------------------- #
# request_file_from_user
# --------------------------------------------------------------------------- #


class TestRequestFileFromUser:
    def test_returns_action_card(self) -> None:
        result = request_file_from_user(
            purpose="Upload ID scan",
            accept="image/*",
            max_size_mb=10,
        )
        assert result["_action_card"] is True
        assert result["kind"] == "file_request"
        assert result["status"] == "file_request_pending"
        assert result["purpose"] == "Upload ID scan"
        assert result["accept"] == "image/*"
        assert result["max_size_mb"] == 10

    def test_optional_fields_default_none(self) -> None:
        result = request_file_from_user(purpose="Send invoice")
        assert result["accept"] is None
        assert result["max_size_mb"] is None


# --------------------------------------------------------------------------- #
# propose_agent_upgrade
# --------------------------------------------------------------------------- #


class TestProposeAgentUpgrade:
    def test_returns_action_card(self) -> None:
        result = propose_agent_upgrade(
            capability="OCR parsing",
            reason="Needed to read scanned PDFs",
            skill_id="skill_ocr_v1",
            tool_name="pdf_ocr",
        )
        assert result["_action_card"] is True
        assert result["kind"] == "agent_upgrade"
        assert result["status"] == "agent_upgrade_pending"
        assert result["capability"] == "OCR parsing"
        assert result["reason"] == "Needed to read scanned PDFs"
        assert result["skill_id"] == "skill_ocr_v1"
        assert result["tool_name"] == "pdf_ocr"

    def test_optional_fields_default_none(self) -> None:
        result = propose_agent_upgrade(
            capability="X",
            reason="Y",
        )
        assert result["skill_id"] is None
        assert result["tool_name"] is None


# --------------------------------------------------------------------------- #
# embed_chart
# --------------------------------------------------------------------------- #


class TestEmbedChart:
    def test_returns_action_card(self) -> None:
        result = embed_chart(chart_id="chart_42", title="Revenue by month")
        assert result["_action_card"] is True
        assert result["kind"] == "chart_embed"
        assert result["status"] == "chart_embed_pending"
        assert result["chart_id"] == "chart_42"
        assert result["title"] == "Revenue by month"

    def test_title_optional(self) -> None:
        result = embed_chart(chart_id="chart_1")
        assert result["title"] is None

    def test_includes_dashboard_id_key(self) -> None:
        # No AGENT_OWNER in unit context -> helper returns None, key present.
        result = embed_chart(chart_id="chart_42")
        assert "dashboard_id" in result

    def test_carries_dashboard_id_from_helper(self) -> None:
        from unittest.mock import patch
        with patch(
            "th2agent.tools_store.portfolio.business_intelligence.ensure_chart_in_chat_dashboard",
            return_value="dash_99",
        ):
            result = embed_chart(chart_id="chart_42")
        assert result["dashboard_id"] == "dash_99"

    def test_resilient_when_helper_raises(self) -> None:
        from unittest.mock import patch
        with patch(
            "th2agent.tools_store.portfolio.business_intelligence.ensure_chart_in_chat_dashboard",
            side_effect=RuntimeError("boom"),
        ):
            result = embed_chart(chart_id="chart_42")
        assert result["_action_card"] is True
        assert result["dashboard_id"] is None


# --------------------------------------------------------------------------- #
# request_location
# --------------------------------------------------------------------------- #


class TestRequestLocation:
    def test_returns_action_card(self) -> None:
        result = request_location(
            reason="Find nearest store",
            precision="coarse",
        )
        assert result["_action_card"] is True
        assert result["kind"] == "location_request"
        assert result["status"] == "location_request_pending"
        assert result["reason"] == "Find nearest store"
        assert result["precision"] == "coarse"

    def test_precision_optional(self) -> None:
        result = request_location(reason="Local weather")
        assert result["precision"] is None


# --------------------------------------------------------------------------- #
# Pause du run ADK : les cartes "en attente d'utilisateur" doivent terminer le
# tour (escalate + skip_summarization) pour ne pas reboucler jusqu'a
# max_llm_calls. embed_chart (affichage) ne doit PAS halter.
# --------------------------------------------------------------------------- #


class _Actions:
    escalate = False
    skip_summarization = False


class _ToolCtx:
    def __init__(self) -> None:
        self.actions = _Actions()


class TestPauseFlags:
    PAUSING = [
        lambda c: request_user_input(question="?", input_type="text", tool_context=c),
        lambda c: confirm_destructive(action="del", impact="x", tool_context=c),
        lambda c: request_payment(amount=1.0, currency="EUR", reason="x", tool_context=c),
        lambda c: schedule_followup(when_iso="2026-01-01T00:00:00Z", recap="x", tool_context=c),
        lambda c: propose_artifact_edit(filename="a", diff="d", tool_context=c),
        lambda c: request_file_from_user(purpose="x", tool_context=c),
        lambda c: propose_agent_upgrade(capability="x", reason="y", tool_context=c),
        lambda c: request_location(reason="x", tool_context=c),
    ]

    @pytest.mark.parametrize("call", PAUSING)
    def test_pause_flags_set(self, call) -> None:
        ctx = _ToolCtx()
        result = call(ctx)
        assert result["_action_card"] is True
        assert ctx.actions.escalate is True
        assert ctx.actions.skip_summarization is True

    def test_pause_flags_noop_without_context(self) -> None:
        # Pas de tool_context (tests / appel hors-ADK) : aucun crash.
        assert request_user_input(question="?", input_type="text")["_action_card"] is True

    def test_embed_chart_does_not_pause(self) -> None:
        ctx = _ToolCtx()
        result = embed_chart(chart_id="c1", title="t")
        assert result["_action_card"] is True
        assert ctx.actions.escalate is False
        assert ctx.actions.skip_summarization is False
