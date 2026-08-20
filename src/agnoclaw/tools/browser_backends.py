"""Backend abstractions for browser/computer-use tooling."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from playwright.sync_api import Browser, Page, Playwright, Request, Route

    from .web import NetworkURLPolicy

logger = logging.getLogger("agnoclaw.tools.browser")


def check_playwright() -> bool:
    """Check if playwright is importable."""
    try:
        import playwright  # noqa: F401

        return True
    except ImportError:
        return False


class BrowserBackend(Protocol):
    """Backend interface for browser/computer-use tool operations."""

    def navigate(self, *, url: str, wait_until: str = "domcontentloaded") -> str: ...

    def click(self, *, selector: str) -> str: ...

    def type(self, *, selector: str, text: str) -> str: ...

    def screenshot(self, *, full_page: bool = False) -> str: ...

    def snapshot(self) -> str: ...

    def scroll(self, *, direction: str = "down", amount: int = 500) -> str: ...

    def fill_form(self, *, fields: str) -> str: ...

    def close(self) -> str: ...

    def set_network_policy(self, policy: NetworkURLPolicy) -> None: ...


class LocalPlaywrightBrowserBackend:
    """Host-local Playwright backend preserving current browser toolkit behavior."""

    def __init__(
        self,
        *,
        headless: bool = True,
        viewport_width: int = 1280,
        viewport_height: int = 720,
        network_policy: NetworkURLPolicy | None = None,
    ) -> None:
        self._headless = headless
        self._viewport = {"width": viewport_width, "height": viewport_height}
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._page: Page | None = None
        if network_policy is None:
            from agnoclaw.runtime.guardrails import RuntimeGuardrails

            network_policy = RuntimeGuardrails(
                workspace_dir=Path.cwd(),
                path_enabled=False,
            )
        self._network_policy = network_policy

    def set_network_policy(self, policy: NetworkURLPolicy) -> None:
        """Install the host URL policy before a browser context exists."""
        if self._page is not None:
            raise RuntimeError("browser network policy cannot change after browser startup")
        self._network_policy = policy

    def _ensure_page(self) -> Page:
        if self._page is not None:
            return self._page

        if not check_playwright():
            raise ImportError(
                "Playwright is required for browser tools. "
                "Install with: pip install agnoclaw[browser]"
            )

        from playwright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        browser = playwright.chromium.launch(headless=self._headless)
        context = browser.new_context(viewport=self._viewport)
        context.route("**/*", self._route_request)
        page = context.new_page()
        self._playwright = playwright
        self._browser = browser
        self._page = page
        logger.debug("Browser initialized (headless=%s)", self._headless)
        return page

    def navigate(self, *, url: str, wait_until: str = "domcontentloaded") -> str:
        self._require_allowed_url(url)
        page = self._ensure_page()
        try:
            page.goto(url, wait_until=wait_until, timeout=30000)
            title = page.title()
            return f"Navigated to: {page.url}\nTitle: {title}"
        except Exception as exc:
            return f"[error] Navigation failed: {exc}"

    def _route_request(self, route: Route, request: Request) -> None:
        try:
            self._require_allowed_url(request.url)
        except ValueError as exc:
            logger.warning("Browser request blocked by network policy: %s", exc)
            route.abort("blockedbyclient")
            return
        route.continue_()

    def _require_allowed_url(self, url: str) -> None:
        violations = self._network_policy.validate_network_url(
            url,
            tool_name="browser_request",
            arg_key="url",
        )
        if not violations:
            return
        first = violations[0]
        code = getattr(first, "code", "NETWORK_POLICY_DENIED")
        message = getattr(first, "message", str(first))
        raise ValueError(f"{code}: {message}")

    def click(self, *, selector: str) -> str:
        page = self._ensure_page()
        try:
            page.click(selector, timeout=10000)
            return f"Clicked: {selector}"
        except Exception as exc:
            return f"[error] Click failed on '{selector}': {exc}"

    def type(self, *, selector: str, text: str) -> str:
        page = self._ensure_page()
        try:
            page.fill(selector, text, timeout=10000)
            suffix = "..." if len(text) > 50 else ""
            return f"Typed into '{selector}': {text[:50]}{suffix}"
        except Exception as exc:
            return f"[error] Type failed on '{selector}': {exc}"

    def screenshot(self, *, full_page: bool = False) -> str:
        page = self._ensure_page()
        try:
            screenshot_bytes = page.screenshot(full_page=full_page)
            encoded = base64.b64encode(screenshot_bytes).decode("ascii")
            return f"data:image/png;base64,{encoded}"
        except Exception as exc:
            return f"[error] Screenshot failed: {exc}"

    def snapshot(self) -> str:
        page = self._ensure_page()
        try:
            return page.evaluate(
                """() => {
                const result = [];
                result.push('URL: ' + location.href);
                result.push('Title: ' + document.title);
                result.push('');

                const headings = document.querySelectorAll('h1, h2, h3');
                if (headings.length > 0) {
                    result.push('## Headings');
                    headings.forEach(h => {
                        const level = h.tagName.toLowerCase();
                        result.push(`  ${level}: ${h.textContent.trim().substring(0, 200)}`);
                    });
                    result.push('');
                }

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

                const buttons = document.querySelectorAll(
                    'button, [role="button"], input[type="submit"]'
                );
                if (buttons.length > 0) {
                    result.push('## Buttons');
                    buttons.forEach(b => {
                        const text = b.textContent.trim().substring(0, 100) || b.value || '';
                        if (text) result.push(`  [${text}]`);
                    });
                    result.push('');
                }

                const main = document.querySelector(
                    'main, article, [role="main"]'
                ) || document.body;
                const text = main.innerText.substring(0, 3000);
                result.push('## Page Text (truncated)');
                result.push(text);

                return result.join('\\n');
            }"""
            )
        except Exception as exc:
            return f"[error] Snapshot failed: {exc}"

    def scroll(self, *, direction: str = "down", amount: int = 500) -> str:
        page = self._ensure_page()
        try:
            delta = amount if direction == "down" else -amount
            page.mouse.wheel(0, delta)
            scroll_y = page.evaluate("() => window.scrollY")
            return f"Scrolled {direction} by {amount}px. Current position: {scroll_y}px"
        except Exception as exc:
            return f"[error] Scroll failed: {exc}"

    def fill_form(self, *, fields: str) -> str:
        page = self._ensure_page()
        try:
            field_map = json.loads(fields) if isinstance(fields, str) else fields
        except (json.JSONDecodeError, TypeError):
            return "[error] fields must be a JSON object mapping selectors to values"

        results = []
        for selector, value in field_map.items():
            try:
                page.fill(selector, str(value), timeout=5000)
                results.append(f"  {selector}: OK")
            except Exception as exc:
                results.append(f"  {selector}: FAILED ({exc})")
        return "Form fill results:\n" + "\n".join(results)

    def close(self) -> str:
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
        except Exception as exc:
            return f"[error] Browser close failed: {exc}"
