"""
Web tools — WebFetch and WebSearch.

Rules:
- WebSearch for current information beyond knowledge cutoff
- WebFetch for reading a specific known URL
- Never guess URLs
"""

from __future__ import annotations

import httpx
from agno.tools import tool
from agno.tools.toolkit import Toolkit


class WebToolkit(Toolkit):
    """Web search and fetch tools."""

    def __init__(self, search_enabled: bool = True, fetch_enabled: bool = True):
        super().__init__(name="web")
        self.search_enabled = search_enabled
        self.fetch_enabled = fetch_enabled
        if search_enabled:
            self.register(self.web_search)
        if fetch_enabled:
            self.register(self.web_fetch)

    def web_search(self, query: str, max_results: int = 5) -> str:
        """
        Search the web for current information.

        Use this when you need up-to-date information beyond your knowledge cutoff,
        or to verify facts. Do NOT guess URLs — use this to find them.

        Args:
            query: Search query string.
            max_results: Maximum number of results to return (1-10).

        Returns:
            Search results with titles, URLs, and snippets.
        """
        try:
            from duckduckgo_search import DDGS

            max_results = max(1, min(10, max_results))
            results = []
            with DDGS() as ddgs:
                for i, r in enumerate(ddgs.text(query, max_results=max_results)):
                    results.append(
                        f"{i + 1}. **{r.get('title', 'No title')}**\n"
                        f"   URL: {r.get('href', '')}\n"
                        f"   {r.get('body', '')[:200]}"
                    )
            return "\n\n".join(results) if results else f"[no results] No results for: {query}"
        except ImportError:
            return "[error] duckduckgo-search not installed. Run: pip install duckduckgo-search"
        except Exception as e:
            return f"[error] Search failed: {e}"

    def web_fetch(self, url: str, prompt: Optional[str] = None) -> str:  # noqa: F821
        """
        Fetch the content of a URL and return it as text.

        Use this to read a specific URL you already know (from search results or user input).
        Do NOT guess URLs.

        Args:
            url: The full URL to fetch (must be https:// or http://).
            prompt: Optional focus prompt — what information to extract from the page.

        Returns:
            Page content as markdown text, or an error message.
        """
        try:
            # Upgrade http to https
            if url.startswith("http://"):
                url = "https://" + url[7:]

            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; agnoclaw/0.1; +https://github.com/agnoclaw/agnoclaw)"
            }
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                response = client.get(url, headers=headers)
                response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            if "text/html" in content_type:
                return _html_to_text(response.text, url)
            elif "application/json" in content_type:
                return f"[JSON from {url}]\n{response.text[:5000]}"
            else:
                return response.text[:5000]
        except httpx.HTTPStatusError as e:
            return f"[error] HTTP {e.response.status_code} fetching {url}"
        except httpx.RequestError as e:
            return f"[error] Request failed for {url}: {e}"
        except Exception as e:
            return f"[error] Could not fetch {url}: {e}"


def _html_to_text(html: str, url: str) -> str:
    """Convert HTML to plain text, extracting meaningful content."""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")

        # Remove noise
        for tag in soup(["script", "style", "nav", "footer", "aside", "head"]):
            tag.decompose()

        # Extract title
        title = soup.title.string.strip() if soup.title and soup.title.string else ""

        # Get main content
        text = soup.get_text(separator="\n", strip=True)

        # Clean up excessive blank lines
        lines = [line for line in text.splitlines() if line.strip()]
        clean = "\n".join(lines)

        header = f"[{title}]({url})\n\n" if title else f"[{url}]\n\n"
        return header + clean[:8000]
    except ImportError:
        return html[:3000]
    except Exception:
        return html[:3000]


# Allow Optional type hint in web_fetch without importing at module level
from typing import Optional  # noqa: E402
