"""
Shell execution tool — safe, auditable bash with timeout.

Rules (aligned with Claude Code's tool guidelines):
- Use for: git, npm/pip/cargo, test runners, docker, build tools, system commands
- NOT for: reading files, searching content, writing files — use dedicated file tools
- Never use interactive flags (-i)
- Always runs in a subprocess with timeout
"""

from __future__ import annotations

import subprocess
from typing import Optional

from agno.tools import tool


def make_bash_tool(timeout: int = 120, workspace_dir: Optional[str] = None):
    """
    Factory that returns a bash tool function with the given timeout.
    Optionally sets a default working directory.
    """

    @tool(
        name="bash",
        description=(
            "Execute a shell command. Use for git, npm, pip, cargo, docker, test runners, "
            "and other system operations. "
            "DO NOT use for file reads/writes/searches — use the dedicated file tools instead. "
            "Always quote paths with spaces. Never use interactive flags (-i). "
            "Commands timeout after "
            + str(timeout)
            + " seconds."
        ),
        show_result=True,
    )
    def bash(
        command: str,
        description: Optional[str] = None,
        working_dir: Optional[str] = None,
    ) -> str:
        """
        Run a bash command and return its output.

        Args:
            command: The shell command to execute.
            description: Optional human-readable description of what this command does.
            working_dir: Working directory for the command. Defaults to current directory.
        """
        cwd = working_dir or workspace_dir

        try:
            result = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=cwd,
            )
            output = result.stdout
            if result.returncode != 0:
                stderr = result.stderr.strip()
                if stderr:
                    output += f"\n[stderr]\n{stderr}"
                output += f"\n[exit code: {result.returncode}]"
            return output.strip() if output.strip() else "[no output]"
        except subprocess.TimeoutExpired:
            return f"[error] Command timed out after {timeout} seconds: {command}"
        except Exception as e:
            return f"[error] Failed to execute command: {e}"

    return bash
