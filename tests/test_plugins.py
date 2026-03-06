"""Tests for the plugin system."""

from unittest.mock import MagicMock, patch
import types

import pytest

from agnoclaw.plugins import PluginLoader, PluginManifest


@pytest.fixture
def loader():
    return PluginLoader()


def test_plugin_manifest_defaults():
    """PluginManifest should have sensible defaults."""
    manifest = PluginManifest(name="test-plugin")
    assert manifest.name == "test-plugin"
    assert manifest.version == "0.0.0"
    assert manifest.tools == []
    assert manifest.skills_dirs == []
    assert manifest.pre_run_hooks == []
    assert manifest.post_run_hooks == []


def test_plugin_manifest_with_tools():
    """PluginManifest should accept tool instances."""
    mock_tool = MagicMock()
    manifest = PluginManifest(
        name="tool-plugin",
        version="1.0.0",
        tools=[mock_tool],
        skills_dirs=["/path/to/skills"],
    )
    assert len(manifest.tools) == 1
    assert len(manifest.skills_dirs) == 1


def test_load_from_path_valid(loader, tmp_path):
    """load_from_path should work with a valid plugin module."""
    # Create a fake module with agnoclaw_plugin()
    module = types.ModuleType("test_plugin")
    module.agnoclaw_plugin = lambda: PluginManifest(
        name="test-from-path",
        version="1.0.0",
        description="Test plugin",
    )

    with patch("agnoclaw.plugins.importlib.import_module", return_value=module):
        manifest = loader.load_from_path("test_plugin")

    assert manifest is not None
    assert manifest.name == "test-from-path"
    assert manifest.version == "1.0.0"
    assert "test-from-path" in loader.loaded_plugins


def test_load_from_path_missing_function(loader):
    """Module without agnoclaw_plugin() should return None."""
    module = types.ModuleType("bad_plugin")

    with patch("agnoclaw.plugins.importlib.import_module", return_value=module):
        manifest = loader.load_from_path("bad_plugin")

    assert manifest is None


def test_load_from_path_wrong_return_type(loader):
    """agnoclaw_plugin() returning wrong type should return None."""
    module = types.ModuleType("wrong_plugin")
    module.agnoclaw_plugin = lambda: {"name": "wrong"}  # dict, not PluginManifest

    with patch("agnoclaw.plugins.importlib.import_module", return_value=module):
        manifest = loader.load_from_path("wrong_plugin")

    assert manifest is None


def test_load_from_path_raises(loader):
    """agnoclaw_plugin() that raises should return None."""
    module = types.ModuleType("error_plugin")
    module.agnoclaw_plugin = lambda: (_ for _ in ()).throw(RuntimeError("boom"))

    with patch("agnoclaw.plugins.importlib.import_module", return_value=module):
        manifest = loader.load_from_path("error_plugin")

    assert manifest is None


def test_load_from_path_import_error(loader):
    """Non-importable module should return None."""
    with patch("agnoclaw.plugins.importlib.import_module", side_effect=ImportError("nope")):
        manifest = loader.load_from_path("nonexistent_module")

    assert manifest is None


def test_get_all_tools(loader):
    """get_all_tools should aggregate tools from all loaded plugins."""
    tool1 = MagicMock()
    tool2 = MagicMock()

    loader._loaded = {
        "p1": PluginManifest(name="p1", tools=[tool1]),
        "p2": PluginManifest(name="p2", tools=[tool2]),
    }

    tools = loader.get_all_tools()
    assert len(tools) == 2
    assert tool1 in tools
    assert tool2 in tools


def test_get_all_skills_dirs(loader):
    """get_all_skills_dirs should aggregate dirs from all loaded plugins."""
    loader._loaded = {
        "p1": PluginManifest(name="p1", skills_dirs=["/a"]),
        "p2": PluginManifest(name="p2", skills_dirs=["/b", "/c"]),
    }

    dirs = loader.get_all_skills_dirs()
    assert dirs == ["/a", "/b", "/c"]


def test_get_all_hooks(loader):
    """get_all_pre/post_run_hooks should aggregate from all plugins."""
    hook1 = MagicMock()
    hook2 = MagicMock()

    loader._loaded = {
        "p1": PluginManifest(name="p1", pre_run_hooks=[hook1]),
        "p2": PluginManifest(name="p2", post_run_hooks=[hook2]),
    }

    pre = loader.get_all_pre_run_hooks()
    post = loader.get_all_post_run_hooks()

    assert len(pre) == 1
    assert len(post) == 1


def test_discover_entry_points(loader):
    """discover should process entry points."""
    module = types.ModuleType("ep_plugin")
    module.agnoclaw_plugin = lambda: PluginManifest(
        name="entry-point-plugin",
        version="2.0.0",
    )

    mock_ep = MagicMock()
    mock_ep.name = "ep_plugin"
    mock_ep.load.return_value = module

    mock_eps = MagicMock()
    mock_eps.select.return_value = [mock_ep]

    with patch("importlib.metadata.entry_points", return_value=mock_eps):
        manifests = loader.discover()

    assert len(manifests) == 1
    assert manifests[0].name == "entry-point-plugin"
