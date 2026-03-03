"""Tests for the media toolkit."""

from pathlib import Path

import pytest

from agnoclaw.tools.media import MediaToolkit


@pytest.fixture
def media_toolkit():
    return MediaToolkit()


def test_media_toolkit_registers_tools(media_toolkit):
    expected = {"read_image", "read_pdf"}
    registered = set(media_toolkit.functions.keys())
    assert expected.issubset(registered)


def test_read_image_not_found(media_toolkit):
    result = media_toolkit.read_image("/nonexistent/path/image.png")
    assert "[error]" in result


def test_read_image_valid(media_toolkit, tmp_path):
    """Read a simple PNG file (1x1 pixel)."""
    # Minimal valid PNG
    png_header = (
        b"\x89PNG\r\n\x1a\n"  # PNG signature
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90wS\xde"
        b"\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05"
        b"\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    img_path = tmp_path / "test.png"
    img_path.write_bytes(png_header)

    result = media_toolkit.read_image(str(img_path))
    assert "data:image/png;base64," in result
    assert "test.png" in result


def test_read_image_jpeg_mime(media_toolkit, tmp_path):
    """JPEG files should get correct MIME type."""
    img_path = tmp_path / "test.jpg"
    img_path.write_bytes(b"\xff\xd8\xff\xe0test")

    result = media_toolkit.read_image(str(img_path))
    assert "image/jpeg" in result


def test_read_pdf_not_found(media_toolkit):
    result = media_toolkit.read_pdf("/nonexistent/path/doc.pdf")
    assert "[error]" in result


def test_parse_page_range():
    """Test page range parsing."""
    result = MediaToolkit._parse_page_range("1-5")
    assert result == [0, 1, 2, 3, 4]

    result = MediaToolkit._parse_page_range("3")
    assert result == [2]

    result = MediaToolkit._parse_page_range("1,3,5")
    assert result == [0, 2, 4]

    result = MediaToolkit._parse_page_range("1-3,5")
    assert result == [0, 1, 2, 4]


def test_read_pdf_no_reader(media_toolkit, tmp_path):
    """Should give helpful error when no PDF reader is installed."""
    pdf_path = tmp_path / "test.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake content")

    # This will try pymupdf then pypdf — both may or may not be installed
    result = media_toolkit.read_pdf(str(pdf_path))
    # Should either succeed or give a helpful error
    assert "PDF" in result or "[error]" in result
