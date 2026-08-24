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
from typing import NoReturn

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

_ELEVATED_MODE_WORDS = {
    "ask",
    "on",
    "full",
    "off",
    "status",
    "enable",
    "disable",
    "enabled",
    "disabled",
    "always",
}


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
@click.option(
    "--no-color",
    is_flag=True,
    default=False,
    envvar="NO_COLOR",
    help="Disable ANSI color for logs, CI, and support transcripts.",
)
def cli(no_color):
    """agnoclaw — a hackable, model-agnostic agent harness built on Agno."""
    if no_color:
        console.no_color = True


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

    console.print(
        Panel(
            "[bold cyan]agnoclaw init[/bold cyan] — personalize your agent\n"
            "[dim]Press Enter to skip any question.[/dim]",
            border_style="cyan",
        )
    )

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
    "--sync",
    "use_sync",
    is_flag=True,
    default=False,
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

        async def _run_repl():
            try:
                await repl.run()
            finally:
                await agent.aclose(policy="cancel")

        try:
            asyncio.run(_run_repl())
        except KeyboardInterrupt:
            console.print("\n[dim]Goodbye.[/dim]")
        return

    try:
        _chat_sync(agent, debug)
    finally:
        agent.close()


def _chat_sync(agent, debug: bool) -> None:
    """Legacy synchronous chat REPL (Click-based)."""
    queued_skill: str | None = None

    console.print(
        Panel(
            f"[bold cyan]agnoclaw[/bold cyan] — interactive session\n"
            f"Workspace: [dim]{agent.workspace.path}[/dim]\n"
            f"Type [bold]/quit[/bold] or [bold]Ctrl+C[/bold] to exit. "
            f"[bold]/skill <name>[/bold] to activate a skill. "
            f"[bold]/clear[/bold] to reset session.",
            border_style="cyan",
        )
    )

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

    if cmd == "/elevated":
        arg_text = args.strip()
        if not arg_text:
            permissions = agent.admin_list_permissions()
            mode = permissions.get("elevated_mode", "off")
            console.print(f"[cyan]Elevated mode: {mode}[/cyan]")
            console.print("[dim]Usage: /elevated <cmd> or /elevated on|ask|full|off [cmd][/dim]")
            return True, queued_skill

        first, _, rest = arg_text.partition(" ")
        selected_mode = None
        if first.lower() in _ELEVATED_MODE_WORDS:
            if first.lower() == "status":
                permissions = agent.admin_list_permissions()
                mode = permissions.get("elevated_mode", "off")
                console.print(f"[cyan]Elevated mode: {mode}[/cyan]")
                return True, queued_skill
            selected_mode = _set_cli_elevated_mode(agent, first.lower())
            if not rest.strip():
                console.print(f"[cyan]Elevated mode: {selected_mode}[/cyan]")
                return True, queued_skill
            arg_text = rest.strip()

        _ensure_cli_elevated_approver(
            agent,
            default=selected_mode == "on",
            skip_for_full=selected_mode == "full",
        )
        result = agent.run_elevated_command(
            arg_text,
            reason="CLI /elevated directive",
            _skip_approval=selected_mode == "full",
        )
        if result.stdout:
            console.print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
        if result.stderr:
            stderr_end = "" if result.stderr.endswith("\n") else "\n"
            console.print(f"[red]{result.stderr}[/red]", end=stderr_end)
        console.print(f"[dim]exit code: {result.exit_code}[/dim]")
        return True, queued_skill

    if cmd in ("/help", "/?"):
        console.print(
            "[bold]Chat commands:[/bold]\n"
            "  /skill <name>  — queue a skill for the next message\n"
            "  /skills        — list available skills\n"
            "  /workspace     — show workspace info\n"
            "  /elevated <cmd> — run an approved host command\n"
            "  /elevated on|ask|full|off — set session bash elevation\n"
            "  /clear         — clear session context\n"
            "  /quit          — exit\n"
        )
        return True, queued_skill

    return False, queued_skill


def _ensure_cli_elevated_approver(
    agent,
    *,
    default: bool = False,
    skip_for_full: bool = False,
) -> None:
    """Install an interactive approver for CLI elevated execution if needed."""
    if skip_for_full:
        return
    from agnoclaw.runtime import InteractivePermissionApprover

    permissions = agent.admin_list_permissions()
    if not permissions.get("has_approver"):
        agent.set_permission_approver(InteractivePermissionApprover(default=default))


def _set_cli_elevated_mode(agent, mode: str) -> str:
    """Set session-wide elevated mode and install the matching CLI approver."""
    from agnoclaw.runtime import normalize_elevated_session_mode

    normalized = normalize_elevated_session_mode(mode).value
    if normalized in {"ask", "on"}:
        _ensure_cli_elevated_approver(
            agent,
            default=normalized == "on",
        )
    agent.set_elevated_mode(normalized)
    return normalized


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

    async def _run_tui():
        try:
            return await app.run_async()
        finally:
            await agent.aclose(policy="cancel")

    asyncio.run(_run_tui())


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
    from agnoclaw.runtime.first_party import first_party_run, uses_lifecycle_route

    if uses_lifecycle_route(agent):

        async def _run_lifecycle():
            cancelled = False
            try:
                lifecycle_run = await first_party_run(agent, task, skill=skill)
                return await lifecycle_run.wait()
            except asyncio.CancelledError:
                cancelled = True
                await agent.aclose(policy="cancel")
                raise
            finally:
                if not cancelled:
                    await agent.aclose()

        response = asyncio.run(_run_lifecycle())
        content = getattr(response, "content", response)
        if content is not None:
            console.print(content)
    else:
        try:
            # Quick/legacy preserve the human-friendly provider token stream.
            agent.print_response(task, stream=True, skill=skill)
        finally:
            agent.close()


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
    """Show provenance and source content without executing the skill."""
    from agnoclaw.skills import SkillRegistry
    from agnoclaw.workspace import Workspace

    ws = Workspace(workspace)
    registry = SkillRegistry(ws.skills_dir())
    skill_spec = registry.inspect_skill(name)
    if skill_spec is None:
        console.print(f"[red]Skill not found: {name}[/red]")
        sys.exit(1)
    record = next(item for item in registry.list_skills() if item["name"] == skill_spec.name)
    console.print(
        Panel(
            f"Trust: {record['trust']}\n"
            f"Model invocable: {'yes' if record['model_invocable'] else 'no'}\n"
            f"Source: {skill_spec.path}",
            title=f"Skill: {skill_spec.name}",
            border_style="cyan",
        )
    )
    console.print(Markdown(skill_spec.path.read_text(encoding="utf-8")))


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
                f"[yellow]Skill '{skill_name}' already exists at {dest}. Overwrite? [y/N][/yellow]"
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

    console.print(
        Panel(
            f"[bold cyan]{manifest.name}[/bold cyan] v{manifest.version}\n"
            f"[dim]{manifest.description or 'No description'}[/dim]\n\n"
            f"Root: {manifest.root}\n"
            f"Trusted locally: {'yes' if is_pack_trusted(manifest.root) else 'no'}\n"
            f"Requires code execution: {manifest.trust.requires_code_execution}\n"
            f"Default trust: {manifest.trust.default}",
            title="Pack",
            border_style="cyan",
        )
    )

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
SCHEDULE_RUNTIME_DB_OPT = click.option(
    "--runtime-db",
    type=click.Path(path_type=Path),
    default=None,
    help="Durable RuntimeStore SQLite path; cannot be combined with --store.",
)


def _scheduler_backend(store: Path | None, runtime_db: Path | None = None):
    from agnoclaw.runtime import (
        JsonSchedulerBackend,
        RuntimeSchedulerBackend,
        SQLiteRuntimeStore,
        scheduler_store_path,
    )

    if store is not None and runtime_db is not None:
        raise click.UsageError("Use only one of --store or --runtime-db.")
    if runtime_db is not None:
        return RuntimeSchedulerBackend(SQLiteRuntimeStore(runtime_db))
    return JsonSchedulerBackend(scheduler_store_path(store))


def _runtime_scheduler_store(backend):
    """Return the shared runtime store only for the durable scheduler adapter."""
    return getattr(backend, "store", None)


def _close_scheduler_backend(backend) -> None:
    """Close a CLI-owned durable store after a one-shot management command."""
    runtime = _runtime_scheduler_store(backend)
    close = getattr(runtime, "close", None)
    if callable(close):
        close()


def _scheduler_learning_policy(
    profile: str,
    *,
    tenant_id: str | None,
    user_id: str | None,
    session_id: str | None,
):
    """Build the small safe CLI learning preset from trusted static identity."""
    if profile == "off":
        return None
    missing = [
        name
        for name, value in (
            ("--tenant-id", tenant_id),
            ("--user-id", user_id),
            ("--session", session_id),
        )
        if not value
    ]
    if missing:
        raise click.UsageError(
            "--learning-profile personal-session requires " + ", ".join(missing) + "."
        )
    from agnoclaw import LearningProfile

    return LearningProfile.personal_and_session(tenant_required=True)


@cli.group()
def schedule():
    """Manage embedded scheduler jobs."""
    pass


@schedule.command("list")
@SCHEDULE_STORE_OPT
@SCHEDULE_RUNTIME_DB_OPT
@click.option("--enabled", is_flag=True, default=False, help="Show only enabled jobs.")
@click.option("--disabled", is_flag=True, default=False, help="Show only disabled jobs.")
def schedule_list(store, runtime_db, enabled, disabled):
    """List local scheduler jobs."""
    if enabled and disabled:
        console.print("[red]Use only one of --enabled or --disabled.[/red]")
        sys.exit(1)
    backend = _scheduler_backend(store, runtime_db)
    try:
        enabled_filter = True if enabled else False if disabled else None
        jobs = backend.list_jobs(enabled=enabled_filter)
        if not jobs:
            console.print("[dim]No scheduler jobs found.[/dim]")
            return

        table = Table(title="Scheduler Jobs", border_style="dim")
        table.add_column("Name", style="cyan bold")
        table.add_column("Schedule")
        table.add_column("Enabled", justify="center")
        table.add_column("Next run")
        table.add_column("Skill")
        table.add_column("Prompt")
        for job in jobs:
            table.add_row(
                job.name,
                job.schedule,
                "yes" if job.enabled else "no",
                job.next_run_at or "",
                job.skill or "",
                job.prompt[:80] + ("..." if len(job.prompt) > 80 else ""),
            )
        console.print(table)
    finally:
        _close_scheduler_backend(backend)


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
@SCHEDULE_RUNTIME_DB_OPT
@click.option("--timezone", default="UTC", show_default=True, help="IANA timezone.")
@click.option("--max-retries", default=0, show_default=True, type=int)
@click.option("--retry-delay", default=30, show_default=True, type=int)
@click.option("--retry-backoff", default=2.0, show_default=True, type=float)
@click.option("--retry-max-delay", default=3_600, show_default=True, type=int)
@click.option("--retry-jitter", default=0, show_default=True, type=int)
@click.option("--jitter", "jitter_seconds", default=0, show_default=True, type=int)
@click.option(
    "--misfire-policy",
    default="fire_once",
    show_default=True,
    type=click.Choice(["fire_once", "catch_up", "skip"]),
)
@click.option("--misfire-grace", default=300, show_default=True, type=int)
@click.option("--concurrency-key", default=None)
@click.option(
    "--overlap-policy",
    default="queue",
    show_default=True,
    type=click.Choice(["queue", "skip"]),
)
@click.option(
    "--learning-consent",
    is_flag=True,
    default=False,
    help="Allow this scheduled job to use configured learning writes.",
)
def schedule_add(
    name,
    schedule_expr,
    prompt,
    skill,
    isolated,
    model_id,
    provider,
    disabled,
    store,
    runtime_db,
    timezone,
    max_retries,
    retry_delay,
    retry_backoff,
    retry_max_delay,
    retry_jitter,
    jitter_seconds,
    misfire_policy,
    misfire_grace,
    concurrency_key,
    overlap_policy,
    learning_consent,
):
    """Create or update a local scheduler job."""
    from agnoclaw.heartbeat.daemon import CronJob, HeartbeatDaemon
    from agnoclaw.runtime import SchedulerConfigurationError

    if HeartbeatDaemon._seconds_until_next(schedule_expr) < 0 and len(schedule_expr.split()) < 5:
        console.print(
            f"[red]Invalid schedule '{schedule_expr}'. Use an interval like '30m' "
            "or a 5-field cron expression.[/red]"
        )
        sys.exit(1)

    backend = _scheduler_backend(store, runtime_db)
    job = CronJob(
        name=name,
        schedule=schedule_expr,
        prompt=prompt,
        skill=skill,
        isolated=isolated,
        model_id=model_id,
        provider=provider,
        enabled=not disabled,
        timezone=timezone,
        max_retries=max_retries,
        retry_delay_seconds=retry_delay,
        retry_backoff_multiplier=retry_backoff,
        retry_max_delay_seconds=retry_max_delay,
        retry_jitter_seconds=retry_jitter,
        jitter_seconds=jitter_seconds,
        misfire_policy=misfire_policy,
        misfire_grace_seconds=misfire_grace,
        concurrency_key=concurrency_key,
        overlap_policy=overlap_policy,
        learning_consent=learning_consent,
    )
    try:
        stored = backend.upsert_job(job.to_scheduler_job())
    except (SchedulerConfigurationError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        _close_scheduler_backend(backend)
    console.print(
        f"[green]Saved schedule '{stored.name}' "
        f"({'enabled' if stored.enabled else 'disabled'})[/green]"
    )


@schedule.command("worker")
@click.option(
    "--runtime-db",
    type=click.Path(path_type=Path),
    default=Path("~/.agnoclaw/runtime.db"),
    show_default=True,
    help="Shared SQLite runtime/scheduler database.",
)
@click.option(
    "--artifacts",
    type=click.Path(path_type=Path),
    default=Path("~/.agnoclaw/artifacts"),
    show_default=True,
    help="Durable artifact directory.",
)
@click.option("--poll-interval", default=1.0, show_default=True, type=float)
@click.option("--claim-limit", default=10, show_default=True, type=int)
@click.option(
    "--learning-profile",
    default="off",
    show_default=True,
    type=click.Choice(["off", "personal-session"]),
    help="Enable a scoped Agno learning policy for jobs that grant consent.",
)
@click.option("--tenant-id", default=None, help="Trusted static owner tenant.")
@click.option("--user-id", default=None, help="Trusted static owner user.")
@SESSION_OPT
@MODEL_OPT
@PROVIDER_OPT
@WORKSPACE_OPT
@PERMISSION_MODE_OPT
def schedule_worker(
    runtime_db,
    artifacts,
    poll_interval,
    claim_limit,
    learning_profile,
    tenant_id,
    user_id,
    session,
    model,
    provider,
    workspace,
    permission_mode,
):
    """Run a durable single-host schedule worker until interrupted."""
    from agnoclaw import AgentHarness, LocalArtifactStore, RuntimeSchedulerBackend
    from agnoclaw.config import HarnessConfig
    from agnoclaw.heartbeat import HeartbeatDaemon
    from agnoclaw.runtime import SQLiteRuntimeStore

    learning = _scheduler_learning_policy(
        learning_profile,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session,
    )
    runtime_path = runtime_db.expanduser().resolve()
    artifact_path = artifacts.expanduser().resolve()
    runtime = SQLiteRuntimeStore(runtime_path)
    artifact_store = LocalArtifactStore(artifact_path)
    try:
        agent = AgentHarness(
            model=model,
            provider=provider,
            session_id=session,
            user_id=user_id,
            tenant_id=tenant_id,
            learning=learning,
            workspace_dir=workspace,
            permission_mode=permission_mode,
            config=HarnessConfig.durable(),
            runtime_store=runtime,
            artifact_store=artifact_store,
        )
    except BaseException:
        runtime.close()
        raise
    backend = RuntimeSchedulerBackend(runtime)
    daemon = HeartbeatDaemon(
        agent,
        scheduler_backend=backend,
        scheduler_poll_interval_seconds=poll_interval,
        scheduler_claim_limit=claim_limit,
        heartbeat_enabled=False,
    )
    console.print(
        "[dim]Durable scheduler worker starting "
        f"(runtime={runtime_path}, poll={poll_interval}s, limit={claim_limit}). "
        "Press Ctrl+C to stop.[/dim]"
    )

    async def _run():
        daemon.start()
        try:
            while True:
                await asyncio.sleep(1)
        finally:
            await daemon.astop()
            await agent.aclose(policy="detach")

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        console.print("\n[dim]Durable scheduler worker stopped.[/dim]")
    finally:
        runtime.close()


@schedule.command("show")
@click.argument("name")
@SCHEDULE_STORE_OPT
@SCHEDULE_RUNTIME_DB_OPT
def schedule_show(name, store, runtime_db):
    """Show a local scheduler job."""
    backend = _scheduler_backend(store, runtime_db)
    try:
        job = backend.get_job(name)
        if job is None:
            console.print(f"[red]Schedule not found: {name}[/red]")
            sys.exit(1)
        console.print(
            Panel(
                f"[bold cyan]{job.name}[/bold cyan]\n"
                f"Revision: {job.revision}\n"
                f"Schedule: {job.schedule} ({job.timezone})\n"
                f"Enabled: {job.enabled}\n"
                f"Next run: {job.next_run_at or 'none'}\n"
                f"Misfire: {job.misfire_policy} (grace={job.misfire_grace_seconds}s)\n"
                f"Overlap: {job.overlap_policy} "
                f"(key={job.concurrency_key or job.name})\n"
                f"Retries: {job.max_retries} (initial={job.retry_delay_seconds}s, "
                f"backoff={job.retry_backoff_multiplier}x, "
                f"max={job.retry_max_delay_seconds}s, "
                f"jitter={job.retry_jitter_seconds}s)\n"
                f"Jitter: {job.jitter_seconds}s\n"
                f"Learning consent: {job.metadata.get('learning_consent') is True}\n"
                f"Skill: {job.skill or 'none'}\n"
                f"Isolated session: {job.isolated}\n"
                f"Model: {job.model_id or 'worker/default'}\n"
                f"Provider: {job.provider or 'worker/default'}\n"
                f"Created: {job.created_at}\n"
                f"Updated: {job.updated_at}\n\n"
                f"{job.prompt}",
                title="Schedule",
                border_style="cyan",
            )
        )
    finally:
        _close_scheduler_backend(backend)


@schedule.command("remove")
@click.argument("name")
@SCHEDULE_STORE_OPT
@SCHEDULE_RUNTIME_DB_OPT
def schedule_remove(name, store, runtime_db):
    """Delete a local scheduler job."""
    backend = _scheduler_backend(store, runtime_db)
    try:
        if backend.delete_job(name):
            console.print(f"[green]Removed schedule '{name}'[/green]")
            return
        console.print(f"[yellow]Schedule not found: {name}[/yellow]")
    finally:
        _close_scheduler_backend(backend)


@schedule.command("enable")
@click.argument("name")
@SCHEDULE_STORE_OPT
@SCHEDULE_RUNTIME_DB_OPT
def schedule_enable(name, store, runtime_db):
    """Enable a local scheduler job."""
    backend = _scheduler_backend(store, runtime_db)
    try:
        if backend.set_job_enabled(name, True) is None:
            console.print(f"[red]Schedule not found: {name}[/red]")
            sys.exit(1)
        console.print(f"[green]Enabled schedule '{name}'[/green]")
    finally:
        _close_scheduler_backend(backend)


@schedule.command("disable")
@click.argument("name")
@SCHEDULE_STORE_OPT
@SCHEDULE_RUNTIME_DB_OPT
def schedule_disable(name, store, runtime_db):
    """Disable a local scheduler job."""
    backend = _scheduler_backend(store, runtime_db)
    try:
        if backend.set_job_enabled(name, False) is None:
            console.print(f"[red]Schedule not found: {name}[/red]")
            sys.exit(1)
        console.print(f"[green]Disabled schedule '{name}'[/green]")
    finally:
        _close_scheduler_backend(backend)


@schedule.command("runs")
@click.argument("name", required=False)
@click.option("--limit", default=20, show_default=True, type=int, help="Maximum runs to show.")
@SCHEDULE_STORE_OPT
@SCHEDULE_RUNTIME_DB_OPT
def schedule_runs(name, limit, store, runtime_db):
    """List scheduler run history."""
    backend = _scheduler_backend(store, runtime_db)
    try:
        runs = backend.list_runs(job_name=name, limit=limit)
        if not runs:
            console.print("[dim]No scheduler runs found.[/dim]")
            return

        table = Table(title="Scheduler Runs", border_style="dim")
        table.add_column("Run ID", style="cyan")
        table.add_column("Job")
        table.add_column("Attempt", justify="right")
        table.add_column("Status")
        table.add_column("Scheduled")
        table.add_column("Runtime run")
        table.add_column("Result")
        for run in runs:
            result = run.error or run.output or ""
            table.add_row(
                run.run_id,
                run.job_name,
                str(run.attempt),
                run.status,
                run.scheduled_at or run.started_at,
                run.runtime_run_id or "",
                result[:80] + ("..." if len(result) > 80 else ""),
            )
        console.print(table)
    finally:
        _close_scheduler_backend(backend)


@schedule.command("trigger")
@click.argument("name")
@SCHEDULE_STORE_OPT
@SCHEDULE_RUNTIME_DB_OPT
@click.option(
    "--artifacts",
    type=click.Path(path_type=Path),
    default=None,
    help="Durable artifact directory; defaults beside --runtime-db.",
)
@WORKSPACE_OPT
@PERMISSION_MODE_OPT
@MODEL_OPT
@PROVIDER_OPT
@click.option(
    "--learning-profile",
    default="off",
    show_default=True,
    type=click.Choice(["off", "personal-session"]),
    help="Enable a scoped Agno learning policy when this job grants consent.",
)
@click.option("--tenant-id", default=None, help="Trusted static owner tenant.")
@click.option("--user-id", default=None, help="Trusted static owner user.")
@SESSION_OPT
def schedule_trigger(
    name,
    store,
    runtime_db,
    artifacts,
    workspace,
    permission_mode,
    model,
    provider,
    learning_profile,
    tenant_id,
    user_id,
    session,
):
    """Run a local scheduler job immediately and record run history."""
    from agnoclaw.heartbeat import HeartbeatDaemon

    learning = _scheduler_learning_policy(
        learning_profile,
        tenant_id=tenant_id,
        user_id=user_id,
        session_id=session,
    )
    backend = _scheduler_backend(store, runtime_db)
    job = backend.get_job(name)
    if job is None:
        _close_scheduler_backend(backend)
        console.print(f"[red]Schedule not found: {name}[/red]")
        sys.exit(1)

    if runtime_db is not None:
        from agnoclaw import AgentHarness, LocalArtifactStore
        from agnoclaw.config import HarnessConfig

        runtime = _runtime_scheduler_store(backend)
        runtime_path = runtime_db.expanduser().resolve()
        artifact_path = (
            artifacts.expanduser().resolve()
            if artifacts is not None
            else runtime_path.parent / "artifacts"
        )
        try:
            agent = AgentHarness(
                model=model or job.model_id,
                provider=provider or job.provider,
                session_id=session,
                user_id=user_id,
                tenant_id=tenant_id,
                learning=learning,
                workspace_dir=workspace,
                permission_mode=permission_mode,
                config=HarnessConfig.durable(),
                runtime_store=runtime,
                artifact_store=LocalArtifactStore(artifact_path),
            )
        except BaseException:
            runtime.close()
            raise
    else:
        runtime = None
        if learning is not None:
            _close_scheduler_backend(backend)
            raise click.UsageError(
                "--learning-profile requires --runtime-db; JSON scheduling is compatibility-only."
            )
        agent = _build_agent(
            model or job.model_id,
            provider or job.provider,
            session,
            workspace,
            False,
            permission_mode,
        )
    daemon = HeartbeatDaemon(agent, scheduler_backend=backend)
    console.print(f"[dim]Triggering schedule '{name}'...[/dim]")

    async def _trigger():
        try:
            return await daemon.trigger_cron(name)
        finally:
            await agent.aclose()
            if runtime is not None:
                runtime.close()

    result = asyncio.run(_trigger())
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

    console.print(
        Panel(
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
        )
    )

    if detail.skill_md_preview:
        console.print("\n[bold]SKILL.md Preview:[/bold]")
        console.print(Markdown(detail.skill_md_preview[:2000]))


@hub.command("install")
@click.argument("name")
@WORKSPACE_OPT
def hub_install(name, workspace):
    """Install a skill from ClawHub to your workspace."""
    from agnoclaw.skills import SkillRegistry, load_skill_from_path
    from agnoclaw.workspace import Workspace

    ws = Workspace(workspace)
    ws.initialize()
    registry = SkillRegistry(ws.skills_dir())

    console.print(f"[dim]Installing '{name}' from ClawHub...[/dim]")
    skill_dir = registry.install_from_hub(name)

    if skill_dir:
        console.print(f"[green]Installed '{name}' to {skill_dir}[/green]")
        # Parse only. Rendering can run local inline commands and is never an
        # appropriate installation-time verification step for remote content.
        skill = load_skill_from_path(skill_dir / "SKILL.md")
        if skill:
            console.print("[green]Verified: skill parses successfully (community trust)[/green]")
        else:
            console.print("[yellow]Warning: skill installed but failed to parse[/yellow]")
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
    table.add_column("Trust", justify="center")
    table.add_column("Tools", style="dim")

    for s in skills:
        user = "✓" if s["user_invocable"] else "—"
        model = "✓" if s["model_invocable"] else "—"
        tools = ", ".join(s["allowed_tools"][:3]) + ("..." if len(s["allowed_tools"]) > 3 else "")
        table.add_row(
            s["name"],
            s["description"],
            user,
            model,
            s["trust"],
            tools or "all",
        )

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
        agent.close()
        return

    def on_alert(msg):
        console.print(Panel(msg, title="[yellow]Heartbeat Alert[/yellow]", border_style="yellow"))

    daemon = HeartbeatDaemon(agent, on_alert=on_alert)

    # Override interval if provided
    if interval is not None:
        daemon._config.heartbeat.interval_minutes = interval

    interval_min = daemon._config.heartbeat.interval_minutes
    console.print(
        f"[dim]Heartbeat daemon starting (interval={interval_min}m). Press Ctrl+C to stop.[/dim]"
    )

    async def _run():
        daemon.start()
        try:
            while True:
                await asyncio.sleep(1)
        finally:
            await daemon.astop()
            await agent.aclose(policy="cancel")

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

    async def _trigger():
        try:
            return await daemon.trigger_now()
        finally:
            await agent.aclose()

    result = asyncio.run(_trigger())
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
        capture_output=True,
        text=True,
    )

    if result.returncode == 0:
        console.print(f"[green]Installed and started: {service_path}[/green]")
        console.print(f"[dim]Status: systemctl --user status {service_name}[/dim]")
        console.print("[dim]To uninstall: agnoclaw heartbeat install-service --uninstall[/dim]")
    else:
        console.print(f"[red]systemctl enable failed: {result.stderr}[/red]")
        console.print(f"[dim]Service file written to: {service_path}[/dim]")


# ── agnoclaw inspect ─────────────────────────────────────────────────────────


@cli.command()
@WORKSPACE_OPT
@click.option("--json", "json_output", is_flag=True, default=False)
def doctor(workspace, json_output):
    """Run bounded offline environment and compatibility checks."""
    import json

    from agnoclaw.diagnostics import collect_diagnostics

    report = collect_diagnostics(workspace=workspace)
    if json_output:
        click.echo(json.dumps(report, sort_keys=True, separators=(",", ":")))
    else:
        table = Table(title="agnoclaw doctor", border_style="dim")
        table.add_column("Check")
        table.add_column("Status")
        table.add_column("Result")
        for check in report["checks"]:
            table.add_row(check["id"], check["status"].upper(), check["summary"])
        console.print(table)
        console.print(
            f"[dim]Offline, redacted: {report['summary']['passed']} passed, "
            f"{report['summary']['warnings']} warnings, "
            f"{report['summary']['errors']} errors.[/dim]"
        )
    if report["summary"]["errors"]:
        raise click.exceptions.Exit(78)


@cli.command("explain")
@click.argument("error_code")
@click.option("--json", "json_output", is_flag=True, default=False)
def explain_error(error_code, json_output):
    """Explain one stable ERROR_CODE without network access."""
    import json

    from agnoclaw.diagnostics import explain_error_code

    explanation = explain_error_code(error_code)
    if json_output:
        click.echo(json.dumps(explanation, sort_keys=True, separators=(",", ":")))
    else:
        console.print(f"[bold]{explanation['code']}: {explanation['title']}[/bold]")
        console.print(explanation["cause"])
        console.print(f"Fix: {explanation['fix']}")
        console.print(f"[dim]{explanation['docs']}[/dim]")
    if not explanation["found"]:
        raise click.exceptions.Exit(2)


@cli.command("support-bundle")
@click.option(
    "--output",
    type=click.Path(path_type=Path, dir_okay=False),
    required=True,
    help="Destination JSON file. Existing files require --overwrite.",
)
@WORKSPACE_OPT
@click.option("--overwrite", is_flag=True, default=False)
@click.option("--json", "json_output", is_flag=True, default=False)
def support_bundle(output, workspace, overwrite, json_output):
    """Write a redacted offline diagnostic bundle for a support request."""
    import json

    from agnoclaw.diagnostics import write_support_bundle

    try:
        payload = write_support_bundle(output, workspace=workspace, overwrite=overwrite)
    except (OSError, ValueError) as exc:
        error = {
            "schema_version": "1.0",
            "status": "error",
            "error": {"code": "SUPPORT_BUNDLE_WRITE_FAILED", "message": str(exc)},
            "exit_code": 73,
        }
        click.echo(
            json.dumps(error, sort_keys=True, separators=(",", ":"))
            if json_output
            else f"SUPPORT_BUNDLE_WRITE_FAILED: {exc}",
            err=True,
        )
        raise click.exceptions.Exit(73) from exc
    result = {
        "schema_version": "1.0",
        "status": "ok",
        "redacted": payload["redacted"],
        "output_created": True,
    }
    if json_output:
        click.echo(json.dumps(result, sort_keys=True, separators=(",", ":")))
    else:
        console.print(f"[green]Redacted support bundle written to {output}[/green]")
        console.print("[dim]Review the JSON before attaching it to a public issue.[/dim]")


@cli.group()
def inspect():
    """Inspect durable runtime state without exposing run content."""
    pass


def _runtime_inspection_error(
    *,
    code: str,
    message: str,
    fix: str,
    exit_code: int,
    json_output: bool,
) -> NoReturn:
    import json

    payload = {
        "schema_version": "1.0",
        "command": "inspect.run",
        "status": "error",
        "ok": False,
        "error": {"code": code, "message": message, "fix": fix},
        "exit_code": exit_code,
    }
    if json_output:
        click.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")), err=True)
    else:
        click.echo(f"{code}: {message}", err=True)
        click.echo(f"Fix: {fix}", err=True)
    raise click.exceptions.Exit(exit_code)


def _runtime_inspection_store(
    *,
    sqlite_db: Path | None,
    postgres_credential_env: str | None,
):
    import os
    import re

    from agnoclaw import PostgresRuntimeStore, SQLiteRuntimeStore

    if (sqlite_db is None) == (postgres_credential_env is None):
        raise ValueError("select exactly one runtime-store backend")
    if sqlite_db is not None:
        return SQLiteRuntimeStore(sqlite_db, read_only=True)
    assert postgres_credential_env is not None
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", postgres_credential_env) is None:
        raise ValueError("PostgreSQL credential must be referenced by environment name")
    dsn = os.getenv(postgres_credential_env)
    if not dsn:
        raise ValueError("PostgreSQL credential environment variable is unavailable")
    return PostgresRuntimeStore(
        dsn,
        min_pool_size=1,
        max_pool_size=2,
        max_waiting=4,
        application_name="agnoclaw-runtime-inspect",
        read_only=True,
    )


@inspect.command("run")
@click.argument("run_id")
@click.option(
    "--sqlite-db",
    type=click.Path(path_type=Path, exists=True, dir_okay=False, readable=True),
    default=None,
    help="Current SQLite RuntimeStore file; opened read-only.",
)
@click.option(
    "--postgres-credential-env",
    metavar="ENV_NAME",
    default=None,
    help="Environment-variable name containing the PostgreSQL DSN; never pass a DSN.",
)
@click.option("--tenant-id", default=None, help="Exact trusted tenant owner, when present.")
@click.option("--user-id", required=True, help="Exact trusted user owner.")
@click.option(
    "--identifier-key-env",
    metavar="ENV_NAME",
    default="AGNOCLAW_TELEMETRY_IDENTIFIER_KEY",
    show_default=True,
    help="Environment-variable name containing at least 32 bytes of HMAC key material.",
)
@click.option("--identifier-key-id", default="default", show_default=True)
@click.option("--json", "json_output", is_flag=True, default=False)
def runtime_inspect(
    run_id,
    sqlite_db,
    postgres_credential_env,
    tenant_id,
    user_id,
    identifier_key_env,
    identifier_key_id,
    json_output,
):
    """Show a bounded, owner-authorized recovery view for RUN_ID.

    The report contains HMAC-linked identifiers and state/count evidence only. It
    never includes prompts, tool arguments/targets, metadata, outputs, or error
    bodies. The selected database is opened in read-only mode.

    \b
    Examples:
      export AGNOCLAW_TELEMETRY_IDENTIFIER_KEY='replace-with-32-byte-secret-value'
      agnoclaw inspect run run_123 --sqlite-db ./runtime.db --user-id user_123
      agnoclaw inspect run run_123 --postgres-credential-env RUNTIME_DSN \\
        --tenant-id tenant_123 --user-id user_123 --json
    """
    import json
    import os
    import re

    from agnoclaw import (
        RUN_INSPECT_SCOPE,
        ExecutionContext,
        RunInspectionAuthorizationError,
        RuntimeRunInspector,
        RuntimeTelemetryPolicy,
    )
    from agnoclaw.runtime.errors import HarnessError
    from agnoclaw.runtime.store import (
        RuntimeStoreConnectionLostError,
        RuntimeStoreDependencyError,
        RuntimeStoreOverloadedError,
    )

    store = None
    try:
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier_key_env) is None:
            raise ValueError("identifier key must be referenced by environment name")
        identifier_key = os.getenv(identifier_key_env)
        if identifier_key is None:
            raise ValueError("identifier key environment variable is unavailable")
        policy = RuntimeTelemetryPolicy(
            identifier_key=identifier_key.encode(),
            key_id=identifier_key_id,
        )
        store = _runtime_inspection_store(
            sqlite_db=sqlite_db,
            postgres_credential_env=postgres_credential_env,
        )
        report = asyncio.run(
            RuntimeRunInspector(store=store, policy=policy).inspect(
                run_id,
                context=ExecutionContext.create(
                    user_id=user_id,
                    session_id=None,
                    workspace_id="agnoclaw:runtime-inspect",
                    tenant_id=tenant_id,
                    scopes=(RUN_INSPECT_SCOPE,),
                ),
            )
        )
    except RunInspectionAuthorizationError:
        _runtime_inspection_error(
            code="RUN_INSPECTION_NOT_AUTHORIZED",
            message="The supplied owner cannot inspect this run, or the run is unavailable.",
            fix="Verify the exact tenant/user owner and run identifier, then retry.",
            exit_code=77,
            json_output=json_output,
        )
    except (RuntimeStoreConnectionLostError, RuntimeStoreOverloadedError):
        _runtime_inspection_error(
            code="RUNTIME_INSPECTION_BACKEND_UNAVAILABLE",
            message="The runtime store is temporarily unavailable.",
            fix="Check store health and retry the same read-only command.",
            exit_code=75,
            json_output=json_output,
        )
    except RuntimeStoreDependencyError:
        _runtime_inspection_error(
            code="RUNTIME_INSPECTION_DEPENDENCY_MISSING",
            message="The selected runtime-store dependency is not installed.",
            fix="Install agnoclaw[postgres] for PostgreSQL inspection.",
            exit_code=78,
            json_output=json_output,
        )
    except (OSError, TypeError, ValueError):
        _runtime_inspection_error(
            code="RUNTIME_INSPECTION_CONFIGURATION_INVALID",
            message="The read-only store, owner, or HMAC-key configuration is invalid.",
            fix="Check --help, use environment credential names, and supply a current store.",
            exit_code=78,
            json_output=json_output,
        )
    except HarnessError:
        _runtime_inspection_error(
            code="RUNTIME_INSPECTION_FAILED",
            message="The runtime inspection could not be completed safely.",
            fix="Inspect store health and schema compatibility before retrying.",
            exit_code=1,
            json_output=json_output,
        )
    finally:
        if store is not None:
            store.close()

    payload = report.to_dict()
    if json_output:
        click.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    table = Table(title="Durable run inspection", border_style="dim")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Run", report.run_id_hash)
    table.add_row("State", f"{report.state} (revision {report.revision})")
    table.add_row("Recovery", report.recommendation.value)
    table.add_row(
        "Evidence",
        (
            f"{report.event_count_inspected} events, "
            f"{report.operation_count_inspected} operations, "
            f"{report.pending_approval_count} pending approvals"
        ),
    )
    table.add_row(
        "Related",
        (
            f"{report.child_count_inspected} children, "
            f"{report.artifact_count_inspected} artifacts / "
            f"{report.artifact_bytes_inspected} bytes"
        ),
    )
    console.print(table)
    console.print(
        "[dim]No prompts, arguments, targets, metadata, outputs, or error bodies read.[/dim]"
    )


# ── agnoclaw migrate 0.12 ────────────────────────────────────────────────────


@cli.group()
def migrate():
    """Inspect and plan versioned data migrations."""
    pass


@migrate.group("0.12")
def migrate_012():
    """Manage the agnoclaw 0.12 persisted-data migration."""
    pass


def _migration_scope_mappings(path: Path | None):
    if path is None:
        return ()
    import json

    from agnoclaw import LegacyLearningScopeMapping

    try:
        if path.stat().st_size > 1024 * 1024:
            raise ValueError("mapping file exceeds 1 MiB")
        payload = json.loads(path.read_text(encoding="utf-8"))
        items = payload.get("scope_mappings") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise ValueError("scope_mappings must be an array")
        return tuple(LegacyLearningScopeMapping(**item) for item in items)
    except (OSError, TypeError, UnicodeError, ValueError) as exc:
        raise click.ClickException(
            "The scope-map file is invalid; expected bounded UTF-8 JSON with "
            "a scope_mappings array. Raw parser details are redacted."
        ) from exc


@migrate_012.command("check")
@click.option("--learning-db", type=click.Path(path_type=Path), default=None)
@click.option("--schedules", type=click.Path(path_type=Path), default=None)
@click.option("--learning-table", "learning_tables", multiple=True)
@click.option("--scope-map-file", type=click.Path(path_type=Path), default=None)
@click.option("--timezone", "schedule_timezone", default=None)
@click.option(
    "--misfire-policy",
    type=click.Choice(["skip", "run_once"], case_sensitive=False),
    default=None,
)
@click.option("--old-writer-fence-plan", default=None)
@click.option("--max-learning-rows", type=click.IntRange(1, 10_000_000), default=100_000)
@click.option(
    "--max-learning-bytes",
    type=click.IntRange(1, 16 * 1024 * 1024 * 1024),
    default=512 * 1024 * 1024,
)
@click.option(
    "--max-schedule-bytes",
    type=click.IntRange(1, 1024 * 1024 * 1024),
    default=16 * 1024 * 1024,
)
@click.option("--json", "json_output", is_flag=True, default=False)
def migrate_012_check(
    learning_db,
    schedules,
    learning_tables,
    scope_map_file,
    schedule_timezone,
    misfire_policy,
    old_writer_fence_plan,
    max_learning_rows,
    max_learning_bytes,
    max_schedule_bytes,
    json_output,
):
    """Read legacy sources and emit a deterministic, non-mutating preflight report."""
    import json

    from agnoclaw import inspect_migration_012

    try:
        report = inspect_migration_012(
            learning_sqlite_path=learning_db,
            schedule_json_path=schedules,
            learning_table_names=learning_tables
            or (
                "agno_learnings",
                "agno_memories",
                "agnoclaw_memories",
            ),
            scope_mappings=_migration_scope_mappings(scope_map_file),
            schedule_default_timezone=schedule_timezone,
            schedule_default_misfire_policy=misfire_policy,
            old_writer_fence_plan=old_writer_fence_plan,
            max_learning_rows=max_learning_rows,
            max_learning_bytes=max_learning_bytes,
            max_schedule_bytes=max_schedule_bytes,
        )
    except (TypeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc

    if json_output:
        click.echo(json.dumps(report.to_dict(), sort_keys=True, separators=(",", ":")))
    else:
        status = "CLEAR" if report.preflight_clear else "BLOCKED"
        style = "green" if report.preflight_clear else "red"
        console.print(f"[{style} bold]0.12 migration preflight: {status}[/{style} bold]")
        console.print(f"Report: {report.report_digest}")
        console.print("Read-only: yes · apply allowed: no")
        for finding in report.findings:
            console.print(
                f"[{finding.severity.value}] {finding.code}: {finding.safe_message} "
                f"Resolution: {finding.resolution}"
            )
    if not report.preflight_clear:
        raise click.exceptions.Exit(3)


def _migration_emit(
    *,
    command: str,
    result,
    json_output: bool,
    next_command: str | None = None,
    next_action: str | None = None,
    ok: bool = True,
):
    """Emit stable migration data on stdout and human guidance when requested."""
    import json

    payload = {
        "schema_version": "1.0",
        "command": command,
        "status": "ok" if ok else "blocked",
        "ok": ok,
        "result": result,
        "next_command": next_command,
        "next_action": next_action,
    }
    if json_output:
        click.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return
    summary = (
        result.get("receipt", result.get("plan", result)) if isinstance(result, dict) else result
    )
    phase = summary.get("phase") if isinstance(summary, dict) else None
    status = str(phase or ("ready" if ok else "blocked")).upper()
    style = "green" if ok else "red"
    console.print(f"[{style} bold]0.12 migration {command}: {status}[/{style} bold]")
    if isinstance(summary, dict):
        if summary.get("migration_id"):
            console.print(f"Migration: {summary['migration_id']}")
        if summary.get("plan_digest"):
            console.print(f"Plan: {summary['plan_digest']}")
        if summary.get("manifest_digest"):
            console.print(f"Manifest: {summary['manifest_digest']}")
        if summary.get("transform_digest"):
            console.print(f"Transform: {summary['transform_digest']}")
        if summary.get("receipt_digest"):
            console.print(f"Receipt: {summary['receipt_digest']}")
        for role, role_phase in summary.get("role_phases", {}).items():
            console.print(f"{role}: {role_phase}")
        if summary.get("rollback_boundary"):
            console.print(f"Rollback boundary: {summary['rollback_boundary']}")
    if isinstance(result, dict):
        for finding in result.get("findings", ()):  # service check report
            count = f" Count: {finding['count']}." if finding.get("count") is not None else ""
            console.print(
                f"[{finding['severity']}] {finding['code']}: {finding['safe_message']}"
                f"{count} Resolution: {finding['resolution']}"
            )
    if next_command:
        console.print(f"[dim]Next: {next_command}[/dim]")
    if next_action:
        console.print(f"[dim]Next: {next_action}[/dim]")


def _migration_shell_arg(value) -> str:
    """Render one copy-paste-safe shell argument for next-step diagnostics."""
    import shlex

    return shlex.quote(str(value))


def _migration_fail(exc, *, command: str, json_output: bool):
    """Return bounded structured migration diagnostics with semantic exit codes."""
    import json

    code = str(getattr(exc, "code", "MIGRATION_INVALID"))
    details = getattr(exc, "details", {})
    transient_codes = {
        "MIGRATION_POSTGRES_APPLY_FAILED",
        "MIGRATION_POSTGRES_CUTOVER_FAILED",
        "MIGRATION_POSTGRES_LOCK_UNAVAILABLE",
        "MIGRATION_POSTGRES_SCAN_FAILED",
        "MIGRATION_POSTGRES_SNAPSHOT_DRIFT",
        "MIGRATION_POSTGRES_SOURCE_CONNECTION_FAILED",
        "MIGRATION_POSTGRES_TARGET_CONNECTION_FAILED",
        "MIGRATION_POSTGRES_TRANSFORM_FAILED",
        "MIGRATION_POSTGRES_VERIFY_FAILED",
        "MIGRATION_POSTGRES_ROLLBACK_FAILED",
    }
    configuration_codes = {
        "MIGRATION_POSTGRES_CREDENTIAL_INVALID",
        "MIGRATION_POSTGRES_CREDENTIAL_UNAVAILABLE",
        "MIGRATION_POSTGRES_DRIVER_UNAVAILABLE",
        "MIGRATION_SCOPE_MAP_INVALID",
        "MIGRATION_SCHEDULE_MAP_DUPLICATE",
        "MIGRATION_SCHEDULE_MAP_INVALID",
        "MIGRATION_SCHEDULE_MAP_SCHEMA_UNSUPPORTED",
        "MIGRATION_POSTGRES_TARGET_SCHEMA_INVALID",
    }
    verification_codes = {
        "MIGRATION_BACKUP_CORRUPT",
        "MIGRATION_MANIFEST_DIGEST_MISMATCH",
        "MIGRATION_PLAN_DIGEST_MISMATCH",
        "MIGRATION_POSTGRES_PLAN_EVIDENCE_DRIFT",
        "MIGRATION_POSTGRES_PROVENANCE_CONFLICT",
        "MIGRATION_POSTGRES_PROVENANCE_MISSING",
        "MIGRATION_POSTGRES_ROLLBACK_DRIFT",
        "MIGRATION_POSTGRES_SOURCE_DRIFT",
        "MIGRATION_POSTGRES_SOURCE_ENDPOINT_DRIFT",
        "MIGRATION_POSTGRES_TARGET_DRIFT",
        "MIGRATION_POSTGRES_TARGET_ENDPOINT_DRIFT",
        "MIGRATION_POSTGRES_UNOWNED_TARGET_DRIFT",
        "MIGRATION_POSTGRES_VERIFICATION_FAILED",
        "MIGRATION_SOURCE_DRIFT",
        "MIGRATION_TARGET_DRIFT",
        "MIGRATION_VERIFICATION_FAILED",
    }
    if code in transient_codes:
        exit_code = 75
        fix = "Retry after checking PostgreSQL reachability, health, and transaction load."
    elif code in configuration_codes:
        exit_code = 78
        fix = "Correct the referenced environment/configuration and rerun the command."
    elif code in verification_codes:
        exit_code = 4
        fix = "Stop and investigate the reported integrity or drift evidence before retrying."
    else:
        exit_code = 3
        fix = "Resolve the reported migration precondition, then rerun the same command."
    payload = {
        "schema_version": "1.0",
        "command": command,
        "status": "error",
        "ok": False,
        "error": {
            "code": code,
            "message": str(getattr(exc, "message", str(exc))),
            "details": details if isinstance(details, dict) else {},
            "fix": fix,
            "transient": code in transient_codes,
        },
        "exit_code": exit_code,
    }
    if json_output:
        click.echo(json.dumps(payload, sort_keys=True, separators=(",", ":")), err=True)
    else:
        message = str(getattr(exc, "message", str(exc)))
        click.echo(f"{code}: {message}", err=True)
        click.echo(f"Fix: {fix}", err=True)
    raise click.exceptions.Exit(exit_code)


def _service_migration_scan_options(function):
    """Keep service check/plan scanner inputs identical and non-secret."""
    options = (
        click.option(
            "--source-credential-env",
            required=True,
            metavar="ENV_NAME",
            help="Environment-variable name containing the source DSN; never pass a DSN.",
        ),
        click.option(
            "--source-schema",
            default="agno",
            show_default=True,
            help="Explicit Agno source schema.",
        ),
        click.option(
            "--target-learning-credential-env",
            required=True,
            metavar="ENV_NAME",
            help="Environment-variable name containing the learning-target DSN.",
        ),
        click.option(
            "--target-learning-schema",
            default="agno",
            show_default=True,
            help="Explicit learning-target schema.",
        ),
        click.option(
            "--target-runtime-credential-env",
            required=True,
            metavar="ENV_NAME",
            help="Environment-variable name containing the runtime-target DSN.",
        ),
        click.option(
            "--target-runtime-schema",
            default="agnoclaw_runtime",
            show_default=True,
            help="Explicit runtime-target schema.",
        ),
        click.option(
            "--schedule-map-file",
            type=click.Path(path_type=Path),
            required=True,
            metavar="PATH",
            help="Bounded private schedule-map JSON; its contents never enter the plan.",
        ),
        click.option(
            "--scope-map-file",
            type=click.Path(path_type=Path),
            default=None,
            metavar="PATH",
            help="Optional bounded institutional map-or-quarantine decisions.",
        ),
        click.option(
            "--statement-timeout-ms",
            type=click.IntRange(1, 3_600_000),
            default=60_000,
            show_default=True,
        ),
        click.option(
            "--lock-timeout-ms",
            type=click.IntRange(1, 60_000),
            default=2_000,
            show_default=True,
        ),
        click.option(
            "--max-rows-per-table",
            type=click.IntRange(1, 1_000_000_000),
            default=10_000_000,
            show_default=True,
        ),
        click.option(
            "--batch-size",
            type=click.IntRange(1, 10_000),
            default=1_000,
            show_default=True,
        ),
        click.option(
            "--max-row-bytes",
            type=click.IntRange(1_024, 256 * 1024 * 1024),
            default=16 * 1024 * 1024,
            show_default=True,
        ),
        click.option(
            "--json",
            "json_output",
            is_flag=True,
            default=False,
            help="Emit the stable schema-v1 automation envelope.",
        ),
    )
    for option in reversed(options):
        function = option(function)
    return function


def _service_migration_scan(
    *,
    source_credential_env,
    source_schema,
    target_learning_credential_env,
    target_learning_schema,
    target_runtime_credential_env,
    target_runtime_schema,
    schedule_map_file,
    scope_map_file,
    statement_timeout_ms,
    lock_timeout_ms,
    max_rows_per_table,
    batch_size,
    max_row_bytes,
):
    from agnoclaw import (
        PostgresMigrationDatabaseRef,
        load_postgres_schedule_map,
        scan_postgres_migration_012,
    )
    from agnoclaw.migration_apply import Migration012Error

    try:
        scope_mappings = _migration_scope_mappings(scope_map_file)
    except click.ClickException as exc:
        raise Migration012Error(
            "MIGRATION_SCOPE_MAP_INVALID",
            "The scope-map file is not valid bounded migration control JSON.",
        ) from exc
    source = PostgresMigrationDatabaseRef("source", source_credential_env, source_schema)
    target_learning = PostgresMigrationDatabaseRef(
        "target_learning",
        target_learning_credential_env,
        target_learning_schema,
    )
    target_runtime = PostgresMigrationDatabaseRef(
        "target_runtime",
        target_runtime_credential_env,
        target_runtime_schema,
    )
    schedule_map = load_postgres_schedule_map(schedule_map_file)
    report = scan_postgres_migration_012(
        source=source,
        target_learning=target_learning,
        target_runtime=target_runtime,
        schedule_map=schedule_map,
        scope_mappings=scope_mappings,
        statement_timeout_ms=statement_timeout_ms,
        lock_timeout_ms=lock_timeout_ms,
        max_rows_per_table=max_rows_per_table,
        batch_size=batch_size,
        max_row_bytes=max_row_bytes,
    )
    return source, target_learning, target_runtime, schedule_map, scope_mappings, report


@migrate_012.group("service")
def migrate_012_service():
    """Run the PostgreSQL/service 0.12 migration lifecycle."""
    pass


@migrate_012_service.command("check")
@_service_migration_scan_options
def migrate_012_service_check(**options):
    """Scan PostgreSQL source and targets without writes.

    \b
    Examples:
      agnoclaw migrate 0.12 service check --help
      agnoclaw migrate 0.12 service check \\
        --source-credential-env AGNO_SOURCE_DSN \\
        --target-learning-credential-env AGNO_TARGET_DSN \\
        --target-runtime-credential-env AGNO_TARGET_DSN \\
        --schedule-map-file private/schedules.json --json
    """
    from agnoclaw.migration_apply import Migration012Error

    json_output = bool(options.pop("json_output"))
    try:
        *_, report = _service_migration_scan(**options)
    except (Migration012Error, OSError, TypeError, ValueError) as exc:
        _migration_fail(exc, command="service.check", json_output=json_output)
    result = report.to_dict()
    _migration_emit(
        command="service.check",
        result=result,
        json_output=json_output,
        ok=report.ready,
        next_command=("agnoclaw migrate 0.12 service plan --help" if report.ready else None),
        next_action=(
            None if report.ready else "Resolve every reported blocker, then rerun this exact check."
        ),
    )
    if not report.ready:
        raise click.exceptions.Exit(3)


@migrate_012_service.command("plan")
@click.option(
    "--target-tenant-id", required=True, help="Trusted tenant authority for transformed rows."
)
@click.option("--target-org-id", default=None, help="Optional trusted organization authority.")
@click.option(
    "--target-agent-id", required=True, help="Trusted agent authority for transformed rows."
)
@click.option(
    "--backup-receipt-id", required=True, help="Opaque reviewed native-backup receipt token."
)
@click.option(
    "--backup-receipt-digest",
    required=True,
    metavar="SHA256",
    help="Canonical sha256 digest of the reviewed backup receipt.",
)
@click.option("--restore-test-id", required=True, help="Opaque successful restore-rehearsal token.")
@click.option(
    "--writer-fence-plan", required=True, help="Opaque reviewed deployment writer-stop token."
)
@click.option(
    "--output",
    type=click.Path(path_type=Path),
    required=True,
    help="Mode-0600 content-free plan destination.",
)
@click.option(
    "--overwrite",
    is_flag=True,
    default=False,
    help="Replace an existing regular plan file; never prompts.",
)
@_service_migration_scan_options
def migrate_012_service_plan(
    target_tenant_id,
    target_org_id,
    target_agent_id,
    backup_receipt_id,
    backup_receipt_digest,
    restore_test_id,
    writer_fence_plan,
    output,
    overwrite,
    **options,
):
    """Rescan PostgreSQL and write a digest-bound review plan.

    The command never mutates a database. It refuses an existing output unless
    --overwrite is explicit. Preview remains mandatory before apply.

    \b
    Examples:
      agnoclaw migrate 0.12 service plan --help
      agnoclaw migrate 0.12 service plan \\
        --source-credential-env AGNO_SOURCE_DSN \\
        --target-learning-credential-env AGNO_TARGET_DSN \\
        --target-runtime-credential-env AGNO_TARGET_DSN \\
        --schedule-map-file private/schedules.json \\
        --target-tenant-id tenant-a --target-agent-id reviewer \\
        --backup-receipt-id backup-v7 \\
        --backup-receipt-digest "$BACKUP_RECEIPT_DIGEST" \\
        --restore-test-id drill-42 --writer-fence-plan deployment-stop:v3 \\
        --output migration-plan.json --json
    """
    from agnoclaw import (
        PostgresMigrationBackupReceipt,
        create_postgres_migration_012_plan_from_scan,
        write_postgres_migration_012_plan,
    )
    from agnoclaw.migration_apply import Migration012Error

    json_output = bool(options.pop("json_output"))
    try:
        if output.exists() and not overwrite:
            raise Migration012Error(
                "MIGRATION_PLAN_OUTPUT_EXISTS",
                "The service migration plan output already exists.",
                output_role="service_plan",
            )
        (
            source,
            target_learning,
            target_runtime,
            schedule_map,
            scope_mappings,
            report,
        ) = _service_migration_scan(**options)
        if not report.ready:
            _migration_emit(
                command="service.plan",
                result=report.to_dict(),
                json_output=json_output,
                ok=False,
                next_action="Resolve every reported blocker, then rerun this exact plan command.",
            )
            raise click.exceptions.Exit(3)
        plan = create_postgres_migration_012_plan_from_scan(
            scan=report,
            source=source,
            target_learning=target_learning,
            target_runtime=target_runtime,
            target_tenant_id=target_tenant_id,
            target_org_id=target_org_id,
            target_agent_id=target_agent_id,
            schedule_map=schedule_map,
            scope_mappings=scope_mappings,
            backup_receipt=PostgresMigrationBackupReceipt(
                receipt_id=backup_receipt_id,
                receipt_digest=backup_receipt_digest,
                restore_test_id=restore_test_id,
            ),
            writer_fence_plan=writer_fence_plan,
        )
        path = write_postgres_migration_012_plan(output, plan)
    except click.exceptions.Exit:
        raise
    except OSError:
        _migration_fail(
            Migration012Error(
                "MIGRATION_PLAN_WRITE_FAILED",
                "The service migration plan could not be written safely.",
                output_role="service_plan",
            ),
            command="service.plan",
            json_output=json_output,
        )
    except (Migration012Error, TypeError, ValueError) as exc:
        _migration_fail(exc, command="service.plan", json_output=json_output)
    _migration_emit(
        command="service.plan",
        result={
            "scan": report.to_dict(),
            "plan": {**plan.to_dict(), "plan_path": str(path)},
            "apply_available": False,
        },
        json_output=json_output,
        next_command=(
            "agnoclaw migrate 0.12 service preview "
            f"--plan {_migration_shell_arg(path)} "
            f"--schedule-map-file {_migration_shell_arg(options['schedule_map_file'])} --json"
        ),
        next_action=(
            "Review and retain the plan and backup receipt, then run the exact service "
            "preview before any apply. Never use the local apply command for PostgreSQL."
        ),
    )


@migrate_012_service.command("preview")
@click.option(
    "--plan",
    "plan_path",
    type=click.Path(path_type=Path),
    required=True,
    metavar="PATH",
    help="Reviewed content-free service plan.",
)
@click.option(
    "--schedule-map-file",
    type=click.Path(path_type=Path),
    required=True,
    metavar="PATH",
    help="Exact private schedule-map JSON bound to the plan.",
)
@click.option(
    "--statement-timeout-ms",
    type=click.IntRange(1, 3_600_000),
    default=60_000,
    show_default=True,
)
@click.option(
    "--lock-timeout-ms",
    type=click.IntRange(1, 60_000),
    default=2_000,
    show_default=True,
)
@click.option(
    "--max-rows-per-table",
    type=click.IntRange(1, 1_000_000_000),
    default=10_000_000,
    show_default=True,
)
@click.option(
    "--batch-size",
    type=click.IntRange(1, 10_000),
    default=1_000,
    show_default=True,
)
@click.option(
    "--max-row-bytes",
    type=click.IntRange(1_024, 256 * 1024 * 1024),
    default=16 * 1024 * 1024,
    show_default=True,
)
@click.option(
    "--json",
    "json_output",
    is_flag=True,
    default=False,
    help="Emit the stable schema-v1 automation envelope.",
)
def migrate_012_service_preview(
    plan_path,
    schedule_map_file,
    statement_timeout_ms,
    lock_timeout_ms,
    max_rows_per_table,
    batch_size,
    max_row_bytes,
    json_output,
):
    """Compile exact transformations without target writes.

    The command rescans all endpoints, streams a fresh source snapshot, detects
    post-rekey collisions, and emits only counts and digests.

    \b
    Examples:
      agnoclaw migrate 0.12 service preview --help
      agnoclaw migrate 0.12 service preview \\
        --plan migration-plan.json \\
        --schedule-map-file private/schedules.json --batch-size 1000 --json
    """
    from agnoclaw import (
        load_postgres_schedule_map,
        preview_postgres_migration_012_transforms,
        read_postgres_migration_012_plan,
    )
    from agnoclaw.migration_apply import Migration012Error

    try:
        plan = read_postgres_migration_012_plan(plan_path)
        schedule_map = load_postgres_schedule_map(schedule_map_file)
        report = preview_postgres_migration_012_transforms(
            plan=plan,
            schedule_map=schedule_map,
            statement_timeout_ms=statement_timeout_ms,
            lock_timeout_ms=lock_timeout_ms,
            max_rows_per_table=max_rows_per_table,
            batch_size=batch_size,
            max_row_bytes=max_row_bytes,
        )
    except (Migration012Error, OSError, TypeError, ValueError) as exc:
        _migration_fail(exc, command="service.preview", json_output=json_output)
    _migration_emit(
        command="service.preview",
        result={"transform": report.to_dict(), "apply_available": True},
        json_output=json_output,
        next_command=(
            "agnoclaw migrate 0.12 service apply "
            f"--plan {_migration_shell_arg(plan_path)} "
            f"--schedule-map-file {_migration_shell_arg(schedule_map_file)} "
            f"--confirm-plan-digest {_migration_shell_arg(plan.plan_digest)} "
            f"--confirm-transform-digest {_migration_shell_arg(report.transform_digest)} "
            "--confirm-backup-receipt-digest "
            f"{_migration_shell_arg(plan.backup_receipt.receipt_digest)} "
            "--confirm-writer-fence-plan "
            f"{_migration_shell_arg(plan.writer_fence_plan)} --writers-stopped --json"
        ),
        next_action=(
            "Review the exact plan, transform digest, restore-tested backup receipt, and "
            "deployment writer fence before running the emitted apply command."
        ),
    )


def _service_migration_lifecycle_options(*, include_write_batch: bool = False):
    """Apply the shared, non-secret service lifecycle option grammar."""

    def decorate(function):
        options = [
            click.option(
                "--plan",
                "plan_path",
                type=click.Path(path_type=Path),
                required=True,
                metavar="PATH",
                help="Reviewed content-free service plan.",
            ),
            click.option(
                "--schedule-map-file",
                type=click.Path(path_type=Path),
                required=True,
                metavar="PATH",
                help="Exact private schedule-map JSON bound to the plan.",
            ),
            click.option(
                "--confirm-plan-digest",
                required=True,
                metavar="SHA256",
                help="Exact digest printed by service plan.",
            ),
            click.option(
                "--confirm-transform-digest",
                required=True,
                metavar="SHA256",
                help="Exact digest printed by service preview.",
            ),
            click.option(
                "--confirm-writer-fence-plan",
                required=True,
                metavar="TOKEN",
                help="Exact opaque deployment writer-fence token bound to the plan.",
            ),
            click.option(
                "--writers-stopped",
                is_flag=True,
                required=True,
                help="Confirm every source and target writer is stopped; never prompts.",
            ),
            click.option(
                "--statement-timeout-ms",
                type=click.IntRange(1, 3_600_000),
                default=60_000,
                show_default=True,
            ),
            click.option(
                "--lock-timeout-ms",
                type=click.IntRange(1, 60_000),
                default=2_000,
                show_default=True,
            ),
            click.option(
                "--max-rows-per-table",
                type=click.IntRange(1, 1_000_000_000),
                default=10_000_000,
                show_default=True,
            ),
            click.option(
                "--read-batch-size",
                type=click.IntRange(1, 10_000),
                default=1_000,
                show_default=True,
            ),
            click.option(
                "--max-row-bytes",
                type=click.IntRange(1_024, 256 * 1024 * 1024),
                default=16 * 1024 * 1024,
                show_default=True,
            ),
            click.option(
                "--dry-run",
                is_flag=True,
                default=False,
                help="Verify exact operation preconditions without target mutation.",
            ),
            click.option(
                "--json",
                "json_output",
                is_flag=True,
                default=False,
                help="Emit the stable schema-v1 automation envelope.",
            ),
        ]
        if include_write_batch:
            options.insert(
                -3,
                click.option(
                    "--write-batch-size",
                    type=click.IntRange(1, 10_000),
                    default=1_000,
                    show_default=True,
                    help="Rows per durable target checkpoint.",
                ),
            )
        for option in reversed(options):
            function = option(function)
        return function

    return decorate


def _service_migration_lifecycle_inputs(plan_path: Path, schedule_map_file: Path):
    from agnoclaw import (
        load_postgres_schedule_map,
        read_postgres_migration_012_plan,
    )

    return (
        read_postgres_migration_012_plan(plan_path),
        load_postgres_schedule_map(schedule_map_file),
    )


def _service_confirmation_prefix(
    *,
    command: str,
    plan_path: Path,
    schedule_map_file: Path,
    plan_digest: str,
    transform_digest: str,
    writer_fence_plan: str,
) -> str:
    return (
        f"agnoclaw migrate 0.12 service {command} "
        f"--plan {_migration_shell_arg(plan_path)} "
        f"--schedule-map-file {_migration_shell_arg(schedule_map_file)} "
        f"--confirm-plan-digest {_migration_shell_arg(plan_digest)} "
        f"--confirm-transform-digest {_migration_shell_arg(transform_digest)} "
        "--confirm-writer-fence-plan "
        f"{_migration_shell_arg(writer_fence_plan)} --writers-stopped"
    )


@migrate_012_service.command("apply")
@click.option(
    "--confirm-backup-receipt-digest",
    required=True,
    metavar="SHA256",
    help="Exact digest of the reviewed restore-tested backup receipt.",
)
@_service_migration_lifecycle_options(include_write_batch=True)
def migrate_012_service_apply(confirm_backup_receipt_digest, **options):
    """Apply reviewed transformations with durable provenance.

    The operation is idempotent and resumes from committed provenance checkpoints.
    It never edits deployment configuration or starts a target writer.

    \b
    Examples:
      agnoclaw migrate 0.12 service apply --help
      agnoclaw migrate 0.12 service apply \\
        --plan migration-plan.json --schedule-map-file private/schedules.json \\
        --confirm-plan-digest "$PLAN_DIGEST" \\
        --confirm-transform-digest "$TRANSFORM_DIGEST" \\
        --confirm-backup-receipt-digest "$BACKUP_RECEIPT_DIGEST" \\
        --confirm-writer-fence-plan deployment-stop:v3 --writers-stopped --json
    """
    from agnoclaw import (
        apply_postgres_migration_012,
        preview_postgres_migration_012_transforms,
    )
    from agnoclaw.migration_apply import Migration012Error

    json_output = bool(options.pop("json_output"))
    dry_run = bool(options.pop("dry_run"))
    plan_path = options.pop("plan_path")
    schedule_map_file = options.pop("schedule_map_file")
    try:
        plan, schedule_map = _service_migration_lifecycle_inputs(plan_path, schedule_map_file)
        if dry_run:
            report = preview_postgres_migration_012_transforms(
                plan=plan,
                schedule_map=schedule_map,
                statement_timeout_ms=options["statement_timeout_ms"],
                lock_timeout_ms=options["lock_timeout_ms"],
                max_rows_per_table=options["max_rows_per_table"],
                batch_size=options["read_batch_size"],
                max_row_bytes=options["max_row_bytes"],
            )
            if options["confirm_plan_digest"] != plan.plan_digest:
                raise Migration012Error(
                    "MIGRATION_CONFIRMATION_MISMATCH",
                    "Apply dry-run requires the exact reviewed plan digest.",
                )
            if options["confirm_transform_digest"] != report.transform_digest:
                raise Migration012Error(
                    "MIGRATION_POSTGRES_TRANSFORM_CONFIRMATION_MISMATCH",
                    "Apply dry-run differs from the reviewed transform digest.",
                )
            if confirm_backup_receipt_digest != plan.backup_receipt.receipt_digest:
                raise Migration012Error(
                    "MIGRATION_BACKUP_CONFIRMATION_MISMATCH",
                    "Apply dry-run requires the exact reviewed backup receipt digest.",
                )
            if options["confirm_writer_fence_plan"] != plan.writer_fence_plan:
                raise Migration012Error(
                    "MIGRATION_WRITER_FENCE_CONFIRMATION_MISMATCH",
                    "Apply dry-run requires the exact reviewed writer-fence token.",
                )
            result = {"dry_run": True, "mutated": False, "transform": report.to_dict()}
        else:
            receipt = apply_postgres_migration_012(
                plan=plan,
                schedule_map=schedule_map,
                confirm_backup_receipt_digest=confirm_backup_receipt_digest,
                **options,
            )
            result = {"dry_run": False, "mutated": True, "receipt": receipt.to_dict()}
    except (Migration012Error, OSError, TypeError, ValueError) as exc:
        _migration_fail(exc, command="service.apply", json_output=json_output)
    verify_command = _service_confirmation_prefix(
        command="verify",
        plan_path=plan_path,
        schedule_map_file=schedule_map_file,
        plan_digest=plan.plan_digest,
        transform_digest=options["confirm_transform_digest"],
        writer_fence_plan=plan.writer_fence_plan,
    )
    _migration_emit(
        command="service.apply",
        result=result,
        json_output=json_output,
        next_command=(verify_command + " --json" if not dry_run else None),
        next_action=(
            "Dry-run completed without writes; remove --dry-run only after reviewing every "
            "confirmation."
            if dry_run
            else "Keep all writers stopped and independently verify through new connections."
        ),
    )


@migrate_012_service.command("verify")
@_service_migration_lifecycle_options()
def migrate_012_service_verify(**options):
    """Recompute source, target, provenance, and unowned-write evidence.

    \b
    Examples:
      agnoclaw migrate 0.12 service verify --help
      agnoclaw migrate 0.12 service verify \\
        --plan migration-plan.json --schedule-map-file private/schedules.json \\
        --confirm-plan-digest "$PLAN_DIGEST" \\
        --confirm-transform-digest "$TRANSFORM_DIGEST" \\
        --confirm-writer-fence-plan deployment-stop:v3 --writers-stopped --json
    """
    from agnoclaw import verify_postgres_migration_012
    from agnoclaw.migration_apply import Migration012Error

    json_output = bool(options.pop("json_output"))
    plan_path = options.pop("plan_path")
    schedule_map_file = options.pop("schedule_map_file")
    dry_run = bool(options.get("dry_run"))
    try:
        plan, schedule_map = _service_migration_lifecycle_inputs(plan_path, schedule_map_file)
        receipt = verify_postgres_migration_012(
            plan=plan,
            schedule_map=schedule_map,
            **options,
        )
    except (Migration012Error, OSError, TypeError, ValueError) as exc:
        _migration_fail(exc, command="service.verify", json_output=json_output)
    _migration_emit(
        command="service.verify",
        result={
            "dry_run": dry_run,
            "mutated": not dry_run,
            "receipt": receipt.to_dict(),
        },
        json_output=json_output,
        next_command=("agnoclaw migrate 0.12 service cutover --help" if not dry_run else None),
        next_action=(
            "Dry-run verification passed without advancing control state."
            if dry_run
            else "Obtain a reviewed deployment cutover receipt before recording cutover."
        ),
    )


@migrate_012_service.command("cutover")
@click.option(
    "--cutover-receipt-id",
    required=True,
    metavar="TOKEN",
    help="Opaque reviewed deployment-change receipt token.",
)
@click.option(
    "--cutover-receipt-digest",
    required=True,
    metavar="SHA256",
    help="Canonical digest of the reviewed deployment-change receipt.",
)
@_service_migration_lifecycle_options()
def migrate_012_service_cutover(cutover_receipt_id, cutover_receipt_digest, **options):
    """Verify and record cutover; never edit deployment configuration.

    \b
    Examples:
      agnoclaw migrate 0.12 service cutover --help
      agnoclaw migrate 0.12 service cutover \\
        --plan migration-plan.json --schedule-map-file private/schedules.json \\
        --confirm-plan-digest "$PLAN_DIGEST" \\
        --confirm-transform-digest "$TRANSFORM_DIGEST" \\
        --confirm-writer-fence-plan deployment-stop:v3 --writers-stopped \\
        --cutover-receipt-id change-42 \\
        --cutover-receipt-digest "$CUTOVER_RECEIPT_DIGEST" --json
    """
    from agnoclaw import cutover_postgres_migration_012
    from agnoclaw.migration_apply import Migration012Error

    json_output = bool(options.pop("json_output"))
    plan_path = options.pop("plan_path")
    schedule_map_file = options.pop("schedule_map_file")
    dry_run = bool(options.get("dry_run"))
    try:
        plan, schedule_map = _service_migration_lifecycle_inputs(plan_path, schedule_map_file)
        receipt = cutover_postgres_migration_012(
            plan=plan,
            schedule_map=schedule_map,
            cutover_receipt_id=cutover_receipt_id,
            cutover_receipt_digest=cutover_receipt_digest,
            **options,
        )
    except (Migration012Error, OSError, TypeError, ValueError) as exc:
        _migration_fail(exc, command="service.cutover", json_output=json_output)
    _migration_emit(
        command="service.cutover",
        result={
            "dry_run": dry_run,
            "mutated": not dry_run,
            "receipt": receipt.to_dict(),
        },
        json_output=json_output,
        next_command=("agnoclaw migrate 0.12 service rollback --help" if not dry_run else None),
        next_action=(
            "Dry-run passed without recording cutover."
            if dry_run
            else "The deployment controller may now perform its separately reviewed rollout; "
            "record the first target write because it closes restore-style rollback."
        ),
    )


@migrate_012_service.command("rollback")
@click.option(
    "--confirm-no-post-cutover-target-writes",
    is_flag=True,
    default=False,
    help="Confirm no target writer has run since recorded cutover.",
)
@_service_migration_lifecycle_options(include_write_batch=True)
def migrate_012_service_rollback(confirm_no_post_cutover_target_writes, **options):
    """Reverse exact migration-owned rows and refuse target drift.

    \b
    Examples:
      agnoclaw migrate 0.12 service rollback --help
      agnoclaw migrate 0.12 service rollback \\
        --plan migration-plan.json --schedule-map-file private/schedules.json \\
        --confirm-plan-digest "$PLAN_DIGEST" \\
        --confirm-transform-digest "$TRANSFORM_DIGEST" \\
        --confirm-writer-fence-plan deployment-stop:v3 --writers-stopped \\
        --confirm-no-post-cutover-target-writes --json
    """
    from agnoclaw import rollback_postgres_migration_012
    from agnoclaw.migration_apply import Migration012Error

    json_output = bool(options.pop("json_output"))
    plan_path = options.pop("plan_path")
    schedule_map_file = options.pop("schedule_map_file")
    dry_run = bool(options.get("dry_run"))
    try:
        plan, schedule_map = _service_migration_lifecycle_inputs(plan_path, schedule_map_file)
        receipt = rollback_postgres_migration_012(
            plan=plan,
            schedule_map=schedule_map,
            confirm_no_post_cutover_target_writes=(confirm_no_post_cutover_target_writes),
            **options,
        )
    except (Migration012Error, OSError, TypeError, ValueError) as exc:
        _migration_fail(exc, command="service.rollback", json_output=json_output)
    _migration_emit(
        command="service.rollback",
        result={
            "dry_run": dry_run,
            "mutated": not dry_run,
            "receipt": receipt.to_dict(),
        },
        json_output=json_output,
        next_action=(
            "Dry-run passed without deleting or changing target rows."
            if dry_run
            else "Retain control/provenance audit rows and verify the deployment still points "
            "to the legacy source before restarting any writer."
        ),
    )


@migrate_012.command("plan")
@click.option("--learning-db", type=click.Path(path_type=Path), default=None)
@click.option("--schedules", type=click.Path(path_type=Path), default=None)
@click.option("--target-learning-db", type=click.Path(path_type=Path), default=None)
@click.option("--target-runtime-db", type=click.Path(path_type=Path), default=None)
@click.option("--target-tenant-id", default=None)
@click.option("--target-org-id", default=None)
@click.option("--target-agent-id", default=None)
@click.option("--learning-table", "learning_tables", multiple=True)
@click.option("--scope-map-file", type=click.Path(path_type=Path), default=None)
@click.option("--timezone", "schedule_timezone", default=None)
@click.option(
    "--misfire-policy",
    type=click.Choice(["skip", "run_once"], case_sensitive=False),
    default=None,
)
@click.option("--old-writer-fence-plan", default=None)
@click.option("--output", type=click.Path(path_type=Path), required=True)
@click.option("--json", "json_output", is_flag=True, default=False)
def migrate_012_plan(
    learning_db,
    schedules,
    target_learning_db,
    target_runtime_db,
    target_tenant_id,
    target_org_id,
    target_agent_id,
    learning_tables,
    scope_map_file,
    schedule_timezone,
    misfire_policy,
    old_writer_fence_plan,
    output,
    json_output,
):
    """Create a content-free, checksum-bound migration plan."""
    from agnoclaw import create_migration_012_plan, write_migration_012_plan
    from agnoclaw.runtime.errors import HarnessError

    try:
        plan = create_migration_012_plan(
            learning_sqlite_path=learning_db,
            schedule_json_path=schedules,
            target_learning_db=target_learning_db,
            target_runtime_db=target_runtime_db,
            target_tenant_id=target_tenant_id,
            target_org_id=target_org_id,
            target_agent_id=target_agent_id,
            learning_table_names=learning_tables
            or ("agno_learnings", "agno_memories", "agnoclaw_memories"),
            scope_mappings=_migration_scope_mappings(scope_map_file),
            schedule_default_timezone=schedule_timezone,
            schedule_default_misfire_policy=misfire_policy,
            old_writer_fence_plan=old_writer_fence_plan,
        )
        path = write_migration_012_plan(output, plan)
    except (HarnessError, OSError, TypeError, ValueError) as exc:
        _migration_fail(exc, command="plan", json_output=json_output)
    result = {**plan.to_dict(), "plan_path": str(path)}
    _migration_emit(
        command="plan",
        result=result,
        json_output=json_output,
        next_command=(
            "agnoclaw migrate 0.12 apply "
            f"--plan {_migration_shell_arg(path)} --state-dir <backup-dir> "
            f"--confirm-plan {plan.plan_digest} --writers-stopped"
        ),
    )


@migrate_012.command("apply")
@click.option("--plan", "plan_path", type=click.Path(path_type=Path), required=True)
@click.option("--state-dir", type=click.Path(path_type=Path), required=True)
@click.option("--confirm-plan", required=True)
@click.option("--writers-stopped", is_flag=True, default=False)
@click.option("--json", "json_output", is_flag=True, default=False)
def migrate_012_apply(plan_path, state_dir, confirm_plan, writers_stopped, json_output):
    """Fence writers, create verified backups, and idempotently import."""
    from agnoclaw import apply_migration_012, read_migration_012_plan
    from agnoclaw.runtime.errors import HarnessError

    try:
        plan = read_migration_012_plan(plan_path)
        result = apply_migration_012(
            plan,
            state_dir=state_dir,
            confirm_plan_digest=confirm_plan,
            writers_stopped=writers_stopped,
        )
    except (HarnessError, OSError, TypeError, ValueError) as exc:
        _migration_fail(exc, command="apply", json_output=json_output)
    _migration_emit(
        command="apply",
        result=result,
        json_output=json_output,
        next_command=(
            f"agnoclaw migrate 0.12 verify --state-dir {_migration_shell_arg(state_dir)}"
        ),
    )


@migrate_012.command("verify")
@click.option("--state-dir", type=click.Path(path_type=Path), required=True)
@click.option("--json", "json_output", is_flag=True, default=False)
def migrate_012_verify(state_dir, json_output):
    """Independently verify imported identities, counts, and logical digests."""
    from agnoclaw import verify_migration_012
    from agnoclaw.runtime.errors import HarnessError

    try:
        result = verify_migration_012(state_dir=state_dir)
    except (HarnessError, OSError, TypeError, ValueError) as exc:
        _migration_fail(exc, command="verify", json_output=json_output)
    _migration_emit(
        command="verify",
        result=result,
        json_output=json_output,
        next_command=(
            "agnoclaw migrate 0.12 cutover "
            f"--state-dir {_migration_shell_arg(state_dir)} "
            f"--confirm-migration {result['migration_id']}"
        ),
    )


@migrate_012.command("cutover")
@click.option("--state-dir", type=click.Path(path_type=Path), required=True)
@click.option("--confirm-migration", required=True)
@click.option("--json", "json_output", is_flag=True, default=False)
def migrate_012_cutover(state_dir, confirm_migration, json_output):
    """Record cutover after verification without removing rollback."""
    from agnoclaw import cutover_migration_012
    from agnoclaw.runtime.errors import HarnessError

    try:
        result = cutover_migration_012(
            state_dir=state_dir,
            confirm_migration_id=confirm_migration,
        )
    except (HarnessError, OSError, TypeError, ValueError) as exc:
        _migration_fail(exc, command="cutover", json_output=json_output)
    _migration_emit(
        command="cutover",
        result=result,
        json_output=json_output,
        next_command=(
            "Rollback if required: agnoclaw migrate 0.12 rollback "
            f"--state-dir {_migration_shell_arg(state_dir)} "
            f"--confirm-migration {result['migration_id']} --writers-stopped"
        ),
    )


@migrate_012.command("rollback")
@click.option("--state-dir", type=click.Path(path_type=Path), required=True)
@click.option("--confirm-migration", required=True)
@click.option("--writers-stopped", is_flag=True, default=False)
@click.option("--json", "json_output", is_flag=True, default=False)
def migrate_012_rollback(state_dir, confirm_migration, writers_stopped, json_output):
    """Restore verified target preimages before the contraction boundary."""
    from agnoclaw import rollback_migration_012
    from agnoclaw.runtime.errors import HarnessError

    try:
        result = rollback_migration_012(
            state_dir=state_dir,
            confirm_migration_id=confirm_migration,
            writers_stopped=writers_stopped,
        )
    except (HarnessError, OSError, TypeError, ValueError) as exc:
        _migration_fail(exc, command="rollback", json_output=json_output)
    _migration_emit(
        command="rollback",
        result=result,
        json_output=json_output,
    )


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
