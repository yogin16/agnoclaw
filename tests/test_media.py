"""Tests for the media toolkit."""


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


# ── PDF reader backend tests (mocked) ──────────────────────────────────

from unittest.mock import patch, MagicMock


def test_read_pdf_pymupdf_success(media_toolkit, tmp_path):
    """Test successful PDF reading via PyMuPDF (mocked)."""
    pdf_path = tmp_path / "test.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    mock_page = MagicMock()
    mock_page.get_text.return_value = "  Hello from page 1  "

    mock_doc = MagicMock()
    mock_doc.__len__ = MagicMock(return_value=1)
    mock_doc.__getitem__ = MagicMock(return_value=mock_page)
    mock_doc.__iter__ = MagicMock(return_value=iter([mock_page]))

    mock_fitz = MagicMock()
    mock_fitz.open.return_value = mock_doc

    with patch.dict("sys.modules", {"fitz": mock_fitz}):
        result = MediaToolkit._read_pdf_pymupdf(pdf_path, None)
    assert "Hello from page 1" in result
    assert "1 pages" in result


def test_read_pdf_pymupdf_with_pages(media_toolkit, tmp_path):
    """Test PyMuPDF reading with specific page indices."""
    pdf_path = tmp_path / "test.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    pages = [MagicMock(), MagicMock(), MagicMock()]
    pages[0].get_text.return_value = "Page 1 text"
    pages[1].get_text.return_value = "Page 2 text"
    pages[2].get_text.return_value = "Page 3 text"

    mock_doc = MagicMock()
    mock_doc.__len__ = MagicMock(return_value=3)
    mock_doc.__getitem__ = MagicMock(side_effect=lambda i: pages[i])

    mock_fitz = MagicMock()
    mock_fitz.open.return_value = mock_doc

    with patch.dict("sys.modules", {"fitz": mock_fitz}):
        result = MediaToolkit._read_pdf_pymupdf(pdf_path, [0, 2])  # pages 1 and 3
    assert "Page 1 text" in result
    assert "Page 3 text" in result
    assert "Page 2 text" not in result


def test_read_pdf_pymupdf_import_error_falls_through(media_toolkit, tmp_path):
    """When pymupdf not installed, read_pdf falls through to pypdf."""
    pdf_path = tmp_path / "test.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    with patch.object(MediaToolkit, "_read_pdf_pymupdf", side_effect=ImportError):
        with patch.object(MediaToolkit, "_read_pdf_pypdf", side_effect=ImportError):
            result = media_toolkit.read_pdf(str(pdf_path))
    assert "[error]" in result
    assert "No PDF reader" in result


def test_read_pdf_pymupdf_exception_returns_error(media_toolkit, tmp_path):
    """When pymupdf raises an exception, read_pdf returns error."""
    pdf_path = tmp_path / "test.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    with patch.object(MediaToolkit, "_read_pdf_pymupdf", side_effect=RuntimeError("corrupt")):
        result = media_toolkit.read_pdf(str(pdf_path))
    assert "[error]" in result
    assert "pymupdf" in result


def test_read_pdf_pypdf_fallback_success(media_toolkit, tmp_path):
    """Test pypdf fallback when pymupdf is not available."""
    pdf_path = tmp_path / "test.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    mock_page = MagicMock()
    mock_page.extract_text.return_value = "  Fallback page text  "

    mock_reader = MagicMock()
    mock_reader.pages = [mock_page]
    mock_reader.__len__ = MagicMock(return_value=1)

    with patch.object(MediaToolkit, "_read_pdf_pymupdf", side_effect=ImportError):
        with patch("agnoclaw.tools.media.MediaToolkit._read_pdf_pypdf") as mock_pypdf:
            mock_pypdf.return_value = "PDF: test.pdf (1 pages)\n\n--- Page 1 ---\nFallback page text"
            result = media_toolkit.read_pdf(str(pdf_path))
    assert "Fallback page text" in result


def test_read_pdf_pypdf_exception_returns_error(media_toolkit, tmp_path):
    """When pypdf raises an exception, read_pdf returns error."""
    pdf_path = tmp_path / "test.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    with patch.object(MediaToolkit, "_read_pdf_pymupdf", side_effect=ImportError):
        with patch.object(MediaToolkit, "_read_pdf_pypdf", side_effect=RuntimeError("bad file")):
            result = media_toolkit.read_pdf(str(pdf_path))
    assert "[error]" in result
    assert "pypdf" in result


def test_read_pdf_with_page_range(media_toolkit, tmp_path):
    """Test read_pdf passes page_indices when pages param given."""
    pdf_path = tmp_path / "test.pdf"
    pdf_path.write_bytes(b"%PDF-1.4 fake")

    with patch.object(MediaToolkit, "_read_pdf_pymupdf", return_value="PDF content") as mock_mupdf:
        result = media_toolkit.read_pdf(str(pdf_path), pages="1-3")
    assert result == "PDF content"
    # Verify page_indices were passed
    mock_mupdf.assert_called_once_with(pdf_path.resolve(), [0, 1, 2])


def test_read_image_exception_handling(media_toolkit, tmp_path):
    """read_image returns error when file read fails."""
    from pathlib import Path as _Path

    img_path = tmp_path / "test.png"
    img_path.write_bytes(b"\x89PNG fake")

    with patch.object(_Path, "read_bytes", side_effect=PermissionError("denied")):
        result = media_toolkit.read_image(str(img_path))
    assert "[error]" in result


def test_read_image_unknown_extension(media_toolkit, tmp_path):
    """Unknown image extension gets application/octet-stream MIME type."""
    img_path = tmp_path / "test.tiff"
    img_path.write_bytes(b"fake tiff data")

    result = media_toolkit.read_image(str(img_path))
    assert "application/octet-stream" in result
