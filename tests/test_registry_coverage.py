"""Additional coverage tests for SkillRegistry — gates, trust, install commands."""

import sys
from unittest.mock import MagicMock, patch

from agnoclaw.skills.registry import SkillRegistry


# ── _build_install_cmd tests ────────────────────────────────────────────


def test_build_install_cmd_uv():
    cmd = SkillRegistry._build_install_cmd("uv", "some-package")
    assert cmd == [sys.executable, "-m", "uv", "pip", "install", "some-package"]


def test_build_install_cmd_pip():
    cmd = SkillRegistry._build_install_cmd("pip", "some-package")
    assert cmd == [sys.executable, "-m", "pip", "install", "--quiet", "some-package"]


def test_build_install_cmd_brew():
    cmd = SkillRegistry._build_install_cmd("brew", "some-package")
    assert cmd == ["brew", "install", "some-package"]


def test_build_install_cmd_go():
    cmd = SkillRegistry._build_install_cmd("go", "github.com/user/tool@latest")
    assert cmd == ["go", "install", "github.com/user/tool@latest"]


def test_build_install_cmd_npm():
    cmd = SkillRegistry._build_install_cmd("npm", "some-package")
    assert cmd == ["npm", "install", "-g", "some-package"]


def test_build_install_cmd_unknown():
    cmd = SkillRegistry._build_install_cmd("cargo", "some-crate")
    assert cmd is None


# ── _passes_gates tests ─────────────────────────────────────────────────


def _mock_skill(
    *,
    always=False,
    os_platforms=None,
    requires_bins=None,
    requires_any_bins=None,
    requires_env=None,
):
    skill = MagicMock()
    skill.meta.always = always
    skill.meta.os_platforms = os_platforms or []
    skill.meta.requires_bins = requires_bins or []
    skill.meta.requires_any_bins = requires_any_bins or []
    skill.meta.requires_env = requires_env or []
    return skill


def test_passes_gates_always_true():
    reg = SkillRegistry.__new__(SkillRegistry)
    skill = _mock_skill(always=True)
    assert reg._passes_gates(skill) is True


def test_passes_gates_wrong_os():
    reg = SkillRegistry.__new__(SkillRegistry)
    skill = _mock_skill(os_platforms=["win32"])  # not darwin/linux

    with patch("platform.system", return_value="Darwin"):
        assert reg._passes_gates(skill) is False


def test_passes_gates_correct_os():
    reg = SkillRegistry.__new__(SkillRegistry)
    skill = _mock_skill(os_platforms=["darwin"])

    with patch("platform.system", return_value="Darwin"):
        assert reg._passes_gates(skill) is True


def test_passes_gates_missing_required_bin():
    reg = SkillRegistry.__new__(SkillRegistry)
    skill = _mock_skill(requires_bins=["nonexistent_binary_xyz"])

    with patch("shutil.which", return_value=None):
        assert reg._passes_gates(skill) is False


def test_passes_gates_has_required_bin():
    reg = SkillRegistry.__new__(SkillRegistry)
    skill = _mock_skill(requires_bins=["python3"])

    with patch("shutil.which", return_value="/usr/bin/python3"):
        assert reg._passes_gates(skill) is True


def test_passes_gates_any_bins_none_found():
    reg = SkillRegistry.__new__(SkillRegistry)
    skill = _mock_skill(requires_any_bins=["bin_a", "bin_b"])

    with patch("shutil.which", return_value=None):
        assert reg._passes_gates(skill) is False


def test_passes_gates_any_bins_one_found():
    reg = SkillRegistry.__new__(SkillRegistry)
    skill = _mock_skill(requires_any_bins=["bin_a", "bin_b"])

    def which_side_effect(name):
        return "/usr/bin/bin_b" if name == "bin_b" else None

    with patch("shutil.which", side_effect=which_side_effect):
        assert reg._passes_gates(skill) is True


def test_passes_gates_missing_env_var():
    reg = SkillRegistry.__new__(SkillRegistry)
    skill = _mock_skill(requires_env=["NONEXISTENT_VAR_XYZ"])

    with patch.dict("os.environ", {}, clear=True):
        assert reg._passes_gates(skill) is False


def test_passes_gates_has_env_var():
    reg = SkillRegistry.__new__(SkillRegistry)
    skill = _mock_skill(requires_env=["MY_API_KEY"])

    with patch.dict("os.environ", {"MY_API_KEY": "secret"}):
        assert reg._passes_gates(skill) is True


# ── _find_bundled_skills_dir tests ──────────────────────────────────────


def test_find_bundled_skills_dir_none_found():
    with patch("pathlib.Path.exists", return_value=False):
        result = SkillRegistry._find_bundled_skills_dir()
    assert result is None


# ── _needs_install tests ────────────────────────────────────────────────


def _mock_installer(itype, pkg):
    """Create a mock SkillInstaller with type and package."""
    from agnoclaw.skills.loader import SkillInstaller

    return SkillInstaller(type=itype, package=pkg)


def _make_registry():
    """Create a bare SkillRegistry instance with required attributes."""
    reg = SkillRegistry.__new__(SkillRegistry)
    reg._dirs = []
    reg._local_dirs = []
    reg._cache = {}
    return reg


def test_needs_install_pip_not_installed():
    reg = _make_registry()
    installer = _mock_installer("pip", "nonexistent_pkg")
    with patch("importlib.metadata.distribution", side_effect=Exception("not found")):
        assert reg._needs_install(installer) is True


def test_needs_install_pip_installed():
    reg = _make_registry()
    installer = _mock_installer("pip", "some_pkg")
    with patch("importlib.metadata.distribution"):
        assert reg._needs_install(installer) is False


def test_needs_install_uv_with_extras():
    reg = _make_registry()
    installer = _mock_installer("uv", "some_pkg[extra]>=1.0")
    with patch("importlib.metadata.distribution"):
        assert reg._needs_install(installer) is False


def test_needs_install_brew_not_found():
    reg = _make_registry()
    installer = _mock_installer("brew", "some_binary")
    with patch("shutil.which", return_value=None):
        assert reg._needs_install(installer) is True


def test_needs_install_brew_found():
    reg = _make_registry()
    installer = _mock_installer("brew", "some_binary")
    with patch("shutil.which", return_value="/usr/local/bin/some_binary"):
        assert reg._needs_install(installer) is False


def test_needs_install_npm_not_found():
    reg = _make_registry()
    installer = _mock_installer("npm", "some_pkg")
    with patch("shutil.which", return_value=None):
        assert reg._needs_install(installer) is True


def test_needs_install_go_not_found():
    reg = _make_registry()
    installer = _mock_installer("go", "github.com/user/tool")
    with patch("shutil.which", return_value=None):
        assert reg._needs_install(installer) is True


def test_needs_install_unknown_type():
    reg = _make_registry()
    installer = _mock_installer("unknown_installer", "pkg")
    assert reg._needs_install(installer) is True


# ── discover_all skip nonexistent dir ───────────────────────────────────


def test_discover_all_skips_nonexistent_dirs(tmp_path):
    reg = _make_registry()
    reg._dirs = [tmp_path / "does_not_exist"]

    skills = reg.discover_all()
    assert skills == []


# ── add_directory with trust ────────────────────────────────────────────


def test_add_directory_with_local_trust(tmp_path):
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    reg = _make_registry()
    reg.add_directory(skills_dir, trust="local")
    assert any(d == skills_dir.resolve() for d in reg._dirs)
    assert any(d == skills_dir.resolve() for d in reg._local_dirs)
