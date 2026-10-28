"""The Mage scheduler's setup checks report through the logger, not stdout.

Messages written with ``print()`` bypass log levels and the log pipeline. These
tests pin where the pipeline and block checks of ``AgentOrchestrator`` report,
and that a failure keeps its traceback.
"""

import logging
from unittest.mock import MagicMock

from apowerb.scheduler.mage import AgentOrchestrator

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
