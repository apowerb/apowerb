"""The Mage scheduler's setup checks report through the logger, not stdout.

Messages written with ``print()`` bypass log levels and the log pipeline. These
tests pin where the pipeline and block checks of ``AgentOrchestrator`` report,
that a failure keeps its traceback, and that the startup line of
``MageAPIClient`` says whether the secrets are set and never what they are.
"""

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

from apowerb.scheduler import mage
from apowerb.scheduler.mage import AgentOrchestrator, MageAPIClient

LOGGER_NAME = "apowerb.scheduler.mage"


def _orchestrator(client: MagicMock) -> AgentOrchestrator:
    return AgentOrchestrator(client=client)


def _records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER_NAME]


class TestPipelineCheck:
    def test_an_existing_pipeline_is_logged_and_nothing_is_printed(
        self, caplog, capsys
    ):
        client = MagicMock()
        client.pipeline_exists.return_value = True
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)

        assert _orchestrator(client)._ensure_pipeline_exists() is True

        assert capsys.readouterr().out == ""
        assert any("already exists" in r.getMessage() for r in _records(caplog))

    def test_a_failed_creation_is_a_warning_that_names_the_result(self, caplog, capsys):
        client = MagicMock()
        client.pipeline_exists.return_value = False
        client.create_pipeline.return_value = None
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)

        assert _orchestrator(client)._ensure_pipeline_exists() is False

        assert capsys.readouterr().out == ""
        warnings = [r for r in _records(caplog) if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "create_pipeline returned None" in warnings[0].getMessage()

    def test_an_unexpected_error_is_logged_with_its_traceback(self, caplog, capsys):
        client = MagicMock()
        client.pipeline_exists.side_effect = RuntimeError("boom")
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)

        assert _orchestrator(client)._ensure_pipeline_exists() is False

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
        errors = [r for r in _records(caplog) if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert errors[0].exc_info is not None
        assert errors[0].exc_info[0] is RuntimeError


class TestBlockCheck:
    def test_an_existing_block_is_logged_and_nothing_is_printed(self, caplog, capsys):
        client = MagicMock()
        client.block_exists.return_value = True
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)

        assert _orchestrator(client)._ensure_block_exists() is True

        assert capsys.readouterr().out == ""
        assert any("already exists" in r.getMessage() for r in _records(caplog))

    def test_a_failed_creation_is_a_warning_that_names_the_result(self, caplog, capsys):
        client = MagicMock()
        client.block_exists.return_value = False
        client.create_block.return_value = None
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)

        assert _orchestrator(client)._ensure_block_exists() is False

        assert capsys.readouterr().out == ""
        warnings = [r for r in _records(caplog) if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "create_block returned None" in warnings[0].getMessage()

    def test_an_unexpected_error_is_logged_with_its_traceback(self, caplog, capsys):
        client = MagicMock()
        client.block_exists.side_effect = RuntimeError("boom")
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)

        assert _orchestrator(client)._ensure_block_exists() is False

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""
        errors = [r for r in _records(caplog) if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert errors[0].exc_info is not None
        assert errors[0].exc_info[0] is RuntimeError


class TestClientStartup:
    API_KEY = "sentinel-api-key-6d1f"
    OAUTH_TOKEN = "sentinel-oauth-token-9c2e"

    def _client(self, monkeypatch, oauth_token: str | None) -> MageAPIClient:
        monkeypatch.setattr(MageAPIClient, "_instance", None)
        monkeypatch.setattr(
            mage,
            "settings",
            SimpleNamespace(
                base_url="http://mage.invalid",
                project_name="bench",
                api_key=self.API_KEY,
                oauth_token=oauth_token,
            ),
        )
        return MageAPIClient()

    def test_the_startup_line_says_the_secrets_are_set_never_their_values(
        self, caplog, capsys, monkeypatch
    ):
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

        self._client(monkeypatch, self.OAUTH_TOKEN)

        logged = "\n".join(r.getMessage() for r in caplog.records)
        written = capsys.readouterr()
        assert "api_key=set" in logged
        assert "oauth_token=set" in logged
        for secret in (self.API_KEY, self.OAUTH_TOKEN):
            assert secret not in logged
            assert secret not in written.out + written.err

    def test_an_unset_oauth_token_is_reported_as_not_set(self, caplog, monkeypatch):
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)

        self._client(monkeypatch, None)

        assert any("oauth_token=not set" in r.getMessage() for r in _records(caplog))
