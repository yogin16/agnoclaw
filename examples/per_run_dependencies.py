"""
Per-run dependencies — one scope contract for caller context (issue #52).

Set caller scope once per run as ``dependencies``; every tool and every custom
dispatch adapter reads it from one agno-native contract (the run's RunContext),
and the values never reach the model.

Two readers of the same per-run scope:
  1. A plain ``@tool`` function — Agno injects ``run_context: RunContext`` and
     strips it from the model-facing schema.
  2. A custom dispatch adapter — reads the active context via
     ``get_current_dependencies()`` (no run_context parameter needed).

Run: uv run python examples/per_run_dependencies.py
"""

from _utils import detect_model
from agno.run.base import RunContext
from agno.tools import tool

from agnoclaw import AgentHarness, get_current_dependencies


@tool
def whoami(run_context: RunContext) -> str:
    """Return the tenant the current request is scoped to."""
    deps = run_context.dependencies or {}
    # A custom dispatch adapter (no run_context param) reads the SAME scope:
    adapter_view = get_current_dependencies() or {}
    assert adapter_view.get("tenant_id") == deps.get("tenant_id")
    return f"tenant={deps.get('tenant_id')} user={deps.get('user_id')}"


agent = AgentHarness(
    name="scoped-agent",
    model=detect_model(),
    tools=[whoami],
    # Construction-time default; per-run dependencies merge over this.
    dependencies={"env": "prod"},
    # Keep caller scope out of the model context (the default).
    add_dependencies_to_context=False,
)

# Set scope once for this single run — restored automatically afterwards.
agent.print_response(
    "Call whoami and report exactly what it returns.",
    dependencies={"tenant_id": "acme", "user_id": "u-123"},
    stream=True,
)

# A second run with different scope never leaks the first run's dependencies.
agent.print_response(
    "Call whoami again and report exactly what it returns.",
    dependencies={"tenant_id": "globex", "user_id": "u-456"},
    stream=True,
)
