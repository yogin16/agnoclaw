"""
Browser toolkit — browser/computer-use automation with pluggable backends.

Matches OpenClaw's browser tool capabilities: navigate, click, type, screenshot,
snapshot (accessible text), scroll, form fill.
"""

from __future__ import annotations

from agno.tools.toolkit import Toolkit

from .browser_backends import BrowserBackend, LocalPlaywrightBrowserBackend, check_playwright


def _check_playwright() -> bool:
    """Backward-compatible wrapper for playwright availability checks."""
    return check_playwright()


class BrowserToolkit(Toolkit):
    """Browser automation toolkit backed by a pluggable browser runtime."""

    def __init__(
        self,
        headless: bool = True,
        viewport_width: int = 1280,
        viewport_height: int = 720,
        backend: BrowserBackend | None = None,
    ):
        super().__init__(name="browser")
        self.backend = backend or LocalPlaywrightBrowserBackend(
            headless=headless,
            viewport_width=viewport_width,
            viewport_height=viewport_height,
        )

        self.register(self.browser_navigate)
        self.register(self.browser_click)
        self.register(self.browser_type)
        self.register(self.browser_screenshot)
        self.register(self.browser_snapshot)
        self.register(self.browser_scroll)
        self.register(self.browser_fill_form)
        self.register(self.browser_close)

    def browser_navigate(self, url: str, wait_until: str = "domcontentloaded") -> str:
        return self.backend.navigate(url=url, wait_until=wait_until)

    def browser_click(self, selector: str) -> str:
        return self.backend.click(selector=selector)

    def browser_type(self, selector: str, text: str) -> str:
        return self.backend.type(selector=selector, text=text)

    def browser_screenshot(self, full_page: bool = False) -> str:
        return self.backend.screenshot(full_page=full_page)

    def browser_snapshot(self) -> str:
        return self.backend.snapshot()

    def browser_scroll(self, direction: str = "down", amount: int = 500) -> str:
        return self.backend.scroll(direction=direction, amount=amount)

    def browser_fill_form(self, fields: str) -> str:
        return self.backend.fill_form(fields=fields)

    def browser_close(self) -> str:
        return self.backend.close()

    def __del__(self):
        try:
            self.browser_close()
        except Exception:
            pass
