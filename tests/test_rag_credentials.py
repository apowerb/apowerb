"""RAG credentials come from the environment, and the password is never guessed.

Until 0.1.22 the service-account address was a literal in ``rag.py`` and was
also used as its own password. Published in a public repository, that is a
working credential for the RAG service, handed to every reader.

The rule these tests hold: an install that has not configured credentials must
fail loudly, never fall back to something derived.
"""

import pytest

from apowerb.tools_store.portfolio import rag

ENV_VARS = ("th2username", "th2password", "RAG_SERVICE_ACCOUNT_EMAIL")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_unconfigured_install_refuses_to_log_in():
    with pytest.raises(RuntimeError) as excinfo:
        rag._credentials()

    message = str(excinfo.value)
    # The message has to name what to set — an operator reads this in a log.
    assert "th2password" in message
    assert "RAG_SERVICE_ACCOUNT_EMAIL" in message


def test_explicit_username_and_password_are_used(monkeypatch):
    monkeypatch.setenv("th2username", "someone@example.com")
    monkeypatch.setenv("th2password", "a-real-password")

    assert rag._credentials() == ("someone@example.com", "a-real-password")


def test_service_account_email_supplies_the_username(monkeypatch):
    monkeypatch.setenv("RAG_SERVICE_ACCOUNT_EMAIL", "service@example.com")
    monkeypatch.setenv("th2password", "a-real-password")

    assert rag._credentials() == ("service@example.com", "a-real-password")


def test_th2username_wins_over_the_service_account_email(monkeypatch):
    monkeypatch.setenv("RAG_SERVICE_ACCOUNT_EMAIL", "service@example.com")
    monkeypatch.setenv("th2username", "someone@example.com")
    monkeypatch.setenv("th2password", "a-real-password")

    username, _ = rag._credentials()
    assert username == "someone@example.com"


def test_an_address_alone_is_not_a_credential(monkeypatch):
    """The exact shape of the old bug: an email, and no password anywhere."""
    monkeypatch.setenv("RAG_SERVICE_ACCOUNT_EMAIL", "service@example.com")

    with pytest.raises(RuntimeError):
        rag._credentials()


def test_the_password_is_never_the_username(monkeypatch):
    """Whatever the configuration, the two must not be silently equal."""
    monkeypatch.setenv("th2username", "service@example.com")

    with pytest.raises(RuntimeError):
        rag._credentials()


def test_no_credential_literal_survives_in_the_module():
    source = __import__("pathlib").Path(rag.__file__).read_text(encoding="utf-8")
    assert "th2agent-service@" not in source
