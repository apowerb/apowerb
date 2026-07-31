"""Tests for tool_convert_image_to_base64 (basic.py)."""

import base64
import io
import os
import tempfile

import pytest
from PIL import Image as PILImage

from apowerb.tools_store.portfolio.basic import tool_convert_image_to_base64


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def small_png(tmp_path):
    """Create a small 100x100 PNG image."""
    img = PILImage.new("RGB", (100, 100), color="red")
    path = tmp_path / "small.png"
    img.save(path, format="PNG")
    return str(path)


@pytest.fixture
def large_png(tmp_path):
    """Create a 3000x2000 PNG that exceeds the default 1600px limit."""
    img = PILImage.new("RGB", (3000, 2000), color="blue")
    path = tmp_path / "large.png"
    img.save(path, format="PNG")
    return str(path)


@pytest.fixture
def rgba_png(tmp_path):
    """Create an RGBA PNG with transparency."""
    img = PILImage.new("RGBA", (200, 200), color=(0, 255, 0, 128))
    path = tmp_path / "transparent.png"
    img.save(path, format="PNG")
    return str(path)


@pytest.fixture
def rgba_jpg(tmp_path):
    """Create an RGBA image saved as .jpg (needs RGBA→RGB conversion)."""
    img = PILImage.new("RGBA", (200, 200), color=(255, 0, 0, 128))
    # Save as PNG first, then rename to .jpg to test the conversion path
    png_path = tmp_path / "temp.png"
    img.save(png_path, format="PNG")
    jpg_path = tmp_path / "photo.jpg"
    png_path.rename(jpg_path)
    return str(jpg_path)


@pytest.fixture
def webp_image(tmp_path):
    """Create a small WEBP image."""
    img = PILImage.new("RGB", (150, 150), color="yellow")
    path = tmp_path / "image.webp"
    img.save(path, format="WEBP")
    return str(path)


# ---------------------------------------------------------------------------
# Tests: Success cases
# ---------------------------------------------------------------------------


class TestConvertSuccess:
    """Happy-path conversion tests."""

    def test_small_png_returns_success(self, small_png):
        result = tool_convert_image_to_base64(small_png)
        assert result["status"] == "success"
        assert result["base64_data"]
        assert result["data_uri"].startswith("data:image/")
        assert result["file_name"] == "small.png"
        assert result["was_resized"] is False

    def test_base64_is_valid(self, small_png):
        result = tool_convert_image_to_base64(small_png)
        decoded = base64.b64decode(result["base64_data"])
        assert len(decoded) > 0
        # Should be a valid image
        img = PILImage.open(io.BytesIO(decoded))
        assert img.size[0] > 0

    def test_webp_format(self, webp_image):
        result = tool_convert_image_to_base64(webp_image)
        assert result["status"] == "success"
        assert result["file_name"] == "image.webp"

    def test_rgba_png_stays_png(self, rgba_png):
        """RGBA images should be saved as PNG to preserve transparency."""
        result = tool_convert_image_to_base64(rgba_png)
        assert result["status"] == "success"
        assert result["image_format"] == "PNG"
        assert "image/png" in result["data_uri"]


# ---------------------------------------------------------------------------
# Tests: Resize
# ---------------------------------------------------------------------------


class TestResize:
    """Tests for automatic image resizing."""

    def test_large_image_is_resized(self, large_png):
        result = tool_convert_image_to_base64(large_png)
        assert result["status"] == "success"
        assert result["was_resized"] is True
        # Verify dimensions are within max_dimension
        w, h = result["final_dimensions"].split("x")
        assert int(w) <= 1600
        assert int(h) <= 1600

    def test_original_dimensions_recorded(self, large_png):
        result = tool_convert_image_to_base64(large_png)
        assert result["original_dimensions"] == "3000x2000"

    def test_small_image_not_resized(self, small_png):
        result = tool_convert_image_to_base64(small_png)
        assert result["was_resized"] is False
        assert result["original_dimensions"] == "100x100"

    def test_custom_max_dimension(self, large_png):
        result = tool_convert_image_to_base64(large_png, max_dimension=800)
        assert result["was_resized"] is True
        w, h = result["final_dimensions"].split("x")
        assert int(w) <= 800
        assert int(h) <= 800

    def test_aspect_ratio_preserved(self, large_png):
        """3000x2000 (3:2 ratio) should remain ~3:2 after resize."""
        result = tool_convert_image_to_base64(large_png)
        w, h = map(int, result["final_dimensions"].split("x"))
        ratio = w / h
        assert abs(ratio - 1.5) < 0.05  # 3:2 = 1.5


# ---------------------------------------------------------------------------
# Tests: Error cases
# ---------------------------------------------------------------------------


class TestConvertErrors:
    """Error handling tests."""

    def test_file_not_found(self):
        result = tool_convert_image_to_base64("/nonexistent/image.png")
        assert result["status"] == "error"
        assert "not found" in result["error_message"].lower()

    def test_unsupported_format(self, tmp_path):
        # Create a fake .svg file
        svg_path = tmp_path / "image.svg"
        svg_path.write_text("<svg></svg>")
        result = tool_convert_image_to_base64(str(svg_path))
        assert result["status"] == "error"
        assert "unsupported" in result["error_message"].lower()

    def test_unsupported_format_txt(self, tmp_path):
        txt_path = tmp_path / "notes.txt"
        txt_path.write_text("not an image")
        result = tool_convert_image_to_base64(str(txt_path))
        assert result["status"] == "error"

    def test_corrupt_image_file(self, tmp_path):
        path = tmp_path / "corrupt.png"
        path.write_bytes(b"not a real png file content")
        result = tool_convert_image_to_base64(str(path))
        # Should return error (Pillow can't open it)
        assert result["status"] == "error"


# ---------------------------------------------------------------------------
# Tests: Size metadata
# ---------------------------------------------------------------------------


class TestSizeMetadata:
    """Verify size-related fields in the response."""

    def test_original_size_kb(self, small_png):
        result = tool_convert_image_to_base64(small_png)
        assert "original_size_kb" in result
        assert result["original_size_kb"] > 0

    def test_final_size_kb(self, small_png):
        result = tool_convert_image_to_base64(small_png)
        assert "final_size_kb" in result
        assert result["final_size_kb"] > 0

    def test_resized_image_smaller(self, large_png):
        result = tool_convert_image_to_base64(large_png)
        assert result["final_size_kb"] < result["original_size_kb"]
