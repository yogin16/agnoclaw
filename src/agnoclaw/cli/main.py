"""
agnoclaw CLI — interactive and one-shot agent execution.

Commands:
    agnoclaw chat              Interactive chat session (like Claude Code)
    agnoclaw run "task"        One-shot task execution
    agnoclaw skill list        List available skills
    agnoclaw skill inspect     Show a skill's full content
    agnoclaw heartbeat start   Start heartbeat daemon
    agnoclaw heartbeat trigger Run one heartbeat check now
    agnoclaw workspace show    Show workspace directory and files
    agnoclaw workspace init    Initialize workspace
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

console = Console()


def _build_agent(
    model: Optional[str],
    provider: Optional[str],
    session: Optional[str],
    workspace: Optional[str],
    debug: bool,
):
    """Shared factory for building a HarnessAgent from CLI options."""
    from agnoclaw import HarnessAgent

    return HarnessAgent(
        model_id=model,
        provider=provider,
        session_id=session,
        workspace_dir=workspace,
        debug=debug,
    )


# ── Root CLI group ─────────────────────────────────────────────────────────────

@click.group()
@click.version_option(package_name="agnoclaw")
def cli():
    """agnoclaw — a hackable, model-agnostic agent harness built on Agno."""
    pass


# ── Global options (shared across subcommands) ─────────────────────────────────

MODEL_OPT = click.option("--model", "-m", default=None, help="Model ID (e.g. claude-sonnet-4-6, gpt-4o)")
PROVIDER_OPT = click.option("--provider", "-p", default=None, help="Provider (anthropic, openai, google, groq, ollama...)")
SESSION_OPT = click.option("--session", "-s", default=None, help="Session ID for persistence")
WORKSPACE_OPT = click.option("--workspace", "-w", default=None, help="Workspace directory path")
DEBUG_OPT = click.option("--debug", is_flag=True, default=False, help="Enable debug mode (show tool calls)")
SKILL_OPT = click.option("--skill", default=None, help="Activate a skill for this run (skill name)")


# ── agnoclaw chat ──────────────────────────────────────────────────────────────

@cli.command()
@MODEL_OPT
@PROVIDER_OPT
@SESSION_OPT
@WORKSPACE_OPT
@DEBUG_OPT
def chat(model, provider, session, workspace, debug):
    """Start an interactive chat session."""
    agent = _build_agent(model, provider, session, workspace, debug)

    console.print(Panel(
        f"[bold cyan]agnoclaw[/bold cyan] — interactive session\n"
        f"Workspace: [dim]{agent.workspace.path}[/dim]\n"
        f"Type [bold]/quit[/bold] or [bold]Ctrl+C[/bold] to exit. "
        f"[bold]/skill <name>[/bold] to activate a skill. "
        f"[bold]/clear[/bold] to reset session.",
        border_style="cyan",
    ))

    while True:
        try:
            user_input = click.prompt("\n[you]", prompt_suffix=" > ")
        except (click.Abort, EOFError, KeyboardInterrupt):
            console.print("\n[dim]Goodbye.[/dim]")
            break

        if not user_input.strip():
            continue

        # Handle slash commands
        if user_input.strip().startswith("/"):
            if _handle_slash_command(user_input.strip(), agent):
                continue
            if user_input.strip() in ("/quit", "/exit", "/q"):
                console.print("[dim]Goodbye.[/dim]")
                break

        # Extract inline skill activation (/skill name at end)
        active_skill = None
        if "--skill" in user_input:
            parts = user_input.split("--skill", 1)
            user_input = parts[0].strip()
            active_skill = parts[1].strip().split()[0] if parts[1].strip() else None

        try:
            console.print("\n[bold green][agent][/bold green]")
            agent.print_response(user_input, stream=True, skill=active_skill)
        except KeyboardInterrupt:
            console.print("\n[dim](interrupted)[/dim]")
        except Exception as e:
            console.print(f"\n[red][error][/red] {e}")
            if debug:
                import traceback
                traceback.print_exc()


def _handle_slash_command(command: str, agent) -> bool:
    """Handle /slash commands in chat mode. Returns True if handled."""
    parts = command.split(None, 1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if cmd == "/skill":
        if not args:
            console.print("[yellow]Usage: /skill <name>[/yellow]")
        else:
            content = agent.skills.load_skill(args.strip())
            if content:
                console.print(f"[green]Activated skill: {args.strip()}[/green]")
                # Will be used on next message via the agent.skills call
            else:
                console.print(f"[red]Skill not found: {args.strip()}[/red]")
        return True

    if cmd in ("/skills", "/skill list"):
        _print_skill_list(agent.skills.list_skills())
        return True

    if cmd == "/clear":
        console.print("[dim]Session context cleared (note: stored history remains)[/dim]")
        return True

    if cmd == "/workspace":
        console.print(f"[cyan]Workspace: {agent.workspace.path}[/cyan]")
        files = agent.workspace.context_files()
        for name, content in files.items():
            console.print(f"  [dim]{name.upper()}.md[/dim]: {len(content)} chars")
        return True

    if cmd in ("/help", "/?"):
        console.print(
            "[bold]Chat commands:[/bold]\n"
            "  /skill <name>  — activate a skill for the next message\n"
            "  /skills        — list available skills\n"
            "  /workspace     — show workspace info\n"
            "  /clear         — clear session context\n"
            "  /quit          — exit\n"
        )
        return True

    return False


# ── agnoclaw run ──────────────────────────────────────────────────────────────

@cli.command()
@click.argument("task")
@MODEL_OPT
@PROVIDER_OPT
@SESSION_OPT
@WORKSPACE_OPT
@DEBUG_OPT
@SKILL_OPT
def run(task, model, provider, session, workspace, debug, skill):
    """Run a single task and exit (non-interactive)."""
    agent = _build_agent(model, provider, session, workspace, debug)
    agent.print_response(task, stream=True, skill=skill)


# ── agnoclaw skill ────────────────────────────────────────────────────────────

@cli.group()
def skill():
    """Manage and inspect skills."""
    pass


@skill.command("list")
@WORKSPACE_OPT
def skill_list(workspace):
    """List all available skills."""
    from agnoclaw.skills import SkillRegistry
    from agnoclaw.workspace import Workspace

    ws = Workspace(workspace)
    registry = SkillRegistry(ws.skills_dir())
    skills = registry.list_skills()
    _print_skill_list(skills)


@skill.command("inspect")
@click.argument("name")
@WORKSPACE_OPT
def skill_inspect(name, workspace):
    """Show the full content of a skill."""
    from agnoclaw.skills import SkillRegistry
    from agnoclaw.workspace import Workspace

    ws = Workspace(workspace)
    registry = SkillRegistry(ws.skills_dir())
    content = registry.load_skill(name)
    if content:
        console.print(Markdown(content))
    else:
        console.print(f"[red]Skill not found: {name}[/red]")
        sys.exit(1)


@skill.command("install")
@click.argument("path_or_url")
@WORKSPACE_OPT
def skill_install(path_or_url, workspace):
    """Install a skill from a local path or GitHub URL."""
    from agnoclaw.workspace import Workspace
    import shutil

    ws = Workspace(workspace)
    ws.initialize()

    if path_or_url.startswith("http"):
        console.print(f"[yellow]Remote skill install not yet implemented.[/yellow]")
        console.print(f"Clone the skill directory to {ws.skills_dir()} manually.")
    else:
        src = Path(path_or_url).expanduser()
        if not src.exists():
            console.print(f"[red]Path not found: {src}[/red]")
            sys.exit(1)

        skill_name = src.name
        dest = ws.skills_dir() / skill_name
        if dest.exists():
            console.print(f"[yellow]Skill '{skill_name}' already exists at {dest}. Overwrite? [y/N][/yellow]")
            if input().strip().lower() != "y":
                return

        shutil.copytree(src, dest, dirs_exist_ok=True)
        console.print(f"[green]Installed skill '{skill_name}' to {dest}[/green]")


def _print_skill_list(skills: list[dict]) -> None:
    if not skills:
        console.print("[dim]No skills found.[/dim]")
        return

    table = Table(title="Available Skills", border_style="dim")
    table.add_column("Name", style="cyan bold")
    table.add_column("Description")
    table.add_column("User", justify="center")
    table.add_column("Model", justify="center")
    table.add_column("Tools", style="dim")

    for s in skills:
        user = "✓" if s["user_invocable"] else "—"
        model = "✓" if s["model_invocable"] else "—"
        tools = ", ".join(s["allowed_tools"][:3]) + ("..." if len(s["allowed_tools"]) > 3 else "")
        table.add_row(s["name"], s["description"], user, model, tools or "all")

    console.print(table)


# ── agnoclaw heartbeat ────────────────────────────────────────────────────────

@cli.group()
def heartbeat():
    """Manage the heartbeat daemon."""
    pass


@heartbeat.command("trigger")
@MODEL_OPT
@PROVIDER_OPT
@WORKSPACE_OPT
def heartbeat_trigger(model, provider, workspace):
    """Run one heartbeat check immediately."""
    from agnoclaw import HarnessAgent
    from agnoclaw.heartbeat import HeartbeatDaemon

    agent = HarnessAgent(model_id=model, provider=provider, workspace_dir=workspace)

    def on_alert(msg):
        console.print(Panel(msg, title="[yellow]Heartbeat Alert[/yellow]", border_style="yellow"))

    daemon = HeartbeatDaemon(agent, on_alert=on_alert)

    console.print("[dim]Running heartbeat check...[/dim]")
    result = asyncio.run(daemon.trigger_now())
    if result is None:
        console.print("[green]HEARTBEAT_OK — nothing needs attention.[/green]")


# ── agnoclaw workspace ────────────────────────────────────────────────────────

@cli.group()
def workspace():
    """Manage the agent workspace."""
    pass


@workspace.command("init")
@WORKSPACE_OPT
def workspace_init(workspace):
    """Initialize the workspace directory with default files."""
    from agnoclaw.workspace import Workspace

    ws = Workspace(workspace)
    ws.initialize()
    console.print(f"[green]Workspace initialized at: {ws.path}[/green]")
    console.print("Created default files: AGENTS.md, SOUL.md, HEARTBEAT.md")


@workspace.command("show")
@WORKSPACE_OPT
def workspace_show(workspace):
    """Show workspace directory contents and context files."""
    from agnoclaw.workspace import Workspace

    ws = Workspace(workspace)
    console.print(f"[cyan bold]Workspace:[/cyan bold] {ws.path}")

    if not ws.path.exists():
        console.print("[yellow]Workspace not initialized. Run: agnoclaw workspace init[/yellow]")
        return

    # Show context files
    table = Table(border_style="dim")
    table.add_column("File")
    table.add_column("Status")
    table.add_column("Size")

    for logical_name in ("agents", "soul", "user", "memory", "heartbeat", "boot"):
        from agnoclaw.workspace import WORKSPACE_FILES
        filename = WORKSPACE_FILES.get(logical_name, f"{logical_name.upper()}.md")
        path = ws.path / filename
        if path.exists():
            size = path.stat().st_size
            table.add_row(filename, "[green]exists[/green]", f"{size} bytes")
        else:
            table.add_row(filename, "[dim]missing[/dim]", "—")

    console.print(table)

    # Skills
    skills_dir = ws.skills_dir()
    skill_count = len(list(skills_dir.glob("*/SKILL.md"))) if skills_dir.exists() else 0
    console.print(f"\n[cyan]Skills:[/cyan] {skill_count} workspace-level skills in {skills_dir}")
