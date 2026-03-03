"""
ClawHub client — HTTP client for the public ClawHub skill registry API.

ClawHub is the community skill registry for OpenClaw-compatible agents.
Skills published there follow the SKILL.md standard (YAML frontmatter + Markdown body)
which agnoclaw already fully supports.

This client enables:
  - Searching for skills by keyword or category
  - Inspecting skill metadata before installing
  - Downloading skills to the local workspace
  - Listing available categories

The API is public (no auth for reads). Metadata is cached locally
in ~/.agnoclaw/cache/hub/ to reduce network calls.

Usage:
    from agnoclaw.skills.hub import ClawHubClient

    client = ClawHubClient()
    results = client.search("code review")
    detail = client.inspect("coding-agent")
    path = client.download("coding-agent", dest_dir="~/.agnoclaw/workspace/skills")
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger("agnoclaw.skills.hub")

DEFAULT_CLAWHUB_URL = "https://clawhub.ai"
DEFAULT_CACHE_DIR = "~/.agnoclaw/cache/hub"
CACHE_TTL_SECONDS = 3600  # 1 hour


@dataclass
class HubSkillInfo:
    """Summary info returned from search results."""

    name: str
    description: str = ""
    author: str = ""
    version: str = ""
    downloads: int = 0
    categories: list[str] = field(default_factory=list)
    emoji: str = ""


@dataclass
class HubSkillDetail(HubSkillInfo):
    """Full detail for a single skill, including content preview."""

    homepage: str = ""
    repository: str = ""
    readme: str = ""
    skill_md_preview: str = ""
    dependencies: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


class ClawHubClient:
    """
    HTTP client for the public ClawHub skill registry.

    All reads are unauthenticated. Metadata is cached locally to reduce
    network round-trips.

    Args:
        base_url: ClawHub API base URL. Defaults to https://clawhub.ai.
        cache_dir: Local cache directory. Defaults to ~/.agnoclaw/cache/hub.
        timeout: HTTP request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str = DEFAULT_CLAWHUB_URL,
        cache_dir: str = DEFAULT_CACHE_DIR,
        timeout: float = 30.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._cache_dir = Path(cache_dir).expanduser().resolve()
        self._timeout = timeout
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)

    def search(self, query: str, category: str = "", limit: int = 20) -> list[HubSkillInfo]:
        """
        Search for skills by keyword.

        Args:
            query: Search query string.
            category: Optional category filter.
            limit: Maximum results to return.

        Returns:
            List of matching skill summaries.
        """
        params = {"q": query, "limit": limit}
        if category:
            params["category"] = category

        data = self._get("/api/v1/skills", params=params)
        if not data:
            return []

        results = data if isinstance(data, list) else data.get("results", data.get("skills", []))
        return [self._parse_skill_info(item) for item in results]

    def inspect(self, name: str) -> Optional[HubSkillDetail]:
        """
        Get full detail for a skill by name.

        Args:
            name: Skill name (e.g., "coding-agent").

        Returns:
            Full skill detail, or None if not found.
        """
        data = self._get(f"/api/v1/skills/{name}")
        if not data:
            return None
        return self._parse_skill_detail(data)

    def download(self, name: str, dest_dir: str | Path) -> Optional[Path]:
        """
        Download a skill's SKILL.md to a local directory.

        Creates dest_dir/name/SKILL.md with the skill content.

        Args:
            name: Skill name to download.
            dest_dir: Parent directory where the skill subdirectory will be created.

        Returns:
            Path to the created skill directory, or None on failure.
        """
        dest = Path(dest_dir).expanduser().resolve()

        # First try the download endpoint
        data = self._get(f"/api/v1/skills/{name}/download")
        if not data:
            # Fallback: inspect and use the skill_md_preview
            detail = self.inspect(name)
            if not detail or not detail.skill_md_preview:
                logger.warning("Could not download skill '%s': no content available", name)
                return None
            content = detail.skill_md_preview
        else:
            content = data if isinstance(data, str) else data.get("content", data.get("skill_md", ""))

        if not content:
            logger.warning("Downloaded empty content for skill '%s'", name)
            return None

        skill_dir = dest / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_md_path = skill_dir / "SKILL.md"
        skill_md_path.write_text(content, encoding="utf-8")

        logger.info("Downloaded skill '%s' to %s", name, skill_dir)
        return skill_dir

    def categories(self) -> list[str]:
        """
        List all available skill categories.

        Returns:
            List of category names.
        """
        data = self._get("/api/v1/categories")
        if not data:
            return []
        if isinstance(data, list):
            return data
        return data.get("categories", [])

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _get(self, path: str, params: dict | None = None) -> dict | list | str | None:
        """
        Make a GET request with caching.

        Returns parsed JSON (dict/list) or raw text, or None on error.
        """
        url = f"{self._base_url}{path}"

        # Check cache
        cached = self._read_cache(path, params)
        if cached is not None:
            return cached

        try:
            response = self._client.get(url, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.debug("ClawHub 404: %s", url)
                return None
            logger.warning("ClawHub HTTP error: %s %s", e.response.status_code, url)
            return None
        except httpx.HTTPError as e:
            logger.warning("ClawHub request failed: %s", e)
            return None

        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            data = response.json()
        else:
            data = response.text

        self._write_cache(path, params, data)
        return data

    # ── Cache helpers ─────────────────────────────────────────────────────────

    def _cache_key(self, path: str, params: dict | None = None) -> str:
        """Generate a filesystem-safe cache key."""
        key = path.replace("/", "_").strip("_")
        if params:
            param_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            key += f"__{param_str}"
        # Sanitize
        key = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
        return key

    def _read_cache(self, path: str, params: dict | None = None) -> dict | list | str | None:
        """Read from cache if fresh enough."""
        cache_file = self._cache_dir / f"{self._cache_key(path, params)}.json"
        if not cache_file.exists():
            return None

        try:
            raw = json.loads(cache_file.read_text(encoding="utf-8"))
            if time.time() - raw.get("_ts", 0) > CACHE_TTL_SECONDS:
                return None  # stale
            return raw.get("data")
        except Exception:
            return None

    def _write_cache(self, path: str, params: dict | None, data) -> None:
        """Write to cache."""
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            cache_file = self._cache_dir / f"{self._cache_key(path, params)}.json"
            cache_file.write_text(
                json.dumps({"_ts": time.time(), "data": data}, default=str),
                encoding="utf-8",
            )
        except Exception as e:
            logger.debug("Cache write failed: %s", e)

    # ── Parsers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _parse_skill_info(data: dict) -> HubSkillInfo:
        return HubSkillInfo(
            name=data.get("name", ""),
            description=data.get("description", ""),
            author=data.get("author", ""),
            version=data.get("version", ""),
            downloads=data.get("downloads", 0),
            categories=data.get("categories", []),
            emoji=data.get("emoji", ""),
        )

    @staticmethod
    def _parse_skill_detail(data: dict) -> HubSkillDetail:
        return HubSkillDetail(
            name=data.get("name", ""),
            description=data.get("description", ""),
            author=data.get("author", ""),
            version=data.get("version", ""),
            downloads=data.get("downloads", 0),
            categories=data.get("categories", []),
            emoji=data.get("emoji", ""),
            homepage=data.get("homepage", ""),
            repository=data.get("repository", ""),
            readme=data.get("readme", ""),
            skill_md_preview=data.get("skill_md_preview", data.get("skill_md", "")),
            dependencies=data.get("dependencies", []),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
        )

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
