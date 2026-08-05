"""Option C (David, PR review 05/08): a session-scoped upload lives under
its own session; a session-less upload lives under a shared, agent-wide
scope -- never mixed into a fabricated/real session_id namespace.
"""

from __future__ import annotations

from apowerb.artifacts.input_scope import SHARED_INPUT_SCOPE, resolve_input_session_id


class TestResolveInputSessionId:
    def test_known_session_id_is_returned_unchanged(self):
        assert resolve_input_session_id("session_123") == "session_123"

    def test_none_resolves_to_shared_scope(self):
        assert resolve_input_session_id(None) == SHARED_INPUT_SCOPE

    def test_empty_string_resolves_to_shared_scope(self):
        assert resolve_input_session_id("") == SHARED_INPUT_SCOPE
