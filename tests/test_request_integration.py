"""Tests for the request_integration native tool."""
from __future__ import annotations

import pytest

from th2agent.core.agent_helpers.agent_utils import (
    VALID_INTEGRATION_PROVIDERS,
    request_integration,
)


class TestRequestIntegrationValidProviders:
    """VALID_INTEGRATION_PROVIDERS must contain the expected set."""

    def test_contains_google_providers(self) -> None:
        google = {"google_drive", "google_gmail", "google_calendar", "google_sheets", "google_docs"}
        assert google.issubset(VALID_INTEGRATION_PROVIDERS)

    def test_contains_microsoft_providers(self) -> None:
        ms = {"microsoft_outlook", "microsoft_teams", "microsoft_onedrive", "microsoft_sharepoint"}
        assert ms.issubset(VALID_INTEGRATION_PROVIDERS)

    def test_contains_github(self) -> None:
        assert "github" in VALID_INTEGRATION_PROVIDERS

    def test_contains_odoo(self) -> None:
        assert "odoo" in VALID_INTEGRATION_PROVIDERS

    def test_total_count(self) -> None:
        assert len(VALID_INTEGRATION_PROVIDERS) == 11


class TestRequestIntegrationFunction:
    """request_integration returns appropriate dicts."""

    def test_valid_provider_returns_integration_required(self) -> None:
        result = request_integration("google_drive", reason="Need to read files")
        assert result["status"] == "integration_required"
        assert result["provider"] == "google_drive"
        assert result["reason"] == "Need to read files"
        assert result["_integration_request"] is True

    def test_valid_provider_without_reason(self) -> None:
        result = request_integration("github")
        assert result["status"] == "integration_required"
        assert result["provider"] == "github"
        assert result["reason"] == ""

    def test_invalid_provider_returns_error(self) -> None:
        result = request_integration("slack")
        assert result["status"] == "error"
        assert "Unknown provider" in result["message"]
        assert "slack" in result["message"]

    def test_invalid_provider_lists_valid_ones(self) -> None:
        result = request_integration("notion")
        for p in sorted(VALID_INTEGRATION_PROVIDERS):
            assert p in result["message"]

    def test_integration_request_marker_absent_on_error(self) -> None:
        result = request_integration("invalid_thing")
        assert "_integration_request" not in result

    @pytest.mark.parametrize("provider", sorted(VALID_INTEGRATION_PROVIDERS) if hasattr(VALID_INTEGRATION_PROVIDERS, '__iter__') else [])
    def test_all_valid_providers_succeed(self, provider: str) -> None:
        result = request_integration(provider, reason="test")
        assert result["status"] == "integration_required"
        assert result["provider"] == provider
