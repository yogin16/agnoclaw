"""
Browser toolkit — Playwright-based browser automation for web interaction.

Matches OpenClaw's browser tool capabilities: navigate, click, type, screenshot,
snapshot (accessible text), scroll, form fill.

Optional extra: agnoclaw[browser] → playwright>=1.40.0

The snapshot approach (structured text representation) is preferred over screenshots
for LLM consumption — it's more token-efficient and gives the model actionable
information about page structure.

Usage:
    from agnoclaw.tools.browser import BrowserToolkit

    toolkit = BrowserToolkit()
    # Tools: browser_navigate, browser_click, browser_type, browser_screenshot,
    #        browser_snapshot, browser_scroll, browser_fill_form, browser_close
"""

from __future__ import annotations

import base64
import logging
from typing import Optional

from agno.tools.toolkit import Toolkit

logger = logging.getLogger("agnoclaw.tools.browser")


def _check_playwright():
    """Check if playwright is importable."""
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


class BrowserToolkit(Toolkit):
    """
    Playwright-based browser automation toolkit.

    Lazily initializes the browser on first use. Supports both headed and headless modes.

    Args:
        headless: Run browser in headless mode (default: True).
        viewport_width: Browser viewport width in pixels.
        viewport_height: Browser viewport height in pixels.
    """

    def __init__(
        self,
        headless: bool = True,
        viewport_width: int = 1280,
        viewport_height: int = 720,
    ):
        super().__init__(name="browser")
        self._headless = headless
        self._viewport = {"width": viewport_width, "height": viewport_height}
        self._playwright = None
        self._browser = None
        self._page = None

        self.register(self.browser_navigate)
        self.register(self.browser_click)
        self.register(self.browser_type)
        self.register(self.browser_screenshot)
        self.register(self.browser_snapshot)
        self.register(self.browser_scroll)
        self.register(self.browser_fill_form)
        self.register(self.browser_close)

    def _ensure_page(self):
        """Lazily initialize Playwright browser and page."""
        if self._page is not None:
            return

        if not _check_playwright():
            raise ImportError(
                "Playwright is required for browser tools. "
                "Install with: pip install agnoclaw[browser]"
            )

        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        self._browser = self._playwright.chromium.launch(headless=self._headless)
        context = self._browser.new_context(viewport=self._viewport)
        self._page = context.new_page()
        logger.debug("Browser initialized (headless=%s)", self._headless)

    def browser_navigate(self, url: str, wait_until: str = "domcontentloaded") -> str:
        """
        Navigate the browser to a URL.

        Args:
            url: The URL to navigate to (must include protocol, e.g. https://).
            wait_until: Wait condition: 'domcontentloaded', 'load', or 'networkidle'.

        Returns:
            Page title and URL after navigation.
        """
        self._ensure_page()
        try:
            self._page.goto(url, wait_until=wait_until, timeout=30000)
            title = self._page.title()
            return f"Navigated to: {self._page.url}\nTitle: {title}"
        except Exception as e:
            return f"[error] Navigation failed: {e}"

    def browser_click(self, selector: str) -> str:
        """
        Click an element on the page.

        Args:
            selector: CSS selector or text selector (e.g., 'button.submit', 'text=Sign In').

        Returns:
            Confirmation of the click action.
        """
        self._ensure_page()
        try:
            self._page.click(selector, timeout=10000)
            return f"Clicked: {selector}"
        except Exception as e:
            return f"[error] Click failed on '{selector}': {e}"

    def browser_type(self, selector: str, text: str) -> str:
        """
        Type text into an input element.

        Args:
            selector: CSS selector for the input element.
            text: Text to type.

        Returns:
            Confirmation of the type action.
        """
        self._ensure_page()
        try:
            self._page.fill(selector, text, timeout=10000)
            return f"Typed into '{selector}': {text[:50]}{'...' if len(text) > 50 else ''}"
        except Exception as e:
            return f"[error] Type failed on '{selector}': {e}"

    def browser_screenshot(self, full_page: bool = False) -> str:
        """
        Take a screenshot of the current page.

        Args:
            full_page: If True, capture the full scrollable page. If False, just the viewport.

        Returns:
            Base64-encoded PNG screenshot.
        """
        self._ensure_page()
        try:
            screenshot_bytes = self._page.screenshot(full_page=full_page)
            b64 = base64.b64encode(screenshot_bytes).decode("ascii")
            return f"data:image/png;base64,{b64}"
        except Exception as e:
            return f"[error] Screenshot failed: {e}"

    def browser_snapshot(self) -> str:
        """
        Get a text representation of the current page — more token-efficient than screenshots.

        Extracts visible text, links, forms, and buttons in a structured format
        that LLMs can reason about effectively.

        Returns:
            Structured text representation of the page.
        """
        self._ensure_page()
        try:
            # Extract structured page content
            snapshot = self._page.evaluate("""() => {
                const result = [];
                result.push('URL: ' + location.href);
                result.push('Title: ' + document.title);
                result.push('');

                // Headings
                const headings = document.querySelectorAll('h1, h2, h3');
                if (headings.length > 0) {
                    result.push('## Headings');
                    headings.forEach(h => {
                        const level = h.tagName.toLowerCase();
                        result.push(`  ${level}: ${h.textContent.trim().substring(0, 200)}`);
                    });
                    result.push('');
                }

                // Links
                const links = document.querySelectorAll('a[href]');
                if (links.length > 0) {
                    result.push('## Links');
                    const seen = new Set();
                    links.forEach(a => {
                        const text = a.textContent.trim().substring(0, 100);
                        const href = a.href;
                        const key = text + href;
                        if (text && !seen.has(key)) {
                            seen.add(key);
                            result.push(`  [${text}](${href})`);
                        }
                    });
                    result.push('');
                }

                // Forms
                const forms = document.querySelectorAll('form');
                if (forms.length > 0) {
                    result.push('## Forms');
                    forms.forEach((form, i) => {
                        result.push(`  Form ${i}: action=${form.action || '(none)'}`);
                        form.querySelectorAll('input, select, textarea').forEach(input => {
                            const type = input.type || input.tagName.toLowerCase();
                            const name = input.name || input.id || '';
                            const placeholder = input.placeholder || '';
                            result.push(`    ${type}: name=${name} placeholder="${placeholder}"`);
                        });
                    });
                    result.push('');
                }

                // Buttons
                const buttons = document.querySelectorAll('button, [role="button"], input[type="submit"]');
                if (buttons.length > 0) {
                    result.push('## Buttons');
                    buttons.forEach(b => {
                        const text = b.textContent.trim().substring(0, 100) || b.value || '';
                        if (text) result.push(`  [${text}]`);
                    });
                    result.push('');
                }

                // Main text content (truncated)
                const main = document.querySelector('main, article, [role="main"]') || document.body;
                const text = main.innerText.substring(0, 3000);
                result.push('## Page Text (truncated)');
                result.push(text);

                return result.join('\\n');
            }""")
            return snapshot
        except Exception as e:
            return f"[error] Snapshot failed: {e}"

    def browser_scroll(self, direction: str = "down", amount: int = 500) -> str:
        """
        Scroll the page.

        Args:
            direction: Scroll direction: 'up' or 'down'.
            amount: Pixels to scroll.

        Returns:
            Confirmation with new scroll position.
        """
        self._ensure_page()
        try:
            delta = amount if direction == "down" else -amount
            self._page.mouse.wheel(0, delta)
            scroll_y = self._page.evaluate("() => window.scrollY")
            return f"Scrolled {direction} by {amount}px. Current position: {scroll_y}px"
        except Exception as e:
            return f"[error] Scroll failed: {e}"

    def browser_fill_form(self, fields: str) -> str:
        """
        Fill multiple form fields at once.

        Args:
            fields: JSON object mapping CSS selectors to values.
                    Example: {"#email": "user@example.com", "#password": "secret"}

        Returns:
            Confirmation of filled fields.
        """
        self._ensure_page()
        import json

        try:
            field_map = json.loads(fields) if isinstance(fields, str) else fields
        except (json.JSONDecodeError, TypeError):
            return "[error] fields must be a JSON object mapping selectors to values"

        results = []
        for selector, value in field_map.items():
            try:
                self._page.fill(selector, str(value), timeout=5000)
                results.append(f"  {selector}: OK")
            except Exception as e:
                results.append(f"  {selector}: FAILED ({e})")

        return "Form fill results:\n" + "\n".join(results)

    def browser_close(self) -> str:
        """
        Close the browser and release resources.

        Returns:
            Confirmation that the browser was closed.
        """
        try:
            if self._page:
                self._page.close()
                self._page = None
            if self._browser:
                self._browser.close()
                self._browser = None
            if self._playwright:
                self._playwright.stop()
                self._playwright = None
            return "Browser closed."
        except Exception as e:
            return f"[error] Browser close failed: {e}"

    def __del__(self):
        try:
            self.browser_close()
        except Exception:
            pass
