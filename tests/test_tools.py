"""Tests for the tool suite (bash, files, web, tasks)."""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


# ── FilesToolkit tests ────────────────────────────────────────────────────


def test_files_toolkit_read_file(tmp_path):
    from agnoclaw.tools.files import FilesToolkit

    toolkit = FilesToolkit()
    test_file = tmp_path / "hello.txt"
    test_file.write_text("Hello, world!", encoding="utf-8")

    result = toolkit.read_file(str(test_file))
    assert "Hello, world!" in result


def test_files_toolkit_read_missing_file(tmp_path):
    from agnoclaw.tools.files import FilesToolkit

    toolkit = FilesToolkit()
    result = toolkit.read_file(str(tmp_path / "nonexistent.txt"))
    assert isinstance(result, str)
    # Should indicate an error or missing file
    assert len(result) > 0


def test_files_toolkit_write_and_read(tmp_path):
    from agnoclaw.tools.files import FilesToolkit

    toolkit = FilesToolkit()
    file_path = str(tmp_path / "output.txt")

    write_result = toolkit.write_file(file_path, "test content")
    assert isinstance(write_result, str)

    read_result = toolkit.read_file(file_path)
    assert "test content" in read_result


def test_files_toolkit_write_creates_parent_dirs(tmp_path):
    from agnoclaw.tools.files import FilesToolkit

    toolkit = FilesToolkit()
    nested_path = str(tmp_path / "a" / "b" / "c.txt")

    toolkit.write_file(nested_path, "nested")
    assert Path(nested_path).exists()


def test_files_toolkit_edit_file(tmp_path):
    from agnoclaw.tools.files import FilesToolkit

    toolkit = FilesToolkit()
    file_path = str(tmp_path / "edit_me.txt")
    Path(file_path).write_text("Hello world\nLine two\n", encoding="utf-8")

    result = toolkit.edit_file(file_path, "Hello world", "Goodbye world")
    content = Path(file_path).read_text(encoding="utf-8")
    assert "Goodbye world" in content
    assert "Hello world" not in content


def test_files_toolkit_edit_nonexistent(tmp_path):
    from agnoclaw.tools.files import FilesToolkit

    toolkit = FilesToolkit()
    result = toolkit.edit_file(str(tmp_path / "missing.txt"), "old", "new")
    assert isinstance(result, str)
    assert len(result) > 0  # should return some error message


def test_files_toolkit_edit_string_not_found(tmp_path):
    from agnoclaw.tools.files import FilesToolkit

    toolkit = FilesToolkit()
    file_path = str(tmp_path / "file.txt")
    Path(file_path).write_text("some content", encoding="utf-8")

    result = toolkit.edit_file(file_path, "NOT_PRESENT", "replacement")
    assert isinstance(result, str)
    assert len(result) > 0  # error message


def test_files_toolkit_glob_finds_py_files(tmp_path):
    from agnoclaw.tools.files import FilesToolkit

    toolkit = FilesToolkit()
    (tmp_path / "a.py").write_text("x=1")
    (tmp_path / "b.py").write_text("y=2")
    (tmp_path / "c.txt").write_text("text")

    result = toolkit.glob_files("*.py", base_dir=str(tmp_path))
    assert "a.py" in result or "b.py" in result
    assert "c.txt" not in result


def test_files_toolkit_grep_finds_content(tmp_path):
    from agnoclaw.tools.files import FilesToolkit

    toolkit = FilesToolkit()
    (tmp_path / "code.py").write_text("def hello():\n    print('hello')\n")
    (tmp_path / "other.py").write_text("x = 1\n")

    result = toolkit.grep_files("def hello", str(tmp_path))
    assert isinstance(result, str)
    assert "hello" in result


def test_files_toolkit_list_dir(tmp_path):
    from agnoclaw.tools.files import FilesToolkit

    toolkit = FilesToolkit()
    (tmp_path / "file1.py").write_text("")
    (tmp_path / "file2.txt").write_text("")
    (tmp_path / "subdir").mkdir()

    result = toolkit.list_dir(str(tmp_path))
    assert isinstance(result, str)
    assert "file1.py" in result or "file2.txt" in result


# ── BashTool tests ───────────────────────────────────────────────────────


def test_make_bash_tool_returns_agno_function():
    from agnoclaw.tools.bash import make_bash_tool

    bash = make_bash_tool()
    # Agno @tool returns a Function object (Pydantic model)
    assert hasattr(bash, "name")
    assert bash.name == "bash"


def test_make_bash_tool_has_entrypoint():
    from agnoclaw.tools.bash import make_bash_tool

    bash = make_bash_tool()
    assert hasattr(bash, "entrypoint")
    assert callable(bash.entrypoint)


def test_make_bash_tool_custom_timeout():
    from agnoclaw.tools.bash import make_bash_tool

    bash = make_bash_tool(timeout=30)
    assert bash is not None
    assert bash.name == "bash"


def test_bash_tool_runs_command():
    from agnoclaw.tools.bash import make_bash_tool

    bash = make_bash_tool(timeout=10)
    result = bash.entrypoint("echo hello")
    assert "hello" in result


def test_bash_tool_captures_stderr():
    from agnoclaw.tools.bash import make_bash_tool

    bash = make_bash_tool(timeout=10)
    result = bash.entrypoint("ls /nonexistent_path_xyz_agnoclaw")
    assert isinstance(result, str)
    assert len(result) > 0  # some error output


def test_bash_tool_exit_code_on_failure():
    from agnoclaw.tools.bash import make_bash_tool

    bash = make_bash_tool(timeout=10)
    result = bash.entrypoint("exit 42")
    assert "42" in result or "exit code" in result.lower()


# ── WebToolkit tests ─────────────────────────────────────────────────────


def test_web_toolkit_has_web_search():
    from agnoclaw.tools.web import WebToolkit

    toolkit = WebToolkit()
    assert hasattr(toolkit, "web_search")


def test_web_toolkit_has_web_fetch():
    from agnoclaw.tools.web import WebToolkit

    toolkit = WebToolkit()
    assert hasattr(toolkit, "web_fetch")


def test_web_toolkit_search_disabled_not_registered():
    """When search_enabled=False, web_search should not be registered as an Agno tool."""
    from agnoclaw.tools.web import WebToolkit

    toolkit = WebToolkit(search_enabled=False)
    # The method should not be in the registered functions
    registered_names = [f.name for f in toolkit.functions.values()] if hasattr(toolkit, "functions") else []
    assert "web_search" not in registered_names


def test_web_toolkit_fetch_disabled_not_registered():
    """When fetch_enabled=False, web_fetch should not be registered as an Agno tool."""
    from agnoclaw.tools.web import WebToolkit

    toolkit = WebToolkit(fetch_enabled=False)
    registered_names = [f.name for f in toolkit.functions.values()] if hasattr(toolkit, "functions") else []
    assert "web_fetch" not in registered_names


def test_web_toolkit_search_returns_string():
    from agnoclaw.tools.web import WebToolkit

    toolkit = WebToolkit()

    with patch("duckduckgo_search.DDGS") as mock_ddgs:
        mock_instance = MagicMock()
        mock_ddgs.return_value.__enter__ = MagicMock(return_value=mock_instance)
        mock_ddgs.return_value.__exit__ = MagicMock(return_value=False)
        mock_instance.text.return_value = [
            {"title": "Test Result", "href": "https://example.com", "body": "Test body"},
        ]
        result = toolkit.web_search("test query")
        assert isinstance(result, str)


def test_web_toolkit_fetch_returns_string():
    from agnoclaw.tools.web import WebToolkit

    toolkit = WebToolkit()

    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__ = MagicMock(return_value=mock_client)
        mock_client_cls.return_value.__exit__ = MagicMock(return_value=False)
        mock_response = MagicMock()
        mock_response.text = "<html><body><p>Hello world</p></body></html>"
        mock_response.headers = {"content-type": "text/html"}
        mock_response.raise_for_status = MagicMock()
        mock_client.get.return_value = mock_response
        result = toolkit.web_fetch("https://example.com")
        assert isinstance(result, str)


# ── TodoToolkit tests ────────────────────────────────────────────────────


def test_todo_toolkit_create():
    from agnoclaw.tools.tasks import TodoToolkit

    toolkit = TodoToolkit()
    result = toolkit.create_todo("Fix the bug")
    assert isinstance(result, str)
    assert "Fix the bug" in result


def test_todo_toolkit_list_empty():
    from agnoclaw.tools.tasks import TodoToolkit

    toolkit = TodoToolkit()
    result = toolkit.list_todos()
    assert isinstance(result, str)
    # Should indicate no todos
    assert "No todos" in result or "found" in result.lower()


def test_todo_toolkit_create_and_list():
    from agnoclaw.tools.tasks import TodoToolkit

    toolkit = TodoToolkit()
    toolkit.create_todo("Task A")
    toolkit.create_todo("Task B")
    result = toolkit.list_todos()
    assert "Task A" in result
    assert "Task B" in result


def test_todo_toolkit_update_status():
    from agnoclaw.tools.tasks import TodoToolkit

    toolkit = TodoToolkit()
    toolkit.create_todo("My task")
    # First todo should have id "1"
    result = toolkit.update_todo("1", "in_progress")
    assert "in_progress" in result or "1" in result


def test_todo_toolkit_update_invalid_status():
    from agnoclaw.tools.tasks import TodoToolkit

    toolkit = TodoToolkit()
    toolkit.create_todo("My task")
    result = toolkit.update_todo("1", "invalid_status")
    assert "error" in result.lower() or "invalid" in result.lower()


def test_todo_toolkit_delete():
    from agnoclaw.tools.tasks import TodoToolkit

    toolkit = TodoToolkit()
    toolkit.create_todo("Task to delete")
    result = toolkit.delete_todo("1")
    assert isinstance(result, str)
    # List should now be empty
    list_result = toolkit.list_todos()
    assert "Task to delete" not in list_result


def test_todo_toolkit_is_stateful_per_instance():
    """Each TodoToolkit instance has its own state."""
    from agnoclaw.tools.tasks import TodoToolkit

    t1 = TodoToolkit()
    t2 = TodoToolkit()

    t1.create_todo("Task in t1")
    result_t2 = t2.list_todos()

    # t2 should not see t1's tasks
    assert "Task in t1" not in result_t2


def test_todo_toolkit_update_nonexistent():
    from agnoclaw.tools.tasks import TodoToolkit

    toolkit = TodoToolkit()
    result = toolkit.update_todo("999", "completed")
    assert "error" in result.lower() or "not found" in result.lower()


def test_todo_toolkit_delete_nonexistent():
    from agnoclaw.tools.tasks import TodoToolkit

    toolkit = TodoToolkit()
    result = toolkit.delete_todo("999")
    assert "error" in result.lower() or "not found" in result.lower()


# ── get_default_tools tests ──────────────────────────────────────────────


def test_get_default_tools_returns_list():
    from agnoclaw.tools import get_default_tools
    from agnoclaw.config import HarnessConfig

    cfg = HarnessConfig()
    tools = get_default_tools(cfg)
    assert isinstance(tools, list)
    assert len(tools) > 0


def test_get_default_tools_includes_files():
    from agnoclaw.tools import get_default_tools
    from agnoclaw.tools.files import FilesToolkit
    from agnoclaw.config import HarnessConfig

    cfg = HarnessConfig()
    tools = get_default_tools(cfg)
    assert any(isinstance(t, FilesToolkit) for t in tools)


def test_get_default_tools_includes_todos():
    from agnoclaw.tools import get_default_tools
    from agnoclaw.tools.tasks import TodoToolkit
    from agnoclaw.config import HarnessConfig

    cfg = HarnessConfig()
    tools = get_default_tools(cfg)
    assert any(isinstance(t, TodoToolkit) for t in tools)


def test_get_default_tools_respects_disable_bash():
    from agnoclaw.tools import get_default_tools
    from agnoclaw.config import HarnessConfig

    cfg = HarnessConfig(enable_bash=False)
    tools = get_default_tools(cfg)
    # Should not include a callable named "bash"
    bash_tools = [t for t in tools if callable(t) and getattr(t, "name", "") == "bash"]
    assert len(bash_tools) == 0


def test_get_default_tools_count_without_bash():
    from agnoclaw.tools import get_default_tools
    from agnoclaw.config import HarnessConfig

    cfg_with = HarnessConfig(enable_bash=True)
    cfg_without = HarnessConfig(enable_bash=False)
    tools_with = get_default_tools(cfg_with)
    tools_without = get_default_tools(cfg_without)
    assert len(tools_with) > len(tools_without)


def test_web_toolkit_both_enabled_by_default():
    from agnoclaw.tools.web import WebToolkit

    toolkit = WebToolkit()
    assert toolkit.search_enabled is True
    assert toolkit.fetch_enabled is True
