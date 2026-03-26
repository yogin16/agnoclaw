"""Tests for the tool suite (bash, files, web, tasks)."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from agnoclaw.backends import RuntimeBackend
from agnoclaw.skills.backends import SkillInstallResult
from agnoclaw.tools.backends import (
    BackgroundCommandHandle,
    BackgroundCommandOutput,
    CommandResult,
)


class FakeWorkspaceAdapter:
    def __init__(self, workspace_dir: str | Path | None = None):
        self.workspace_dir = (
            Path(workspace_dir).expanduser().resolve()
            if workspace_dir is not None
            else Path.cwd().resolve()
        )

    def read_file(self, path: str, offset: int = 0, limit: int = 2000) -> str:
        return f"adapter-read:{path}:{offset}:{limit}"

    def write_file(self, path: str, content: str) -> str:
        return f"adapter-write:{path}:{content}"

    def edit_file(self, path: str, old_string: str, new_string: str) -> str:
        return f"adapter-edit:{path}:{old_string}->{new_string}"

    def multi_edit_file(self, path: str, edits: list[dict[str, str]]) -> str:
        return f"adapter-multi:{path}:{len(edits)}"

    def glob_files(self, pattern: str, base_dir: str | None = None, path: str | None = None) -> str:
        return f"adapter-glob:{pattern}:{base_dir}:{path}"

    def grep_files(
        self,
        pattern: str,
        path: str | None = None,
        glob: str | None = None,
        case_insensitive: bool = False,
        context_lines: int = 0,
        max_results: int = 50,
    ) -> str:
        return (
            f"adapter-grep:{pattern}:{path}:{glob}:{case_insensitive}:"
            f"{context_lines}:{max_results}"
        )

    def list_dir(self, path: str | None = None) -> str:
        return f"adapter-list:{path}"


class FakeCommandExecutor:
    def __init__(self, workspace_dir: str | Path | None = None):
        self.workspace_dir = (
            str(Path(workspace_dir).expanduser().resolve())
            if workspace_dir is not None
            else None
        )
        self.calls: list[tuple] = []

    def run(self, *, command: str, workdir: str | None, timeout_seconds: int | None) -> CommandResult:
        self.calls.append(("run", command, workdir, timeout_seconds))
        return CommandResult(stdout=f"executor-run:{command}:{workdir}:{timeout_seconds}")

    def start(
        self,
        *,
        command: str,
        workdir: str | None,
        description: str | None = None,
    ) -> BackgroundCommandHandle:
        self.calls.append(("start", command, workdir, description))
        return BackgroundCommandHandle(
            task_id="task_custom",
            pid=1234,
            status="running",
            log_path="/tmp/custom.log",
        )

    def output(
        self,
        *,
        task_id: str,
        max_chars: int = 8000,
        tail: bool = True,
    ) -> BackgroundCommandOutput:
        self.calls.append(("output", task_id, max_chars, tail))
        return BackgroundCommandOutput(
            task_id=task_id,
            status="exited",
            output="executor-output",
            exit_code=0,
            pid=1234,
        )

    def kill(self, *, task_id: str, force: bool = False) -> str:
        self.calls.append(("kill", task_id, force))
        return f"executor-kill:{task_id}:{force}"


class FakeSkillRuntimeBackend:
    def __init__(self):
        self.calls = []

    def run_inline_command(self, *, command: str, timeout_seconds: int = 10, working_dir: str | None = None) -> str:
        self.calls.append(("inline", command, timeout_seconds, working_dir))
        return f"skill-inline:{command}"

    def has_binary(self, name: str) -> bool:
        self.calls.append(("binary", name))
        return True

    def has_env_var(self, name: str) -> bool:
        self.calls.append(("env", name))
        return True

    def has_python_distribution(self, name: str) -> bool:
        self.calls.append(("dist", name))
        return True

    def run_install(self, *, installer_type: str, package_spec: str, timeout_seconds: int = 120) -> SkillInstallResult:
        self.calls.append(("install", installer_type, package_spec, timeout_seconds))
        return SkillInstallResult(success=True, exit_code=0)


class FakeBrowserBackend:
    def navigate(self, *, url: str, wait_until: str = "domcontentloaded") -> str:
        return f"browser:{url}:{wait_until}"

    def click(self, *, selector: str) -> str:
        return f"click:{selector}"

    def type(self, *, selector: str, text: str) -> str:
        return f"type:{selector}:{text}"

    def screenshot(self, *, full_page: bool = False) -> str:
        return f"screenshot:{full_page}"

    def snapshot(self) -> str:
        return "snapshot"

    def scroll(self, *, direction: str = "down", amount: int = 500) -> str:
        return f"scroll:{direction}:{amount}"

    def fill_form(self, *, fields: str) -> str:
        return f"fill:{fields}"

    def close(self) -> str:
        return "closed"


class FakeRuntimeBackend(RuntimeBackend):
    def __init__(
        self,
        *,
        command_executor=None,
        workspace_adapter=None,
        browser_backend=None,
        skill_runtime=None,
    ):
        super().__init__(
            command_executor=command_executor,
            workspace_adapter=workspace_adapter,
            browser_backend=browser_backend,
        )
        self._skill_runtime = skill_runtime

    def resolve_skill_runtime(self, *, workspace_dir: str | Path, command_executor=None):
        if self._skill_runtime is not None:
            return self._skill_runtime
        return super().resolve_skill_runtime(
            workspace_dir=workspace_dir,
            command_executor=command_executor,
        )


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

    toolkit.edit_file(file_path, "Hello world", "Goodbye world")
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


def test_files_toolkit_delegates_to_custom_adapter(tmp_path):
    from agnoclaw.tools.files import FilesToolkit

    adapter = FakeWorkspaceAdapter(workspace_dir=tmp_path)
    toolkit = FilesToolkit(workspace_dir=tmp_path, adapter=adapter)

    assert toolkit.read_file("/tmp/demo.txt") == "adapter-read:/tmp/demo.txt:0:2000"
    assert toolkit.write_file("/tmp/demo.txt", "hello") == "adapter-write:/tmp/demo.txt:hello"
    assert toolkit.edit_file("/tmp/demo.txt", "a", "b") == "adapter-edit:/tmp/demo.txt:a->b"
    assert toolkit.multi_edit_file("/tmp/demo.txt", [{"old_string": "a", "new_string": "b"}]) == (
        "adapter-multi:/tmp/demo.txt:1"
    )
    assert toolkit.glob_files("*.py") == "adapter-glob:*.py:None:None"
    assert toolkit.grep_files("needle", path="/tmp") == "adapter-grep:needle:/tmp:None:False:0:50"
    assert toolkit.list_dir("/tmp") == "adapter-list:/tmp"


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


def test_bash_tool_expands_workspace_home():
    from agnoclaw.tools.bash import make_bash_tool

    bash = make_bash_tool(timeout=10, workspace_dir="~")
    result = bash.entrypoint("pwd")
    assert result.strip() == str(Path.home())


def test_bash_tool_raises_for_invalid_working_dir():
    from agnoclaw.tools.bash import BashToolError, make_bash_tool

    bash = make_bash_tool(timeout=10)
    with pytest.raises(BashToolError, match="Failed to execute command"):
        bash.entrypoint("pwd", working_dir="/definitely/not/a/real/agnoclaw-dir")


def test_bash_toolkit_registers_background_functions():
    from agnoclaw.tools.bash import BashToolkit

    toolkit = BashToolkit(timeout=10)
    names = set(toolkit.functions.keys())
    assert {"bash", "bash_start", "bash_output", "bash_kill"}.issubset(names)


def test_bash_toolkit_background_lifecycle():
    import sys
    import time
    from agnoclaw.tools.bash import BashToolkit

    toolkit = BashToolkit(timeout=10)
    bash_start = toolkit.functions["bash_start"].entrypoint
    bash_output = toolkit.functions["bash_output"].entrypoint
    bash_kill = toolkit.functions["bash_kill"].entrypoint
    cmd = (
        f'"{sys.executable}" -c "import time; print(\'ready\', flush=True); '
        "time.sleep(5); print('done', flush=True)\""
    )
    start = bash_start(command=cmd)
    assert "Started background task task_" in start
    task_id = start.split("Started background task ", 1)[1].splitlines()[0].strip()

    time.sleep(0.2)
    out_running = bash_output(task_id=task_id)
    assert "status=running" in out_running or "status=exited" in out_running
    assert "ready" in out_running or "[no output yet]" in out_running

    killed = bash_kill(task_id=task_id)
    assert "Killed task" in killed or "already exited" in killed

    out_after = bash_output(task_id=task_id)
    assert "status=exited" in out_after


def test_bash_toolkit_background_start_raises_for_invalid_working_dir():
    from agnoclaw.tools.bash import BashToolError, BashToolkit

    toolkit = BashToolkit(timeout=10)
    bash_start = toolkit.functions["bash_start"].entrypoint

    with pytest.raises(BashToolError, match="Failed to start background command"):
        bash_start(command="pwd", working_dir="/definitely/not/a/real/agnoclaw-dir")


def test_bash_toolkit_delegates_to_custom_executor(tmp_path):
    from agnoclaw.tools.bash import BashToolkit

    executor = FakeCommandExecutor(workspace_dir=tmp_path)
    toolkit = BashToolkit(timeout=10, workspace_dir=tmp_path, executor=executor)

    bash = toolkit.functions["bash"].entrypoint
    bash_start = toolkit.functions["bash_start"].entrypoint
    bash_output = toolkit.functions["bash_output"].entrypoint
    bash_kill = toolkit.functions["bash_kill"].entrypoint

    assert bash("echo hi") == "executor-run:echo hi:None:10"
    assert bash_start(command="sleep 5") == (
        "Started background task task_custom\n"
        "pid: 1234\n"
        "status: running\n"
        "log: /tmp/custom.log"
    )
    assert bash_output(task_id="task_custom") == (
        "[task task_custom] status=exited exit_code=0 pid=1234\nexecutor-output"
    )
    assert bash_kill(task_id="task_custom") == "executor-kill:task_custom:False"
    assert ("run", "echo hi", None, 10) in executor.calls


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
    from agnoclaw.tools.bash import BashToolkit
    from agnoclaw.config import HarnessConfig

    cfg = HarnessConfig(enable_bash=False)
    tools = get_default_tools(cfg)
    bash_tools = [t for t in tools if isinstance(t, BashToolkit)]
    assert len(bash_tools) == 0


def test_get_default_tools_background_bash_opt_in():
    from agnoclaw.tools import get_default_tools
    from agnoclaw.tools.bash import BashToolkit
    from agnoclaw.config import HarnessConfig

    cfg = HarnessConfig(enable_bash=True, enable_background_bash_tools=True)
    tools = get_default_tools(cfg)
    assert any(isinstance(t, BashToolkit) for t in tools)


def test_get_default_tools_count_without_bash():
    from agnoclaw.tools import get_default_tools
    from agnoclaw.config import HarnessConfig

    cfg_with = HarnessConfig(enable_bash=True)
    cfg_without = HarnessConfig(enable_bash=False)
    tools_with = get_default_tools(cfg_with)
    tools_without = get_default_tools(cfg_without)
    assert len(tools_with) > len(tools_without)


def test_get_default_tools_workspace_override_applies_to_files_and_progress(tmp_path):
    from agnoclaw.tools import get_default_tools
    from agnoclaw.tools.files import FilesToolkit
    from agnoclaw.tools.tasks import ProgressToolkit
    from agnoclaw.config import HarnessConfig

    cfg = HarnessConfig(workspace_dir="~/.agnoclaw/workspace")
    tools = get_default_tools(cfg, workspace_dir=tmp_path / "embedder-ws")

    files = next(t for t in tools if isinstance(t, FilesToolkit))
    progress = next(t for t in tools if isinstance(t, ProgressToolkit))

    assert files.workspace_dir == (tmp_path / "embedder-ws").resolve()
    assert Path(progress._project_dir) == (tmp_path / "embedder-ws").resolve()


def test_get_default_tools_sandbox_routes_relative_file_ops_and_allows_workspace_paths(tmp_path):
    from agnoclaw.config import HarnessConfig
    from agnoclaw.tools import get_default_tools
    from agnoclaw.tools.files import FilesToolkit

    workspace_dir = tmp_path / "workspace"
    sandbox_dir = tmp_path / "sandbox"
    workspace_dir.mkdir()

    tools = get_default_tools(
        HarnessConfig(),
        workspace_dir=workspace_dir,
        sandbox_dir=sandbox_dir,
    )
    files = next(t for t in tools if isinstance(t, FilesToolkit))

    assert files.workspace_dir == sandbox_dir.resolve()

    files.write_file("scratch.txt", "sandbox")
    files.write_file(str(workspace_dir / "workspace.txt"), "workspace")

    assert (sandbox_dir / "scratch.txt").read_text(encoding="utf-8") == "sandbox"
    assert (workspace_dir / "workspace.txt").read_text(encoding="utf-8") == "workspace"


def test_get_default_tools_sandboxed_bash_can_read_workspace_and_write_both_outputs(tmp_path):
    import sys

    from agnoclaw.config import HarnessConfig
    from agnoclaw.tools import get_default_tools

    workspace_dir = tmp_path / "workspace"
    sandbox_dir = tmp_path / "sandbox"
    workspace_dir.mkdir()
    (workspace_dir / "input.txt").write_text("alpha", encoding="utf-8")

    tools = get_default_tools(
        HarnessConfig(enable_bash=True),
        workspace_dir=workspace_dir,
        sandbox_dir=sandbox_dir,
    )
    bash = next(t for t in tools if getattr(t, "name", None) == "bash")

    command = (
        f'"{sys.executable}" -c "from pathlib import Path; '
        f'workspace = Path(r\'{workspace_dir / "input.txt"}\').read_text(); '
        f'Path(\'session.txt\').write_text(workspace.upper()); '
        f'Path(r\'{workspace_dir / "output.txt"}\').write_text(workspace + \'!\')"'
    )
    bash.entrypoint(command)

    assert (sandbox_dir / "session.txt").read_text(encoding="utf-8") == "ALPHA"
    assert (workspace_dir / "output.txt").read_text(encoding="utf-8") == "alpha!"


def test_get_default_tools_uses_custom_backend(tmp_path):
    from agnoclaw.config import HarnessConfig
    from agnoclaw.tools import get_default_tools
    from agnoclaw.tools.files import FilesToolkit

    cfg = HarnessConfig(workspace_dir="~/.agnoclaw/workspace")
    adapter = FakeWorkspaceAdapter(workspace_dir=tmp_path)
    executor = FakeCommandExecutor(workspace_dir=tmp_path)
    backend = RuntimeBackend(command_executor=executor, workspace_adapter=adapter)

    tools = get_default_tools(
        cfg,
        workspace_dir=tmp_path,
        backend=backend,
    )

    files = next(t for t in tools if isinstance(t, FilesToolkit))
    bash = next(t for t in tools if getattr(t, "name", None) == "bash")

    assert files.adapter is adapter
    assert files.read_file("/tmp/demo.txt") == "adapter-read:/tmp/demo.txt:0:2000"
    assert bash.entrypoint("echo hi") == "executor-run:echo hi:None:120"


def test_get_default_tools_uses_runtime_backend(tmp_path):
    from agnoclaw.config import HarnessConfig
    from agnoclaw.tools import get_default_tools
    from agnoclaw.tools.files import FilesToolkit

    adapter = FakeWorkspaceAdapter(workspace_dir=tmp_path)
    executor = FakeCommandExecutor(workspace_dir=tmp_path)
    skill_runtime = FakeSkillRuntimeBackend()
    browser_backend = FakeBrowserBackend()
    backend = FakeRuntimeBackend(
        command_executor=executor,
        workspace_adapter=adapter,
        skill_runtime=skill_runtime,
        browser_backend=browser_backend,
    )

    tools = get_default_tools(
        HarnessConfig(enable_browser=True),
        workspace_dir=tmp_path,
        backend=backend,
    )

    files = next(t for t in tools if isinstance(t, FilesToolkit))
    bash = next(t for t in tools if getattr(t, "name", None) == "bash")
    browser = next(t for t in tools if getattr(t, "name", None) == "browser")

    assert files.adapter is adapter
    assert bash.entrypoint("echo hi") == "executor-run:echo hi:None:120"
    assert browser.browser_snapshot() == "snapshot"


def test_runtime_backend_requires_command_and_workspace_together(tmp_path):
    with pytest.raises(ValueError, match="both command_executor and workspace_adapter"):
        RuntimeBackend(command_executor=FakeCommandExecutor(workspace_dir=tmp_path))


def test_custom_runtime_backend_does_not_silently_fallback_to_host(tmp_path):
    class IncompleteRuntimeBackend(RuntimeBackend):
        pass

    with pytest.raises(RuntimeError, match="must provide command execution"):
        IncompleteRuntimeBackend().resolve(workspace_dir=tmp_path)


def test_get_default_tools_rejects_custom_backend_without_browser_support(tmp_path):
    from agnoclaw.config import HarnessConfig
    from agnoclaw.tools import get_default_tools

    backend = RuntimeBackend(
        command_executor=FakeCommandExecutor(workspace_dir=tmp_path),
        workspace_adapter=FakeWorkspaceAdapter(workspace_dir=tmp_path),
    )

    with pytest.raises(ValueError, match="does not provide browser support"):
        get_default_tools(
            HarnessConfig(enable_browser=True),
            workspace_dir=tmp_path,
            backend=backend,
        )


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
    from pathlib import Path
    from agnoclaw.tools.tasks import ProgressToolkit
    tk = ProgressToolkit()
    # Default resolves to absolute CWD path (not relative ".")
    assert tk._project_dir == str(Path(".").expanduser().resolve())


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


def test_make_subagent_tool_passes_workspace_and_config(tmp_path):
    from agnoclaw.config import HarnessConfig
    from agnoclaw.tools.tasks import make_subagent_tool

    cfg = HarnessConfig(default_provider="openai")
    workspace_dir = tmp_path / "subagent-ws"
    tool = make_subagent_tool(workspace_dir=workspace_dir, config=cfg)

    with patch("agnoclaw.tools.tasks._run_subagent") as mock_run:
        mock_run.return_value = "done"
        tool.entrypoint("Do X")
        assert mock_run.call_args.kwargs["workspace_dir"] == workspace_dir
        assert mock_run.call_args.kwargs["config"] is cfg


def test_make_subagent_tool_handles_errors():
    """spawn_subagent should raise so the harness can emit a failed tool call."""
    from agnoclaw.tools.tasks import make_subagent_tool

    tool = make_subagent_tool()

    with patch("agnoclaw.tools.tasks._run_subagent", side_effect=RuntimeError("boom")):
        with pytest.raises(RuntimeError, match="Subagent failed: boom"):
            tool.entrypoint("fail task")


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


def test_build_subagent_tools_workspace_override_applies_to_files_and_bash(tmp_path):
    """Subagent tools should inherit an explicit workspace root."""
    from agnoclaw.tools.files import FilesToolkit
    from agnoclaw.tools.tasks import _build_subagent_tools

    workspace_dir = tmp_path / "subagent-workspace"
    workspace_dir.mkdir()
    tools = _build_subagent_tools(["files", "bash"], workspace_dir=workspace_dir)
    files = next(t for t in tools if isinstance(t, FilesToolkit))
    bash = next(t for t in tools if getattr(t, "name", None) == "bash")

    assert files.workspace_dir == workspace_dir.resolve()
    assert bash.entrypoint("pwd").strip() == str(workspace_dir.resolve())


def test_build_subagent_tools_uses_custom_backends(tmp_path):
    from agnoclaw.tools.files import FilesToolkit
    from agnoclaw.tools.tasks import _build_subagent_tools

    adapter = FakeWorkspaceAdapter(workspace_dir=tmp_path)
    executor = FakeCommandExecutor(workspace_dir=tmp_path)
    backend = RuntimeBackend(command_executor=executor, workspace_adapter=adapter)

    tools = _build_subagent_tools(
        ["files", "bash"],
        workspace_dir=tmp_path,
        backend=backend,
    )
    files = next(t for t in tools if isinstance(t, FilesToolkit))
    bash = next(t for t in tools if getattr(t, "name", None) == "bash")

    assert files.adapter is adapter
    assert bash.entrypoint("echo hi") == "executor-run:echo hi:None:120"


def test_run_subagent_truncates_long_output():
    """_run_subagent should truncate responses over 8000 chars."""
    from agnoclaw.tools.tasks import _run_subagent

    mock_response = MagicMock()
    mock_response.content = "x" * 10000

    with patch("agnoclaw.agent.Agent") as mock_agent_cls, \
         patch("agnoclaw.agent._make_db", return_value=MagicMock()):
        mock_agent = MagicMock()
        mock_agent.run.return_value = mock_response
        mock_agent_cls.return_value = mock_agent
        result = _run_subagent("task", "instructions", "model")
        assert len(result) < 10000
        assert "truncated" in result


def test_run_subagent_resolves_model_before_agent_creation():
    from agnoclaw.tools.tasks import _run_subagent

    mock_response = MagicMock()
    mock_response.content = "ok"

    with patch("agnoclaw.agent.Agent") as mock_agent_cls, \
         patch("agnoclaw.agent._make_db", return_value=MagicMock()):
        mock_agent = MagicMock()
        mock_agent.run.return_value = mock_response
        mock_agent_cls.return_value = mock_agent

        cfg = MagicMock()
        cfg.default_provider = "openai"
        cfg.default_model = "gpt-4o"
        cfg.workspace_dir = "/tmp/ws"
        cfg.global_workspace_dir = "~/.agnoclaw/global"
        cfg.project_workspace_dir = ".agnoclaw"
        cfg.enable_plugins = False
        cfg.enable_learning = False
        cfg.learning_mode = "agentic"
        cfg.enable_compression = False
        cfg.compress_token_limit = None
        cfg.enable_session_summary = False
        cfg.session_history_runs = 10
        cfg.guardrails_enabled = True
        cfg.path_guardrails_enabled = True
        cfg.path_allowed_roots = []
        cfg.path_blocked_roots = []
        cfg.network_enabled = True
        cfg.network_enforce_https = True
        cfg.network_allowed_hosts = []
        cfg.network_blocked_hosts = []
        cfg.network_block_private_hosts = True
        cfg.network_block_in_bash = True
        cfg.event_sink_mode = "best_effort"
        cfg.policy_fail_open = False
        cfg.permission_mode = "bypass"
        cfg.permission_require_approver = False
        cfg.permission_preapproved_tools = []
        cfg.permission_preapproved_categories = []
        cfg.debug = False

        result = _run_subagent("task", "instructions", "gpt-4", config=cfg)

        assert result == "ok"
        assert mock_agent_cls.call_args[1]["model"] == "openai:gpt-4"


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


def test_get_default_tools_passes_config_to_subagent_tool():
    """get_default_tools should propagate config to make_subagent_tool."""
    from agnoclaw.config import HarnessConfig
    from agnoclaw.tools import get_default_tools

    cfg = HarnessConfig(default_provider="openai")

    with patch("agnoclaw.tools.make_subagent_tool") as mock_make:
        mock_make.return_value = MagicMock()
        get_default_tools(cfg)
        assert mock_make.call_args[1]["config"] is cfg


def test_get_default_tools_passes_custom_backends_to_subagent_tool(tmp_path):
    from agnoclaw.config import HarnessConfig
    from agnoclaw.tools import get_default_tools

    cfg = HarnessConfig()
    adapter = FakeWorkspaceAdapter(workspace_dir=tmp_path)
    executor = FakeCommandExecutor(workspace_dir=tmp_path)
    backend = RuntimeBackend(command_executor=executor, workspace_adapter=adapter)

    with patch("agnoclaw.tools.make_subagent_tool") as mock_make:
        mock_make.return_value = MagicMock()
        get_default_tools(
            cfg,
            workspace_dir=tmp_path,
            backend=backend,
        )
        assert mock_make.call_args[1]["backend"] is backend


def test_get_default_tools_passes_skill_runtime_backend_to_subagent_tool(tmp_path):
    from agnoclaw.config import HarnessConfig
    from agnoclaw.tools import get_default_tools

    cfg = HarnessConfig()
    backend = FakeRuntimeBackend(
        command_executor=FakeCommandExecutor(workspace_dir=tmp_path),
        workspace_adapter=FakeWorkspaceAdapter(workspace_dir=tmp_path),
        skill_runtime=FakeSkillRuntimeBackend(),
    )

    with patch("agnoclaw.tools.make_subagent_tool") as mock_make:
        mock_make.return_value = MagicMock()
        get_default_tools(
            cfg,
            workspace_dir=tmp_path,
            backend=backend,
        )
        assert mock_make.call_args[1]["backend"] is backend
