"""Tests pour B19 — structured logging JSON.

``configure_structured_logging()`` installe un handler qui émet des
lignes JSON parseables sur stdout/stderr. Les champs minimums présents
à chaque émission : ``timestamp``, ``level``, ``logger``, ``message``.
Les extras (``user_id``, ``event``, ``request_id``) sont propagés via
``logger.info("msg", extra={...})`` ou via ``bind_context(...)``.

En dev (``TH2_LOG_FORMAT=text``), la sortie doit rester lisible humain
— on vérifie simplement qu'aucune exception n'est levée.
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from apowerb.configs import th2logger


@pytest.fixture(autouse=True)
def _reset_logging():
    # Capture root logger state and restore after test.
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    root.handlers = original_handlers
    root.setLevel(original_level)


class TestStructuredLogging:
    def test_json_output_contains_required_fields(self):
        stream = io.StringIO()
        th2logger.configure_structured_logging(
            level=logging.INFO, fmt="json", stream=stream
        )
        logger = logging.getLogger("test.structured")
        logger.info("hello world", extra={"user_id": 42, "event": "ping"})

        raw = stream.getvalue().strip().splitlines()[-1]
        payload = json.loads(raw)
        assert payload["message"] == "hello world"
        assert payload["level"] == "INFO"
        assert payload["logger"] == "test.structured"
        assert "timestamp" in payload
        assert payload["user_id"] == 42
        assert payload["event"] == "ping"

    def test_text_output_does_not_error_in_dev_mode(self):
        stream = io.StringIO()
        th2logger.configure_structured_logging(
            level=logging.INFO, fmt="text", stream=stream
        )
        logger = logging.getLogger("test.text")
        logger.info("dev-friendly")
        out = stream.getvalue()
        assert "dev-friendly" in out
        # Text mode must NOT produce JSON on the happy path.
        assert not out.strip().startswith("{")

    def test_setup_logging_backward_compat_returns_logger(self):
        # Existing call-sites rely on ``setup_logging(__name__)`` returning
        # a ``logging.Logger`` object. Do not regress that contract.
        logger = th2logger.setup_logging("test.compat")
        assert hasattr(logger, "info")
        assert logger.name == "test.compat"
