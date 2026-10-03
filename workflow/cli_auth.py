"""`wf auth` -- manage which Claude account each project logs in as.

Claude Pro/Max accounts are OAuth logins, so switching them means switching
config directories rather than keys. See workflow/claude_accounts.py.
"""

import os
import subprocess
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

from workflow import claude_accounts

console = Console()

auth_app = typer.Typer(help="Manage per-project Claude accounts", no_args_is_help=True)


@auth_app.command("status")
def auth_status():
    """Show which Claude account directory each project uses."""
    from workflow.config import load_config
    from workflow.projects import get_current_project

    cfg = load_config()
    projects = cfg.get("projects") or {}
    if not projects:
        console.print("No projects configured.")
        return

    current = get_current_project()
    console.print("Claude accounts by project:\n")
    for name in sorted(projects):
        directory = claude_accounts.config_dir_for_project(name, cfg)
        marker = " [bold](current)[/bold]" if name == current else ""
        if not directory:
            console.print(f"  {name}{marker}: [dim]default (~/.claude)[/dim]")
            continue
        state = "logged in" if claude_accounts.is_logged_in(directory) else "[yellow]not set up[/yellow]"
        console.print(f"  {name}{marker}: {directory}  ({state})")

    active = os.environ.get(claude_accounts.ENV_VAR)
    console.print(f"\nThis shell: {active or '[dim]unset — Claude Code uses ~/.claude[/dim]'}")
    console.print("💡 'wf auth login <project>' sets a project's account up.")


@auth_app.command("set")
def auth_set(
    project_name: str = typer.Argument(..., help="Project name"),
    directory: Optional[str] = typer.Argument(None, help="Config directory (default: ~/.claude-<project>)"),
    clear: bool = typer.Option(False, "--clear", help="Remove the mapping and fall back to ~/.claude"),
):
    """Point a project at a Claude config directory."""
    try:
        if clear:
            claude_accounts.set_config_dir(project_name, None)
            console.print(f"✅ {project_name} now uses the default ~/.claude")
            return

        target = directory or claude_accounts.default_dir_for(project_name)
        resolved = claude_accounts.set_config_dir(project_name, target)
        console.print(f"✅ {project_name} → {resolved}")
        if not claude_accounts.is_logged_in(resolved):
            console.print(f"💡 Log that account in with: wf auth login {project_name}")
    except ValueError as e:
        console.print(f"❌ {e}")
        raise typer.Exit(1)


@auth_app.command("login")
def auth_login(
    project_name: Optional[str] = typer.Argument(None, help="Project (default: current)"),
):
    """Launch Claude Code against a project's account so you can run /login.

    Each config directory holds its own credential, so this is a one-time
    setup per project. Both accounts stay logged in afterwards -- switching
    projects never re-authenticates.
    """
    from workflow.projects import get_current_project

    project_name = project_name or get_current_project()
    if not project_name:
        console.print("❌ No project specified and no current project set.")
        raise typer.Exit(1)

    directory = claude_accounts.config_dir_for_project(project_name)
    if not directory:
        directory = claude_accounts.default_dir_for(project_name)
        claude_accounts.set_config_dir(project_name, directory)
        console.print(f"📁 Created mapping: {project_name} → {directory}")

    Path(directory).mkdir(parents=True, exist_ok=True)

    console.print(f"🔐 Starting Claude Code with {claude_accounts.ENV_VAR}={directory}")
    console.print("   Run [bold]/login[/bold] inside, sign in as this project's account,")
    console.print("   then [bold]/status[/bold] to confirm the email, and exit.\n")

    env = {**os.environ, claude_accounts.ENV_VAR: directory}
    # Drop key-based credentials for this launch: they outrank a /login
    # credential, so leaving them set would authenticate the session as the
    # key's account and the login would appear not to take effect.
    for override in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        env.pop(override, None)

    try:
        subprocess.run(["claude"], env=env)
    except FileNotFoundError:
        console.print("❌ 'claude' is not on PATH.")
        raise typer.Exit(1)

    if claude_accounts.is_logged_in(directory):
        console.print(f"\n✅ {project_name} is set up.")
    else:
        console.print(f"\n⚠️  {directory} still looks empty — did /login complete?")


@auth_app.command("env")
def auth_env(project_name: Optional[str] = typer.Argument(None, help="Project (default: current)")):
    """Print the export that switches this shell to a project's account."""
    line = claude_accounts.shell_export(project_name)
    if line:
        typer.echo(line)
    else:
        # Unset rather than print nothing, so `eval "$(wf auth env)"` moves a
        # shell back to the default account instead of leaving a stale value.
        typer.echo(f"unset {claude_accounts.ENV_VAR}")
