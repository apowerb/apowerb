"""The Mage scheduler's initialisation and trigger paths report through the logger.

Messages written with ``print()`` bypass log levels and the log pipeline. These
tests pin where ``AgentOrchestrator._initialize_pipeline`` and the two trigger
builders report, that what they return is unchanged, and that the trigger token
the API-trigger path hands back to its caller is not written to the log.
"""

import logging
from unittest.mock import MagicMock

import pytest

from apowerb.scheduler.mage import AgentOrchestrator
from apowerb.scheduler.th2etl_client import OrchestratorUnavailable

LOGGER_NAME = "apowerb.scheduler.mage"
TRIGGER_TOKEN = "trigger-token-0123456789"


def _orchestrator(client: MagicMock, initialized: bool = True) -> AgentOrchestrator:
    orchestrator = AgentOrchestrator(client=client)
    orchestrator._pipeline_initialized = initialized
    return orchestrator


def _records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER_NAME]


def _messages(caplog) -> list[str]:
    return [r.getMessage() for r in _records(caplog)]


class TestInitializePipeline:
    def test_a_successful_initialisation_is_logged_and_nothing_is_printed(
        self, caplog, capsys
    ):
        client = MagicMock()
        client.get_all_pipelines.return_value = [{}, {}]
        orchestrator = _orchestrator(client, initialized=False)
        # The pipeline and block checks report on their own; this test is
        # about the connectivity step and the summary around them.
        orchestrator._ensure_pipeline_exists = lambda: True
        orchestrator._ensure_block_exists = lambda: True
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)

        assert orchestrator._initialize_pipeline() is True

        assert capsys.readouterr().out == ""
        messages = _messages(caplog)
        assert "Connected to the Mage API, 2 existing pipelines" in messages
        assert "Pipeline infrastructure initialized" in messages

    def test_an_unexpected_error_in_the_connectivity_check_is_logged_with_its_traceback(
        self, caplog, capsys
    ):
        client = MagicMock()
        client.base_url = "http://mage.test"
        client.get_all_pipelines.side_effect = RuntimeError("unexpected")
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)

        assert _orchestrator(client, initialized=False)._initialize_pipeline() is False

        assert capsys.readouterr().out == ""
        errors = [r for r in _records(caplog) if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "http://mage.test" in errors[0].getMessage()
        assert errors[0].exc_info is not None
        assert errors[0].exc_info[0] is RuntimeError
        client.pipeline_exists.assert_not_called()

    def test_an_unreachable_orchestrator_is_re_raised_and_not_logged_here(self, caplog):
        client = MagicMock()
        client.get_all_pipelines.side_effect = OrchestratorUnavailable(
            "nobody answered"
        )
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)

        with pytest.raises(OrchestratorUnavailable):
            _orchestrator(client, initialized=False)._initialize_pipeline()

        assert not [r for r in _records(caplog) if r.levelno >= logging.ERROR]


class TestCreateScheduleTrigger:
    def test_the_trigger_is_logged_and_returned_as_before(self, caplog, capsys):
        client = MagicMock()
        client.create_schedule_trigger.return_value = {"id": 7}
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)

        result = _orchestrator(client).create_schedule_trigger_for_agent(
            "agent1",
            {"agent_name": "Mailer"},
            schedule_interval="@daily",
            start_time="2026-01-01 10:00:00",
        )

        assert result == {
            "schedule_id": 7,
            "agent_id": "agent1",
            "agent_name": "Mailer",
            "agent_meta": {"agent_name": "Mailer"},
            "schedule_interval": "@daily",
            "start_time": "2026-01-01 10:00:00",
            "schedule_type": "time",
            "status": "active",
        }
        assert capsys.readouterr().out == ""
        messages = _messages(caplog)
        assert any("Mailer" in m and "@daily" in m for m in messages)
        assert any("schedule ID: 7" in m for m in messages)
        assert "First run: 2026-01-01 10:00:00" in messages

    def test_a_trigger_without_a_start_time_logs_no_first_run(self, caplog):
        client = MagicMock()
        client.create_schedule_trigger.return_value = {"id": 8}
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)

        _orchestrator(client).create_schedule_trigger_for_agent("agent1", {})

        messages = _messages(caplog)
        assert any("schedule ID: 8" in m for m in messages)
        assert not any("first run" in m.lower() for m in messages)


class TestCreateApiTrigger:
    def test_an_existing_trigger_is_reused_and_its_token_is_not_logged(
        self, caplog, capsys
    ):
        client = MagicMock()
        client.get_pipeline_schedules.return_value = [
            {"name": "agent1", "id": 3, "token": TRIGGER_TOKEN}
        ]
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

        result = _orchestrator(client).create_agent_trigger(
            "agent1", {"agent_name": "Mailer"}
        )

        assert result["schedule_id"] == 3
        assert result["trigger_token"] == TRIGGER_TOKEN
        client.create_api_trigger.assert_not_called()
        assert capsys.readouterr().out == ""
        assert any("schedule ID: 3" in m for m in _messages(caplog))
        self._assert_token_not_logged(caplog)

    def test_a_new_trigger_is_created_and_its_token_is_not_logged(self, caplog, capsys):
        client = MagicMock()
        client.get_pipeline_schedules.return_value = []
        client.create_api_trigger.return_value = {"id": 4, "token": TRIGGER_TOKEN}
        caplog.set_level(logging.DEBUG, logger=LOGGER_NAME)

        result = _orchestrator(client).create_agent_trigger(
            "agent1", {"agent_name": "Mailer"}
        )

        assert result["schedule_id"] == 4
        assert result["trigger_token"] == TRIGGER_TOKEN
        assert capsys.readouterr().out == ""
        assert any("schedule ID: 4" in m for m in _messages(caplog))
        self._assert_token_not_logged(caplog)

    def test_a_failed_initial_run_is_a_warning(self, caplog, capsys):
        client = MagicMock()
        client.get_pipeline_schedules.return_value = [
            {"name": "agent1", "id": 3, "token": TRIGGER_TOKEN}
        ]
        client.trigger_pipeline.return_value = None
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)

        result = _orchestrator(client).create_agent_trigger(
            "agent1", {}, create_initial_run=True
        )

        assert result["status"] == "trigger_ready"
        assert capsys.readouterr().out == ""
        warnings = [
            r.getMessage() for r in _records(caplog) if r.levelno == logging.WARNING
        ]
        assert any("initial run" in m and "agent1" in m for m in warnings)

    def test_a_triggered_initial_run_records_its_id_and_status(self, caplog):
        client = MagicMock()
        client.get_pipeline_schedules.return_value = [
            {"name": "agent1", "id": 3, "token": TRIGGER_TOKEN}
        ]
        client.trigger_pipeline.return_value = {
            "pipeline_run": {"id": 11, "status": "running"}
        }
        caplog.set_level(logging.INFO, logger=LOGGER_NAME)

        result = _orchestrator(client).create_agent_trigger(
            "agent1", {}, create_initial_run=True
        )

        assert result["run_id"] == 11
        assert result["status"] == "running"
        assert any("run ID 11, status running" in m for m in _messages(caplog))

    @staticmethod
    def _assert_token_not_logged(caplog) -> None:
        for record in caplog.records:
            assert TRIGGER_TOKEN not in record.getMessage()
            assert TRIGGER_TOKEN not in str(record.args)
