"""
Per-run tool argument binding — partial application for a tool (issue #54).

Bind specific tool arguments for a single run: the bound args are removed from
the schema the model sees AND supplied at dispatch. Effectively the model is
handed `functools.partial(tool, **bound)` for that run, with the pre-bound
values chosen per run (e.g. resolved from the active skill or per-request scope).

Composes with `tool_schema_overrides` (re-type the remaining visible args) and a
skill's `allowed_tools`. Restored after the run; a later run without the binding
sees the full signature.

Run: uv run python examples/tool_arg_bindings.py
"""

from _utils import detect_model
from agno.tools import tool

from agnoclaw import AgentHarness


@tool
def save_artifact(name: str, kind: str, schema: str, content: str) -> str:
    """Persist an artifact.

    Args:
      name: human label for the artifact
      kind: artifact kind (bound per run)
      schema: payload schema id (bound per run)
      content: the artifact body the model produces
    """
    return f"saved {name} [{kind}/{schema}] ({len(content)} chars)"


agent = AgentHarness(
    name="binder",
    model=detect_model(),
    tools=[save_artifact],
)

# This run: the model only sees `name` and `content`; `kind`/`schema` are
# pre-bound and injected at dispatch (it never sees or sets them).
agent.print_response(
    "Create a short note artifact named 'welcome' and save it.",
    tool_arg_bindings={"save_artifact": {"kind": "note", "schema": "v1"}},
    stream=True,
)

# A later run without the binding sees the full four-arg signature again.
agent.print_response(
    "Save an artifact named 'report' of kind 'doc' with schema 'v2'.",
    stream=True,
)
