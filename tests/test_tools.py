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


# ── MultiEdit tests ──────────────────────────────────────────────────────


def test_multi_edit_file_applies_all_edits(tmp_path):
    from agnoclaw.tools.files import FilesToolkit

    toolkit = FilesToolkit()
    file_path = str(tmp_path / "multi.py")
    content = "def foo():\n    return 1\n\ndef bar():\n    return 2\n"
    (tmp_path / "multi.py").write_text(content, encoding="utf-8")

    result = toolkit.multi_edit_file(file_path, [
        {"old_string": "return 1", "new_string": "return 100"},
        {"old_string": "return 2", "new_string": "return 200"},
    ])
    assert "2 replacements" in result
    final = (tmp_path / "multi.py").read_text(encoding="utf-8")
    assert "return 100" in final
    assert "return 200" in final
    assert "return 1\n" not in final
    assert "return 2\n" not in final


def test_multi_edit_file_fails_on_missing_old_string(tmp_path):
    from agnoclaw.tools.files import FilesToolkit

    toolkit = FilesToolkit()
    file_path = str(tmp_path / "multi.py")
    (tmp_path / "multi.py").write_text("hello world", encoding="utf-8")

    result = toolkit.multi_edit_file(file_path, [
        {"old_string": "NOT_PRESENT", "new_string": "x"},
    ])
    assert "[error]" in result
    # File should be unchanged
    assert (tmp_path / "multi.py").read_text(encoding="utf-8") == "hello world"


def test_multi_edit_file_fails_on_duplicate_old_string(tmp_path):
    from agnoclaw.tools.files import FilesToolkit

    toolkit = FilesToolkit()
    file_path = str(tmp_path / "multi.py")
    (tmp_path / "multi.py").write_text("x = 1\nx = 1\n", encoding="utf-8")

    result = toolkit.multi_edit_file(file_path, [
        {"old_string": "x = 1", "new_string": "x = 2"},
    ])
    assert "[error]" in result


def test_multi_edit_file_empty_edits(tmp_path):
    from agnoclaw.tools.files import FilesToolkit

    toolkit = FilesToolkit()
    file_path = str(tmp_path / "f.txt")
    (tmp_path / "f.txt").write_text("abc", encoding="utf-8")

    result = toolkit.multi_edit_file(file_path, [])
    assert "[error]" in result


def test_multi_edit_file_nonexistent(tmp_path):
    from agnoclaw.tools.files import FilesToolkit

    toolkit = FilesToolkit()
    result = toolkit.multi_edit_file(str(tmp_path / "missing.py"), [{"old_string": "x", "new_string": "y"}])
    assert "[error]" in result


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


# ── ProgressToolkit tests ────────────────────────────────────────────────


def test_progress_toolkit_write_and_read(tmp_path):
    from agnoclaw.tools.tasks import ProgressToolkit

    tk = ProgressToolkit(project_dir=str(tmp_path))
    result = tk.write_progress("Completed auth module", "Implement API endpoints")
    assert isinstance(result, str)
    assert "progress.md" in result

    content = tk.read_progress()
    assert "Completed auth module" in content
    assert "Implement API endpoints" in content


def test_progress_toolkit_write_progress_includes_context(tmp_path):
    from agnoclaw.tools.tasks import ProgressToolkit

    tk = ProgressToolkit(project_dir=str(tmp_path))
    tk.write_progress("Done", "Next: tests", context="Use pytest, not unittest")
    content = tk.read_progress()
    assert "Use pytest, not unittest" in content


def test_progress_toolkit_read_missing(tmp_path):
    from agnoclaw.tools.tasks import ProgressToolkit

    tk = ProgressToolkit(project_dir=str(tmp_path))
    result = tk.read_progress()
    assert "No previous progress" in result or "fresh" in result.lower()


def test_progress_toolkit_write_features(tmp_path):
    from agnoclaw.tools.tasks import ProgressToolkit
    import json

    tk = ProgressToolkit(project_dir=str(tmp_path))
    features = json.dumps([
        {"id": "auth-01", "description": "Users can register"},
        {"id": "auth-02", "description": "Users can log in"},
    ])
    result = tk.write_features(features)
    assert isinstance(result, str)
    assert "features.md" in result


def test_progress_toolkit_read_features(tmp_path):
    from agnoclaw.tools.tasks import ProgressToolkit
    import json

    tk = ProgressToolkit(project_dir=str(tmp_path))
    features = json.dumps([{"id": "api-01", "description": "GET /items returns list"}])
    tk.write_features(features)

    content = tk.read_features()
    assert "api-01" in content
    assert "GET /items" in content
    assert "failing" in content  # default status


def test_progress_toolkit_read_features_missing(tmp_path):
    from agnoclaw.tools.tasks import ProgressToolkit

    tk = ProgressToolkit(project_dir=str(tmp_path))
    result = tk.read_features()
    assert "No features" in result or "write_features" in result


def test_progress_toolkit_update_feature_passing(tmp_path):
    from agnoclaw.tools.tasks import ProgressToolkit
    import json

    tk = ProgressToolkit(project_dir=str(tmp_path))
    tk.write_features(json.dumps([{"id": "feat-01", "description": "Basic CRUD"}]))

    result = tk.update_feature_status("feat-01", "passing")
    assert "passing" in result

    content = tk.read_features()
    assert "`passing`" in content
    assert "✅" in content


def test_progress_toolkit_update_feature_failing(tmp_path):
    from agnoclaw.tools.tasks import ProgressToolkit
    import json

    tk = ProgressToolkit(project_dir=str(tmp_path))
    tk.write_features(json.dumps([{"id": "feat-01", "description": "Basic CRUD", "status": "passing"}]))

    result = tk.update_feature_status("feat-01", "failing")
    assert "failing" in result

    content = tk.read_features()
    assert "`failing`" in content


def test_progress_toolkit_update_feature_not_found(tmp_path):
    from agnoclaw.tools.tasks import ProgressToolkit
    import json

    tk = ProgressToolkit(project_dir=str(tmp_path))
    tk.write_features(json.dumps([{"id": "feat-01", "description": "CRUD"}]))

    result = tk.update_feature_status("nonexistent-99", "passing")
    assert "error" in result.lower() or "not found" in result.lower()


def test_progress_toolkit_update_feature_invalid_status(tmp_path):
    from agnoclaw.tools.tasks import ProgressToolkit
    import json

    tk = ProgressToolkit(project_dir=str(tmp_path))
    tk.write_features(json.dumps([{"id": "feat-01", "description": "CRUD"}]))

    result = tk.update_feature_status("feat-01", "unknown_status")
    assert "error" in result.lower()


def test_progress_toolkit_write_features_invalid_json(tmp_path):
    from agnoclaw.tools.tasks import ProgressToolkit

    tk = ProgressToolkit(project_dir=str(tmp_path))
    result = tk.write_features("not valid json {{")
    assert "error" in result.lower()


def test_progress_toolkit_update_without_features_file(tmp_path):
    from agnoclaw.tools.tasks import ProgressToolkit

    tk = ProgressToolkit(project_dir=str(tmp_path))
    result = tk.update_feature_status("feat-01", "passing")
    assert "error" in result.lower()


def test_progress_toolkit_default_project_dir():
    from agnoclaw.tools.tasks import ProgressToolkit
    tk = ProgressToolkit()
    assert tk._project_dir == "."


def test_get_default_tools_includes_progress():
    from agnoclaw.tools import get_default_tools
    from agnoclaw.tools.tasks import ProgressToolkit
    from agnoclaw.config import HarnessConfig

    cfg = HarnessConfig()
    tools = get_default_tools(cfg)
    assert any(isinstance(t, ProgressToolkit) for t in tools)


# ── SubagentDefinition tests ─────────────────────────────────────────────


def test_subagent_definition_fields():
    """SubagentDefinition should have expected fields with correct defaults."""
    from agnoclaw.tools.tasks import SubagentDefinition

    defn = SubagentDefinition(description="A test agent")
    assert defn.description == "A test agent"
    assert defn.prompt == ""
    assert defn.tools == ["all"]
    assert defn.model is None


def test_subagent_definition_custom_fields():
    from agnoclaw.tools.tasks import SubagentDefinition

    defn = SubagentDefinition(
        description="Code reviewer",
        prompt="Review code for security issues.",
        tools=["files", "bash"],
        model="anthropic:claude-sonnet-4-6",
    )
    assert defn.description == "Code reviewer"
    assert defn.prompt == "Review code for security issues."
    assert defn.tools == ["files", "bash"]
    assert defn.model == "anthropic:claude-sonnet-4-6"


def test_subagent_definition_importable_from_package():
    """SubagentDefinition should be importable from the top-level package."""
    from agnoclaw import SubagentDefinition
    assert SubagentDefinition is not None


def test_subagent_definition_importable_from_tools():
    """SubagentDefinition should be importable from agnoclaw.tools."""
    from agnoclaw.tools import SubagentDefinition
    assert SubagentDefinition is not None


# ── make_subagent_tool tests ─────────────────────────────────────────────


def test_make_subagent_tool_returns_function():
    from agnoclaw.tools.tasks import make_subagent_tool

    tool = make_subagent_tool()
    assert hasattr(tool, "name")
    assert tool.name == "spawn_subagent"


def test_make_subagent_tool_with_named_agents():
    """Named agents should appear in the tool description."""
    from agnoclaw.tools.tasks import make_subagent_tool, SubagentDefinition

    subagents = {
        "researcher": SubagentDefinition(description="Searches the web for information"),
        "reviewer": SubagentDefinition(description="Reviews code for bugs"),
    }
    tool = make_subagent_tool(subagents=subagents)
    desc = tool.description
    assert "researcher" in desc
    assert "reviewer" in desc
    assert "Searches the web" in desc
    assert "Reviews code" in desc


def test_make_subagent_tool_no_named_agents():
    """Without named agents, description should not mention 'Named agents'."""
    from agnoclaw.tools.tasks import make_subagent_tool

    tool = make_subagent_tool()
    assert "Named agents" not in tool.description


def test_make_subagent_tool_runs_subagent():
    """spawn_subagent should create and run an Agent."""
    from agnoclaw.tools.tasks import make_subagent_tool

    tool = make_subagent_tool(default_model="anthropic:test-model")

    with patch("agnoclaw.tools.tasks._run_subagent") as mock_run:
        mock_run.return_value = "subagent result"
        result = tool.entrypoint("Analyze this code")
        mock_run.assert_called_once()
        assert result == "subagent result"


def test_make_subagent_tool_named_agent_lookup():
    """When agent_name matches, use the named definition's prompt and model."""
    from agnoclaw.tools.tasks import make_subagent_tool, SubagentDefinition

    subagents = {
        "analyst": SubagentDefinition(
            description="Data analyst",
            prompt="You are an expert data analyst.",
            model="openai:gpt-4o",
        ),
    }
    tool = make_subagent_tool(subagents=subagents)

    with patch("agnoclaw.tools.tasks._run_subagent") as mock_run:
        mock_run.return_value = "analysis done"
        result = tool.entrypoint("Analyze sales data", agent_name="analyst")
        call_args = mock_run.call_args
        assert call_args[0][1] == "You are an expert data analyst."  # instructions
        assert call_args[0][2] == "openai:gpt-4o"  # model_id
        assert result == "analysis done"


def test_make_subagent_tool_ad_hoc_type():
    """Ad-hoc agent_type should set instructions from _TYPE_INSTRUCTIONS."""
    from agnoclaw.tools.tasks import make_subagent_tool

    tool = make_subagent_tool()

    with patch("agnoclaw.tools.tasks._run_subagent") as mock_run:
        mock_run.return_value = "done"
        tool.entrypoint("Research AI trends", agent_type="research")
        instructions = mock_run.call_args[0][1]
        assert "research" in instructions.lower()


def test_make_subagent_tool_custom_prompt_overrides_type():
    """Custom prompt should override agent_type instructions."""
    from agnoclaw.tools.tasks import make_subagent_tool

    tool = make_subagent_tool()

    with patch("agnoclaw.tools.tasks._run_subagent") as mock_run:
        mock_run.return_value = "done"
        tool.entrypoint("Do X", prompt="Custom instructions here", agent_type="research")
        instructions = mock_run.call_args[0][1]
        assert instructions == "Custom instructions here"


def test_make_subagent_tool_handles_errors():
    """spawn_subagent should return error string on failure, not raise."""
    from agnoclaw.tools.tasks import make_subagent_tool

    tool = make_subagent_tool()

    with patch("agnoclaw.tools.tasks._run_subagent", side_effect=RuntimeError("boom")):
        result = tool.entrypoint("fail task")
        assert "[error]" in result
        assert "boom" in result


# ── _run_subagent / _build_subagent_tools tests ─────────────────────────


def test_build_subagent_tools_all():
    """'all' should include web, files, and bash tools."""
    from agnoclaw.tools.tasks import _build_subagent_tools

    tools = _build_subagent_tools(["all"])
    assert len(tools) == 3  # WebToolkit, FilesToolkit, bash


def test_build_subagent_tools_specific():
    """Specific tool names should only include those tools."""
    from agnoclaw.tools.tasks import _build_subagent_tools

    tools = _build_subagent_tools(["web"])
    assert len(tools) == 1

    tools = _build_subagent_tools(["files", "bash"])
    assert len(tools) == 2


def test_build_subagent_tools_none_defaults_to_all():
    """None should default to all tools."""
    from agnoclaw.tools.tasks import _build_subagent_tools

    tools = _build_subagent_tools(None)
    assert len(tools) == 3


def test_run_subagent_truncates_long_output():
    """_run_subagent should truncate responses over 8000 chars."""
    from agnoclaw.tools.tasks import _run_subagent

    mock_response = MagicMock()
    mock_response.content = "x" * 10000

    with patch("agno.agent.Agent") as mock_agent_cls:
        mock_agent = MagicMock()
        mock_agent.run.return_value = mock_response
        mock_agent_cls.return_value = mock_agent
        result = _run_subagent("task", "instructions", "model")
        assert len(result) < 10000
        assert "truncated" in result


# ── get_default_tools subagent passthrough test ──────────────────────────


def test_get_default_tools_passes_subagents():
    """get_default_tools should pass subagents to make_subagent_tool."""
    from agnoclaw.tools import get_default_tools
    from agnoclaw.tools.tasks import SubagentDefinition
    from agnoclaw.config import HarnessConfig

    subagents = {
        "test-agent": SubagentDefinition(description="test"),
    }
    cfg = HarnessConfig()

    with patch("agnoclaw.tools.make_subagent_tool") as mock_make:
        mock_make.return_value = MagicMock()
        get_default_tools(cfg, subagents=subagents)
        mock_make.assert_called_once()
        call_kwargs = mock_make.call_args[1]
        assert call_kwargs["subagents"] is subagents
