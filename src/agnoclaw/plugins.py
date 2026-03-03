"""
Plugin system — Python entry-point-based plugin discovery.

Pythonic alternative to OpenClaw's JS plugin system. Plugins can provide:
  - Additional tools (Toolkit instances)
  - Skill directories
  - Pre/post-run hooks

Discovery methods:
  1. Python entry points (group: 'agnoclaw.plugins') — for pip-installed plugins
  2. Explicit module path — for local/dev plugins

Plugin contract:
  A plugin module must expose a function `agnoclaw_plugin()` that returns
  a PluginManifest describing the plugin's contributions.

Example plugin module:

    from agnoclaw.plugins import PluginManifest

    def agnoclaw_plugin() -> PluginManifest:
        from my_toolkit import MyToolkit
        return PluginManifest(
            name="my-plugin",
            version="1.0.0",
            tools=[MyToolkit()],
            skills_dirs=["path/to/my/skills"],
        )

Register as entry point in pyproject.toml:

    [project.entry-points."agnoclaw.plugins"]
    my-plugin = "my_package.plugin"

Usage:
    from agnoclaw.plugins import PluginLoader

    loader = PluginLoader()
    manifests = loader.discover()
    for m in manifests:
        print(f"Plugin: {m.name} — {len(m.tools)} tools, {len(m.skills_dirs)} skill dirs")
"""

from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("agnoclaw.plugins")


@dataclass
class PluginManifest:
    """
    Describes a plugin's contributions to the agent harness.

    Returned by a plugin module's `agnoclaw_plugin()` function.
    """

    name: str
    version: str = "0.0.0"
    description: str = ""

    # Tools to add to the agent's tool suite
    tools: list[Any] = field(default_factory=list)

    # Additional skill directories to scan
    skills_dirs: list[str] = field(default_factory=list)

    # Pre/post-run hooks
    pre_run_hooks: list[Callable] = field(default_factory=list)
    post_run_hooks: list[Callable] = field(default_factory=list)

    # Extra config overrides
    config_overrides: dict[str, Any] = field(default_factory=dict)


class PluginLoader:
    """
    Discovers and loads agnoclaw plugins.

    Searches two sources:
      1. Python entry points (group: 'agnoclaw.plugins')
      2. Explicit module paths (for local development)
    """

    ENTRY_POINT_GROUP = "agnoclaw.plugins"
    PLUGIN_FUNC_NAME = "agnoclaw_plugin"

    def __init__(self):
        self._loaded: dict[str, PluginManifest] = {}

    def discover(self) -> list[PluginManifest]:
        """
        Discover all plugins via entry points.

        Returns:
            List of PluginManifest from discovered plugins.
        """
        manifests = []

        try:
            from importlib.metadata import entry_points
        except ImportError:
            logger.debug("importlib.metadata not available, skipping entry point discovery")
            return manifests

        eps = entry_points()
        # Python 3.12+: eps.select(); 3.9-3.11: eps.get()
        if hasattr(eps, "select"):
            plugin_eps = eps.select(group=self.ENTRY_POINT_GROUP)
        else:
            plugin_eps = eps.get(self.ENTRY_POINT_GROUP, [])

        for ep in plugin_eps:
            try:
                module = ep.load()
                manifest = self._extract_manifest(module, ep.name)
                if manifest:
                    manifests.append(manifest)
                    self._loaded[manifest.name] = manifest
                    logger.info("Loaded plugin: %s v%s", manifest.name, manifest.version)
            except Exception as e:
                logger.warning("Failed to load plugin '%s': %s", ep.name, e)

        return manifests

    def load_from_path(self, module_path: str) -> Optional[PluginManifest]:
        """
        Load a plugin from an explicit Python module path.

        Args:
            module_path: Dotted module path (e.g., "my_package.plugin").

        Returns:
            PluginManifest if successful, None otherwise.
        """
        try:
            module = importlib.import_module(module_path)
            manifest = self._extract_manifest(module, module_path)
            if manifest:
                self._loaded[manifest.name] = manifest
                logger.info("Loaded plugin from path: %s v%s", manifest.name, manifest.version)
            return manifest
        except Exception as e:
            logger.warning("Failed to load plugin from '%s': %s", module_path, e)
            return None

    def _extract_manifest(self, module, source_name: str) -> Optional[PluginManifest]:
        """Extract PluginManifest from a loaded module."""
        func = getattr(module, self.PLUGIN_FUNC_NAME, None)
        if func is None:
            logger.warning(
                "Plugin '%s' has no %s() function", source_name, self.PLUGIN_FUNC_NAME
            )
            return None

        if not callable(func):
            logger.warning(
                "Plugin '%s': %s is not callable", source_name, self.PLUGIN_FUNC_NAME
            )
            return None

        try:
            manifest = func()
        except Exception as e:
            logger.warning("Plugin '%s': %s() raised: %s", source_name, self.PLUGIN_FUNC_NAME, e)
            return None

        if not isinstance(manifest, PluginManifest):
            logger.warning(
                "Plugin '%s': %s() returned %s, expected PluginManifest",
                source_name, self.PLUGIN_FUNC_NAME, type(manifest).__name__,
            )
            return None

        return manifest

    @property
    def loaded_plugins(self) -> dict[str, PluginManifest]:
        """Return all loaded plugins."""
        return dict(self._loaded)

    def get_all_tools(self) -> list:
        """Collect all tools from all loaded plugins."""
        tools = []
        for manifest in self._loaded.values():
            tools.extend(manifest.tools)
        return tools

    def get_all_skills_dirs(self) -> list[str]:
        """Collect all skill directories from all loaded plugins."""
        dirs = []
        for manifest in self._loaded.values():
            dirs.extend(manifest.skills_dirs)
        return dirs

    def get_all_pre_run_hooks(self) -> list[Callable]:
        """Collect all pre-run hooks from all loaded plugins."""
        hooks = []
        for manifest in self._loaded.values():
            hooks.extend(manifest.pre_run_hooks)
        return hooks

    def get_all_post_run_hooks(self) -> list[Callable]:
        """Collect all post-run hooks from all loaded plugins."""
        hooks = []
        for manifest in self._loaded.values():
            hooks.extend(manifest.post_run_hooks)
        return hooks
