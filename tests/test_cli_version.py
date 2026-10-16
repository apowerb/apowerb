import importlib.metadata
from unittest.mock import patch
from typer.testing import CliRunner

from apowerb.cli.main import app

runner = CliRunner()


def test_cli_version_flag():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    installed_version = importlib.metadata.version("apowerb")
    assert installed_version in result.output.strip()


def test_cli_version_flag_mocked():
    with patch("importlib.metadata.version", return_value="1.2.3.test"):
        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert "1.2.3.test" in result.output
