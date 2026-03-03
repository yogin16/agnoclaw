"""
Example: Browser Demo — Playwright-based web automation with AgentHarness

Demonstrates the browser toolkit for navigating websites, extracting content,
filling forms, and taking snapshots.

Run: uv run --extra browser python examples/browser_demo.py
Requires: ANTHROPIC_API_KEY, playwright installed (npx playwright install chromium)
"""

from agnoclaw import AgentHarness
from agnoclaw.config import HarnessConfig


def main():
    print("=" * 60)
    print("Browser Toolkit Demo")
    print("=" * 60)

    # Check if playwright is available
    try:
        from agnoclaw.tools.browser import BrowserToolkit, _check_playwright
        if not _check_playwright():
            print("\nPlaywright not installed. Install with:")
            print("  pip install agnoclaw[browser]")
            print("  npx playwright install chromium")
            return
    except ImportError:
        print("\nBrowser toolkit not available.")
        return

    # Create agent with browser toolkit
    config = HarnessConfig(enable_browser=True)
    browser = BrowserToolkit(headless=True)

    agent = AgentHarness(
        name="browser-agent",
        tools=[browser],
        config=config,
        instructions=(
            "You have a browser toolkit for web automation. "
            "Use browser_navigate to visit pages, browser_snapshot for "
            "text representation (preferred), and browser_screenshot "
            "only when visual details matter."
        ),
    )

    # Demo tasks
    print("\n1. Navigate and snapshot a page:")
    agent.print_response(
        "Navigate to https://example.com and give me a snapshot of the page content.",
        stream=True,
    )

    print("\n2. Research a topic (snapshot-based):")
    agent.print_response(
        "Navigate to https://news.ycombinator.com and tell me the top 5 stories.",
        stream=True,
    )

    # Cleanup
    browser.browser_close()
    print("\nBrowser closed. Done.")


if __name__ == "__main__":
    main()
