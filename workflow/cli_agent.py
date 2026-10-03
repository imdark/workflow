"""`wf agent` -- run agent jobs dispatched from NotesGraph on this device."""

from typing import Optional

import typer
from rich.console import Console

console = Console()

agent_app = typer.Typer(help="Serve remote agent jobs on this machine", no_args_is_help=True)


def _client_and_device(device: Optional[str]):
    """Resolve the inventory client and which device key this machine is."""
    from workflow.config import load_effective_config
    from workflow.deploy.jobs import JobClient
    from workflow.deploy.registry import get_registry
    from workflow.deploy.ssh import is_local_host

    cfg = load_effective_config()
    ng = (cfg.get("deploy") or {}).get("notesgraph") or {}
    if not ng.get("workspace"):
        console.print("❌ No NotesGraph workspace configured.")
        console.print("💡 Set deploy.notesgraph.workspace, then 'wf deploy notes-login <token>'")
        raise typer.Exit(1)

    registry = get_registry()
    if device:
        resolved = registry.get(device)
        if not resolved:
            console.print(f"❌ No target '{device}' registered")
            raise typer.Exit(1)
    else:
        # Default to whichever registered agent target is this machine.
        candidates = [d for d in registry.list(agent_target=True)
                      if is_local_host(d.host) and d.kind == "machine"]
        if not candidates:
            console.print("❌ This machine is not registered as an agent target.")
            console.print("💡 Register it: wf deploy add <user>@$(hostname -s) --agent")
            raise typer.Exit(1)
        if len(candidates) > 1:
            console.print(f"❌ Several local agent targets: "
                          f"{', '.join(d.id for d in candidates)}. Name one with --device.")
            raise typer.Exit(1)
        resolved = candidates[0]

    client = JobClient(url=ng.get("url", "https://app.notesgraph.com"),
                       workspace=ng["workspace"])
    return client, resolved


@agent_app.command("serve")
def agent_serve(
    device: Optional[str] = typer.Option(None, "--device", "-d", help="Target id (default: this machine)"),
    interval: int = typer.Option(5, "--interval", "-i", help="Seconds between polls"),
    once: bool = typer.Option(False, "--once", help="Run at most one job, then exit"),
):
    """Claim and run agent jobs queued for this device.

    Polls the inventory; nothing connects inward, so this works from behind
    NAT. Ctrl-C to stop — a job already running is left to finish and its
    lease lapses if this process dies.
    """
    from workflow.deploy.jobs import serve as serve_jobs
    from workflow.deploy.prereqs import check_tmux, tmux_missing_message
    from workflow.deploy.ssh import LocalRunner

    # Jobs run in tmux so they can be watched; refuse to start without it
    # rather than take a job this machine can't run the way it's meant to.
    check = check_tmux(LocalRunner(None))
    if not check.installed:
        for line in tmux_missing_message(check, "this machine"):
            console.print(line)
        raise typer.Exit(1)

    client, target = _client_and_device(device)
    console.print(f"🤖 Serving agent jobs for [bold]{target.id}[/bold] "
                  f"(every {interval}s, Ctrl-C to stop)")

    def on_event(kind, job=None, detail=""):
        if kind == "claimed":
            console.print(f"   ▶ {job.agent_name} [dim]{job.id[:8]}[/dim]")
            if detail:
                console.print(f"     [dim]watch it: tmux attach -t {detail}[/dim]")
        elif kind == "done":
            console.print(f"   [green]✓[/green] {job.agent_name} finished")
        elif kind == "cancelled":
            console.print(f"   [dim]■ {job.agent_name} cancelled from NotesGraph[/dim]")
        elif kind == "failed":
            console.print(f"   [red]✗[/red] {job.agent_name}: {detail}")
        elif kind == "error":
            console.print(f"   [yellow]![/yellow] {job or detail}")

    try:
        ran = serve_jobs(client, target.id, poll_seconds=interval, once=once,
                         on_event=on_event)
    except KeyboardInterrupt:
        console.print("\n🛑 Stopped")
        return
    if once:
        console.print(f"Ran {ran} job(s)")


@agent_app.command("jobs")
def agent_jobs(
    device: Optional[str] = typer.Option(None, "--device", "-d"),
    limit: int = typer.Option(15, "--limit", "-l"),
):
    """List recent agent jobs for a device."""
    import time

    from workflow.deploy.registry.notesgraph import NotesGraphError

    client, target = _client_and_device(device)
    try:
        jobs = client.list_jobs(target.id)[:limit]
    except NotesGraphError as e:
        console.print(f"❌ {e}")
        raise typer.Exit(1)

    if not jobs:
        console.print(f"No jobs for {target.id}.")
        return

    styles = {"done": "green", "error": "red", "running": "yellow",
              "queued": "cyan", "cancelled": "dim"}
    for job in jobs:
        when = time.strftime("%m-%d %H:%M", time.localtime(job.get("createdAt", 0)))
        status = job.get("status", "?")
        console.print(f"  [{styles.get(status, 'white')}]●[/] {job['id'][:8]}  {when}  "
                      f"{job.get('agentName', '?'):<20} {status}")
        if status == "error" and job.get("error"):
            console.print(f"      [dim]{job['error'][:120]}[/dim]")


@agent_app.command("attach")
def agent_attach(
    job: Optional[str] = typer.Argument(None, help="Job id or its first 8 characters (default: the newest running job)"),
):
    """Attach to a job's tmux session on this machine to watch it run.

    Detach again with Ctrl-B d; the job keeps running either way.
    """
    import os
    import shutil
    import subprocess

    from workflow.deploy.jobs import tmux_session_name

    if not shutil.which("tmux"):
        console.print("❌ tmux is not installed, so jobs here run without a session to attach to.")
        raise typer.Exit(1)

    listed = subprocess.run(
        ["tmux", "list-sessions", "-F", "#{session_created} #{session_name}"],
        capture_output=True, text=True,
    )
    # (created, name), newest first.
    sessions = sorted(
        (tuple(line.split(" ", 1)) for line in listed.stdout.splitlines()),
        reverse=True,
    )
    sessions = [(created, s) for created, s in sessions if s.startswith("wf-job-")]
    if job:
        name = tmux_session_name(job)
        if name not in {s for _, s in sessions}:
            console.print(f"❌ No session {name} on this machine. It may have finished "
                          "more than 10 minutes ago, or run on another device.")
            raise typer.Exit(1)
    elif sessions:
        name = sessions[0][1]
    else:
        console.print("No agent job sessions on this machine.")
        raise typer.Exit(1)

    # Inside tmux already: switch instead of nesting a client.
    verb = "switch-client" if os.environ.get("TMUX") else "attach-session"
    os.execvp("tmux", ["tmux", verb, "-t", name])


@agent_app.command("logs")
def agent_logs(
    job: Optional[str] = typer.Argument(None, help="Job id or its first characters (default: this device's newest job)"),
    follow: bool = typer.Option(False, "--follow", "-f", help="Keep printing until the job finishes"),
    device: Optional[str] = typer.Option(None, "--device", "-d", help="Newest job of this target instead"),
):
    """Show a job's transcript: tool calls, results, questions, the answer.

    Read from NotesGraph, so it works for a job on any device. Falls back to
    this machine's own copy if NotesGraph can't be reached.
    """
    import sys

    from workflow.deploy.jobs import JOB_ROOT, follow_log
    from workflow.deploy.registry.notesgraph import NotesGraphError

    client, target = _client_and_device(device)

    def local_copy(prefix: str) -> bool:
        logs = sorted(JOB_ROOT.glob(f"{prefix}*/log.txt"),
                      key=lambda p: p.stat().st_mtime, reverse=True)
        if not logs:
            return False
        console.print(f"[dim]From this machine's copy: {logs[0]}[/dim]")
        sys.stdout.write(logs[0].read_text(encoding="utf-8", errors="replace"))
        return True

    try:
        if job:
            job_id = client.resolve_job(job)
            if not job_id:
                console.print(f"❌ No job starting with '{job}' in this workspace.")
                raise typer.Exit(1)
        else:
            jobs = client.list_jobs(target.id)
            if not jobs:
                console.print(f"No jobs for {target.id}.")
                return
            job_id = jobs[0]["id"]

        for kind, payload in follow_log(client, job_id, follow=follow):
            if kind == "log":
                sys.stdout.write(payload)
                sys.stdout.flush()
            elif kind == "question":
                what = "wants permission" if payload.get("kind") == "permission" else "is asking you"
                console.print(f"\n[yellow]? {what}:[/yellow] {payload.get('text', '')} "
                              "[dim](answer in NotesGraph)[/dim]")
            else:
                status = payload.get("status", "?")
                style = {"done": "green", "error": "red", "running": "yellow"}.get(status, "dim")
                console.print(f"\n[{style}]● {payload.get('agentName', 'job')} "
                              f"{job_id[:8]} — {status}[/]")
                if status == "error" and payload.get("error"):
                    console.print(f"  [dim]{payload['error'][:300]}[/dim]")
                if status == "running" and payload.get("tmuxSession") and not follow:
                    console.print(f"  [dim]still running: wf agent logs {job_id[:8]} -f, "
                                  f"or tmux attach -t {payload['tmuxSession']} on "
                                  f"{payload.get('deviceKey', 'its device')}[/dim]")
    except NotesGraphError as e:
        console.print(f"⚠️  {e}")
        if not local_copy(job or ""):
            raise typer.Exit(1)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped following; the job keeps running[/dim]")
