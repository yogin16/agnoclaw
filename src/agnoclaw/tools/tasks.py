"""
Task and planning tools.

TodoToolkit — no-op planning tool (context engineering, not execution).
              Models that write todos reason more clearly about multi-step work.
              Inspired by Claude Code's TodoWrite/TaskUpdate pattern and
              LangChain DeepAgents' TodoListMiddleware.

ProgressToolkit — multi-context-window project persistence.
                  Writes progress.md (session continuity) and features.md
                  (requirement checklist). Inspired by Claude Code's
                  progress.md pattern and the initializer-then-coder pattern
                  for complex multi-phase projects.

SubagentTool — spawns an isolated sub-agent for a discrete subtask.
               Protects main context window from bloat (research, analysis,
               code generation). Inspired by Claude Code's Task tool and
               LangChain DeepAgents' SubAgentMiddleware.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from agno.tools import tool
from agno.tools.toolkit import Toolkit


class TodoToolkit(Toolkit):
    """
    In-memory todo list for planning multi-step work.

    This is a pure context-engineering mechanism — it does not execute tasks,
    it helps the model think through them. Todos are stored in memory for the
    session duration.

    Rules embedded in the tool descriptions:
    - Create todos when a task has 3+ distinct steps
    - Mark tasks in_progress BEFORE starting them
    - Mark tasks completed IMMEDIATELY after finishing (not in batches)
    - Update status in real-time so the user sees progress
    """

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

    def list_todos(self, filter_status: Optional[str] = None) -> str:
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


class ProgressToolkit(Toolkit):
    """
    Multi-context-window project persistence toolkit.

    Designed for complex, long-running projects that span multiple sessions
    or require more context than a single window can hold. The agent uses
    this to:

    - Save progress before context compaction or session end so the NEXT
      session picks up exactly where things left off (progress.md)
    - Track feature-level requirements as a pass/fail checklist — all
      features start failing and get marked passing as they're implemented
      (features.md)

    Typical workflow for a large project:
      1. write_features — define all requirements upfront (all start failing)
      2. Do work across multiple sessions / context windows
      3. update_feature_status — mark each feature passing as it's completed
      4. write_progress — save state before session ends / context compacts
      5. read_progress — at the start of the next session to resume

    Modeled after Claude Code's progress.md + initializer-then-coder pattern
    and the OpenClaw pre-compaction memory flush.

    Args:
        project_dir: Directory where progress.md and features.md are written.
                     Defaults to the current working directory.
    """

    def __init__(self, project_dir: str = "."):
        super().__init__(name="progress")
        self._project_dir = project_dir
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


def make_subagent_tool(default_model: Optional[str] = None):
    """
    Returns a SubagentTool function for spawning isolated sub-agents.

    The sub-agent runs with its own context — keeping the main agent's context clean.
    Results are summarized back to the main agent.
    """

    @tool(
        name="spawn_subagent",
        description=(
            "Spawn an isolated sub-agent to handle a discrete subtask. "
            "Use this to protect the main context from bloat (research, analysis, "
            "code generation, web scraping). "
            "The sub-agent gets its own context and tools. "
            "Results are summarized back. "
            "Do NOT use for simple one-step tasks — those belong in the main agent."
        ),
    )
    def spawn_subagent(
        task: str,
        agent_type: str = "general",
        tools: Optional[list[str]] = None,
        model: Optional[str] = None,
    ) -> str:
        """
        Spawn a sub-agent to handle a specific task.

        Args:
            task: The task description for the sub-agent. Be specific and complete.
            agent_type: Agent specialization: 'general', 'research', 'code', 'data'.
            tools: Tool names the sub-agent should have access to.
            model: Optional model override for this sub-agent.

        Returns:
            The sub-agent's result/summary.
        """
        try:
            from agno.agent import Agent
            from agno.models.anthropic import Claude

            model_id = model or default_model or "claude-haiku-4-5-20251001"

            # Specialize system prompt by agent type
            type_instructions = {
                "research": "You are a research specialist. Search the web, read URLs, and synthesize findings into a clear summary.",
                "code": "You are a code specialist. Write clean, efficient code following best practices.",
                "data": "You are a data analysis specialist. Analyze data, identify patterns, and present insights clearly.",
                "general": "You are a capable assistant. Complete the assigned task thoroughly and efficiently.",
            }
            instructions = type_instructions.get(agent_type, type_instructions["general"])

            # Build tool list
            agent_tools = []
            if not tools or "web" in (tools or []):
                from agnoclaw.tools.web import WebToolkit
                agent_tools.append(WebToolkit())
            if not tools or "files" in (tools or []):
                from agnoclaw.tools.files import FilesToolkit
                agent_tools.append(FilesToolkit())
            if not tools or "bash" in (tools or []):
                from agnoclaw.tools.bash import make_bash_tool
                agent_tools.append(make_bash_tool())

            subagent = Agent(
                model=Claude(id=model_id),
                instructions=instructions,
                tools=agent_tools,
                markdown=True,
            )

            response = subagent.run(task)
            content = response.content if response else "[no response]"

            # Truncate very long responses
            if isinstance(content, str) and len(content) > 8000:
                content = content[:8000] + f"\n... [truncated, {len(content)} chars total]"

            return str(content)

        except Exception as e:
            return f"[error] Subagent failed: {e}"

    return spawn_subagent
