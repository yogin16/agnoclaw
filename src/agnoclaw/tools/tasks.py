"""
Task and planning tools.

TodoTool — no-op planning tool (context engineering, not execution).
            Models that write todos reason more clearly about multi-step work.
            Inspired by Claude Code's TodoWrite/TaskUpdate pattern and
            LangChain DeepAgents' TodoListMiddleware.

SubagentTool — spawns an isolated sub-agent for a discrete subtask.
               Protects main context window from bloat (research, analysis,
               code generation). Inspired by Claude Code's Task tool and
               LangChain DeepAgents' SubAgentMiddleware.
"""

from __future__ import annotations

import json
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
