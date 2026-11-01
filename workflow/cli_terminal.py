"""`wf terminal` -- open Claude Code and other harnesses on this machine from NotesGraph."""

from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

console = Console()

terminal_app = typer.Typer(help="Serve NotesGraph's remote terminal on this machine",
                           no_args_is_help=True)


def _settings(device: Optional[str], cwd: Path):
    from workflow.claude_accounts import config_dir_for_project
    from workflow.cli_agent import _client_and_device
    from workflow.config import load_effective_config
    from workflow.deploy.terminal_serve import Settings

    client, target = _client_and_device(device)
    if not client.token:
        console.print("❌ No NotesGraph token. Run 'wf deploy notes-login <token>' first.")
        raise typer.Exit(1)
    terminal_cfg = (load_effective_config() or {}).get("terminal") or {}
    return Settings(
        workspace=client.workspace, device_key=target.id, url=client.url.rstrip("/"),
        token=client.token, cwd=cwd, allow_shell=bool(terminal_cfg.get("allow_shell")),
        claude_config_dir=config_dir_for_project())


@terminal_app.command("serve")
def terminal_serve(
    device: Optional[str] = typer.Option(None, "--device", "-d", help="Target id (default: this machine)"),
    cwd: Path = typer.Option(Path.cwd(), "--cwd", help="Folder harnesses start in"),
    exit_on_update: bool = typer.Option(
        False, "--exit-on-update",
        help="When wf's code changes and no terminal is open, exit (for a "
             "supervisor to restart it on the new code; see `wf terminal install`)"),
):
    """Let NotesGraph open Claude Code, Codex, Gemini CLI or a shell here.

    Connects out to NotesGraph with this device's token and waits. Each
    terminal opened from the Deployments or Agents view runs here in a pty.
    Ctrl-C stops it and ends any open terminals.
    """
    try:
        import socketio  # noqa: F401
    except ImportError:
        console.print("❌ python-socketio isn't installed in wf's environment.")
        console.print("💡 pip install 'python-socketio[client]'")
        raise typer.Exit(1)
    from workflow.deploy.terminal_serve import serve

    settings = _settings(device, cwd.resolve())
    console.print(f"🖥  Serving terminals for [bold]{settings.device_key}[/bold] "
                  f"from {settings.cwd} (Ctrl-C to stop)")
    if not settings.allow_shell:
        console.print("   [dim]plain shell is off (terminal.allow_shell in ~/.wf/config.yaml)[/dim]")

    stop_when = None
    if exit_on_update:
        from workflow.deploy.updates import UpdateWatch
        stop_when = UpdateWatch()
    try:
        serve(settings, log=lambda message: console.print(f"   {message}"), stop_when=stop_when)
    except KeyboardInterrupt:
        console.print("\n🛑 Stopped")


@terminal_app.command("install")
def terminal_install(
    device: Optional[str] = typer.Option(None, "--device", "-d", help="Target id (default: this machine)"),
    cwd: Path = typer.Option(Path.cwd(), "--cwd", help="Folder harnesses start in"),
):
    """Run `wf terminal serve` as a service beside `wf agent`'s runner."""
    from workflow.deploy import supervisor

    _settings(device, cwd)  # fail now, not in the background, if unconfigured
    how = supervisor.install_program(supervisor.TERMINAL_PROGRAM,
                                     supervisor.terminal_command(device), cwd.resolve())
    console.print(f"✓ Installed {supervisor.TERMINAL_PROGRAM}, started by {how}")
    console.print(f"   {supervisor.status(name=supervisor.TERMINAL_PROGRAM)}")
    console.print(f"   [dim]logs: {supervisor.default_paths().logs / (supervisor.TERMINAL_PROGRAM + '.log')}[/dim]")


@terminal_app.command("uninstall")
def terminal_uninstall():
    """Stop the terminal service and remove it."""
    from workflow.deploy import supervisor

    supervisor.uninstall(name=supervisor.TERMINAL_PROGRAM)
    console.print(f"✓ Removed {supervisor.TERMINAL_PROGRAM}")


@terminal_app.command("status")
def terminal_status():
    """Whether the terminal service is installed and running."""
    from workflow.deploy import supervisor

    console.print(supervisor.status(name=supervisor.TERMINAL_PROGRAM))
