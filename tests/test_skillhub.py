"""Tests for the SkillHub / ClawHub integration."""

from unittest.mock import MagicMock, patch

import pytest

from agnoclaw.skills.hub import ClawHubClient
from agnoclaw.skills.registry import SkillRegistry


# ── ClawHubClient tests ─────────────────────────────────────────────────


@pytest.fixture
def mock_httpx_client():
    """Mock httpx.Client for testing without network."""
    with patch("agnoclaw.skills.hub.httpx.Client") as mock_cls:
        mock_instance = MagicMock()
        mock_cls.return_value = mock_instance
        yield mock_instance


@pytest.fixture
def hub_client(mock_httpx_client, tmp_path):
    """ClawHubClient with mocked HTTP and temp cache."""
    return ClawHubClient(
        base_url="https://test-clawhub.example.com",
        cache_dir=str(tmp_path / "cache"),
    )


def _mock_response(data, status_code=200, content_type="application/json"):
    """Create a mock httpx response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"content-type": content_type}
    resp.json.return_value = data
    resp.text = str(data)
    resp.raise_for_status = MagicMock()
    return resp


def test_search_returns_skill_info(hub_client, mock_httpx_client):
    # Mock matches real ClawHub /api/search response (uses slug/summary)
    mock_httpx_client.get.return_value = _mock_response({
        "results": [
            {
                "slug": "code-review",
                "summary": "Automated code review",
                "author": "community",
                "version": "1.0.0",
                "downloads": 500,
                "categories": ["development"],
                "emoji": "\U0001F50D",
            },
        ]
    })

    results = hub_client.search("code review")

    assert len(results) == 1
    assert results[0].name == "code-review"
    assert results[0].description == "Automated code review"
    assert results[0].downloads == 500


def test_search_empty_results(hub_client, mock_httpx_client):
    mock_httpx_client.get.return_value = _mock_response({"results": []})
    results = hub_client.search("nonexistent")
    assert results == []


def test_inspect_returns_detail(hub_client, mock_httpx_client):
    # Mock matches real ClawHub API response shape: nested skill/latestVersion/owner
    mock_httpx_client.get.return_value = _mock_response({
        "skill": {
            "slug": "coding-agent",
            "summary": "Autonomous coding agent",
            "categories": ["development", "automation"],
            "homepage": "https://clawhub.ai/skills/coding-agent",
            "repository": "https://github.com/clawhub/coding-agent",
            "dependencies": ["httpx", "git"],
            "stats": {"downloads": 1000},
            "tags": {"latest": "2.0.0"},
        },
        "latestVersion": {
            "version": "2.0.0",
        },
        "owner": {
            "handle": "clawhub",
        },
    })

    detail = hub_client.inspect("coding-agent")

    assert detail is not None
    assert detail.name == "coding-agent"
    assert detail.version == "2.0.0"
    assert detail.author == "clawhub"
    assert "development" in detail.categories


def test_inspect_not_found(hub_client, mock_httpx_client):
    import httpx
    resp = MagicMock()
    resp.status_code = 404
    mock_httpx_client.get.side_effect = httpx.HTTPStatusError(
        "Not Found", request=MagicMock(), response=resp
    )
    detail = hub_client.inspect("nonexistent")
    assert detail is None


def test_download_creates_skill_dir(hub_client, mock_httpx_client, tmp_path):
    import io
    import zipfile

    skill_content = "---\nname: test-skill\ndescription: A test\n---\n\n# Test Skill\nDo things."

    # Build a ZIP in memory (matches real ClawHub /api/download response)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", skill_content)
    buf.seek(0)

    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"content-type": "application/zip"}
    resp.content = buf.getvalue()
    resp.raise_for_status = MagicMock()
    mock_httpx_client.get.return_value = resp

    dest = tmp_path / "skills"
    dest.mkdir()
    result = hub_client.download("test-skill", dest)

    assert result is not None
    assert result.name == "test-skill"
    assert (result / "SKILL.md").exists()
    assert "# Test Skill" in (result / "SKILL.md").read_text()


def test_categories(hub_client, mock_httpx_client):
    mock_httpx_client.get.return_value = _mock_response({
        "categories": ["development", "research", "devops", "writing"],
    })

    cats = hub_client.categories()
    assert "development" in cats
    assert "research" in cats


# ── Cache tests ─────────────────────────────────────────────────────────


def test_cache_write_and_read(hub_client, mock_httpx_client):
    """Second request should use cache, not HTTP."""
    mock_httpx_client.get.return_value = _mock_response({"results": [{"name": "cached-skill"}]})

    # First call — hits HTTP
    hub_client.search("test")
    assert mock_httpx_client.get.call_count == 1

    # Second call — should use cache
    hub_client.search("test")
    assert mock_httpx_client.get.call_count == 1  # no additional HTTP call


# ── Registry integration ────────────────────────────────────────────────


def test_install_from_hub(tmp_path):
    """install_from_hub should download and make the skill available."""
    import io
    import zipfile

    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    registry = SkillRegistry(workspace_skills_dir=skills_dir)

    skill_content = "---\nname: hub-skill\ndescription: From ClawHub\n---\n\n# Hub Skill\nHello."

    # Build a ZIP in memory (matches real ClawHub /api/download response)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", skill_content)
    buf.seek(0)

    with patch("agnoclaw.skills.hub.httpx.Client") as mock_cls:
        mock_client = MagicMock()
        mock_cls.return_value = mock_client

        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "application/zip"}
        resp.content = buf.getvalue()
        resp.raise_for_status = MagicMock()
        mock_client.get.return_value = resp

        result = registry.install_from_hub(
            "hub-skill",
            hub_url="https://test.example.com",
            cache_dir=str(tmp_path / "cache"),
        )

    assert result is not None
    assert (result / "SKILL.md").exists()

    # Verify the skill is loadable
    content = registry.load_skill("hub-skill")
    assert content is not None
    assert "Hub Skill" in content


# ── Bundled skillhub skill ──────────────────────────────────────────────


def test_bundled_skillhub_discoverable():
    """The skillhub skill should be discoverable from bundled skills."""
    registry = SkillRegistry()
    skills = registry.discover_all()
    names = [s.name for s in skills]
    assert "skillhub" in names, f"skillhub not found in: {names}"


def test_bundled_contract_analyzer_discoverable():
    """The contract-analyzer skill should be discoverable."""
    registry = SkillRegistry()
    skills = registry.discover_all()
    names = [s.name for s in skills]
    assert "contract-analyzer" in names, f"contract-analyzer not found in: {names}"
