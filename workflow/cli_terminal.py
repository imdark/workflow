"""`wf terminal` -- let NotesGraph open a harness on this device, live in the browser."""

import asyncio
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console

console = Console()

terminal_app = typer.Typer(help="Serve remote terminals to NotesGraph", no_args_is_help=True)


def _settings() -> dict:
    """The device's terminal settings: terminal.harnesses, terminal.allow_shell."""
    from workflow.config import load_effective_config

    return load_effective_config().get("terminal") or {}


@terminal_app.command("serve")
def terminal_serve(
    device: Optional[str] = typer.Option(None, "--device", "-d", help="Target id (default: this machine)"),
    cwd: Optional[Path] = typer.Option(None, "--cwd", help="Folder harnesses start in"),
    exit_on_update: bool = typer.Option(
        False, "--exit-on-update",
        help="When wf's code changes, exit once no session is running (for a "
             "supervisor to restart it on the new code; see `wf terminal install`)"),
):
    """Let NotesGraph open Claude Code, Codex or Gemini CLI here.

    Connects out over a websocket, so nothing listens on this machine.
    Claude starts with the project's Claude account and NotesGraph's MCP
    tools. Closing the browser leaves the harness running. A plain shell is
    refused unless terminal.allow_shell is true in ~/.wf/config.yaml.
    """
    from workflow.cli_agent import _client_and_device
    from workflow.deploy.terminal_serve import NotesGraphMcp, serve

    client, target = _client_and_device(device)
    if not client.token:
        console.print("❌ No NotesGraph token; run 'wf deploy notes-login <token>'")
        raise typer.Exit(1)
    settings = _settings()
    allow_shell = bool(settings.get("allow_shell"))
    overrides = settings.get("harnesses") or {}
    mcp = NotesGraphMcp(client.url, client.workspace, client.token)
    console.print(f"🖥️  Serving terminals for [bold]{target.id}[/bold]"
                  f"{' (shell allowed)' if allow_shell else ''} (Ctrl-C to stop)")

    def on_event(kind, detail=None):
        if kind == "spawn":
            console.print(f"   ▶ {detail.get('harness')} [dim]{str(detail.get('id'))[:8]}[/dim]")
        elif kind == "attached":
            console.print(f"   [green]✓[/green] connected to {client.url}")
        elif kind == "stopping":
            console.print(f"♻️  wf was updated: starting no new sessions ({detail} running), "
                          "then exiting to restart on the new code")

    stop_when = None
    if exit_on_update:
        from workflow.deploy.updates import UpdateWatch
        stop_when = UpdateWatch()

    try:
        asyncio.run(serve(client.url, client.token, client.workspace, target.id,
                          cwd=str(cwd.resolve()) if cwd else None, overrides=overrides,
                          allow_shell=allow_shell, mcp=mcp, stop_when=stop_when,
                          on_event=on_event))
    except KeyboardInterrupt:
        console.print("\n🛑 Stopped")
        return
    if stop_when is not None:
        console.print("♻️  Exiting for the update")


@terminal_app.command("install")
def terminal_install(
    device: Optional[str] = typer.Option(None, "--device", "-d", help="Target id (default: this machine)"),
    cwd: Path = typer.Option(Path.cwd(), "--cwd", help="Folder harnesses start in"),
):
    """Run `wf terminal serve` as a service, beside the agent runner.

    Same supervisord as `wf agent install`: started at login, and back on the
    new code after a `git pull` in wf's checkout, once no session is running.
    Logs: ~/.wf/logs/wf-terminal.log.
    """
    from workflow.cli_agent import _client_and_device
    from workflow.deploy import supervisor

    _client_and_device(device)  # fail now, not in the background, if unconfigured
    how = supervisor.install_program(supervisor.TERMINAL_PROGRAM,
                                     supervisor.terminal_command(device), cwd.resolve())
    console.print(f"✓ Installed {supervisor.TERMINAL_PROGRAM}, started by {how}")
    console.print(f"   {_status()}")
    console.print(f"   [dim]harnesses start in {cwd.resolve()}; "
                  f"shell {'allowed' if _settings().get('allow_shell') else 'off'}[/dim]")
    logs = supervisor.default_paths().logs / (supervisor.TERMINAL_PROGRAM + ".log")
    console.print(f"   [dim]logs: {logs}[/dim]")


@terminal_app.command("uninstall")
def terminal_uninstall():
    """Stop the terminal service and remove it."""
    from workflow.deploy import supervisor

    supervisor.uninstall(name=supervisor.TERMINAL_PROGRAM)
    console.print(f"✓ Removed {supervisor.TERMINAL_PROGRAM}")


def _status() -> str:
    from workflow.deploy import supervisor

    return supervisor.status(name=supervisor.TERMINAL_PROGRAM,
                             install_hint="wf terminal install")


@terminal_app.command("status")
def terminal_status():
    """Whether the terminal service is installed and running."""
    console.print(_status())
