"""Tests for the SkillHub / ClawHub integration."""

import json
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
    policy = MagicMock()
    policy.validate_network_url.return_value = ()
    policy.resolve_network_host.return_value = ("93.184.216.34",)
    return ClawHubClient(
        base_url="https://test-clawhub.example.com",
        cache_dir=str(tmp_path / "cache"),
        network_policy=policy,
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
    mock_httpx_client.get.return_value = _mock_response(
        {
            "results": [
                {
                    "slug": "code-review",
                    "summary": "Automated code review",
                    "author": "community",
                    "version": "1.0.0",
                    "downloads": 500,
                    "categories": ["development"],
                    "emoji": "\U0001f50d",
                },
            ]
        }
    )

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
    mock_httpx_client.get.return_value = _mock_response(
        {
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
        }
    )

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
    provenance = json.loads((result / ".agnoclaw-source.json").read_text(encoding="utf-8"))
    assert provenance["source"] == "clawhub"
    assert len(provenance["archive_sha256"]) == 64


@pytest.mark.parametrize(
    "member_name",
    [
        "nested/../../../escaped.txt",
        "/tmp/agnoclaw-escaped.txt",
        "nested\\..\\escaped.txt",
        "C:/escaped.txt",
    ],
)
def test_download_rejects_archive_path_escape(
    hub_client,
    mock_httpx_client,
    tmp_path,
    member_name,
):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("SKILL.md", "---\nname: test-skill\n---\nBody")
        zf.writestr(member_name, "owned")

    resp = MagicMock()
    resp.headers = {"content-type": "application/zip"}
    resp.content = buf.getvalue()
    resp.raise_for_status = MagicMock()
    mock_httpx_client.get.return_value = resp

    dest = tmp_path / "skills"
    result = hub_client.download("test-skill", dest)

    assert result is None
    assert not (dest / "test-skill").exists()
    assert not (tmp_path / "escaped.txt").exists()


def test_download_refuses_to_merge_existing_skill(hub_client, mock_httpx_client, tmp_path):
    dest = tmp_path / "skills"
    existing = dest / "test-skill"
    existing.mkdir(parents=True)
    marker = existing / "keep.txt"
    marker.write_text("original", encoding="utf-8")

    result = hub_client.download("test-skill", dest)

    assert result is None
    assert marker.read_text(encoding="utf-8") == "original"
    mock_httpx_client.get.assert_called_once()


def test_categories(hub_client, mock_httpx_client):
    mock_httpx_client.get.return_value = _mock_response(
        {
            "categories": ["development", "research", "devops", "writing"],
        }
    )

    cats = hub_client.categories()
    assert "development" in cats
    assert "research" in cats


def test_hub_redirect_to_private_network_is_blocked_before_second_request(
    mock_httpx_client,
    monkeypatch,
    tmp_path,
):
    import socket

    from agnoclaw.runtime.guardrails import RuntimeGuardrails

    monkeypatch.setattr(
        "agnoclaw.runtime.guardrails.socket.getaddrinfo",
        lambda _host, port, **_kwargs: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
        ],
    )
    redirect = _mock_response({}, status_code=302)
    redirect.headers["location"] = "http://169.254.169.254/latest/meta-data"
    redirect.request.url = "https://test-clawhub.example.com/api/search?q=agent"
    mock_httpx_client.get.return_value = redirect
    client = ClawHubClient(
        base_url="https://test-clawhub.example.com",
        cache_dir=str(tmp_path / "cache"),
        network_policy=RuntimeGuardrails(workspace_dir=tmp_path, path_enabled=False),
    )

    result = client.search("agent")

    assert result == []
    assert mock_httpx_client.get.call_count == 1


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

    marker = tmp_path / "community-inline-command-ran"
    skill_content = (
        "---\nname: hub-skill\ndescription: From ClawHub\n---\n\n"
        f"# Hub Skill\nHello.\n!`touch {marker}`"
    )

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
            network_policy=MagicMock(
                validate_network_url=MagicMock(return_value=()),
                resolve_network_host=MagicMock(return_value=("93.184.216.34",)),
            ),
        )

    assert result is not None
    assert (result / "SKILL.md").exists()
    assert result.parent == skills_dir / ".community"

    # The skill is explicitly loadable, but remains community provenance.
    content = registry.load_skill("hub-skill")
    assert content is not None
    assert "Hub Skill" in content
    assert "!`touch" in content
    assert not marker.exists()
    skill = registry._get_skill("hub-skill")
    assert skill is not None
    assert registry._trust_level(skill) == "community"
    assert "hub-skill" not in registry.get_skill_descriptions()

    reopened = SkillRegistry(workspace_skills_dir=skills_dir)
    reopened_record = next(
        item for item in reopened.list_skills() if item["name"] == "hub-skill"
    )
    assert reopened_record["trust"] == "community"
    assert reopened_record["model_invocable"] is False
    assert "!`touch" in (reopened.load_skill("hub-skill") or "")
    assert not marker.exists()


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
