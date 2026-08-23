"""Planning state, durable progress files, and isolated child-agent tools."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agno.tools import tool
from agno.tools.toolkit import Toolkit

from agnoclaw.runtime import HarnessError, PlanExitSignal, PlanQuestionSignal

if TYPE_CHECKING:
    from agno.models.base import Model

    from agnoclaw.backends import RuntimeBackend

from .backends import bind_session_sandbox

logger = logging.getLogger("agnoclaw.tools")


def _legacy_subagent_config(config: Any = None) -> Any:
    """Resolve and validate the named raw-subagent compatibility profile."""
    from agnoclaw.config import RuntimeProfile, get_config

    cfg = config or get_config()
    profile = RuntimeProfile(cfg.profile)
    if profile is not RuntimeProfile.LEGACY:
        raise HarnessError(
            code="RAW_SUBAGENT_LIFECYCLE_UNSUPPORTED",
            category="lifecycle",
            message=(
                "Raw spawn_subagent execution has no durable child lineage. Use "
                "DeclaredChildTemplate/declared child runs, or select "
                "HarnessConfig.legacy() for the temporary compatibility surface."
            ),
            retryable=False,
            details={"profile": profile.value, "replacement": "declared_child_runs"},
        )
    return cfg


class TodoToolkit(Toolkit):
    """In-memory planning state; tool descriptions define its usage protocol."""

    def __init__(self):
        super().__init__(name="todo")
        self._todos: dict[str, dict[str, Any]] = {}
        self._next_id = 1
        self.register(self.create_todo)
        self.register(self.update_todo)
        self.register(self.list_todos)
        self.register(self.delete_todo)

    def create_todo(self, subject: str, description: str = "", priority: str = "medium") -> str:
        """
        Create a new todo item for planning multi-step work.

        Use this when a task has 3 or more distinct steps. Create all todos upfront,
        then work through them one at a time.

        Args:
            subject: Short title for the task (imperative form: "Run tests").
            description: Detailed description of what needs to be done.
            priority: Task priority: 'low', 'medium', or 'high'.

        Returns:
            The created todo's ID.
        """
        todo_id = str(self._next_id)
        self._next_id += 1
        self._todos[todo_id] = {
            "id": todo_id,
            "subject": subject,
            "description": description,
            "status": "pending",
            "priority": priority,
        }
        return f"Created todo #{todo_id}: {subject}"

    def update_todo(self, todo_id: str, status: str, notes: str = "") -> str:
        """
        Update a todo's status. Mark in_progress BEFORE starting, completed RIGHT AFTER finishing.

        Args:
            todo_id: The todo ID to update.
            status: New status: 'pending', 'in_progress', or 'completed'.
            notes: Optional notes about progress or completion.

        Returns:
            Updated todo summary.
        """
        if todo_id not in self._todos:
            return f"[error] Todo #{todo_id} not found. Use list_todos to see available todos."
        valid = {"pending", "in_progress", "completed", "cancelled"}
        if status not in valid:
            return f"[error] Invalid status '{status}'. Must be one of: {', '.join(valid)}"
        self._todos[todo_id]["status"] = status
        if notes:
            self._todos[todo_id]["notes"] = notes
        subject = self._todos[todo_id]["subject"]
        return f"Todo #{todo_id} '{subject}' → {status}"

    def list_todos(self, filter_status: str | None = None) -> str:
        """
        List all todos, optionally filtered by status.

        Args:
            filter_status: Optional status filter: 'pending', 'in_progress', 'completed'.

        Returns:
            Formatted list of todos.
        """
        todos = list(self._todos.values())
        if filter_status:
            todos = [t for t in todos if t["status"] == filter_status]
        if not todos:
            return "No todos found."

        status_icons = {
            "pending": "○",
            "in_progress": "◉",
            "completed": "✓",
            "cancelled": "✗",
        }
        lines = []
        for t in todos:
            icon = status_icons.get(t["status"], "?")
            line = f"{icon} [{t['id']}] {t['subject']} ({t['status']})"
            if t.get("description"):
                line += f"\n     {t['description'][:80]}"
            lines.append(line)
        return "\n".join(lines)

    def delete_todo(self, todo_id: str) -> str:
        """
        Delete a todo item.

        Args:
            todo_id: The todo ID to delete.
        """
        if todo_id not in self._todos:
            return f"[error] Todo #{todo_id} not found."
        subject = self._todos.pop(todo_id)["subject"]
        return f"Deleted todo #{todo_id}: {subject}"


class PlanSignalToolkit(Toolkit):
    """Structured plan-mode signals for questions and plan completion."""

    def __init__(self):
        super().__init__(name="plan_signals")
        self._signals: list[PlanQuestionSignal | PlanExitSignal] = []
        self.register(self.ask_user_question)
        self.register(self.exit_plan_mode)

    @property
    def signals(self) -> tuple[PlanQuestionSignal | PlanExitSignal, ...]:
        """Return captured plan signals."""
        return tuple(self._signals)

    def clear(self) -> None:
        """Clear captured plan signals."""
        self._signals.clear()

    def record_question(
        self,
        question: str,
        *,
        options: list[str] | tuple[str, ...] | str | None = None,
        allow_freeform: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> PlanQuestionSignal:
        signal = PlanQuestionSignal(
            signal_id=f"planq_{uuid4().hex}",
            question=str(question).strip(),
            options=self._normalize_options(options),
            allow_freeform=bool(allow_freeform),
            metadata=dict(metadata or {}),
        )
        self._signals.append(signal)
        return signal

    def record_exit(
        self,
        summary: str,
        *,
        plan_path: str | None = None,
        ready_for_approval: bool = True,
        metadata: dict[str, Any] | None = None,
    ) -> PlanExitSignal:
        signal = PlanExitSignal(
            signal_id=f"planx_{uuid4().hex}",
            summary=str(summary).strip(),
            plan_path=str(plan_path).strip() if plan_path else None,
            ready_for_approval=bool(ready_for_approval),
            metadata=dict(metadata or {}),
        )
        self._signals.append(signal)
        return signal

    @tool(
        name="AskUserQuestion",
        description=(
            "Ask the user a structured planning question with optional choices. "
            "Use in plan mode when requirements are ambiguous."
        ),
        show_result=True,
    )
    def ask_user_question(
        self,
        question: str,
        options: str | list[str] | None = None,
        allow_freeform: bool = True,
    ) -> str:
        """Capture a structured user question signal."""
        signal = self.record_question(
            question,
            options=options,
            allow_freeform=allow_freeform,
            metadata={"tool": "AskUserQuestion"},
        )
        return json.dumps(
            {
                "signal": "AskUserQuestion",
                "signal_id": signal.signal_id,
                "question": signal.question,
                "options": signal.options,
                "allow_freeform": signal.allow_freeform,
                "status": "waiting_for_user",
            },
            ensure_ascii=True,
        )

    @tool(
        name="ExitPlanMode",
        description=(
            "Signal that the implementation plan is ready for user review. "
            "Use before making changes from plan mode."
        ),
        show_result=True,
    )
    def exit_plan_mode(
        self,
        summary: str,
        plan_path: str | None = None,
        ready_for_approval: bool = True,
    ) -> str:
        """Capture a structured plan completion signal."""
        signal = self.record_exit(
            summary,
            plan_path=plan_path,
            ready_for_approval=ready_for_approval,
            metadata={"tool": "ExitPlanMode"},
        )
        return json.dumps(
            {
                "signal": "ExitPlanMode",
                "signal_id": signal.signal_id,
                "summary": signal.summary,
                "plan_path": signal.plan_path,
                "ready_for_approval": signal.ready_for_approval,
                "status": "plan_ready",
            },
            ensure_ascii=True,
        )

    @staticmethod
    def _normalize_options(options: list[str] | tuple[str, ...] | str | None) -> list[str]:
        if options is None:
            return []
        if isinstance(options, (list, tuple)):
            return [str(option).strip() for option in options if str(option).strip()]
        text = str(options).strip()
        if not text:
            return []
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            return [str(option).strip() for option in parsed if str(option).strip()]
        separators = "\n" if "\n" in text else ","
        return [part.strip() for part in text.split(separators) if part.strip()]


class ProgressToolkit(Toolkit):
    """Persist progress and feature status across sessions and context windows."""

    def __init__(self, project_dir: str | Path = "."):
        super().__init__(name="progress")
        self._project_dir = str(Path(project_dir).expanduser().resolve())
        self.register(self.write_progress)
        self.register(self.read_progress)
        self.register(self.write_features)
        self.register(self.read_features)
        self.register(self.update_feature_status)

    def _path(self, filename: str) -> Path:
        return Path(self._project_dir).expanduser() / filename

    def write_progress(self, summary: str, next_steps: str, context: str = "") -> str:
        """
        Save progress for the next session/context window to pick up.

        Call this BEFORE a session ends, when context is nearly full, or
        after completing a major milestone. The next session should call
        read_progress first thing to understand the current state.

        Args:
            summary: What was accomplished so far (plain prose or bullet list).
            next_steps: Concrete next steps for the next session to execute.
            context: Important context the next session needs — file paths,
                     decisions made, known issues, architecture notes.

        Returns:
            Confirmation with the file path written.
        """
        from datetime import datetime

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        parts = [
            f"# Progress — {timestamp}\n",
            f"## Summary\n{summary}",
            f"## Next Steps\n{next_steps}",
        ]
        if context:
            parts.append(f"## Context\n{context}")

        content = "\n\n".join(parts) + "\n"
        path = self._path("progress.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Progress saved to {path}"

    def read_progress(self) -> str:
        """
        Read the progress file from a previous session.

        Call this at the START of a new session or after context compaction
        to understand what was done and what needs to happen next.

        Returns:
            Progress file content, or a message if no progress file exists.
        """
        path = self._path("progress.md")
        if not path.exists():
            return "No previous progress found. Starting fresh."
        return path.read_text(encoding="utf-8")

    def write_features(self, features: str) -> str:
        """
        Write a feature requirements checklist for a complex project.

        Use this at the START of a complex project (or in an initializer
        session) to define all requirements upfront. All features begin as
        'failing'. Mark them 'passing' as they are implemented and verified
        with update_feature_status.

        Args:
            features: JSON array of feature objects, each with:
                - id: short identifier (e.g. "auth-01", "api-pagination")
                - description: what this feature should do (one sentence)
                - status (optional): "failing" (default) or "passing"
              Example: [{"id": "auth-01", "description": "Users can register"}]

        Returns:
            Confirmation with the feature count.
        """
        try:
            items = json.loads(features)
        except (json.JSONDecodeError, TypeError):
            return "[error] features must be a JSON array of {id, description} objects"

        lines = ["# Feature Requirements\n"]
        for item in items:
            fid = item.get("id", "?")
            desc = item.get("description", "")
            status = item.get("status", "failing")
            icon = "✅" if status == "passing" else "❌"
            lines.append(f"{icon} **{fid}**: {desc} `{status}`")

        content = "\n".join(lines) + "\n"
        path = self._path("features.md")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return f"Features written to {path} ({len(items)} items)"

    def read_features(self) -> str:
        """
        Read the feature requirements checklist.

        Shows which features are passing and which are still failing.
        Use this to understand overall project completion at a glance.

        Returns:
            Features checklist, or a message if no features file exists.
        """
        path = self._path("features.md")
        if not path.exists():
            return "No features file found. Use write_features to create one."
        return path.read_text(encoding="utf-8")

    def update_feature_status(self, feature_id: str, status: str) -> str:
        """
        Mark a feature as passing or failing.

        Call this after implementing and verifying each feature. Keeping
        features.md up to date gives you an accurate project completion view
        across sessions.

        Args:
            feature_id: The feature ID to update (e.g. "auth-01").
            status: New status — "passing" or "failing".

        Returns:
            Confirmation, or an error if the feature ID was not found.
        """
        path = self._path("features.md")
        if not path.exists():
            return "[error] No features file found. Use write_features first."
        if status not in {"passing", "failing"}:
            return "[error] status must be 'passing' or 'failing'"

        content = path.read_text(encoding="utf-8")
        lines = content.splitlines()
        new_lines = []
        updated = False

        for line in lines:
            if f"**{feature_id}**" in line:
                if status == "passing":
                    line = line.replace("❌", "✅").replace("`failing`", "`passing`")
                else:
                    line = line.replace("✅", "❌").replace("`passing`", "`failing`")
                updated = True
            new_lines.append(line)

        if not updated:
            return f"[error] Feature '{feature_id}' not found in features.md"

        path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        icon = "✅" if status == "passing" else "❌"
        return f"{icon} Feature '{feature_id}' → {status}"


@dataclass
class SubagentDefinition:
    """
    Pre-registered named subagent definition.

    Mirrors Claude Agent SDK's AgentDefinition pattern: declare subagents
    upfront with fixed description, prompt, tools, and model. The model
    sees named agents in the tool description and can invoke them by name.

    Args:
        description: What this subagent does — shown to the model for selection.
        prompt: System instructions for the subagent.
        tools: Tool names the subagent can use: "web", "files", "bash", or "all".
        model: Model override (e.g. "anthropic:claude-haiku-4-5-20251001").
               None inherits from the parent's default.
    """

    description: str
    prompt: str = ""
    tools: list[str] = field(default_factory=lambda: ["all"])
    model: str | None = None


# Default agent type instructions (used for ad-hoc agent_type= invocations)
_TYPE_INSTRUCTIONS = {
    "research": (
        "You are a research specialist. Search the web, read URLs, and "
        "synthesize findings into a clear summary."
    ),
    "code": "You are a code specialist. Write clean, efficient code following best practices.",
    "data": (
        "You are a data analysis specialist. Analyze data, identify patterns, "
        "and present insights clearly."
    ),
    "general": (
        "You are a capable assistant. Complete the assigned task thoroughly and efficiently."
    ),
}


def _build_subagent_tools(
    tool_names: list[str] | None,
    workspace_dir: str | Path | None = None,
    sandbox_dir: str | Path | None = None,
    sandbox_mode: str | None = None,
    backend: RuntimeBackend | None = None,
    config=None,
) -> list:
    """Build tool instances for a subagent from tool name list."""
    agent_tools: list[Any] = []
    names = tool_names or ["all"]
    resolved_workspace = (
        Path(workspace_dir).expanduser().resolve() if workspace_dir is not None else None
    )
    resolved_sandbox = Path(sandbox_dir).expanduser().resolve() if sandbox_dir is not None else None
    resolved_backend = (
        backend.resolve(workspace_dir=resolved_workspace)
        if backend is not None and resolved_workspace is not None
        else None
    )
    resolved_command_executor = None
    resolved_workspace_adapter = None
    tool_surface_dir: Path | None
    if resolved_backend is not None:
        from agnoclaw.tools.backends import normalize_sandbox_mode

        assert resolved_workspace is not None
        effective_sandbox_mode = normalize_sandbox_mode(
            sandbox_mode if sandbox_mode is not None else resolved_backend.sandbox_mode
        )
        resolved_command_executor, resolved_workspace_adapter = bind_session_sandbox(
            command_executor=resolved_backend.command_executor,
            workspace_adapter=resolved_backend.workspace_adapter,
            workspace_dir=resolved_workspace,
            sandbox_dir=resolved_sandbox,
            sandbox_mode=effective_sandbox_mode,
        )
        adapter_workspace = getattr(
            resolved_workspace_adapter,
            "workspace_dir",
            resolved_sandbox or resolved_workspace,
        )
        if adapter_workspace is None:
            raise RuntimeError("Resolved subagent backend did not provide a workspace root.")
        tool_surface_dir = Path(adapter_workspace).expanduser().resolve(strict=False)
    else:
        tool_surface_dir = resolved_sandbox or resolved_workspace
    if "all" in names or "web" in names:
        from agnoclaw.tools.web import WebToolkit

        if config is None:
            agent_tools.append(WebToolkit())
        else:
            # Subagents inherit the parent's configured network posture; a
            # bare WebToolkit would silently fall back to hard-coded defaults.
            from agnoclaw.runtime.guardrails import RuntimeGuardrails

            agent_tools.append(
                WebToolkit(
                    search_enabled=config.enable_web_search,
                    fetch_enabled=config.enable_web_fetch,
                    network_policy=RuntimeGuardrails(
                        workspace_dir=tool_surface_dir or Path.cwd(),
                        enabled=config.guardrails_enabled,
                        path_enabled=False,
                        network_enabled=config.network_enabled,
                        network_enforce_https=config.network_enforce_https,
                        network_allowed_hosts=config.network_allowed_hosts,
                        network_blocked_hosts=config.network_blocked_hosts,
                        network_block_private_hosts=config.network_block_private_hosts,
                        network_block_in_bash=config.network_block_in_bash,
                    ),
                )
            )
    if "all" in names or "files" in names:
        from agnoclaw.tools.files import FilesToolkit

        agent_tools.append(
            FilesToolkit(
                workspace_dir=tool_surface_dir,
                adapter=resolved_workspace_adapter,
            )
        )
    if "all" in names or "bash" in names:
        from agnoclaw.tools.bash import make_bash_tool

        agent_tools.append(
            make_bash_tool(
                workspace_dir=tool_surface_dir,
                executor=resolved_command_executor,
            )
        )
    return agent_tools


def _resolve_subagent_model(model_id: str, config=None):
    """Resolve a subagent model string to an Agno Model object."""
    from agno.models.utils import get_model

    from agnoclaw.agent import _resolve_model
    from agnoclaw.config import get_config

    cfg = config or get_config()
    model_ref = _resolve_model(model_id, None, cfg)
    return get_model(model_ref)


def _run_subagent(
    task: str,
    instructions: str,
    model_id: str | Model,
    tool_names: list[str] | None = None,
    workspace_dir: str | Path | None = None,
    sandbox_dir: str | Path | None = None,
    sandbox_mode: str | None = None,
    config=None,
    backend: RuntimeBackend | None = None,
) -> str:
    """Create and run an isolated subagent synchronously. Returns result string."""
    from agnoclaw.agent import AgentHarness, get_current_tool_runtime

    cfg = _legacy_subagent_config(config)

    parent_runtime = get_current_tool_runtime()
    subagent_context = AgentHarness._build_subagent_execution_context(
        parent_runtime,
        workspace_id=str(Path(workspace_dir).resolve()) if workspace_dir is not None else None,
    )

    subagent = AgentHarness(
        model=model_id,
        config=cfg,
        workspace_dir=workspace_dir,
        sandbox_dir=sandbox_dir,
        include_default_tools=False,
        tools=_build_subagent_tools(
            tool_names,
            workspace_dir=workspace_dir,
            sandbox_dir=sandbox_dir,
            sandbox_mode=sandbox_mode,
            backend=backend,
            config=cfg,
        ),
        instructions=instructions,
        event_sink=(parent_runtime.get("event_sink") if isinstance(parent_runtime, dict) else None),
        event_sink_mode=(
            parent_runtime.get("event_sink_mode") if isinstance(parent_runtime, dict) else None
        ),
        session_metadata=(
            parent_runtime.get("session_metadata") if isinstance(parent_runtime, dict) else None
        ),
        backend=backend,
    )
    try:
        response = subagent.run(task, context=subagent_context)
    finally:
        subagent.close()
    content = getattr(response, "content", "[no response]") if response else "[no response]"

    # Truncate very long responses to protect parent context
    if isinstance(content, str) and len(content) > 8000:
        content = content[:8000] + f"\n... [truncated, {len(content)} chars total]"

    return str(content)


async def _arun_subagent(
    task: str,
    instructions: str,
    model_id: str | Model,
    tool_names: list[str] | None = None,
    workspace_dir: str | Path | None = None,
    sandbox_dir: str | Path | None = None,
    sandbox_mode: str | None = None,
    config=None,
    backend: RuntimeBackend | None = None,
) -> str:
    """Create and run an isolated subagent asynchronously. Returns result string."""
    from agnoclaw.agent import AgentHarness, get_current_tool_runtime

    cfg = _legacy_subagent_config(config)

    parent_runtime = get_current_tool_runtime()
    subagent_context = AgentHarness._build_subagent_execution_context(
        parent_runtime,
        workspace_id=str(Path(workspace_dir).resolve()) if workspace_dir is not None else None,
    )

    subagent = AgentHarness(
        model=model_id,
        config=cfg,
        workspace_dir=workspace_dir,
        sandbox_dir=sandbox_dir,
        include_default_tools=False,
        tools=_build_subagent_tools(
            tool_names,
            workspace_dir=workspace_dir,
            sandbox_dir=sandbox_dir,
            sandbox_mode=sandbox_mode,
            backend=backend,
            config=cfg,
        ),
        instructions=instructions,
        event_sink=(parent_runtime.get("event_sink") if isinstance(parent_runtime, dict) else None),
        event_sink_mode=(
            parent_runtime.get("event_sink_mode") if isinstance(parent_runtime, dict) else None
        ),
        session_metadata=(
            parent_runtime.get("session_metadata") if isinstance(parent_runtime, dict) else None
        ),
        backend=backend,
    )

    try:
        response = await subagent.arun(task, context=subagent_context)
    finally:
        await subagent.aclose()
    content = getattr(response, "content", "[no response]") if response else "[no response]"

    if isinstance(content, str) and len(content) > 8000:
        content = content[:8000] + f"\n... [truncated, {len(content)} chars total]"

    return str(content)


def make_subagent_tool(
    default_model: str | None = None,
    subagents: dict[str, SubagentDefinition] | None = None,
    workspace_dir: str | Path | None = None,
    sandbox_dir: str | Path | None = None,
    sandbox_mode: str | None = None,
    config=None,
    backend: RuntimeBackend | None = None,
):
    """
    Returns a SubagentTool function for spawning isolated sub-agents.

    The sub-agent runs with its own context — keeping the main agent's context clean.
    Results are summarized back to the main agent.

    Args:
        default_model: Default model for ad-hoc subagents.
        subagents: Named subagent definitions. When provided, the model can
                   invoke them by name via the `agent_name` parameter.
        workspace_dir: Workspace root propagated to spawned files/bash tools.
        sandbox_dir: Session sandbox propagated to spawned files/bash tools.
        sandbox_mode: Session sandbox mode propagated to spawned files/bash tools.
        config: HarnessConfig propagated for model/provider and runtime settings.
    """
    cfg = _legacy_subagent_config(config)
    _subagents = subagents or {}
    _default_model = default_model or "anthropic:claude-haiku-4-5-20251001"

    # Build description that includes named agents if any are registered
    base_desc = (
        "Spawn an isolated sub-agent to handle a discrete subtask. "
        "Use this to protect the main context from bloat (research, analysis, "
        "code generation, web scraping). "
        "The sub-agent gets its own context and tools. "
        "Results are summarized back. "
        "Do NOT use for simple one-step tasks — those belong in the main agent."
    )
    if _subagents:
        agent_lines = []
        for name, defn in _subagents.items():
            agent_lines.append(f"  - '{name}': {defn.description}")
        base_desc += "\n\nNamed agents available:\n" + "\n".join(agent_lines)

    @tool(name="spawn_subagent", description=base_desc)
    def spawn_subagent(
        task: str,
        agent_name: str | None = None,
        agent_type: str = "general",
        prompt: str | None = None,
        tools: list[str] | None = None,
        model: str | None = None,
    ) -> str:
        """
        Spawn a sub-agent to handle a specific task in an isolated context.

        Args:
            task: The task description for the sub-agent. Be specific and complete.
            agent_name: Name of a pre-registered agent (use this when available).
            agent_type: Ad-hoc agent type: 'general', 'research', 'code', 'data'.
                        Ignored when agent_name is provided.
            prompt: Custom system prompt for the sub-agent. Overrides agent_type.
            tools: Tool names: 'web', 'files', 'bash', or 'all' (default).
            model: Optional model override (e.g. 'anthropic:claude-haiku-4-5-20251001').

        Returns:
            The sub-agent's result/summary.
        """
        tool_names: list[str] | None
        try:
            # Resolve named agent definition
            if agent_name and agent_name in _subagents:
                defn = _subagents[agent_name]
                instructions = defn.prompt or _TYPE_INSTRUCTIONS["general"]
                model_id = defn.model or model or _default_model
                tool_names = tools or defn.tools
                logger.debug("Spawning named subagent '%s'", agent_name)
            else:
                # Ad-hoc: use prompt, agent_type, or default
                if prompt:
                    instructions = prompt
                else:
                    instructions = _TYPE_INSTRUCTIONS.get(agent_type, _TYPE_INSTRUCTIONS["general"])
                model_id = model or _default_model
                tool_names = tools

            return _run_subagent(
                task,
                instructions,
                model_id,
                tool_names,
                workspace_dir=workspace_dir,
                sandbox_dir=sandbox_dir,
                sandbox_mode=sandbox_mode,
                config=cfg,
                backend=backend,
            )

        except Exception as e:
            raise RuntimeError(f"Subagent failed: {e}") from e

    return spawn_subagent
