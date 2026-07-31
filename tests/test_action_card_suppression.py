"""TDD tests for action-card tool suppression in structured-output agents.

Agents with ``output_schema_name`` set (e.g. SCEI pipeline sub-agents) are
fully-automatic webhook pipelines with no human in the loop. They MUST NOT
receive chat-oriented action-card tools (``request_user_input``,
``confirm_destructive``, ``request_payment``, ``schedule_followup``,
``propose_artifact_edit``) nor the ``INTERACTIVE_UI_INSTRUCTION`` text that
pushes the LLM to call them.

Chat agents (``output_schema_name`` is None / empty) keep all tools so the
existing UI pipelines are not broken.

These tests are RED against the current code and turn GREEN after the fix.
"""

from __future__ import annotations



# ---------------------------------------------------------------------------
# Constants tests
# ---------------------------------------------------------------------------


class TestChatActionCardToolNames:
    """The constant must list exactly the 5 action-card tools that are
    suppressed for structured-output agents."""

    def test_constant_exists_and_is_frozenset(self):
        from apowerb.core.agent_helpers.agent_utils import (
            _CHAT_ACTION_CARD_TOOL_NAMES,
        )

        assert isinstance(_CHAT_ACTION_CARD_TOOL_NAMES, frozenset)

    def test_constant_contains_exactly_5_tools(self):
        from apowerb.core.agent_helpers.agent_utils import (
            _CHAT_ACTION_CARD_TOOL_NAMES,
        )

        assert len(_CHAT_ACTION_CARD_TOOL_NAMES) == 5

    def test_constant_contains_all_expected_names(self):
        from apowerb.core.agent_helpers.agent_utils import (
            _CHAT_ACTION_CARD_TOOL_NAMES,
        )

        assert _CHAT_ACTION_CARD_TOOL_NAMES == frozenset({
            "request_user_input",
            "confirm_destructive",
            "request_payment",
            "schedule_followup",
            "propose_artifact_edit",
        })


# ---------------------------------------------------------------------------
# _should_inject_chat_action_tools() tests
# ---------------------------------------------------------------------------


class TestShouldInjectChatActionTools:
    def test_returns_false_when_output_schema_name_set(self):
        from apowerb.core.agent_helpers.agent_utils import (
            _should_inject_chat_action_tools,
        )

        assert _should_inject_chat_action_tools(
            {"output_schema_name": "SCEIIntakePayload"}
        ) is False

    def test_returns_false_for_any_non_empty_schema_name(self):
        from apowerb.core.agent_helpers.agent_utils import (
            _should_inject_chat_action_tools,
        )

        for schema in ("ARMatchPayload", "ARRecordPayload", "ARNotifyPayload",
                       "SomeOtherPayload"):
            assert _should_inject_chat_action_tools(
                {"output_schema_name": schema}
            ) is False, schema

    def test_returns_true_when_output_schema_name_is_none(self):
        from apowerb.core.agent_helpers.agent_utils import (
            _should_inject_chat_action_tools,
        )

        assert _should_inject_chat_action_tools({"output_schema_name": None}) is True

    def test_returns_true_when_output_schema_name_is_empty_string(self):
        from apowerb.core.agent_helpers.agent_utils import (
            _should_inject_chat_action_tools,
        )

        assert _should_inject_chat_action_tools({"output_schema_name": ""}) is True

    def test_returns_true_when_output_schema_name_key_absent(self):
        from apowerb.core.agent_helpers.agent_utils import (
            _should_inject_chat_action_tools,
        )

        assert _should_inject_chat_action_tools({}) is True

    def test_returns_true_for_chat_agent_with_no_schema(self):
        """Regression guard: chat agents without output_schema_name keep all
        tools. Breaking this would suppress chips in the emailing pipeline."""
        from apowerb.core.agent_helpers.agent_utils import (
            _should_inject_chat_action_tools,
        )

        chat_agent_details = {
            "agent_instruction": "You are a helpful assistant.",
            "agent_model": "openai/gpt-4o",
            # No output_schema_name key at all
        }
        assert _should_inject_chat_action_tools(chat_agent_details) is True
