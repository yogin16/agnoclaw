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

import hashlib
import json
import logging
import os
import re
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urljoin
from zipfile import ZipInfo

import httpx

from agnoclaw.runtime.network import (
    NetworkPolicyError,
    NetworkURLPolicy,
    PinnedHTTPTransport,
    require_allowed_network_url,
)

logger = logging.getLogger("agnoclaw.skills.hub")

DEFAULT_CLAWHUB_URL = "https://clawhub.ai"
DEFAULT_CACHE_DIR = "~/.agnoclaw/cache/hub"
CACHE_TTL_SECONDS = 3600  # 1 hour
MAX_SKILL_ARCHIVE_BYTES = 10 * 1024 * 1024
MAX_SKILL_FILES = 256
MAX_SKILL_FILE_BYTES = 2 * 1024 * 1024
MAX_SKILL_EXPANDED_BYTES = 16 * 1024 * 1024
MAX_HUB_REDIRECTS = 5
_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
_SKILL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def _validated_archive_member(file_info: ZipInfo) -> PurePosixPath | None:
    """Return one safe relative member path or reject the entire archive."""
    filename = file_info.filename
    if not filename or "\x00" in filename or "\\" in filename:
        raise ValueError(f"invalid archive member name: {filename!r}")
    raw_parts = filename.split("/")
    if any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError(f"ambiguous archive member path: {filename!r}")
    if any(":" in part for part in raw_parts):
        raise ValueError(f"archive member contains a drive or stream marker: {filename!r}")

    member = PurePosixPath(filename)
    if member.is_absolute():
        raise ValueError(f"absolute archive member path: {filename!r}")
    if any(part.startswith(".") for part in member.parts):
        return None
    if file_info.flag_bits & 0x1:
        raise ValueError(f"encrypted archive member is unsupported: {filename!r}")

    file_type = stat.S_IFMT(file_info.external_attr >> 16)
    if file_type not in {0, stat.S_IFREG}:
        raise ValueError(f"link or special archive member is forbidden: {filename!r}")
    return member


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
        *,
        network_policy: NetworkURLPolicy | None = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._cache_dir = Path(cache_dir).expanduser().resolve()
        self._timeout = timeout
        if network_policy is None:
            from agnoclaw.runtime.guardrails import RuntimeGuardrails

            network_policy = RuntimeGuardrails(
                workspace_dir=self._cache_dir,
                path_enabled=False,
            )
        self._network_policy = network_policy
        self._client = httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            transport=PinnedHTTPTransport(network_policy),
        )

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

        if isinstance(data, dict):
            results = data.get("results", data.get("items", []))
        elif isinstance(data, list):
            results = data
        else:
            return []
        if not isinstance(results, list):
            return []
        return [self._parse_skill_info(item) for item in results if isinstance(item, dict)]

    def inspect(self, name: str) -> HubSkillDetail | None:
        """
        Get full detail for a skill by name/slug.

        Args:
            name: Skill slug (e.g., "code", "sensitive-data-masker").

        Returns:
            Full skill detail, or None if not found.
        """
        data = self._get(f"/api/v1/skills/{name}")
        if not isinstance(data, dict):
            return None
        return self._parse_skill_detail(data)

    def download(self, name: str, dest_dir: str | Path, version: str = "") -> Path | None:
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
        if not _SKILL_NAME_RE.fullmatch(name) or name in {".", ".."}:
            logger.warning("Refusing invalid ClawHub skill name: %r", name)
            return None

        params = {"slug": name}
        if version:
            params["version"] = version

        url = f"{self._base_url}/api/download"
        try:
            response = self._safe_get(url, params=params)
            response.raise_for_status()
        except (httpx.HTTPError, NetworkPolicyError) as e:
            logger.warning("Failed to download skill '%s': %s", name, e)
            return None

        content_type = response.headers.get("content-type", "")
        if "zip" not in content_type and "octet" not in content_type:
            logger.warning("Unexpected content-type for skill '%s': %s", name, content_type)
            return None
        if len(response.content) > MAX_SKILL_ARCHIVE_BYTES:
            logger.warning(
                "ClawHub archive for '%s' exceeds the %d-byte compressed limit",
                name,
                MAX_SKILL_ARCHIVE_BYTES,
            )
            return None

        skill_dir = dest / name
        if skill_dir.exists():
            logger.warning("Refusing to merge ClawHub skill into existing directory: %s", skill_dir)
            return None
        dest.mkdir(parents=True, exist_ok=True)
        staging_dir = Path(tempfile.mkdtemp(prefix=".agnoclaw-skill-", dir=dest))

        try:
            with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
                file_infos = [item for item in zf.infolist() if not item.is_dir()]
                if len(file_infos) > MAX_SKILL_FILES:
                    raise ValueError(
                        f"archive contains {len(file_infos)} files; limit is {MAX_SKILL_FILES}"
                    )
                expanded_bytes = sum(item.file_size for item in file_infos)
                if expanded_bytes > MAX_SKILL_EXPANDED_BYTES:
                    raise ValueError(
                        f"archive expands to {expanded_bytes} bytes; "
                        f"limit is {MAX_SKILL_EXPANDED_BYTES}"
                    )

                extracted = 0
                for file_info in file_infos:
                    member = _validated_archive_member(file_info)
                    if member is None:
                        continue
                    if file_info.file_size > MAX_SKILL_FILE_BYTES:
                        raise ValueError(
                            f"archive member {file_info.filename!r} exceeds "
                            f"the {MAX_SKILL_FILE_BYTES}-byte file limit"
                        )
                    target = (staging_dir / Path(*member.parts)).resolve()
                    if not target.is_relative_to(staging_dir):
                        raise ValueError(
                            f"archive member escapes the skill directory: {file_info.filename!r}"
                        )
                    target.parent.mkdir(parents=True, exist_ok=True)
                    payload = zf.read(file_info)
                    if len(payload) != file_info.file_size:
                        raise ValueError(f"archive member size mismatch: {file_info.filename!r}")
                    target.write_bytes(payload)
                    extracted += 1
                    logger.debug("Extracted %s", target)

            if extracted == 0 or not (staging_dir / "SKILL.md").is_file():
                raise ValueError("archive must contain a root SKILL.md")

            provenance = {
                "schema_version": 1,
                "source": "clawhub",
                "registry_url": self._base_url,
                "requested_name": name,
                "requested_version": version or None,
                "archive_sha256": hashlib.sha256(response.content).hexdigest(),
            }
            (staging_dir / ".agnoclaw-source.json").write_text(
                json.dumps(provenance, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(staging_dir, skill_dir)
        except (OSError, ValueError, zipfile.BadZipFile, zipfile.LargeZipFile) as e:
            logger.warning("Invalid ClawHub archive for skill '%s': %s", name, e)
            return None
        finally:
            if staging_dir.exists():
                shutil.rmtree(staging_dir, ignore_errors=True)

        logger.info(
            "Downloaded skill '%s' (%d files) to %s",
            name,
            sum(1 for path in skill_dir.rglob("*") if path.is_file()),
            skill_dir,
        )
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
            return [item for item in data if isinstance(item, str)]
        if not isinstance(data, dict):
            return []
        categories = data.get("categories", [])
        if not isinstance(categories, list):
            return []
        return [item for item in categories if isinstance(item, str)]

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
            response = self._safe_get(url, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                logger.debug("ClawHub 404: %s", url)
                return None
            logger.warning("ClawHub HTTP error: %s %s", e.response.status_code, url)
            return None
        except (httpx.HTTPError, NetworkPolicyError) as e:
            logger.warning("ClawHub request failed: %s", e)
            return None

        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            data = response.json()
        else:
            data = response.text

        self._write_cache(path, params, data)
        return data

    def _safe_get(self, url: str, *, params: dict | None = None) -> httpx.Response:
        """GET with request-time DNS pinning and validation of every redirect."""
        current_url = url
        current_params = params
        for redirect_count in range(MAX_HUB_REDIRECTS + 1):
            require_allowed_network_url(
                self._network_policy,
                current_url,
                tool_name="clawhub",
                arg_key="url",
            )
            response = self._client.get(current_url, params=current_params)
            current_params = None
            if response.status_code not in _REDIRECT_STATUS_CODES:
                return response
            location = response.headers.get("location")
            if not location:
                raise NetworkPolicyError("redirect response omitted Location")
            if redirect_count == MAX_HUB_REDIRECTS:
                raise NetworkPolicyError(
                    f"redirect limit exceeded ({MAX_HUB_REDIRECTS})"
                )
            request_url = getattr(getattr(response, "request", None), "url", current_url)
            next_url = urljoin(str(request_url), location)
            require_allowed_network_url(
                self._network_policy,
                next_url,
                tool_name="clawhub",
                arg_key="url",
            )
            current_url = next_url
        raise NetworkPolicyError("redirect handling did not settle")  # pragma: no cover

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
