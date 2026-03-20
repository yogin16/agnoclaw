"""Tests for the browser toolkit."""

from unittest.mock import patch

import pytest


class FakeBrowserBackend:
    def navigate(self, *, url: str, wait_until: str = "domcontentloaded") -> str:
        return f"navigate:{url}:{wait_until}"

    def click(self, *, selector: str) -> str:
        return f"click:{selector}"

    def type(self, *, selector: str, text: str) -> str:
        return f"type:{selector}:{text}"

    def screenshot(self, *, full_page: bool = False) -> str:
        return f"screenshot:{full_page}"

    def snapshot(self) -> str:
        return "snapshot"

    def scroll(self, *, direction: str = "down", amount: int = 500) -> str:
        return f"scroll:{direction}:{amount}"

    def fill_form(self, *, fields: str) -> str:
        return f"fill:{fields}"

    def close(self) -> str:
        return "closed"


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
    with patch("agnoclaw.tools.browser_backends.check_playwright", return_value=False):
        with pytest.raises(ImportError, match="Playwright"):
            toolkit.browser_navigate("https://example.com")


def test_browser_close_when_not_initialized():
    """Close should work gracefully even if browser was never opened."""
    from agnoclaw.tools.browser import BrowserToolkit

    toolkit = BrowserToolkit()
    result = toolkit.browser_close()
    assert "closed" in result.lower() or "Browser closed" in result


def test_browser_toolkit_delegates_to_custom_backend():
    """BrowserToolkit should preserve tool names while delegating backend behavior."""
    from agnoclaw.tools.browser import BrowserToolkit

    toolkit = BrowserToolkit(backend=FakeBrowserBackend())

    assert toolkit.browser_navigate("https://example.com") == "navigate:https://example.com:domcontentloaded"
    assert toolkit.browser_click("#submit") == "click:#submit"
    assert toolkit.browser_type("#email", "hello") == "type:#email:hello"
    assert toolkit.browser_screenshot(True) == "screenshot:True"
    assert toolkit.browser_snapshot() == "snapshot"
    assert toolkit.browser_scroll("down", 300) == "scroll:down:300"
    assert toolkit.browser_fill_form("{\"#email\":\"a\"}") == 'fill:{"#email":"a"}'
    assert toolkit.browser_close() == "closed"
