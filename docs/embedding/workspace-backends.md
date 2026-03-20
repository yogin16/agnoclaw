# Workspace Backends

`agnoclaw` can now separate orchestration from the underlying execution/filesystem plane for its built-in workspace tool family.

This matters for hosted or sandboxed runtimes where:

- shell commands must run in a container or remote sandbox
- file operations must read/write the sandbox filesystem, not the host process filesystem
- `bash` and `files` need to stay coherent instead of pointing at different environments

## Extension points

Two tool-level backend interfaces are available:

- `CommandExecutor`
- `WorkspaceAdapter`

The default implementations remain host-local:

- `LocalCommandExecutor`
- `LocalWorkspaceAdapter`

## What they cover

`CommandExecutor` backs:

- `bash`
- `bash_start`
- `bash_output`
- `bash_kill`

`WorkspaceAdapter` backs:

- `read_file`
- `write_file`
- `edit_file`
- `multi_edit_file`
- `glob_files`
- `grep_files`
- `list_dir`

These backends can be injected through:

- `AgentHarness(...)`
- `get_default_tools(...)`
- team factories such as `code_team(...)` and `data_team(...)`
- subagents spawned through the built-in `spawn_subagent` tool

## Example

```python
from agnoclaw import AgentHarness
from agnoclaw.tools import CommandExecutor, WorkspaceAdapter


class SandboxExecutor(CommandExecutor):
    def run(self, *, command, workdir, timeout_seconds):
        ...

    def start(self, *, command, workdir, description=None):
        ...

    def output(self, *, task_id, max_chars=8000, tail=True):
        ...

    def kill(self, *, task_id, force=False):
        ...


class SandboxWorkspace(WorkspaceAdapter):
    workspace_dir = "/sandbox/workspace"

    def read_file(self, path, offset=0, limit=2000):
        ...

    def write_file(self, path, content):
        ...

    def edit_file(self, path, old_string, new_string):
        ...

    def multi_edit_file(self, path, edits):
        ...

    def glob_files(self, pattern, base_dir=None, path=None):
        ...

    def grep_files(self, pattern, path=None, glob=None, case_insensitive=False, context_lines=0, max_results=50):
        ...

    def list_dir(self, path=None):
        ...


agent = AgentHarness(
    workspace_dir="/logical/workspace",
    command_executor=SandboxExecutor(),
    workspace_adapter=SandboxWorkspace(),
)
```

## Architectural boundary

This backend abstraction does not replace policy or guardrails.

The layering is:

1. `AgentHarness` owns prompts, sessions, hooks, events, policy, permissions, and guardrails.
2. Built-in workspace tools delegate execution/storage to the injected backends.
3. Your sandbox/container/remote runtime owns the actual command execution and filesystem behavior.

That keeps the harness opinionated at the orchestration layer while allowing embedders to control the runtime plane.
