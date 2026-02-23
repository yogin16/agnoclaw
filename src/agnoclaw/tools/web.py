"""
Web tools — WebFetch and WebSearch.

WebSearch auto-detects the best available backend (OpenClaw-inspired):
  1. Tavily  (TAVILY_API_KEY)  — best quality, deep research, paid
  2. Exa     (EXA_API_KEY)     — neural search, good for technical content, paid
  3. Brave   (BRAVE_API_KEY)   — privacy-first, good quality, paid
  4. DDGS    (no key needed)   — DuckDuckGo, free, default fallback

To use a paid backend: set the corresponding env var and install the client:
  pip install tavily-python      # for Tavily
  pip install exa-py             # for Exa
  pip install brave-search       # for Brave

Rules:
- WebSearch for current information beyond knowledge cutoff
- WebFetch for reading a specific known URL
- Never guess URLs
"""

from __future__ import annotations

import os
from typing import Optional

import httpx
from agno.tools.toolkit import Toolkit


class WebToolkit(Toolkit):
    """
    Web search and fetch tools with auto-detected search backend.

    Search backend priority (first available wins):
      Tavily → Exa → Brave → DuckDuckGo (always available)
    """

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

        Auto-selects the best available backend:
        Tavily (if TAVILY_API_KEY) → Exa (if EXA_API_KEY) →
        Brave (if BRAVE_API_KEY) → DuckDuckGo (always available, no key needed).

        Use this when you need up-to-date information beyond your knowledge cutoff,
        or to verify facts. Do NOT guess URLs — use this to find them.

        Args:
            query: Search query string.
            max_results: Maximum number of results to return (1-10).

        Returns:
            Search results with titles, URLs, and snippets.
        """
        max_results = max(1, min(10, max_results))

        if os.environ.get("TAVILY_API_KEY"):
            return self._search_tavily(query, max_results)
        if os.environ.get("EXA_API_KEY"):
            return self._search_exa(query, max_results)
        if os.environ.get("BRAVE_API_KEY"):
            return self._search_brave(query, max_results)
        return self._search_ddgs(query, max_results)

    def web_fetch(self, url: str, prompt: Optional[str] = None) -> str:
        """
        Fetch the content of a URL and return it as text.

        Use this to read a specific URL you already know (from search results or user input).
        Do NOT guess URLs.

        Args:
            url: The full URL to fetch (must be https:// or http://).
            prompt: Optional focus — what information to extract from the page.

        Returns:
            Page content as markdown text, or an error message.
        """
        try:
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

    # ── Search backends ────────────────────────────────────────────────────────

    def _search_tavily(self, query: str, max_results: int) -> str:
        """Tavily search — best quality, structured results with content extraction."""
        try:
            from tavily import TavilyClient
            client = TavilyClient(api_key=os.environ["TAVILY_API_KEY"])
            resp = client.search(query, max_results=max_results, search_depth="basic")
            results = []
            for i, r in enumerate(resp.get("results", []), 1):
                results.append(
                    f"{i}. **{r.get('title', 'No title')}**\n"
                    f"   URL: {r.get('url', '')}\n"
                    f"   {r.get('content', '')[:300]}"
                )
            return "\n\n".join(results) if results else f"[no results] No results for: {query}"
        except ImportError:
            return self._search_ddgs(query, max_results)  # graceful fallback
        except Exception as e:
            return f"[error] Tavily search failed: {e}"

    def _search_exa(self, query: str, max_results: int) -> str:
        """Exa neural search — good for technical and research content."""
        try:
            from exa_py import Exa
            client = Exa(api_key=os.environ["EXA_API_KEY"])
            resp = client.search_and_contents(
                query,
                num_results=max_results,
                text={"max_characters": 300},
            )
            results = []
            for i, r in enumerate(resp.results, 1):
                snippet = getattr(r, "text", "") or ""
                results.append(
                    f"{i}. **{r.title or 'No title'}**\n"
                    f"   URL: {r.url}\n"
                    f"   {snippet[:300]}"
                )
            return "\n\n".join(results) if results else f"[no results] No results for: {query}"
        except ImportError:
            return self._search_ddgs(query, max_results)
        except Exception as e:
            return f"[error] Exa search failed: {e}"

    def _search_brave(self, query: str, max_results: int) -> str:
        """Brave Search — privacy-first, good quality."""
        try:
            from brave_search import BraveSearch
            client = BraveSearch(api_key=os.environ["BRAVE_API_KEY"])
            resp = client.search(q=query, count=max_results)
            results = []
            for i, r in enumerate(resp.web.results, 1):
                results.append(
                    f"{i}. **{r.title}**\n"
                    f"   URL: {r.url}\n"
                    f"   {r.description or ''}"
                )
            return "\n\n".join(results) if results else f"[no results] No results for: {query}"
        except ImportError:
            return self._search_ddgs(query, max_results)
        except Exception as e:
            return f"[error] Brave search failed: {e}"

    def _search_ddgs(self, query: str, max_results: int) -> str:
        """DuckDuckGo search — free fallback, no API key needed."""
        try:
            from duckduckgo_search import DDGS
            results = []
            with DDGS() as ddgs:
                for i, r in enumerate(ddgs.text(query, max_results=max_results), 1):
                    results.append(
                        f"{i}. **{r.get('title', 'No title')}**\n"
                        f"   URL: {r.get('href', '')}\n"
                        f"   {r.get('body', '')[:200]}"
                    )
            return "\n\n".join(results) if results else f"[no results] No results for: {query}"
        except ImportError:
            return "[error] duckduckgo-search not installed. Run: pip install duckduckgo-search"
        except Exception as e:
            return f"[error] Search failed: {e}"


def _html_to_text(html: str, url: str) -> str:
    """Convert HTML to plain text, extracting meaningful content."""
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")

        # Remove noise
        for tag in soup(["script", "style", "nav", "footer", "aside", "head"]):
            tag.decompose()

        title = soup.title.string.strip() if soup.title and soup.title.string else ""
        text = soup.get_text(separator="\n", strip=True)
        lines = [line for line in text.splitlines() if line.strip()]
        clean = "\n".join(lines)

        header = f"[{title}]({url})\n\n" if title else f"[{url}]\n\n"
        return header + clean[:8000]
    except ImportError:
        return html[:3000]
    except Exception:
        return html[:3000]
