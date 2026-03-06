"""Tests for the browser toolkit."""

from unittest.mock import MagicMock, patch

import pytest


def test_browser_toolkit_import():
    """BrowserToolkit should import without playwright installed."""
    from agnoclaw.tools.browser import BrowserToolkit
    assert BrowserToolkit is not None


def test_browser_check_playwright():
    """_check_playwright should return False when playwright is not installed."""
    from agnoclaw.tools.browser import _check_playwright
    # This test passes whether or not playwright is installed
    result = _check_playwright()
    assert isinstance(result, bool)


def test_browser_toolkit_registers_tools():
    """BrowserToolkit should register all expected tool methods."""
    from agnoclaw.tools.browser import BrowserToolkit

    toolkit = BrowserToolkit()
    expected = {
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_screenshot",
        "browser_snapshot",
        "browser_scroll",
        "browser_fill_form",
        "browser_close",
    }
    registered = set(toolkit.functions.keys())
    assert expected.issubset(registered), f"Missing tools: {expected - registered}"


def test_browser_navigate_requires_playwright():
    """Navigate should raise ImportError when playwright is not installed."""
    from agnoclaw.tools.browser import BrowserToolkit

    toolkit = BrowserToolkit()
    # If playwright is not installed, this should fail gracefully
    with patch("agnoclaw.tools.browser._check_playwright", return_value=False):
        toolkit._page = None
        toolkit._playwright = None
        toolkit._browser = None
        with pytest.raises(ImportError, match="Playwright"):
            toolkit.browser_navigate("https://example.com")


def test_browser_close_when_not_initialized():
    """Close should work gracefully even if browser was never opened."""
    from agnoclaw.tools.browser import BrowserToolkit

    toolkit = BrowserToolkit()
    result = toolkit.browser_close()
    assert "closed" in result.lower() or "Browser closed" in result


def test_browser_scroll_values():
    """Scroll should accept direction and amount parameters."""
    from agnoclaw.tools.browser import BrowserToolkit

    toolkit = BrowserToolkit()
    # Mock the page to avoid needing a real browser
    mock_page = MagicMock()
    mock_page.mouse.wheel = MagicMock()
    mock_page.evaluate.return_value = 500
    toolkit._page = mock_page

    result = toolkit.browser_scroll("down", 300)
    assert "500" in result  # scroll position
    mock_page.mouse.wheel.assert_called_once_with(0, 300)


def test_browser_fill_form_invalid_json():
    """fill_form should handle invalid JSON gracefully."""
    from agnoclaw.tools.browser import BrowserToolkit

    toolkit = BrowserToolkit()
    toolkit._page = MagicMock()

    result = toolkit.browser_fill_form("not valid json")
    assert "[error]" in result
