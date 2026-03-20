# Runtime Backends

`agnoclaw` separates orchestration from execution with one public integration point:

- `AgentHarness(..., backend=...)`
- `get_default_tools(..., backend=...)`
- built-in team factories such as `code_team(..., backend=...)`
- built-in `spawn_subagent`, which inherits the same backend automatically

## Design intent

The backend object is the runtime boundary.

That means one backend owns:

- shell execution
- file access
- skill inline command execution
- skill dependency probes and installs
- browser automation, when enabled

The goal is to avoid split-brain setups where `bash` runs in a sandbox, files touch the host, or skill installs happen somewhere different from later command execution.

## Host mode

If you do not pass `backend=...`, `agnoclaw` uses host-local implementations for:

- bash tools
- file tools
- skill runtime behavior
- browser tools

This is the default local developer experience.

## Custom backend rules

Custom backends should be passed as a single object.

Two safety rules matter:

1. A composed `RuntimeBackend(...)` must receive both `command_executor` and `workspace_adapter` together, or neither.
2. Once a custom backend is in use, missing capabilities do not silently fall back to the host.

In practice:

- if your backend does not provide shell and file support, backend resolution fails
- if browser tools are enabled and your backend does not provide browser support, tool construction fails explicitly

## Recommended integration shape

Consumer code should wrap its sandbox/session object in one backend class.

This matches how current sandbox SDKs are typically structured: a single sandbox/session object exposes command execution and filesystem access from one handle.

```python
from agnoclaw import AgentHarness, RuntimeBackend


class E2BLikeBackend(RuntimeBackend):
    def __init__(self, sandbox):
        self.sandbox = sandbox

    def resolve_command_executor(self, *, workspace_dir):
        return SandboxCommandExecutor(self.sandbox, workspace_dir=workspace_dir)

    def resolve_workspace_adapter(self, *, workspace_dir):
        return SandboxWorkspaceAdapter(self.sandbox, workspace_dir=workspace_dir)

    def resolve_browser_backend(self):
        return SandboxBrowserBackend(self.sandbox)


agent = AgentHarness(
    workspace_dir="/srv/workspace",
    backend=E2BLikeBackend(sandbox),
)
```

This is the intended consumer-facing pattern for E2B-style or LLM-sandbox-style integrations.

## First-party LLMSandboxBackend

`agnoclaw` now ships an optional first-party `LLMSandboxBackend` in
`agnoclaw.integrations`.

Install it with:

```bash
uv sync --extra llm-sandbox
```

Use it like this:

```python
from agnoclaw import AgentHarness
from agnoclaw.integrations import LLMSandboxBackend

backend = LLMSandboxBackend(
    sync_paths=["workspace/inputs"],
)

agent = AgentHarness(
    workspace_dir="/srv/workspace",
    backend=backend,
)

# Later, pull selected outputs back to the host.
backend.sync_from_runtime("workspace/outputs")
```

Important behavior:

- it is Docker-first by default
- it mirrors the same absolute workspace paths inside the sandbox
- it does not auto-copy the entire workspace
- `bash`, file tools, and skill installs all run against the same sandbox session
- browser tools still require a separate browser backend
- consumers explicitly choose which paths to push or pull

This keeps one coherent runtime plane without guessing which host paths should be
present inside the sandbox.

## Advanced composition

For tests or thin embeddings, `RuntimeBackend` can also compose lower-level adapters directly:

```python
from agnoclaw import AgentHarness, RuntimeBackend

backend = RuntimeBackend(
    command_executor=SandboxExecutor(sandbox),
    workspace_adapter=SandboxWorkspace(sandbox),
    browser_backend=SandboxBrowser(sandbox),
)

agent = AgentHarness(workspace_dir="/srv/workspace", backend=backend)
```

This is supported, but the cleaner public experience is still “wrap one sandbox/session object once, pass one backend everywhere.”

## What the backend covers

`RuntimeBackend` resolution feeds:

- `bash`, `bash_start`, `bash_output`, `bash_kill`
- `read_file`, `write_file`, `edit_file`, `multi_edit_file`, `glob_files`, `grep_files`, `list_dir`
- skill `!` inline command execution for trusted skills
- skill install checks and dependency installs
- `BrowserToolkit`, when browser tools are enabled

## What it does not replace

The backend abstraction does not replace:

- prompts
- sessions and storage
- event sinks
- policy checkpoints
- permission approval
- path/network guardrails

The layering stays:

1. `AgentHarness` owns orchestration, policy, permissions, hooks, and guardrails.
2. `RuntimeBackend` owns the runtime plane.
3. Your sandbox/container/remote service owns the actual execution and storage semantics.
