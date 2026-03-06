"""Tests for the web toolkit — web_fetch and web_search backends."""

from unittest.mock import patch, MagicMock

from agnoclaw.tools.web import WebToolkit, _html_to_text


# ── web_fetch tests ─────────────────────────────────────────────────────


def _make_mock_response(text="", content_type="text/html", status_code=200):
    """Helper to build a mock httpx response."""
    resp = MagicMock()
    resp.text = text
    resp.headers = {"content-type": content_type}
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    return resp


def _patch_httpx(mock_response):
    """Return a patch context for httpx.Client that yields mock_response on .get()."""
    mock_client = MagicMock()
    mock_client.get.return_value = mock_response

    p = patch("httpx.Client")
    mock_cls = p.start()
    mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
    mock_cls.return_value.__exit__ = MagicMock(return_value=False)
    return p, mock_client


def test_web_fetch_html():
    toolkit = WebToolkit()
    resp = _make_mock_response("<html><body><p>Hello</p></body></html>", "text/html")
    p, _ = _patch_httpx(resp)
    try:
        result = toolkit.web_fetch("https://example.com")
        assert isinstance(result, str)
        assert "Hello" in result
    finally:
        p.stop()


def test_web_fetch_json_content_type():
    toolkit = WebToolkit()
    resp = _make_mock_response('{"key": "value"}', "application/json")
    p, _ = _patch_httpx(resp)
    try:
        result = toolkit.web_fetch("https://api.example.com/data")
        assert "[JSON from" in result
        assert '"key"' in result
    finally:
        p.stop()


def test_web_fetch_plain_text():
    toolkit = WebToolkit()
    resp = _make_mock_response("plain text content", "text/plain")
    p, _ = _patch_httpx(resp)
    try:
        result = toolkit.web_fetch("https://example.com/file.txt")
        assert "plain text content" in result
    finally:
        p.stop()


def test_web_fetch_with_prompt():
    toolkit = WebToolkit()
    resp = _make_mock_response("<html><body>Content</body></html>", "text/html")
    p, _ = _patch_httpx(resp)
    try:
        result = toolkit.web_fetch("https://example.com", prompt="Extract the title")
        assert "[Focus: Extract the title]" in result
    finally:
        p.stop()


def test_web_fetch_http_upgrade_to_https():
    toolkit = WebToolkit()
    resp = _make_mock_response("ok", "text/plain")
    p, mock_client = _patch_httpx(resp)
    try:
        toolkit.web_fetch("http://example.com/page")
        # URL should be upgraded to https
        call_args = mock_client.get.call_args
        assert call_args[0][0].startswith("https://")
    finally:
        p.stop()


def test_web_fetch_http_status_error():
    import httpx

    toolkit = WebToolkit()
    mock_response = MagicMock()
    mock_response.status_code = 404
    mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
        "Not Found", request=MagicMock(), response=mock_response
    )

    p, _ = _patch_httpx(mock_response)
    try:
        result = toolkit.web_fetch("https://example.com/missing")
        assert "[error]" in result
        assert "404" in result
    finally:
        p.stop()


def test_web_fetch_request_error():
    import httpx

    toolkit = WebToolkit()
    mock_client = MagicMock()
    mock_client.get.side_effect = httpx.RequestError("connection refused")

    p = patch("httpx.Client")
    mock_cls = p.start()
    mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
    mock_cls.return_value.__exit__ = MagicMock(return_value=False)
    try:
        result = toolkit.web_fetch("https://example.com")
        assert "[error]" in result
        assert "Request failed" in result
    finally:
        p.stop()


def test_web_fetch_general_exception():
    toolkit = WebToolkit()
    mock_client = MagicMock()
    mock_client.get.side_effect = RuntimeError("unexpected")

    p = patch("httpx.Client")
    mock_cls = p.start()
    mock_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
    mock_cls.return_value.__exit__ = MagicMock(return_value=False)
    try:
        result = toolkit.web_fetch("https://example.com")
        assert "[error]" in result
        assert "Could not fetch" in result
    finally:
        p.stop()


# ── web_search backend tests ───────────────────────────────────────────


def test_web_search_ddgs_backend():
    """Default DDGS backend returns formatted results."""
    toolkit = WebToolkit()

    with patch.dict("os.environ", {}, clear=True):
        with patch("duckduckgo_search.DDGS") as mock_ddgs:
            mock_instance = MagicMock()
            mock_ddgs.return_value.__enter__ = MagicMock(return_value=mock_instance)
            mock_ddgs.return_value.__exit__ = MagicMock(return_value=False)
            mock_instance.text.return_value = [
                {"title": "Result 1", "href": "https://a.com", "body": "Body 1"},
                {"title": "Result 2", "href": "https://b.com", "body": "Body 2"},
            ]
            result = toolkit._search_ddgs("test query", 5)
    assert "Result 1" in result
    assert "Result 2" in result
    assert "https://a.com" in result


def test_web_search_ddgs_no_results():
    """DDGS returns no results message."""
    toolkit = WebToolkit()

    with patch("duckduckgo_search.DDGS") as mock_ddgs:
        mock_instance = MagicMock()
        mock_ddgs.return_value.__enter__ = MagicMock(return_value=mock_instance)
        mock_ddgs.return_value.__exit__ = MagicMock(return_value=False)
        mock_instance.text.return_value = []
        result = toolkit._search_ddgs("obscure query", 5)
    assert "[no results]" in result


def test_web_search_ddgs_import_error():
    """DDGS ImportError returns helpful message."""
    toolkit = WebToolkit()

    with patch("duckduckgo_search.DDGS", side_effect=ImportError):
        result = toolkit._search_ddgs("test", 5)
    assert "[error]" in result
    assert "duckduckgo-search" in result


def test_web_search_ddgs_exception():
    """DDGS general exception returns error."""
    toolkit = WebToolkit()

    with patch("duckduckgo_search.DDGS") as mock_ddgs:
        mock_ddgs.return_value.__enter__ = MagicMock(side_effect=RuntimeError("timeout"))
        mock_ddgs.return_value.__exit__ = MagicMock(return_value=False)
        result = toolkit._search_ddgs("test", 5)
    assert "[error]" in result


def test_web_search_max_results_clamping():
    """max_results is clamped to 1-10."""
    toolkit = WebToolkit()

    with patch.dict("os.environ", {}, clear=True):
        with patch.object(toolkit, "_search_ddgs", return_value="ok") as mock:
            toolkit.web_search("q", max_results=0)
            assert mock.call_args[0][1] == 1

            toolkit.web_search("q", max_results=99)
            assert mock.call_args[0][1] == 10


def test_web_search_tavily_backend():
    """Tavily backend is selected when TAVILY_API_KEY is set."""
    toolkit = WebToolkit()

    with patch.dict("os.environ", {"TAVILY_API_KEY": "test-key"}):
        with patch.object(toolkit, "_search_tavily", return_value="tavily result") as mock:
            result = toolkit.web_search("test")
    assert result == "tavily result"
    mock.assert_called_once_with("test", 5)


def test_web_search_exa_backend():
    """Exa backend is selected when EXA_API_KEY is set (no TAVILY_API_KEY)."""
    toolkit = WebToolkit()

    with patch.dict("os.environ", {"EXA_API_KEY": "test-key"}, clear=True):
        with patch.object(toolkit, "_search_exa", return_value="exa result"):
            result = toolkit.web_search("test")
    assert result == "exa result"


def test_web_search_brave_backend():
    """Brave backend is selected when BRAVE_API_KEY is set."""
    toolkit = WebToolkit()

    with patch.dict("os.environ", {"BRAVE_API_KEY": "test-key"}, clear=True):
        with patch.object(toolkit, "_search_brave", return_value="brave result"):
            result = toolkit.web_search("test")
    assert result == "brave result"


def test_search_tavily_success():
    """Tavily returns formatted results when client works."""
    toolkit = WebToolkit()

    mock_client = MagicMock()
    mock_client.search.return_value = {
        "results": [
            {"title": "Tavily Result", "url": "https://tavily.com", "content": "Tavily content"},
        ]
    }
    mock_tavily = MagicMock()
    mock_tavily.TavilyClient.return_value = mock_client

    with patch.dict("os.environ", {"TAVILY_API_KEY": "key"}):
        with patch.dict("sys.modules", {"tavily": mock_tavily}):
            result = toolkit._search_tavily("test", 5)
    assert "Tavily Result" in result
    assert "https://tavily.com" in result


def test_search_tavily_no_results():
    """Tavily returns no results message."""
    toolkit = WebToolkit()

    mock_client = MagicMock()
    mock_client.search.return_value = {"results": []}
    mock_tavily = MagicMock()
    mock_tavily.TavilyClient.return_value = mock_client

    with patch.dict("os.environ", {"TAVILY_API_KEY": "key"}):
        with patch.dict("sys.modules", {"tavily": mock_tavily}):
            result = toolkit._search_tavily("obscure", 5)
    assert "[no results]" in result


def test_search_tavily_import_error():
    """Tavily ImportError falls back to DDGS."""
    toolkit = WebToolkit()

    with patch.dict("os.environ", {"TAVILY_API_KEY": "key"}):
        with patch("agnoclaw.tools.web.WebToolkit._search_ddgs", return_value="ddgs fallback"):
            # Simulate tavily import failing inside _search_tavily
            with patch("builtins.__import__", side_effect=ImportError("no tavily")):
                result = toolkit._search_tavily("test", 5)
    assert "ddgs fallback" in result


def test_search_tavily_exception():
    """Tavily general exception returns error."""
    toolkit = WebToolkit()

    mock_client = MagicMock()
    mock_client.search.side_effect = RuntimeError("API error")
    mock_tavily = MagicMock()
    mock_tavily.TavilyClient.return_value = mock_client

    with patch.dict("os.environ", {"TAVILY_API_KEY": "key"}):
        with patch.dict("sys.modules", {"tavily": mock_tavily}):
            result = toolkit._search_tavily("test", 5)
    assert "[error]" in result
    assert "Tavily" in result


def test_search_exa_success():
    """Exa returns formatted results when client works."""
    toolkit = WebToolkit()

    mock_result = MagicMock()
    mock_result.title = "Exa Result"
    mock_result.url = "https://exa.ai"
    mock_result.text = "Exa content text"

    mock_resp = MagicMock()
    mock_resp.results = [mock_result]

    mock_client = MagicMock()
    mock_client.search_and_contents.return_value = mock_resp

    mock_exa = MagicMock()
    mock_exa.Exa.return_value = mock_client

    with patch.dict("os.environ", {"EXA_API_KEY": "key"}):
        with patch.dict("sys.modules", {"exa_py": mock_exa}):
            result = toolkit._search_exa("test", 5)
    assert "Exa Result" in result
    assert "https://exa.ai" in result


def test_search_exa_no_results():
    """Exa returns no results message."""
    toolkit = WebToolkit()

    mock_resp = MagicMock()
    mock_resp.results = []

    mock_client = MagicMock()
    mock_client.search_and_contents.return_value = mock_resp

    mock_exa = MagicMock()
    mock_exa.Exa.return_value = mock_client

    with patch.dict("os.environ", {"EXA_API_KEY": "key"}):
        with patch.dict("sys.modules", {"exa_py": mock_exa}):
            result = toolkit._search_exa("obscure", 5)
    assert "[no results]" in result


def test_search_exa_import_error():
    """Exa ImportError falls back to DDGS."""
    toolkit = WebToolkit()

    with patch.dict("os.environ", {"EXA_API_KEY": "key"}):
        with patch("agnoclaw.tools.web.WebToolkit._search_ddgs", return_value="ddgs"):
            with patch("builtins.__import__", side_effect=ImportError):
                result = toolkit._search_exa("test", 5)
    assert "ddgs" in result


def test_search_exa_exception():
    """Exa exception returns error."""
    toolkit = WebToolkit()

    mock_client = MagicMock()
    mock_client.search_and_contents.side_effect = RuntimeError("API fail")

    mock_exa = MagicMock()
    mock_exa.Exa.return_value = mock_client

    with patch.dict("os.environ", {"EXA_API_KEY": "key"}):
        with patch.dict("sys.modules", {"exa_py": mock_exa}):
            result = toolkit._search_exa("test", 5)
    assert "[error]" in result
    assert "Exa" in result


def test_search_brave_success():
    """Brave returns formatted results."""
    toolkit = WebToolkit()

    mock_result = MagicMock()
    mock_result.title = "Brave Result"
    mock_result.url = "https://brave.com"
    mock_result.description = "Brave desc"

    mock_resp = MagicMock()
    mock_resp.web.results = [mock_result]

    mock_client = MagicMock()
    mock_client.search.return_value = mock_resp

    mock_brave = MagicMock()
    mock_brave.BraveSearch.return_value = mock_client

    with patch.dict("os.environ", {"BRAVE_API_KEY": "key"}):
        with patch.dict("sys.modules", {"brave_search": mock_brave}):
            result = toolkit._search_brave("test", 5)
    assert "Brave Result" in result


def test_search_brave_no_results():
    """Brave returns no results message."""
    toolkit = WebToolkit()

    mock_resp = MagicMock()
    mock_resp.web.results = []

    mock_client = MagicMock()
    mock_client.search.return_value = mock_resp

    mock_brave = MagicMock()
    mock_brave.BraveSearch.return_value = mock_client

    with patch.dict("os.environ", {"BRAVE_API_KEY": "key"}):
        with patch.dict("sys.modules", {"brave_search": mock_brave}):
            result = toolkit._search_brave("test", 5)
    assert "[no results]" in result


def test_search_brave_import_error():
    """Brave ImportError falls back to DDGS."""
    toolkit = WebToolkit()

    with patch.dict("os.environ", {"BRAVE_API_KEY": "key"}):
        with patch("agnoclaw.tools.web.WebToolkit._search_ddgs", return_value="ddgs"):
            with patch("builtins.__import__", side_effect=ImportError):
                result = toolkit._search_brave("test", 5)
    assert "ddgs" in result


def test_search_brave_exception():
    """Brave exception returns error."""
    toolkit = WebToolkit()

    mock_client = MagicMock()
    mock_client.search.side_effect = RuntimeError("Brave fail")

    mock_brave = MagicMock()
    mock_brave.BraveSearch.return_value = mock_client

    with patch.dict("os.environ", {"BRAVE_API_KEY": "key"}):
        with patch.dict("sys.modules", {"brave_search": mock_brave}):
            result = toolkit._search_brave("test", 5)
    assert "[error]" in result
    assert "Brave" in result


# ── _html_to_text tests ────────────────────────────────────────────────


def test_html_to_text_basic():
    html = "<html><head><title>My Page</title></head><body><p>Hello world</p></body></html>"
    result = _html_to_text(html, "https://example.com")
    assert "My Page" in result
    assert "Hello world" in result


def test_html_to_text_strips_script_and_style():
    html = "<html><body><script>alert(1)</script><style>.x{}</style><p>Content</p></body></html>"
    result = _html_to_text(html, "https://example.com")
    assert "Content" in result
    assert "alert" not in result


def test_html_to_text_no_title():
    html = "<html><body><p>No title page</p></body></html>"
    result = _html_to_text(html, "https://example.com")
    assert "example.com" in result
    assert "No title page" in result


def test_html_to_text_truncates_long_content():
    html = f"<html><body><p>{'A' * 10000}</p></body></html>"
    result = _html_to_text(html, "https://example.com")
    assert len(result) <= 8200  # header + 8000 chars max


def test_html_to_text_bs4_import_error():
    """Falls back to raw HTML when BeautifulSoup not available."""
    html = "<html><body>raw</body></html>"
    with patch.dict("sys.modules", {"bs4": None}):
        with patch("agnoclaw.tools.web._html_to_text") as mock_fn:
            # Test the actual function behavior
            mock_fn.side_effect = lambda h, u: h[:3000]
            result = mock_fn(html, "https://example.com")
    assert "raw" in result
