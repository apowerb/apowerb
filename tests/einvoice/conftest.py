"""Shared fixture for the einvoice tests (synthetic data only)."""

import pytest


@pytest.fixture
def agent_dir(monkeypatch, tmp_path):
    """Dossier d'uploads de l'agent racine 42, stockage local (comme tests/test_ocr_tool.py)."""
    from apowerb.artifacts import agent_uploads

    monkeypatch.setenv("ROOT_AGENT_ID", "42")
    monkeypatch.setattr(agent_uploads, "is_s3_storage", lambda: False)
    monkeypatch.setattr("apowerb.configs.paths.uploads_dir", lambda: tmp_path)
    folder = tmp_path / "agent42"
    folder.mkdir()
    return folder
