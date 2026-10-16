"""Tests for the --version flag on the root CLI command."""

from typer.testing import CliRunner
from apowerb.cli.main import app


def test_version_flag_exits_zero():
    """apowerb --version must print a version and exit with code 0."""
    runner = CliRunner()
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0


def test_version_flag_prints_something():
    """The output must not be empty."""
    runner = CliRunner()
    result = runner.invoke(app, ["--version"])
    assert result.output.strip() != ""