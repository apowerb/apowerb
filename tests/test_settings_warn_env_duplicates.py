"""Tests for ``_warn_duplicate_env_keys`` — warn at startup when the same
variable is declared multiple times in ``.env``.

Background
----------
On 2026-05-07, the SCEI ``.env`` ended up with duplicate ``JWT_SECRET_KEY``
declarations after a series of hot patches. pydantic-settings / python-dotenv
silently keep the *last* definition, so the wrong value was loaded without
any warning. This helper makes such mistakes loud at boot.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_env(tmp_path: Path, content: str) -> Path:
    p = tmp_path / ".env"
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


class TestDuplicateDetection:
    def test_single_duplicate_emits_one_warning(self, tmp_path):
        from th2agent.configs.settings import _warn_duplicate_env_keys

        env = _write_env(
            tmp_path,
            "FOO=bar\nBAZ=qux\nFOO=other\n",
        )

        with patch("th2agent.configs.settings._logger") as logger:
            _warn_duplicate_env_keys(str(env))

        assert logger.warning.call_count == 1
        msg = logger.warning.call_args[0][0]
        args = logger.warning.call_args[0][1:]
        # The format string mentions %r (the key) and the line list
        assert "duplicate variable" in msg
        assert "FOO" in args  # first %r argument
        assert "1, 3" in args  # the line list

    def test_multiple_distinct_duplicates_each_warn(self, tmp_path):
        from th2agent.configs.settings import _warn_duplicate_env_keys

        env = _write_env(
            tmp_path,
            "FOO=1\nBAR=1\nFOO=2\nBAR=2\nQUX=1\n",
        )

        with patch("th2agent.configs.settings._logger") as logger:
            _warn_duplicate_env_keys(str(env))

        assert logger.warning.call_count == 2
        warned_keys = {call.args[1] for call in logger.warning.call_args_list}
        assert warned_keys == {"FOO", "BAR"}

    def test_no_duplicate_no_warning(self, tmp_path):
        from th2agent.configs.settings import _warn_duplicate_env_keys

        env = _write_env(
            tmp_path,
            "FOO=1\nBAR=2\nQUX=3\n",
        )

        with patch("th2agent.configs.settings._logger") as logger:
            _warn_duplicate_env_keys(str(env))

        logger.warning.assert_not_called()

    def test_missing_file_is_silent(self, tmp_path):
        """Production deployments often set every variable via the process
        env (no .env on disk). The helper must be a no-op then."""
        from th2agent.configs.settings import _warn_duplicate_env_keys

        with patch("th2agent.configs.settings._logger") as logger:
            _warn_duplicate_env_keys(str(tmp_path / "does-not-exist"))

        logger.warning.assert_not_called()


# ---------------------------------------------------------------------------
# Edge cases — comments, blanks, export prefix, malformed lines
# ---------------------------------------------------------------------------


class TestEnvParsingTolerance:
    def test_comments_and_blanks_are_ignored(self, tmp_path):
        from th2agent.configs.settings import _warn_duplicate_env_keys

        env = _write_env(
            tmp_path,
            "# this is a comment\n"
            "\n"
            "FOO=bar\n"
            "  # indented comment\n"
            "FOO=baz\n",
        )

        with patch("th2agent.configs.settings._logger") as logger:
            _warn_duplicate_env_keys(str(env))

        # FOO is on lines 3 and 5 — comments / blanks must NOT shift the count
        assert logger.warning.call_count == 1
        args = logger.warning.call_args[0][1:]
        assert "3, 5" in args

    def test_export_prefix_is_stripped(self, tmp_path):
        from th2agent.configs.settings import _warn_duplicate_env_keys

        env = _write_env(
            tmp_path,
            "export FOO=1\nFOO=2\n",
        )

        with patch("th2agent.configs.settings._logger") as logger:
            _warn_duplicate_env_keys(str(env))

        assert logger.warning.call_count == 1
        assert "FOO" in logger.warning.call_args[0][1:]

    def test_quoted_keys_normalise_to_unquoted(self, tmp_path):
        """A mix of ``"FOO"=bar`` and ``FOO=baz`` must still trigger the
        duplicate warning — review feedback after the original PR
        otherwise let this through silently."""
        from th2agent.configs.settings import _warn_duplicate_env_keys

        env = _write_env(
            tmp_path,
            '"FOO"=1\nFOO=2\n',
        )

        with patch("th2agent.configs.settings._logger") as logger:
            _warn_duplicate_env_keys(str(env))

        assert logger.warning.call_count == 1
        # Both quoted and unquoted should normalise to ``FOO``.
        assert "FOO" in logger.warning.call_args[0][1:]

    def test_single_quoted_keys_normalise_to_unquoted(self, tmp_path):
        from th2agent.configs.settings import _warn_duplicate_env_keys

        env = _write_env(
            tmp_path,
            "'FOO'=1\nFOO=2\n",
        )

        with patch("th2agent.configs.settings._logger") as logger:
            _warn_duplicate_env_keys(str(env))

        assert logger.warning.call_count == 1
        assert "FOO" in logger.warning.call_args[0][1:]

    def test_quoted_value_with_equals_does_not_split_key(self, tmp_path):
        """``FOO="a=b"`` must NOT be parsed as key ``"a`` — the value side
        keeps everything past the first ``=``."""
        from th2agent.configs.settings import _warn_duplicate_env_keys

        env = _write_env(
            tmp_path,
            'FOO="a=b"\nBAR=1\n',
        )

        with patch("th2agent.configs.settings._logger") as logger:
            _warn_duplicate_env_keys(str(env))

        logger.warning.assert_not_called()

    def test_lines_without_equals_are_ignored(self, tmp_path):
        from th2agent.configs.settings import _warn_duplicate_env_keys

        env = _write_env(
            tmp_path,
            "FOO=1\n"
            "this is not an assignment\n"
            "FOO=2\n",
        )

        with patch("th2agent.configs.settings._logger") as logger:
            _warn_duplicate_env_keys(str(env))

        assert logger.warning.call_count == 1

    def test_value_with_equals_is_handled(self, tmp_path):
        """A value containing ``=`` must not look like a duplicate of the key."""
        from th2agent.configs.settings import _warn_duplicate_env_keys

        env = _write_env(
            tmp_path,
            "URL=https://example.com/?a=1&b=2\n"
            "OTHER=plain\n",
        )

        with patch("th2agent.configs.settings._logger") as logger:
            _warn_duplicate_env_keys(str(env))

        logger.warning.assert_not_called()


# ---------------------------------------------------------------------------
# Integration — get_settings calls the helper
# ---------------------------------------------------------------------------


class TestGetSettingsWiring:
    def test_get_settings_invokes_warn_helper(self, tmp_path, monkeypatch):
        """Boot path must run the duplicate scan."""
        from th2agent.configs import settings as settings_mod

        # Make sure get_settings actually re-runs (it's lru_cached).
        settings_mod.get_settings.cache_clear()

        called = {}

        def fake_warn(env_path=settings_mod._ENV_FILE):
            called["env_path"] = env_path

        monkeypatch.setattr(settings_mod, "_warn_duplicate_env_keys", fake_warn)

        # Avoid hitting the real .env and Settings() failures here — we only
        # care that the helper was invoked. The call to Settings() may raise
        # because env is incomplete, so we catch that.
        try:
            settings_mod.get_settings()
        except Exception:
            pass

        assert "env_path" in called
        # default uses the module constant
        assert called["env_path"] == settings_mod._ENV_FILE
