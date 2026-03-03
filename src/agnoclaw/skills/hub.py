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

        Uses ClawHub's vector search endpoint (/api/search) for relevance-ranked results.

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

        data = self._get("/api/search", params=params)
        if not data:
            return []

        results = data if isinstance(data, list) else data.get("results", data.get("items", []))
        return [self._parse_skill_info(item) for item in results]

    def inspect(self, name: str) -> Optional[HubSkillDetail]:
        """
        Get full detail for a skill by name/slug.

        Args:
            name: Skill slug (e.g., "code", "sensitive-data-masker").

        Returns:
            Full skill detail, or None if not found.
        """
        data = self._get(f"/api/v1/skills/{name}")
        if not data:
            return None
        return self._parse_skill_detail(data)

    def download(self, name: str, dest_dir: str | Path, version: str = "") -> Optional[Path]:
        """
        Download a skill as a ZIP and extract to a local directory.

        Creates dest_dir/name/ with all skill files (SKILL.md + auxiliary files).

        Args:
            name: Skill slug to download.
            dest_dir: Parent directory where the skill subdirectory will be created.
            version: Optional version to download. Defaults to latest.

        Returns:
            Path to the created skill directory, or None on failure.
        """
        import io
        import zipfile

        dest = Path(dest_dir).expanduser().resolve()

        params = {"slug": name}
        if version:
            params["version"] = version

        url = f"{self._base_url}/api/download"
        try:
            response = self._client.get(url, params=params)
            response.raise_for_status()
        except httpx.HTTPError as e:
            logger.warning("Failed to download skill '%s': %s", name, e)
            return None

        content_type = response.headers.get("content-type", "")
        if "zip" not in content_type and "octet" not in content_type:
            logger.warning("Unexpected content-type for skill '%s': %s", name, content_type)
            return None

        skill_dir = dest / name
        skill_dir.mkdir(parents=True, exist_ok=True)

        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
                for file_info in zf.infolist():
                    # Skip directories and hidden files
                    if file_info.is_dir() or file_info.filename.startswith("."):
                        continue
                    target = skill_dir / file_info.filename
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(zf.read(file_info.filename))
                    logger.debug("Extracted %s", target)
        except zipfile.BadZipFile as e:
            logger.warning("Invalid ZIP for skill '%s': %s", name, e)
            return None

        logger.info("Downloaded skill '%s' (%d files) to %s", name, len(list(skill_dir.iterdir())), skill_dir)
        return skill_dir

    def categories(self) -> list[str]:
        """
        List all available skill categories.

        Note: ClawHub currently does not expose a categories endpoint.
        This method returns an empty list until the API adds support.

        Returns:
            List of category names (currently empty).
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
        """Parse a skill info from search results or listing.

        ClawHub API uses: slug, displayName, summary, score, updatedAt
        """
        return HubSkillInfo(
            name=data.get("slug", data.get("name", "")),
            description=data.get("summary", data.get("description", "")),
            author=data.get("author", ""),
            version=data.get("version", ""),
            downloads=data.get("downloads", 0),
            categories=data.get("categories", []),
            emoji=data.get("emoji", ""),
        )

    @staticmethod
    def _parse_skill_detail(data: dict) -> HubSkillDetail:
        """Parse full skill detail from /api/v1/skills/<slug>.

        ClawHub API wraps in: {"skill": {...}, "latestVersion": {...}, "owner": {...}}
        """
        skill = data.get("skill", data)
        latest = data.get("latestVersion", {})
        owner = data.get("owner", {})
        stats = skill.get("stats", {})
        tags = skill.get("tags", {})

        return HubSkillDetail(
            name=skill.get("slug", skill.get("name", "")),
            description=skill.get("summary", skill.get("description", "")),
            author=owner.get("handle", owner.get("displayName", "")),
            version=tags.get("latest", latest.get("version", "")),
            downloads=stats.get("downloads", 0),
            categories=skill.get("categories", []),
            emoji=skill.get("emoji", ""),
            homepage=skill.get("homepage", ""),
            repository=skill.get("repository", ""),
            readme=latest.get("changelog", ""),
            skill_md_preview="",
            dependencies=skill.get("dependencies", []),
            created_at=str(skill.get("createdAt", "")),
            updated_at=str(skill.get("updatedAt", "")),
        )

    def close(self) -> None:
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
