"""Tests pour B19 — helper ``audit()`` pour les events sensibles.

Tout appel à ``audit(event, user_id=..., **details)`` doit :

- Émettre un log INFO sur le logger dédié ``apowerb.audit``.
- Contenir ``audit=True``, le nom de l'événement, l'``user_id``, et les
  détails supplémentaires via ``extra=``.
"""

from __future__ import annotations

import json
import io
import logging

import pytest

from apowerb.configs import th2logger
from apowerb.helpers.audit_log import audit, AUDIT_LOGGER_NAME


@pytest.fixture(autouse=True)
def _reset_logging():
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    root.handlers = original_handlers
    root.setLevel(original_level)


class TestAuditLog:
    def test_audit_emits_json_with_event_and_user(self):
        stream = io.StringIO()
        th2logger.configure_structured_logging(
            level=logging.INFO, fmt="json", stream=stream
        )
        audit("auth.login", user_id=7, ip="127.0.0.1")

        lines = [l for l in stream.getvalue().splitlines() if l.strip()]
        assert lines, "no log line emitted"
        payload = json.loads(lines[-1])
        assert payload["event"] == "auth.login"
        assert payload["user_id"] == 7
        assert payload["ip"] == "127.0.0.1"
        assert payload["audit"] is True
        assert payload["logger"] == AUDIT_LOGGER_NAME
