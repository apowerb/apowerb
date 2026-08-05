"""A failed decryption must not put the API key row anywhere near a log line.

Both call sites used to build their warning from `d['api_key_id']`, reading a
field back out of the same dict that carries `api_key_value`. The id itself is
not a secret, but the log line is one careless edit away from the value, and
the row is the wrong place to read it from when a safe source is in scope.

What is pinned here is the property, not the wording: the decrypted secret
never reaches the log, and the operator still learns which key (or how many)
failed.
"""
from __future__ import annotations

import logging

import pytest

from apowerb.core import api_key_main

_REAL_STORE = api_key_main.api_key_store


SECRET = "sk-live-DO-NOT-LOG-THIS"


class _StubStore:
    """Stands in for the module-level ApiKeyStore.

    Carries the table attribute the queries are built from, untouched, so the
    functions under test still build their real SELECT.
    """

    def __init__(self, rows):
        self._rows = rows
        self.api_key_table = _REAL_STORE.api_key_table

    def get_list(self, query):
        return self._rows


class _Row:
    def __init__(self, d):
        self._d = d

    def _asdict(self):
        return dict(self._d)


@pytest.fixture()
def rows(monkeypatch):
    """Two keys for one owner; decryption always blows up."""

    def _install(n):
        made = [
            _Row({"api_key_id": i, "owner_id": "u1", "api_key_value": SECRET, "status": "active"})
            for i in range(1, n + 1)
        ]
        # ApiKeyStore is a pydantic model, so it refuses a grafted attribute.
        # Swap the whole module-level instance for a stub instead.
        monkeypatch.setattr(api_key_main, "api_key_store", _StubStore(made))
        monkeypatch.setattr(
            api_key_main,
            "decrypt_value_in_dict",
            lambda d, fields: (_ for _ in ()).throw(ValueError("bad key")),
        )
        return made

    return _install


def test_list_never_logs_the_secret(rows, caplog):
    rows(2)
    with caplog.at_level(logging.WARNING):
        keys = api_key_main.list_user_api_keys("u1")

    assert SECRET not in caplog.text
    assert all(k["api_key_value"] == "" for k in keys)


def test_list_reports_how_many_failed_and_for_whom(rows, caplog):
    """Support's actual question is "one key or all of them?"."""
    rows(3)
    with caplog.at_level(logging.WARNING):
        api_key_main.list_user_api_keys("u1")

    assert "3" in caplog.text
    assert "u1" in caplog.text


def test_list_stays_quiet_when_everything_decrypts(monkeypatch, caplog):
    made = [_Row({"api_key_id": 1, "owner_id": "u1", "api_key_value": SECRET, "status": "active"})]
    monkeypatch.setattr(api_key_main, "api_key_store", _StubStore(made))
    monkeypatch.setattr(api_key_main, "decrypt_value_in_dict", lambda d, fields: d)

    with caplog.at_level(logging.WARNING):
        api_key_main.list_user_api_keys("u1")

    assert "Failed to decrypt" not in caplog.text


def test_get_never_logs_the_secret(rows, caplog, monkeypatch):
    rows(1)
    monkeypatch.setattr(api_key_main, "_parse_api_key_id", lambda v: 1)

    with caplog.at_level(logging.WARNING):
        got = api_key_main.get_api_key("apikey1", "u1")

    assert SECRET not in caplog.text
    assert got["api_key_value"] == ""


def test_get_names_the_key_from_the_caller_parameter(rows, caplog, monkeypatch):
    """The id the caller asked for is a safe source; the row is not."""
    rows(1)
    monkeypatch.setattr(api_key_main, "_parse_api_key_id", lambda v: 1)

    with caplog.at_level(logging.WARNING):
        api_key_main.get_api_key("apikey1", "u1")

    assert "apikey1" in caplog.text
