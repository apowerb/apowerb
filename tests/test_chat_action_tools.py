"""Unit tests for chat action card tools.

The card is built by the frontend from the call arguments (``useChat.js``,
``onToolCall``). Cards that wait for the user return ``None`` (roadmap#79, see
``test_chat_action_tools_adk.py``); ``embed_chart`` returns a short
acknowledgement ``{"status": "displayed", "kind": "chart_embed", ...}``.

Contract reference:
``scratchpad/action-cards-contract.md``
"""

from __future__ import annotations

import pytest

from apowerb.core.agent_helpers.chat_action_tools import (
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
        assert result is None

    def test_select_input_with_choices(self) -> None:
        choices = ["Red", "Green", "Blue"]
        result = request_user_input(
            question="Pick a color",
            input_type="select",
            choices=choices,
        )
        assert result is None

    @pytest.mark.parametrize(
        "valid_type",
        ["text", "number", "select", "multiline", "date"],
    )
    def test_all_valid_input_types_accepted(self, valid_type: str) -> None:
        result = request_user_input(question="Q?", input_type=valid_type)
        assert result is None

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
        assert result is None

    def test_item_defaults_to_none(self) -> None:
        result = confirm_destructive(
            action="drop_table",
            impact="All rows lost",
        )
        assert result is None


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
        assert result is None

    def test_checkout_url_optional(self) -> None:
        result = request_payment(amount=5.0, currency="EUR", reason="Tip")
        assert result is None


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
        assert result is None

    def test_calendar_link_optional(self) -> None:
        result = schedule_followup(
            when_iso="2026-05-01T10:00:00Z", recap="Check in"
        )
        assert result is None


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
        assert result is None

    def test_summary_optional(self) -> None:
        result = propose_artifact_edit(filename="x.md", diff="@@ ...")
        assert result is None


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
        assert result is None

    def test_optional_fields_default_none(self) -> None:
        result = request_file_from_user(purpose="Send invoice")
        assert result is None


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
        assert result is None

    def test_optional_fields_default_none(self) -> None:
        result = propose_agent_upgrade(
            capability="X",
            reason="Y",
        )
        assert result is None


# --------------------------------------------------------------------------- #
# embed_chart
# --------------------------------------------------------------------------- #


class TestEmbedChart:
    def test_returns_action_card(self) -> None:
        result = embed_chart(chart_id="chart_42", title="Revenue by month")
        assert result["status"] == "displayed"
        assert result["kind"] == "chart_embed"

    def test_title_optional(self) -> None:
        result = embed_chart(chart_id="chart_1")
        assert result["status"] == "displayed"

    def test_refuses_a_chart_that_does_not_exist(self) -> None:
        # Un chart_id invente rendait une carte en 404 : on refuse, avec une
        # consigne que le modele peut suivre.
        from unittest.mock import patch
        with patch(
            "apowerb.tools_store.portfolio.business_intelligence.resolve_chart_for_embed",
            return_value=("missing", None),
        ):
            result = embed_chart(chart_id="invente")
        assert result["success"] is False
        assert "tool_create_chart" in result["error"]

    def test_fails_open_when_the_lookup_breaks(self) -> None:
        # Une panne de base ne doit jamais bloquer un vrai graphique.
        from unittest.mock import patch
        with patch(
            "apowerb.tools_store.portfolio.business_intelligence.resolve_chart_for_embed",
            side_effect=RuntimeError("boom"),
        ):
            result = embed_chart(chart_id="chart_42")
        assert result["status"] == "displayed"


# --------------------------------------------------------------------------- #
# request_location
# --------------------------------------------------------------------------- #


class TestRequestLocation:
    def test_returns_action_card(self) -> None:
        result = request_location(
            reason="Find nearest store",
            precision="coarse",
        )
        assert result is None

    def test_precision_optional(self) -> None:
        result = request_location(reason="Local weather")
        assert result is None


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
        assert result is None
        assert ctx.actions.escalate is True
        assert ctx.actions.skip_summarization is True

    def test_pause_flags_noop_without_context(self) -> None:
        # Pas de tool_context (tests / appel hors-ADK) : aucun crash.
        assert request_user_input(question="?", input_type="text") is None

    def test_embed_chart_does_not_pause(self) -> None:
        ctx = _ToolCtx()
        result = embed_chart(chart_id="c1", title="t")
        assert result["status"] == "displayed"
        assert ctx.actions.escalate is False
        assert ctx.actions.skip_summarization is False
