"""Tests for image generation tools (image_generation.py)."""

import base64
import os
import time
from unittest.mock import MagicMock, patch

import pytest

from th2agent.tools_store.portfolio.image_generation import (
    _sanitize_filename,
    _DALLE_SIZE_MAP,
    _STABILITY_AR_MAP,
    _PROVIDERS,
    _PROVIDER_ORDER,
    tool_generate_image,
)


# ---------------------------------------------------------------------------
# _sanitize_filename
# ---------------------------------------------------------------------------


class TestSanitizeFilename:
    def test_basic(self):
        assert _sanitize_filename("A cute cat") == "a_cute_cat"

    def test_special_chars_stripped(self):
        assert _sanitize_filename("hello!@#world") == "helloworld"

    def test_spaces_become_underscore(self):
        assert _sanitize_filename("hello  world") == "hello_world"

    def test_max_length(self):
        result = _sanitize_filename("a" * 100, max_len=10)
        assert len(result) == 10

    def test_empty_string_fallback(self):
        assert _sanitize_filename("!!!") == "generated"

    def test_dashes_and_underscores(self):
        result = _sanitize_filename("my-cool_image--test")
        assert result == "my_cool_image_test"


# ---------------------------------------------------------------------------
# Provider registry sanity
# ---------------------------------------------------------------------------


class TestProviderRegistry:
    def test_all_providers_in_registry(self):
        assert "gemini" in _PROVIDERS
        assert "openai" in _PROVIDERS
        assert "stability" in _PROVIDERS

    def test_provider_order(self):
        assert _PROVIDER_ORDER == ["gemini", "openai", "stability"]

    def test_provider_tuple_structure(self):
        for key, (name, fn, env_key) in _PROVIDERS.items():
            assert isinstance(name, str)
            assert callable(fn)
            assert isinstance(env_key, str)


# ---------------------------------------------------------------------------
# Aspect ratio maps
# ---------------------------------------------------------------------------


class TestAspectRatioMaps:
    def test_dalle_sizes(self):
        assert _DALLE_SIZE_MAP["1:1"] == "1024x1024"
        assert _DALLE_SIZE_MAP["16:9"] == "1792x1024"
        assert _DALLE_SIZE_MAP["9:16"] == "1024x1792"

    def test_stability_sizes(self):
        assert _STABILITY_AR_MAP["1:1"] == (1024, 1024)
        assert _STABILITY_AR_MAP["16:9"] == (1344, 768)
        assert _STABILITY_AR_MAP["9:16"] == (768, 1344)


# ---------------------------------------------------------------------------
# tool_generate_image: validation
# ---------------------------------------------------------------------------


class TestGenerateImageValidation:
    def test_invalid_aspect_ratio(self):
        result = tool_generate_image("a cat", aspect_ratio="2:1")
        assert result["status"] == "error"
        assert "aspect_ratio" in result["error_message"].lower()

    def test_invalid_provider(self):
        result = tool_generate_image("a cat", provider="midjourney")
        assert result["status"] == "error"
        assert "unknown provider" in result["error_message"].lower()


# ---------------------------------------------------------------------------
# tool_generate_image: no API keys
# ---------------------------------------------------------------------------


class TestGenerateImageNoKeys:
    def test_auto_fails_when_no_keys_set(self):
        """With no API keys, auto mode should try all and fail."""
        env_patch = {
            "GEMINI_API_KEY": "",
            "OPENAI_API_KEY": "",
            "STABILITY_API_KEY": "",
        }
        with patch.dict(os.environ, env_patch, clear=False):
            result = tool_generate_image("a cat")
        assert result["status"] == "error"
        assert "all providers" in result["error_message"].lower()

    def test_specific_provider_missing_key(self):
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=False):
            result = tool_generate_image("a cat", provider="gemini")
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# tool_generate_image: mocked provider success
# ---------------------------------------------------------------------------


def _fake_image_bytes():
    """Generate a tiny valid PNG for mock returns."""
    from PIL import Image as PILImage
    import io

    img = PILImage.new("RGB", (64, 64), color="green")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _patch_provider(name, mock_fn):
    """Patch a provider's gen_fn inside _PROVIDERS (dict holds original refs)."""
    import th2agent.tools_store.portfolio.image_generation as mod

    original = mod._PROVIDERS[name]
    mod._PROVIDERS[name] = (original[0], mock_fn, original[2])
    return original


class TestGenerateImageWithMock:
    """Tests with mocked providers. We chdir to tmp_path so ./uploads/ is created there."""

    def test_gemini_success(self, tmp_path, monkeypatch):
        mock_fn = MagicMock(return_value=(_fake_image_bytes(), "image/png"))
        original = _patch_provider("gemini", mock_fn)
        try:
            monkeypatch.chdir(tmp_path)
            monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
            monkeypatch.setenv("ROOT_AGENT_ID", "test123")

            result = tool_generate_image("a green square", provider="gemini")

            assert result["status"] == "success"
            assert result["provider_used"] == "Gemini Imagen"
            assert result["base64_data"]
            assert result["image_format"] in ("PNG", "JPEG")
            assert result["aspect_ratio"] == "1:1"
            mock_fn.assert_called_once()
        finally:
            _patch_provider("gemini", original[1])

    def test_openai_success(self, tmp_path, monkeypatch):
        mock_fn = MagicMock(return_value=(_fake_image_bytes(), "image/png"))
        original = _patch_provider("openai", mock_fn)
        try:
            monkeypatch.chdir(tmp_path)
            monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
            monkeypatch.setenv("GEMINI_API_KEY", "")
            monkeypatch.setenv("ROOT_AGENT_ID", "test456")

            result = tool_generate_image("a dog", provider="openai")

            assert result["status"] == "success"
            assert result["provider_used"] == "OpenAI DALL-E 3"
            mock_fn.assert_called_once()
        finally:
            _patch_provider("openai", original[1])

    def test_auto_fallback_to_openai(self, tmp_path, monkeypatch):
        """If Gemini fails, auto should fall back to OpenAI."""
        mock_gemini = MagicMock(side_effect=RuntimeError("Gemini quota exceeded"))
        mock_openai = MagicMock(return_value=(_fake_image_bytes(), "image/png"))
        orig_g = _patch_provider("gemini", mock_gemini)
        orig_o = _patch_provider("openai", mock_openai)
        try:
            monkeypatch.chdir(tmp_path)
            monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
            monkeypatch.setenv("OPENAI_API_KEY", "fake-key")
            monkeypatch.setenv("ROOT_AGENT_ID", "test789")

            result = tool_generate_image("a mountain", provider="auto")

            assert result["status"] == "success"
            assert result["provider_used"] == "OpenAI DALL-E 3"
            mock_gemini.assert_called_once()
            mock_openai.assert_called_once()
        finally:
            _patch_provider("gemini", orig_g[1])
            _patch_provider("openai", orig_o[1])

    def test_style_prefix(self):
        """Verify that style is included in the error context (no key = no call)."""
        with patch.dict(os.environ, {
            "GEMINI_API_KEY": "",
            "OPENAI_API_KEY": "",
            "STABILITY_API_KEY": "",
        }):
            result = tool_generate_image("a cat", style="watercolor")
        assert result["status"] == "error"

    def test_file_saved_to_disk(self, tmp_path, monkeypatch):
        mock_fn = MagicMock(return_value=(_fake_image_bytes(), "image/png"))
        original = _patch_provider("gemini", mock_fn)
        try:
            monkeypatch.chdir(tmp_path)
            monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
            monkeypatch.setenv("ROOT_AGENT_ID", "TEST")

            result = tool_generate_image("a test", provider="gemini")

            assert result["status"] == "success"
            assert result["file_name"].startswith("gen_")
            assert result["file_name"].endswith(".png")
            assert result["size_kb"] > 0
            # Verify file was actually written
            saved = list((tmp_path / "uploads" / "agentTEST").glob("gen_*"))
            assert len(saved) == 1
        finally:
            _patch_provider("gemini", original[1])
