"""Tests for APOWERB_HOST / APOWERB_PORT environment variable defaults."""

from typer.testing import CliRunner
from apowerb.cli.main import app


def test_host_env_var_sets_default(monkeypatch):
    """APOWERB_HOST doit apparaître dans le help."""
    monkeypatch.setenv("APOWERB_HOST", "0.0.0.0")
    runner = CliRunner()
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "APOWERB_HOST" in result.output


def test_port_env_var_sets_default(monkeypatch):
    """APOWERB_PORT doit apparaître dans le help."""
    monkeypatch.setenv("APOWERB_PORT", "9000")
    runner = CliRunner()
    result = runner.invoke(app, ["serve", "--help"])
    assert result.exit_code == 0
    assert "APOWERB_PORT" in result.output