"""
agnoclaw CLI — interactive and one-shot agent execution.

Commands:
    agnoclaw init              Interactive onboarding wizard (first run)
    agnoclaw chat              Interactive chat session (like Claude Code)
    agnoclaw run "task"        One-shot task execution
    agnoclaw skill list        List available skills
    agnoclaw skill inspect     Show a skill's full content
    agnoclaw heartbeat start   Start heartbeat daemon
    agnoclaw heartbeat trigger Run one heartbeat check now
    agnoclaw schedule list     Manage persisted scheduler jobs
    agnoclaw workspace show    Show workspace directory and files
    agnoclaw workspace init    Initialize workspace
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

try:
    import click
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.table import Table
except ImportError as e:
    raise ImportError(
        "CLI dependencies not installed. Install with: pip install 'agnoclaw[cli]'"
    ) from e

console = Console()


def _build_agent(
    model: str | None,
    provider: str | None,
    session: str | None,
    workspace: str | None,
    debug: bool,
    permission_mode: str | None,
):
    """Shared factory for building an AgentHarness from CLI options."""
    from agnoclaw import AgentHarness

    return AgentHarness(
        model=model,
        provider=provider,
        session_id=session,
        workspace_dir=workspace,
        debug=debug,
        permission_mode=permission_mode,
    )


# ── Root CLI group ─────────────────────────────────────────────────────────────

@click.group()
@click.version_option(package_name="agnoclaw")
def cli():
    """agnoclaw — a hackable, model-agnostic agent harness built on Agno."""
    pass


# ── Global options (shared across subcommands) ─────────────────────────────────

MODEL_OPT = click.option(
    "--model",
    "-m",
    default=None,
    help="Model ID (e.g. claude-sonnet-4-6, gpt-4o)",
)
PROVIDER_OPT = click.option(
    "--provider",
    "-p",
    default=None,
    help="Provider (anthropic, openai, google, groq, ollama...)",
)
SESSION_OPT = click.option("--session", "-s", default=None, help="Session ID for persistence")
WORKSPACE_OPT = click.option("--workspace", "-w", default=None, help="Workspace directory path")
DEBUG_OPT = click.option(
    "--debug",
    is_flag=True,
    default=False,
    help="Enable debug mode (show tool calls)",
)
SKILL_OPT = click.option("--skill", default=None, help="Activate a skill for this run (skill name)")
PERMISSION_MODE_OPT = click.option(
    "--permission-mode",
    default=None,
    type=click.Choice(
        ["bypass", "default", "accept_edits", "plan", "dont_ask"],
        case_sensitive=False,
    ),
    help="Runtime permission mode for tool calls.",
)


# ── agnoclaw init ─────────────────────────────────────────────────────────────

@cli.command()
@WORKSPACE_OPT
def init(workspace):
    """Interactive onboarding wizard — personalize your agent workspace."""
    from agnoclaw.workspace import Workspace

    ws = Workspace(workspace)
    ws.initialize()

    console.print(Panel(
        "[bold cyan]agnoclaw init[/bold cyan] — personalize your agent\n"
        "[dim]Press Enter to skip any question.[/dim]",
        border_style="cyan",
    ))

    # Q1: Agent persona / soul
    console.print("\n[bold]1. Agent persona[/bold]")
    console.print("[dim]Describe how your agent should behave (tone, style, values).[/dim]")
    console.print("[dim]Example: 'Direct and concise. Prefers bullet points. No fluff.'[/dim]")
    soul_input = click.prompt("Persona", default="", show_default=False)

    # Q2: User identity
    console.print("\n[bold]2. About you[/bold]")
    console.print("[dim]Your name, timezone, communication preferences.[/dim]")
    console.print("[dim]Example: 'Alice, UTC-8, prefers brief responses, uses Python 3.12'[/dim]")
    user_input = click.prompt("User identity", default="", show_default=False)

    # Q3: Agent capabilities / identity
    console.print("\n[bold]3. Agent capabilities[/bold]")
    console.print("[dim]What should this agent specialize in?[/dim]")
    console.print("[dim]Example: 'Full-stack developer, expert in Python and React'[/dim]")
    identity_input = click.prompt("Capabilities", default="", show_default=False)

    # Q4: Default model
    console.print("\n[bold]4. Default model[/bold]")
    console.print("[dim]Which model should the agent use by default?[/dim]")
    model_input = click.prompt(
        "Model ID",
        default="claude-sonnet-4-6",
        show_default=True,
    )

    # Q5: Enable bash tool
    console.print("\n[bold]5. Shell access[/bold]")
    enable_bash = click.confirm("Allow the agent to run shell commands (bash tool)?", default=True)

    # ── Write files ──────────────────────────────────────────────────────────

    if soul_input.strip():
        existing_soul = ws.read_file("soul") or ""
        # Append persona note below the default
        new_soul = existing_soul.rstrip() + f"\n\n## Persona (from init)\n{soul_input.strip()}\n"
        ws.write_file("soul", new_soul)

    if user_input.strip():
        ws.write_file("user", f"# User\n\n{user_input.strip()}\n")

    if identity_input.strip():
        ws.write_file(
            "identity",
            f"# Identity\n\n{identity_input.strip()}\n",
        )

    # TOOLS.md is prompt context, not executable config.
    tools_lines = [
        "# Tool Preferences",
        "",
        f"- Preferred model for this workspace: `{model_input.strip()}`",
        (
            "- Shell usage preference: "
            f"`{'enabled' if enable_bash else 'avoid unless explicitly needed'}`"
        ),
        "- Note: this file is advisory workspace context for the agent.",
        (
            "- Actual runtime configuration comes from constructor args, environment "
            "variables, or `.agnoclaw.toml`."
        ),
    ]
    ws.write_file("tools", "\n".join(tools_lines) + "\n")

    console.print(f"\n[green]Workspace initialized at: {ws.path}[/green]")
    console.print(
        f"  SOUL.md, USER.md, IDENTITY.md, TOOLS.md written\n"
        f"  Preferred model recorded: [cyan]{model_input.strip()}[/cyan]\n"
        "  Shell preference recorded: "
        f"[cyan]{'enabled' if enable_bash else 'avoid unless needed'}[/cyan]\n"
        f"\nRun [bold]agnoclaw chat[/bold] to start."
    )


# ── agnoclaw chat ──────────────────────────────────────────────────────────────

@cli.command()
@MODEL_OPT
@PROVIDER_OPT
@SESSION_OPT
@WORKSPACE_OPT
@DEBUG_OPT
@PERMISSION_MODE_OPT
@click.option(
    "--sync", "use_sync", is_flag=True, default=False,
    help="Use legacy blocking REPL instead of async",
)
def chat(model, provider, session, workspace, debug, permission_mode, use_sync):
    """Start an interactive chat session.

    By default uses the async REPL with heartbeat notification support.
    Use --sync for the legacy blocking REPL.
    """
    agent = _build_agent(model, provider, session, workspace, debug, permission_mode)

    if not use_sync:
        # Async REPL with heartbeat support
        from agnoclaw.cli.async_repl import AsyncREPL

        repl = AsyncREPL(agent, enable_heartbeat=True, debug=debug)
        try:
            asyncio.run(repl.run())
        except KeyboardInterrupt:
            console.print("\n[dim]Goodbye.[/dim]")
        return

    # Legacy sync REPL
    _chat_sync(agent, debug)


def _chat_sync(agent, debug: bool) -> None:
    """Legacy synchronous chat REPL (Click-based)."""
    queued_skill: str | None = None

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
            handled, queued_skill = _handle_slash_command(user_input.strip(), agent, queued_skill)
            if handled:
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
        elif queued_skill:
            # One-shot /skill activation applies to the next user message only.
            active_skill = queued_skill
            queued_skill = None

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


def _handle_slash_command(
    command: str,
    agent,
    queued_skill: str | None = None,
) -> tuple[bool, str | None]:
    """Handle /slash commands in chat mode. Returns (handled, queued_skill)."""
    parts = command.split(None, 1)
    cmd = parts[0].lower()
    args = parts[1] if len(parts) > 1 else ""

    if cmd == "/skill":
        if not args:
            console.print("[yellow]Usage: /skill <name>[/yellow]")
        else:
            skill_name = args.strip().split()[0]
            names = {s["name"] for s in agent.skills.list_skills()}
            if skill_name in names:
                queued_skill = skill_name
                console.print(f"[green]Queued skill for next message: {skill_name}[/green]")
            else:
                console.print(f"[red]Skill not found: {skill_name}[/red]")
        return True, queued_skill

    if cmd in ("/skills", "/skill list"):
        _print_skill_list(agent.skills.list_skills())
        return True, queued_skill

    if cmd == "/clear":
        new_session = None
        if hasattr(agent, "clear_session_context"):
            new_session = agent.clear_session_context()
        if new_session:
            console.print(
                f"[dim]Session context cleared. New session: {new_session} "
                "(stored history remains).[/dim]"
            )
        else:
            console.print("[dim]Session context cleared (stored history remains).[/dim]")
        return True, queued_skill

    if cmd == "/workspace":
        console.print(f"[cyan]Workspace: {agent.workspace.path}[/cyan]")
        files = agent.workspace.context_files()
        for name, content in files.items():
            console.print(f"  [dim]{name.upper()}.md[/dim]: {len(content)} chars")
        return True, queued_skill

    if cmd in ("/help", "/?"):
        console.print(
            "[bold]Chat commands:[/bold]\n"
            "  /skill <name>  — queue a skill for the next message\n"
            "  /skills        — list available skills\n"
            "  /workspace     — show workspace info\n"
            "  /clear         — clear session context\n"
            "  /quit          — exit\n"
        )
        return True, queued_skill

    return False, queued_skill


# ── agnoclaw tui ──────────────────────────────────────────────────────────────

@cli.command()
@MODEL_OPT
@PROVIDER_OPT
@SESSION_OPT
@WORKSPACE_OPT
@DEBUG_OPT
@PERMISSION_MODE_OPT
def tui(model, provider, session, workspace, debug, permission_mode):
    """Launch the full Textual TUI (requires agnoclaw[tui])."""
    try:
        from agnoclaw.tui import AgnoClawApp
    except ImportError:
        console.print(
            "[red]TUI dependencies not installed.[/red]\n"
            "Install with: [bold]pip install agnoclaw\\[tui][/bold]"
        )
        sys.exit(1)

    agent = _build_agent(model, provider, session, workspace, debug, permission_mode)
    app = AgnoClawApp(agent=agent, debug=debug)
    app.run()


# ── agnoclaw run ──────────────────────────────────────────────────────────────

@cli.command()
@click.argument("task")
@MODEL_OPT
@PROVIDER_OPT
@SESSION_OPT
@WORKSPACE_OPT
@DEBUG_OPT
@SKILL_OPT
@PERMISSION_MODE_OPT
def run(task, model, provider, session, workspace, debug, skill, permission_mode):
    """Run a single task and exit (non-interactive)."""
    agent = _build_agent(model, provider, session, workspace, debug, permission_mode)
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
    import shutil

    from agnoclaw.workspace import Workspace

    ws = Workspace(workspace)
    ws.initialize()

    if path_or_url.startswith("http"):
        console.print("[yellow]Remote skill install not yet implemented.[/yellow]")
        console.print(f"Clone the skill directory to {ws.skills_dir()} manually.")
    else:
        src = Path(path_or_url).expanduser()
        if not src.exists():
            console.print(f"[red]Path not found: {src}[/red]")
            sys.exit(1)

        skill_name = src.name
        dest = ws.skills_dir() / skill_name
        if dest.exists():
            console.print(
                f"[yellow]Skill '{skill_name}' already exists at {dest}. "
                "Overwrite? [y/N][/yellow]"
            )
            if input().strip().lower() != "y":
                return

        shutil.copytree(src, dest, dirs_exist_ok=True)
        console.print(f"[green]Installed skill '{skill_name}' to {dest}[/green]")


# ── agnoclaw pack ─────────────────────────────────────────────────────────────

@cli.group()
def pack():
    """Manage and inspect agnoclaw packs."""
    pass


@pack.command("list")
@click.option("--root", type=click.Path(path_type=Path), default=None, help="Pack store root.")
def pack_list(root):
    """List installed packs."""
    from agnoclaw.packs import is_pack_trusted, list_installed_packs

    packs = list_installed_packs(root=root)
    if not packs:
        console.print("[dim]No packs installed.[/dim]")
        return

    table = Table(title="Installed Packs", border_style="dim")
    table.add_column("Name", style="cyan bold")
    table.add_column("Version")
    table.add_column("Trusted", justify="center")
    table.add_column("Description")
    for manifest in packs:
        table.add_row(
            manifest.name,
            manifest.version,
            "yes" if is_pack_trusted(manifest.root) else "no",
            manifest.description,
        )
    console.print(table)


@pack.command("inspect")
@click.argument("path", type=click.Path(path_type=Path))
def pack_inspect(path):
    """Inspect a pack manifest without executing pack code."""
    from agnoclaw.packs import PackError, inspect_pack, is_pack_trusted

    try:
        manifest = inspect_pack(path)
    except PackError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(1)

    console.print(Panel(
        f"[bold cyan]{manifest.name}[/bold cyan] v{manifest.version}\n"
        f"[dim]{manifest.description or 'No description'}[/dim]\n\n"
        f"Root: {manifest.root}\n"
        f"Trusted locally: {'yes' if is_pack_trusted(manifest.root) else 'no'}\n"
        f"Requires code execution: {manifest.trust.requires_code_execution}\n"
        f"Default trust: {manifest.trust.default}",
        title="Pack",
        border_style="cyan",
    ))

    provides = Table(title="Provides", border_style="dim")
    provides.add_column("Type", style="cyan")
    provides.add_column("Entries")
    for label, entries in (
        ("skills", manifest.provides.skills),
        ("tools", manifest.provides.tools),
        ("hooks", manifest.provides.hooks),
        ("context providers", manifest.provides.context_providers),
        ("policies", manifest.provides.policies),
        ("commands", manifest.provides.commands),
    ):
        provides.add_row(label, ", ".join(entries) or "none")
    console.print(provides)


@pack.command("install")
@click.argument("source")
@click.option("--root", type=click.Path(path_type=Path), default=None, help="Pack store root.")
@click.option("--overwrite", is_flag=True, default=False, help="Replace an existing pack.")
def pack_install(source, root, overwrite):
    """Install a local or git+ pack."""
    from agnoclaw.packs import PackError, install_pack

    try:
        manifest = install_pack(source, root=root, overwrite=overwrite)
    except PackError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(1)
    console.print(f"[green]Installed pack '{manifest.name}' to {manifest.root}[/green]")


@pack.command("trust")
@click.argument("name")
@click.option("--root", type=click.Path(path_type=Path), default=None, help="Pack store root.")
def pack_trust(name, root):
    """Trust an installed pack for code-executing registrations."""
    from agnoclaw.packs import PackError, trust_pack

    try:
        manifest = trust_pack(name, root=root)
    except PackError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(1)
    console.print(f"[green]Trusted pack '{manifest.name}'[/green]")


@pack.command("remove")
@click.argument("name")
@click.option("--root", type=click.Path(path_type=Path), default=None, help="Pack store root.")
def pack_remove(name, root):
    """Remove an installed pack."""
    from agnoclaw.packs import remove_pack

    if remove_pack(name, root=root):
        console.print(f"[green]Removed pack '{name}'[/green]")
        return
    console.print(f"[yellow]Pack not installed: {name}[/yellow]")


# ── agnoclaw schedule ─────────────────────────────────────────────────────────

SCHEDULE_STORE_OPT = click.option(
    "--store",
    type=click.Path(path_type=Path),
    default=None,
    help="Scheduler JSON store path. Defaults to ~/.agnoclaw/schedules.json.",
)


def _scheduler_backend(store: Path | None):
    from agnoclaw.runtime import JsonSchedulerBackend, scheduler_store_path

    return JsonSchedulerBackend(scheduler_store_path(store))


@cli.group()
def schedule():
    """Manage embedded scheduler jobs."""
    pass


@schedule.command("list")
@SCHEDULE_STORE_OPT
@click.option("--enabled", is_flag=True, default=False, help="Show only enabled jobs.")
@click.option("--disabled", is_flag=True, default=False, help="Show only disabled jobs.")
def schedule_list(store, enabled, disabled):
    """List local scheduler jobs."""
    if enabled and disabled:
        console.print("[red]Use only one of --enabled or --disabled.[/red]")
        sys.exit(1)
    backend = _scheduler_backend(store)
    enabled_filter = True if enabled else False if disabled else None
    jobs = backend.list_jobs(enabled=enabled_filter)
    if not jobs:
        console.print("[dim]No scheduler jobs found.[/dim]")
        return

    table = Table(title="Scheduler Jobs", border_style="dim")
    table.add_column("Name", style="cyan bold")
    table.add_column("Schedule")
    table.add_column("Enabled", justify="center")
    table.add_column("Skill")
    table.add_column("Isolated", justify="center")
    table.add_column("Prompt")
    for job in jobs:
        table.add_row(
            job.name,
            job.schedule,
            "yes" if job.enabled else "no",
            job.skill or "",
            "yes" if job.isolated else "no",
            job.prompt[:80] + ("..." if len(job.prompt) > 80 else ""),
        )
    console.print(table)


@schedule.command("add")
@click.argument("name")
@click.option("--schedule", "schedule_expr", required=True, help="Cron expression or interval.")
@click.option("--prompt", required=True, help="Prompt to run when the job fires.")
@click.option("--skill", default=None, help="Skill to activate for this job.")
@click.option("--isolated", is_flag=True, default=False, help="Run in a fresh session.")
@click.option("--model", "model_id", default=None, help="Model override for this job.")
@click.option("--provider", default=None, help="Provider override for this job.")
@click.option("--disabled", is_flag=True, default=False, help="Create disabled.")
@SCHEDULE_STORE_OPT
def schedule_add(name, schedule_expr, prompt, skill, isolated, model_id, provider, disabled, store):
    """Create or update a local scheduler job."""
    from agnoclaw.heartbeat.daemon import CronJob, HeartbeatDaemon

    if HeartbeatDaemon._seconds_until_next(schedule_expr) < 0 and len(schedule_expr.split()) < 5:
        console.print(
            f"[red]Invalid schedule '{schedule_expr}'. Use an interval like '30m' "
            "or a 5-field cron expression.[/red]"
        )
        sys.exit(1)

    backend = _scheduler_backend(store)
    job = CronJob(
        name=name,
        schedule=schedule_expr,
        prompt=prompt,
        skill=skill,
        isolated=isolated,
        model_id=model_id,
        provider=provider,
        enabled=not disabled,
    )
    stored = backend.upsert_job(job.to_scheduler_job())
    console.print(
        f"[green]Saved schedule '{stored.name}' "
        f"({'enabled' if stored.enabled else 'disabled'})[/green]"
    )


@schedule.command("show")
@click.argument("name")
@SCHEDULE_STORE_OPT
def schedule_show(name, store):
    """Show a local scheduler job."""
    backend = _scheduler_backend(store)
    job = backend.get_job(name)
    if job is None:
        console.print(f"[red]Schedule not found: {name}[/red]")
        sys.exit(1)
    console.print(Panel(
        f"[bold cyan]{job.name}[/bold cyan]\n"
        f"Schedule: {job.schedule}\n"
        f"Enabled: {job.enabled}\n"
        f"Skill: {job.skill or 'none'}\n"
        f"Isolated: {job.isolated}\n"
        f"Model: {job.model_id or 'default'}\n"
        f"Provider: {job.provider or 'default'}\n"
        f"Created: {job.created_at}\n"
        f"Updated: {job.updated_at}\n\n"
        f"{job.prompt}",
        title="Schedule",
        border_style="cyan",
    ))


@schedule.command("remove")
@click.argument("name")
@SCHEDULE_STORE_OPT
def schedule_remove(name, store):
    """Delete a local scheduler job."""
    backend = _scheduler_backend(store)
    if backend.delete_job(name):
        console.print(f"[green]Removed schedule '{name}'[/green]")
        return
    console.print(f"[yellow]Schedule not found: {name}[/yellow]")


@schedule.command("enable")
@click.argument("name")
@SCHEDULE_STORE_OPT
def schedule_enable(name, store):
    """Enable a local scheduler job."""
    backend = _scheduler_backend(store)
    if backend.set_job_enabled(name, True) is None:
        console.print(f"[red]Schedule not found: {name}[/red]")
        sys.exit(1)
    console.print(f"[green]Enabled schedule '{name}'[/green]")


@schedule.command("disable")
@click.argument("name")
@SCHEDULE_STORE_OPT
def schedule_disable(name, store):
    """Disable a local scheduler job."""
    backend = _scheduler_backend(store)
    if backend.set_job_enabled(name, False) is None:
        console.print(f"[red]Schedule not found: {name}[/red]")
        sys.exit(1)
    console.print(f"[green]Disabled schedule '{name}'[/green]")


@schedule.command("runs")
@click.argument("name", required=False)
@click.option("--limit", default=20, show_default=True, type=int, help="Maximum runs to show.")
@SCHEDULE_STORE_OPT
def schedule_runs(name, limit, store):
    """List scheduler run history."""
    backend = _scheduler_backend(store)
    runs = backend.list_runs(job_name=name, limit=limit)
    if not runs:
        console.print("[dim]No scheduler runs found.[/dim]")
        return

    table = Table(title="Scheduler Runs", border_style="dim")
    table.add_column("Run ID", style="cyan")
    table.add_column("Job")
    table.add_column("Status")
    table.add_column("Started")
    table.add_column("Finished")
    table.add_column("Result")
    for run in runs:
        result = run.error or run.output or ""
        table.add_row(
            run.run_id,
            run.job_name,
            run.status,
            run.started_at,
            run.finished_at or "",
            result[:80] + ("..." if len(result) > 80 else ""),
        )
    console.print(table)


@schedule.command("trigger")
@click.argument("name")
@SCHEDULE_STORE_OPT
@WORKSPACE_OPT
@PERMISSION_MODE_OPT
@MODEL_OPT
@PROVIDER_OPT
def schedule_trigger(name, store, workspace, permission_mode, model, provider):
    """Run a local scheduler job immediately and record run history."""
    from agnoclaw.heartbeat import HeartbeatDaemon

    backend = _scheduler_backend(store)
    job = backend.get_job(name)
    if job is None:
        console.print(f"[red]Schedule not found: {name}[/red]")
        sys.exit(1)

    agent = _build_agent(
        model or job.model_id,
        provider or job.provider,
        None,
        workspace,
        False,
        permission_mode,
    )
    daemon = HeartbeatDaemon(agent, scheduler_backend=backend)
    console.print(f"[dim]Triggering schedule '{name}'...[/dim]")
    result = asyncio.run(daemon.trigger_cron(name))
    if result is None:
        console.print("[yellow]Schedule completed without output.[/yellow]")
        return
    console.print(result)


# ── agnoclaw hub ─────────────────────────────────────────────────────────────

@cli.group()
def hub():
    """Browse, search, and install skills from ClawHub."""
    pass


@hub.command("search")
@click.argument("query")
@click.option("--category", "-c", default="", help="Filter by category")
@click.option("--limit", "-n", default=20, type=int, help="Max results")
def hub_search(query, category, limit):
    """Search for skills on ClawHub."""
    from agnoclaw.skills.hub import ClawHubClient

    client = ClawHubClient()
    try:
        results = client.search(query, category=category, limit=limit)
    finally:
        client.close()

    if not results:
        console.print(f"[dim]No skills found for '{query}'.[/dim]")
        return

    table = Table(title=f"ClawHub: '{query}'", border_style="dim")
    table.add_column("Name", style="cyan bold")
    table.add_column("Description")
    table.add_column("Author", style="dim")
    table.add_column("Downloads", justify="right")

    for skill in results:
        table.add_row(
            f"{skill.emoji} {skill.name}" if skill.emoji else skill.name,
            skill.description[:60] + ("..." if len(skill.description) > 60 else ""),
            skill.author,
            str(skill.downloads),
        )

    console.print(table)


@hub.command("inspect")
@click.argument("name")
def hub_inspect(name):
    """Show full details of a ClawHub skill."""
    from agnoclaw.skills.hub import ClawHubClient

    client = ClawHubClient()
    try:
        detail = client.inspect(name)
    finally:
        client.close()

    if not detail:
        console.print(f"[red]Skill not found: {name}[/red]")
        sys.exit(1)

    console.print(Panel(
        f"[bold cyan]{detail.emoji} {detail.name}[/bold cyan] v{detail.version}\n"
        f"[dim]{detail.description}[/dim]\n\n"
        f"Author: {detail.author}\n"
        f"Downloads: {detail.downloads}\n"
        f"Categories: {', '.join(detail.categories) or 'none'}\n"
        f"Homepage: {detail.homepage or 'none'}\n"
        f"Repository: {detail.repository or 'none'}\n"
        f"Dependencies: {', '.join(detail.dependencies) or 'none'}",
        title=f"ClawHub: {name}",
        border_style="cyan",
    ))

    if detail.skill_md_preview:
        console.print("\n[bold]SKILL.md Preview:[/bold]")
        console.print(Markdown(detail.skill_md_preview[:2000]))


@hub.command("install")
@click.argument("name")
@WORKSPACE_OPT
def hub_install(name, workspace):
    """Install a skill from ClawHub to your workspace."""
    from agnoclaw.skills import SkillRegistry
    from agnoclaw.workspace import Workspace

    ws = Workspace(workspace)
    ws.initialize()
    registry = SkillRegistry(ws.skills_dir())

    console.print(f"[dim]Installing '{name}' from ClawHub...[/dim]")
    skill_dir = registry.install_from_hub(name)

    if skill_dir:
        console.print(f"[green]Installed '{name}' to {skill_dir}[/green]")
        # Verify it loads
        content = registry.load_skill(name)
        if content:
            console.print("[green]Verified: skill loads successfully[/green]")
        else:
            console.print("[yellow]Warning: skill installed but failed to load[/yellow]")
    else:
        console.print(f"[red]Failed to install '{name}' from ClawHub[/red]")
        sys.exit(1)


@hub.command("categories")
def hub_categories():
    """List available skill categories on ClawHub."""
    from agnoclaw.skills.hub import ClawHubClient

    client = ClawHubClient()
    try:
        cats = client.categories()
    finally:
        client.close()

    if not cats:
        console.print("[dim]No categories found.[/dim]")
        return

    console.print("[bold]ClawHub Categories:[/bold]")
    for cat in cats:
        console.print(f"  - {cat}")


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


@heartbeat.command("start")
@MODEL_OPT
@PROVIDER_OPT
@WORKSPACE_OPT
@PERMISSION_MODE_OPT
@click.option(
    "--interval",
    "-i",
    default=None,
    type=int,
    help="Check interval in minutes (overrides config)",
)
def heartbeat_start(model, provider, workspace, permission_mode, interval):
    """Start the heartbeat daemon (runs until Ctrl+C)."""
    from agnoclaw import AgentHarness
    from agnoclaw.heartbeat import HeartbeatDaemon

    agent = AgentHarness(
        model=model,
        provider=provider,
        workspace_dir=workspace,
        permission_mode=permission_mode,
    )

    if agent.workspace.is_empty_heartbeat():
        console.print(
            "[yellow]HEARTBEAT.md is empty — nothing to check.[/yellow]\n"
            f"Edit {agent.workspace.path / 'HEARTBEAT.md'} to add checklist items."
        )
        return

    def on_alert(msg):
        console.print(Panel(msg, title="[yellow]Heartbeat Alert[/yellow]", border_style="yellow"))

    daemon = HeartbeatDaemon(agent, on_alert=on_alert)

    # Override interval if provided
    if interval is not None:
        daemon._config.heartbeat.interval_minutes = interval

    interval_min = daemon._config.heartbeat.interval_minutes
    console.print(
        f"[dim]Heartbeat daemon starting (interval={interval_min}m). "
        f"Press Ctrl+C to stop.[/dim]"
    )

    async def _run():
        daemon.start()
        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            daemon.stop()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[dim]Heartbeat daemon stopped.[/dim]")


@heartbeat.command("trigger")
@MODEL_OPT
@PROVIDER_OPT
@WORKSPACE_OPT
@PERMISSION_MODE_OPT
def heartbeat_trigger(model, provider, workspace, permission_mode):
    """Run one heartbeat check immediately."""
    from agnoclaw import AgentHarness
    from agnoclaw.heartbeat import HeartbeatDaemon

    agent = AgentHarness(
        model=model,
        provider=provider,
        workspace_dir=workspace,
        permission_mode=permission_mode,
    )

    def on_alert(msg):
        console.print(Panel(msg, title="[yellow]Heartbeat Alert[/yellow]", border_style="yellow"))

    daemon = HeartbeatDaemon(agent, on_alert=on_alert)

    console.print("[dim]Running heartbeat check...[/dim]")
    result = asyncio.run(daemon.trigger_now())
    if result is None:
        console.print("[green]HEARTBEAT_OK — nothing needs attention.[/green]")


@heartbeat.command("install-service")
@MODEL_OPT
@PROVIDER_OPT
@WORKSPACE_OPT
@click.option(
    "--interval",
    "-i",
    default=30,
    type=int,
    show_default=True,
    help="Heartbeat interval in minutes",
)
@click.option("--uninstall", is_flag=True, default=False, help="Remove the installed service")
def heartbeat_install_service(model, provider, workspace, interval, uninstall):
    """Register heartbeat as a launchd (macOS) or systemd (Linux) persistent service.

    Once installed, the heartbeat daemon starts automatically on login and
    survives terminal close — matching OpenClaw's always-on Gateway behavior.
    """
    import platform
    import shutil

    os_name = platform.system().lower()
    agnoclaw_bin = shutil.which("agnoclaw")
    if not agnoclaw_bin:
        console.print(
            "[red]agnoclaw binary not found on PATH. Install with "
            "'pip install agnoclaw' or 'uv tool install agnoclaw'.[/red]"
        )
        return

    if os_name == "darwin":
        _manage_launchd_service(agnoclaw_bin, workspace, interval, uninstall, model, provider)
    elif os_name == "linux":
        _manage_systemd_service(agnoclaw_bin, workspace, interval, uninstall, model, provider)
    else:
        console.print(
            f"[yellow]Service install not supported on {platform.system()}. "
            "Run 'agnoclaw heartbeat start' manually in a persistent session "
            "(tmux/screen).[/yellow]"
        )


def _manage_launchd_service(
    agnoclaw_bin: str,
    workspace,
    interval: int,
    uninstall: bool,
    model: str | None = None,
    provider: str | None = None,
) -> None:
    """Install/uninstall launchd LaunchAgent on macOS."""
    import subprocess
    from pathlib import Path

    label = "ai.agnoclaw.heartbeat"
    plist_dir = Path.home() / "Library" / "LaunchAgents"
    plist_path = plist_dir / f"{label}.plist"

    if uninstall:
        if plist_path.exists():
            subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
            plist_path.unlink()
            console.print(f"[green]Uninstalled: {plist_path}[/green]")
        else:
            console.print("[yellow]No launchd service found to uninstall.[/yellow]")
        return

    plist_dir.mkdir(parents=True, exist_ok=True)

    cmd_args = [agnoclaw_bin, "heartbeat", "start", "--interval", str(interval)]
    if workspace:
        cmd_args += ["--workspace", workspace]
    if model:
        cmd_args += ["--model", model]
    if provider:
        cmd_args += ["--provider", provider]

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
        {"".join(f"        <string>{a}</string>{chr(10)}" for a in cmd_args)}    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{Path.home()}/.agnoclaw/logs/heartbeat.log</string>
    <key>StandardErrorPath</key>
    <string>{Path.home()}/.agnoclaw/logs/heartbeat.error.log</string>
</dict>
</plist>"""

    # Ensure log directory
    (Path.home() / ".agnoclaw" / "logs").mkdir(parents=True, exist_ok=True)

    plist_path.write_text(plist_content)
    result = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True, text=True)

    if result.returncode == 0:
        console.print(f"[green]Installed and started: {plist_path}[/green]")
        console.print("[dim]Logs: ~/.agnoclaw/logs/heartbeat.log[/dim]")
        console.print("[dim]To uninstall: agnoclaw heartbeat install-service --uninstall[/dim]")
    else:
        console.print(f"[red]launchctl load failed: {result.stderr}[/red]")
        console.print(f"[dim]Plist written to: {plist_path}[/dim]")


def _manage_systemd_service(
    agnoclaw_bin: str,
    workspace,
    interval: int,
    uninstall: bool,
    model: str | None = None,
    provider: str | None = None,
) -> None:
    """Install/uninstall systemd user service on Linux."""
    import shlex
    import subprocess
    from pathlib import Path

    service_name = "agnoclaw-heartbeat"
    service_dir = Path.home() / ".config" / "systemd" / "user"
    service_path = service_dir / f"{service_name}.service"

    if uninstall:
        subprocess.run(["systemctl", "--user", "stop", service_name], capture_output=True)
        subprocess.run(["systemctl", "--user", "disable", service_name], capture_output=True)
        if service_path.exists():
            service_path.unlink()
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        console.print(f"[green]Uninstalled systemd service: {service_name}[/green]")
        return

    service_dir.mkdir(parents=True, exist_ok=True)
    (Path.home() / ".agnoclaw" / "logs").mkdir(parents=True, exist_ok=True)

    cmd_args = [agnoclaw_bin, "heartbeat", "start", "--interval", str(interval)]
    if workspace:
        cmd_args += ["--workspace", workspace]
    if model:
        cmd_args += ["--model", model]
    if provider:
        cmd_args += ["--provider", provider]

    service_content = f"""[Unit]
Description=agnoclaw Heartbeat Daemon
After=network.target

[Service]
Type=simple
ExecStart={" ".join(shlex.quote(a) for a in cmd_args)}
Restart=on-failure
RestartSec=30
StandardOutput=append:{Path.home()}/.agnoclaw/logs/heartbeat.log
StandardError=append:{Path.home()}/.agnoclaw/logs/heartbeat.error.log

[Install]
WantedBy=default.target
"""

    service_path.write_text(service_content)
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
    result = subprocess.run(
        ["systemctl", "--user", "enable", "--now", service_name],
        capture_output=True, text=True,
    )

    if result.returncode == 0:
        console.print(f"[green]Installed and started: {service_path}[/green]")
        console.print(f"[dim]Status: systemctl --user status {service_name}[/dim]")
        console.print("[dim]To uninstall: agnoclaw heartbeat install-service --uninstall[/dim]")
    else:
        console.print(f"[red]systemctl enable failed: {result.stderr}[/red]")
        console.print(f"[dim]Service file written to: {service_path}[/dim]")


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

    for logical_name in (
        "agents",
        "soul",
        "identity",
        "user",
        "memory",
        "tools",
        "heartbeat",
        "boot",
    ):
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
