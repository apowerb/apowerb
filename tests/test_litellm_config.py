"""Tests for OVHCloudMessageHandler.modify_messages_for_ovhcloud."""

import copy
import pytest

from th2agent.helpers.litellm_config import OVHCloudMessageHandler


@pytest.fixture
def handler():
    return OVHCloudMessageHandler()


def _call(handler, messages, model="ovhcloud/Mistral-Small"):
    """Shortcut: feed messages through the handler and return the result list."""
    kwargs = {"messages": copy.deepcopy(messages), "model": model}
    result = handler.modify_messages_for_ovhcloud(kwargs)
    return result["messages"]


# ── Fix 1: synthetic assistant between tool → user ─────────────────────


class TestToolToUserTransition:
    """A user message must never follow a tool message directly."""

    def test_inserts_assistant_between_tool_and_user(self, handler):
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "What is the weather?"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "Sunny, 25°C"},
            {"role": "user", "content": "Thanks!"},
        ]

        result = _call(handler, messages)
        roles = [m["role"] for m in result]

        # There must be an assistant between the tool result and the user
        for i in range(1, len(roles)):
            if roles[i] == "user" and roles[i - 1] == "tool":
                pytest.fail(f"user directly follows tool at index {i}: {roles}")

        # Verify the synthetic assistant exists
        assert "assistant" in roles
        tool_idx = next(i for i, m in enumerate(result) if m["role"] == "tool")
        assert result[tool_idx + 1]["role"] == "assistant"
        # The original user message must still be there
        assert result[tool_idx + 2]["role"] == "user"
        assert result[tool_idx + 2]["content"] == "Thanks!"


# ── Fix 2: missing tool results ────────────────────────────────────────


class TestMissingToolResults:
    """Every tool_call_id must have a matching tool-result message."""

    def test_inserts_placeholder_for_missing_tool_result(self, handler):
        messages = [
            {"role": "user", "content": "Do two things."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_a", "type": "function", "function": {"name": "tool_a", "arguments": "{}"}},
                    {"id": "call_b", "type": "function", "function": {"name": "tool_b", "arguments": "{}"}},
                ],
            },
            # Only one tool result — call_b is missing
            {"role": "tool", "tool_call_id": "call_a", "content": "result A"},
        ]

        result = _call(handler, messages)

        # Collect all tool messages
        tool_msgs = [m for m in result if m["role"] == "tool"]
        tool_ids = {m["tool_call_id"] for m in tool_msgs}

        assert "call_a" in tool_ids
        assert "call_b" in tool_ids
        assert len(tool_msgs) == 2

        # The placeholder should have a content string
        placeholder = next(m for m in tool_msgs if m["tool_call_id"] == "call_b")
        assert placeholder["content"] == "No result available"

    def test_no_placeholder_when_all_results_present(self, handler):
        messages = [
            {"role": "user", "content": "Do two things."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_a", "type": "function", "function": {"name": "tool_a", "arguments": "{}"}},
                    {"id": "call_b", "type": "function", "function": {"name": "tool_b", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call_a", "content": "result A"},
            {"role": "tool", "tool_call_id": "call_b", "content": "result B"},
        ]

        result = _call(handler, messages)

        tool_msgs = [m for m in result if m["role"] == "tool"]
        assert len(tool_msgs) == 2
        # No placeholder — both have real content
        assert all(m["content"] != "No result available" for m in tool_msgs)


# ── No modification on normal sequences ────────────────────────────────


class TestNormalSequences:
    """Sequences without tool calls should not be altered (beyond system merge)."""

    def test_simple_conversation_unchanged(self, handler):
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there!"},
            {"role": "user", "content": "How are you?"},
            {"role": "assistant", "content": "I'm fine."},
        ]

        result = _call(handler, messages)
        roles = [m["role"] for m in result]

        assert roles == ["system", "user", "assistant", "user", "assistant"]
        assert result[1]["content"] == "Hello"
        assert result[2]["content"] == "Hi there!"

    def test_non_ovhcloud_model_skipped(self, handler):
        messages = [
            {"role": "system", "content": "Sys"},
            {"role": "tool", "tool_call_id": "x", "content": "res"},
            {"role": "user", "content": "Hi"},
        ]

        result = _call(handler, messages, model="anthropic/claude-3")
        # Should be returned untouched
        assert result == messages


# ── Combined scenarios ─────────────────────────────────────────────────


class TestCombinedScenarios:
    """End-to-end sequences that exercise both fixes together."""

    def test_missing_result_and_tool_to_user(self, handler):
        """assistant(2 tool_calls) → 1 tool result → user.
        Must insert placeholder AND synthetic assistant."""
        messages = [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "Do things."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "tc1", "type": "function", "function": {"name": "f1", "arguments": "{}"}},
                    {"id": "tc2", "type": "function", "function": {"name": "f2", "arguments": "{}"}},
                ],
            },
            {"role": "tool", "tool_call_id": "tc1", "content": "ok"},
            {"role": "user", "content": "Next question"},
        ]

        result = _call(handler, messages)
        roles = [m["role"] for m in result]

        # tc2 placeholder must exist
        tool_ids = {m["tool_call_id"] for m in result if m["role"] == "tool"}
        assert "tc2" in tool_ids

        # No tool→user transition
        for i in range(1, len(roles)):
            assert not (roles[i] == "user" and roles[i - 1] == "tool"), (
                f"Forbidden tool→user at index {i}: {roles}"
            )

        # Expected order: system, user, assistant(tc), tool, tool(placeholder), assistant(synthetic), user
        assert roles[0] == "system"
        assert roles[1] == "user"
        assert roles[2] == "assistant"  # with tool_calls
        assert roles[-1] == "user"
        assert roles[-2] == "assistant"  # synthetic

    def test_multiple_tool_call_rounds(self, handler):
        """Two consecutive rounds of tool calls."""
        messages = [
            {"role": "user", "content": "Start"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "r1c1", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "r1c1", "content": "res1"},
            {"role": "assistant", "content": "Got it. Let me check more."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "r2c1", "type": "function", "function": {"name": "g", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "r2c1", "content": "res2"},
            {"role": "user", "content": "Done?"},
        ]

        result = _call(handler, messages)
        roles = [m["role"] for m in result]

        # No tool→user
        for i in range(1, len(roles)):
            assert not (roles[i] == "user" and roles[i - 1] == "tool"), (
                f"Forbidden tool→user at index {i}: {roles}"
            )

    def test_system_messages_merged(self, handler):
        """Multiple system messages should be merged into one."""
        messages = [
            {"role": "system", "content": "Rule 1"},
            {"role": "system", "content": "Rule 2"},
            {"role": "user", "content": "Hi"},
        ]

        result = _call(handler, messages)

        system_msgs = [m for m in result if m["role"] == "system"]
        assert len(system_msgs) == 1
        assert "Rule 1" in system_msgs[0]["content"]
        assert "Rule 2" in system_msgs[0]["content"]
