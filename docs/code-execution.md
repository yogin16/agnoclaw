# Code execution models

Agnoclaw has multiple code-related surfaces because programming interface, execution
location, and security boundary are separate decisions. Agno CodeMode complements the
existing runtime backend; it does not replace or inherit its isolation boundary.

## Capability map

| Surface | What the model receives | Where work executes | Persistent state | Security boundary |
|---|---|---|---|---|
| Built-in Bash/files | Explicit governed tool schemas | Host by default, or the configured `RuntimeBackend` | Files and background-task handles | Policy/permissions plus the selected backend |
| `LLMSandboxBackend` | The same Bash/file/skill tools | One Docker-first `llm-sandbox` session | Selected files synchronized by the host | The container/runtime configuration |
| Agno 3 CodeMode | One `execute` tool with awaitable handles for the original tools | A harness-owned IPython kernel on the host | Owner/user/session-scoped kernel variables and optional DB snapshots | None for arbitrary Python; `allow_shell=false` only removes CodeMode's shell helper |
| Notebook toolkit | Read/edit/add-cell document tools | Through the configured workspace adapter | `.ipynb` files | The selected workspace backend; it does not execute cells |
| `sandbox_dir` / `sandbox_mode` | No new tool | Routes built-in Bash/file paths | Session scratch files | Filesystem routing, not OS/process/network isolation |

ResultStore and media offloading are storage optimizations. They do not execute code
and do not change any boundary in this table.

## The three integration models

### Governed host tools

This is the default developer model. The model makes explicit Bash/file tool calls;
Agnoclaw applies tool policy, permission, lifecycle, and output handling around each
call. `sandbox_dir` gives relative work a session scratch directory, but the underlying
shell still has the host process's authority.

```python
agent = AgentHarness(
    workspace_dir="/srv/project",
    sandbox_dir="/tmp/agent-session",
    sandbox_mode="read_only",
)
```

Use this for trusted local development where auditability and predictable file routing
matter more than hostile-code containment.

### Backend-contained tools

Pass one `RuntimeBackend` when Bash, files, skill commands, and optional browser work
must share an external execution plane. The first-party `LLMSandboxBackend` is
Docker-first and is the current supported route for sandboxed code execution.

```python
from agnoclaw import AgentHarness
from agnoclaw.integrations import LLMSandboxBackend

backend = LLMSandboxBackend(sync_paths=["workspace/inputs"])
agent = AgentHarness(workspace_dir="/srv/project", backend=backend)
```

Keep `code_mode` disabled when containment is the objective. The model can still write
and execute Python through the backend-routed Bash tool, including multi-step scripts;
it simply does not receive a persistent in-process IPython kernel.

### CodeMode host kernel

CodeMode reduces a wide tool schema to one programmable surface and lets the model use
Python control flow across awaitable tool handles. It is useful for trusted analytical
work, complex tool composition, and carrying bounded variables between cells.

```python
agent = AgentHarness(
    code_mode=True,
    config=HarnessConfig(
        code_mode_allow_shell=False,
        code_mode_snapshot=True,
        code_mode_max_kernels=4,
    ),
)
```

The owned kernel is a host child process whose initial working directory is
`sandbox_dir`. A working directory is not confinement: normal Python imports,
`open()`, `subprocess`, and network APIs retain the host process's permissions.
CodeMode is therefore explicit-only and limited to quick/legacy profiles.

## What happens when CodeMode and a backend are combined

The two planes compose only at Agnoclaw tool handles:

```text
model -> CodeMode execute -> host IPython kernel
                              |-- await bash(...) -> governed tool -> RuntimeBackend
                              |-- await read_file(...) -----------> RuntimeBackend
                              `-- open/subprocess/socket/import ---> host directly
```

This can be useful when trusted host-side Python coordinates remote tools, but it is
not a sandboxed CodeMode. Direct Python can bypass the backend, tool permissions, and
tool-level output handling. Do not combine the two when the kernel code is untrusted.

A genuinely contained persistent CodeMode needs a remote-kernel transport whose
process, filesystem, network, snapshots, and shutdown are all owned by the same
`RuntimeBackend`. Agno 3.0.6 CodeMode exposes a local kernel process rather than that
backend contract, so Agnoclaw does not claim this integration today.

## Selection guide

| Need | Choose |
|---|---|
| Auditable file/shell steps on a trusted workstation | Built-in Bash/files with a session `sandbox_dir` |
| Execute model-written code outside the host | `LLMSandboxBackend`, CodeMode off |
| Fewer tool schemas and Python control flow for trusted work | CodeMode |
| Edit notebooks without executing them | Notebook toolkit |
| Durable/service restart recovery | Governed tools through the lifecycle; CodeMode is currently rejected |
| Sandboxed persistent Python kernel | Not yet supported as one integrated plane |

The runtime backend remains the execution-location abstraction. CodeMode remains a
model-interface optimization until a backend-owned kernel adapter is available.
