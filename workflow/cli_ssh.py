"""`wf ssh` -- the `ssh` MCP server and the daemon holding its sessions.

See workflow/ssh_mcp.py for the tools and workflow/ssh_daemon.py for how a
session works.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import typer
from rich.console import Console
from rich.table import Table

from workflow import ssh_mcp

console = Console()

ssh_app = typer.Typer(help="Persistent SSH sessions for Claude (the `ssh` MCP server)",
                      no_args_is_help=True)

SERVER_NAME = "ssh"


def claude_config_dirs() -> List[Optional[str]]:
    """Every Claude config directory on the machine; None is the default ~/.claude.

    The ones projects are configured with, plus any ~/.claude-* that holds a
    login, so `claude` sessions started outside `wf` see the server too.
    """
    from workflow.config import load_config

    dirs = {None}
    for project in (load_config().get("projects") or {}).values():
        raw = (project or {}).get("claude_config_dir")
        if raw:
            dirs.add(str(Path(raw).expanduser()))
    for path in Path.home().glob(".claude-*"):
        if (path / ".claude.json").exists():
            dirs.add(str(path))
    return sorted(dirs, key=lambda d: d or "")


def _claude(args: List[str], config_dir: Optional[str]) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("CLAUDE_CONFIG_DIR", None)
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = config_dir
    return subprocess.run(["claude", "mcp", *args], env=env, capture_output=True, text=True)


@ssh_app.command("install")
def install():
    """Register the `ssh` MCP server at user scope in every Claude config directory."""
    if not shutil.which("claude"):
        console.print("❌ `claude` is not on PATH")
        raise typer.Exit(1)
    server = json.dumps({"type": "stdio", "command": sys.executable,
                         "args": ["-m", "workflow.ssh_mcp"]})
    failed = False
    for config_dir in claude_config_dirs():
        name = config_dir or "~/.claude (default)"
        _claude(["remove", "-s", "user", SERVER_NAME], config_dir)
        result = _claude(["add-json", "-s", "user", SERVER_NAME, server], config_dir)
        if result.returncode == 0:
            console.print(f"✅ {name}")
        else:
            failed = True
            console.print(f"❌ {name}: {(result.stderr or result.stdout).strip()}")
    console.print("New Claude sessions get the ssh_* tools; running ones need a restart.")
    if failed:
        raise typer.Exit(1)


@ssh_app.command("uninstall")
def uninstall():
    """Remove the `ssh` MCP server from every Claude config directory."""
    for config_dir in claude_config_dirs():
        result = _claude(["remove", "-s", "user", SERVER_NAME], config_dir)
        state = "removed" if result.returncode == 0 else "not registered"
        console.print(f"{config_dir or '~/.claude (default)'}: {state}")


@ssh_app.command("status")
def status():
    """Show whether the daemon is running and the sessions it holds."""
    if not ssh_mcp.daemon_running():
        console.print("Daemon not running (it starts on the first ssh_* tool call).")
        return
    sessions = ssh_mcp.call("list", 10)["sessions"]
    if not sessions:
        console.print("Daemon running, no open sessions.")
        return
    table = Table("Session", "Host", "Label", "State", "Cwd", "Idle")
    for s in sessions:
        state = "closed" if not s["alive"] else (f"running: {s['running_command']}" if s["busy"] else "idle")
        table.add_row(s["session_id"], s["host"], s["label"] or "", state, s["cwd"] or "",
                      f"{s['idle_seconds']}s")
    console.print(table)


@ssh_app.command("close")
def close(session_id: str = typer.Argument(..., help="Session id from `wf ssh status`")):
    """Close one session."""
    try:
        ssh_mcp.call("close", 15, session_id=session_id)
    except ssh_mcp.DaemonError as e:
        console.print(f"❌ {e}")
        raise typer.Exit(1)
    console.print(f"Closed {session_id}")


@ssh_app.command("stop")
def stop():
    """Close every session and stop the daemon."""
    if not ssh_mcp.daemon_running():
        console.print("Daemon not running.")
        return
    ssh_mcp.call("shutdown", 15)
    console.print("Daemon stopped.")


@ssh_app.command("mcp")
def serve_mcp():
    """Run the MCP server on stdio (what `wf ssh install` registers)."""
    ssh_mcp.main()
