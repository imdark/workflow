"""`wf deploy` -- the deployment registry and the fleet it records.

Registration and inspection live here. The registry is the concept; where
it publishes to (local config, a NotesGraph device registry) is
configuration, not a different command.
"""

import re
from typing import List, Optional

import typer
from rich.console import Console

from workflow.deploy.registry import (
    Device,
    KIND_FOLDER,
    KIND_MACHINE,
    STATE_DEGRADED,
    STATE_OFFLINE,
    STATE_ONLINE,
    folder_id,
    get_registry,
    slugify,
)

console = Console()

deploy_app = typer.Typer(help="Register and manage deployment targets", no_args_is_help=True)

STATE_STYLE = {
    STATE_ONLINE: "green", STATE_DEGRADED: "yellow", STATE_OFFLINE: "red",
}

# user@host[:port]
_DESTINATION = re.compile(r"^(?:(?P<user>[^@/\s]+)@)?(?P<host>[^@:/\s]+)(?::(?P<port>\d+))?$")


def parse_destination(destination: str) -> dict:
    """Split `user@host:port` into parts. Raises ValueError if malformed."""
    match = _DESTINATION.match(destination.strip())
    if not match:
        raise ValueError(f"'{destination}' is not a user@host destination")
    parts = match.groupdict()
    return {
        "user": parts["user"] or "",
        "host": parts["host"],
        "port": int(parts["port"]) if parts["port"] else 22,
    }


@deploy_app.command("add")
def deploy_add(
    destination: str = typer.Argument(..., help="user@host[:port]"),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Target id (default: derived from host)"),
    recipe: str = typer.Option("generic", "--recipe", "-r", help="Deployment recipe"),
    repo: Optional[str] = typer.Option(None, "--repo", help="Checkout path on the target"),
    branch: str = typer.Option("main", "--branch", "-b"),
    channel: str = typer.Option("stable", "--channel", help="OTA channel"),
    agent: bool = typer.Option(False, "--agent", help="Allow agents to execute here"),
    label: Optional[List[str]] = typer.Option(None, "--label", "-l", help="key=value, repeatable"),
):
    """Register a machine as a deployment target."""
    try:
        parts = parse_destination(destination)
    except ValueError as e:
        console.print(f"❌ {e}")
        raise typer.Exit(1)

    device = Device(
        id=name or slugify(parts["host"]),
        name=name or parts["host"],
        kind=KIND_MACHINE,
        host=parts["host"], user=parts["user"], port=parts["port"],
        recipe=recipe, repo=repo, branch=branch, channel=channel,
        agent_target=agent, labels=_parse_labels(label),
    )

    registry = get_registry()
    registry.register(device)
    console.print(f"✅ Registered [bold]{device.id}[/bold] → {device.ssh_destination}:{device.port}")
    if agent:
        console.print("   🤖 agent execution allowed")
    _report_publication(registry)


@deploy_app.command("add-folder")
def deploy_add_folder(
    machine: str = typer.Argument(..., help="Id of an already-registered machine"),
    path: str = typer.Argument(..., help="Absolute path on that machine"),
    name: Optional[str] = typer.Option(None, "--name", "-n"),
    agent: bool = typer.Option(True, "--agent/--no-agent", help="Allow agents to execute here"),
    recipe: Optional[str] = typer.Option(None, "--recipe", "-r"),
):
    """Register a folder on a machine as a target in its own right.

    Lets an agent be pointed at one checkout without being given the host.
    """
    registry = get_registry()
    parent = registry.get(machine)
    if not parent:
        console.print(f"❌ No machine '{machine}' registered. Add it first with 'wf deploy add'.")
        raise typer.Exit(1)

    device = Device(
        id=name or folder_id(machine, path),
        name=name or path.rstrip("/").split("/")[-1] or path,
        kind=KIND_FOLDER,
        host=parent.host, user=parent.user, port=parent.port,
        parent=machine, path=path,
        recipe=recipe or parent.recipe, repo=path,
        branch=parent.branch, channel=parent.channel,
        agent_target=agent,
    )
    registry.register(device)
    console.print(f"✅ Registered folder [bold]{device.id}[/bold] → {parent.ssh_destination}:{path}")
    _report_publication(registry)


@deploy_app.command("list")
def deploy_list(
    kind: Optional[str] = typer.Option(None, "--kind", help="machine | folder"),
    agents_only: bool = typer.Option(False, "--agents", help="Only agent-execution targets"),
):
    """List registered targets."""
    registry = get_registry()
    devices = registry.list(kind=kind, agent_target=True if agents_only else None)
    if not devices:
        console.print("No targets registered.")
        console.print("💡 Register one with 'wf deploy add user@host'")
        return

    for device in devices:
        state = device.status.state
        dot = f"[{STATE_STYLE.get(state, 'dim')}]●[/]"
        where = f"{device.ssh_destination}:{device.path}" if device.kind == KIND_FOLDER \
            else f"{device.ssh_destination}:{device.port}"
        flags = " 🤖" if device.agent_target else ""
        console.print(f"  {dot} [bold]{device.id:<24}[/bold] {device.kind:<8} "
                      f"{where:<44} {device.recipe}{flags}")


@deploy_app.command("show")
def deploy_show(target: str = typer.Argument(..., help="Target id")):
    """Show one target in full, including its last health run."""
    device = get_registry().get(target)
    if not device:
        console.print(f"❌ No target '{target}'")
        raise typer.Exit(1)

    console.print(f"[bold]{device.id}[/bold] ({device.kind})")
    for label, value in (
        ("host", f"{device.ssh_destination}:{device.port}"),
        ("path", device.path),
        ("parent", device.parent),
        ("recipe", device.recipe),
        ("repo", device.repo),
        ("ref", device.target_ref),
        ("channel", device.channel),
        ("agent target", "yes" if device.agent_target else "no"),
        ("labels", ", ".join(f"{k}={v}" for k, v in (device.labels or {}).items()) or None),
    ):
        if value:
            console.print(f"  {label:<14} {value}")

    status = device.status
    style = STATE_STYLE.get(status.state, "dim")
    console.print(f"  {'state':<14} [{style}]{status.state}[/]"
                  + (f" — {status.detail}" if status.detail else ""))
    if status.version:
        console.print(f"  {'deployed':<14} {status.version}")
    for check in status.checks or []:
        mark = "✓" if check.get("ok") else "✗"
        console.print(f"     {mark} {check.get('name')} {check.get('detail', '')}")


@deploy_app.command("remove")
def deploy_remove(target: str = typer.Argument(..., help="Target id")):
    """Deregister a target. Removing a machine removes its folders too."""
    registry = get_registry()
    if not registry.get(target):
        console.print(f"❌ No target '{target}'")
        raise typer.Exit(1)
    registry.deregister(target)
    console.print(f"🗑  Deregistered {target}")


# ── NotesGraph publication ───────────────────────────────────────────────────

@deploy_app.command("notes-login")
def deploy_notes_login(
    token: str = typer.Argument(..., help="NotesGraph Personal Access Token"),
    url: str = typer.Option("https://app.notesgraph.com", "--url"),
):
    """Store a NotesGraph token and list the workspaces it can reach."""
    from workflow.deploy.registry.notesgraph import NotesGraphClient, NotesGraphError, save_token

    client = NotesGraphClient(url=url, token=token)
    try:
        session = client.session()
    except NotesGraphError as e:
        console.print(f"❌ {e}")
        raise typer.Exit(1)

    if not save_token(token):
        console.print("⚠️  Keychain unavailable — the token was not stored.")
        raise typer.Exit(1)

    user = session.get("user", {})
    console.print(f"✅ Signed in as {user.get('email', 'unknown')}")
    console.print("\nWorkspaces:")
    for workspace_id in session.get("workspaceIds", []):
        console.print(f"  {workspace_id}")
    console.print("\n💡 Publish the fleet there with:")
    console.print(r"   wf config set deploy.mirrors '\[notesgraph]'")
    console.print("   wf config set deploy.notesgraph.workspace <workspace id>")


@deploy_app.command("publish")
def deploy_publish():
    """Re-publish every local target to the configured mirrors.

    Useful after adding a mirror, or after one was unreachable during a
    registration.
    """
    from workflow.deploy.registry import MultiRegistry

    registry = get_registry()
    if not isinstance(registry, MultiRegistry) or not registry.mirrors:
        console.print("ℹ️  No registry mirrors configured; nothing to publish to.")
        console.print("💡 Add one with: wf config set deploy.mirrors '[notesgraph]'")
        return

    devices = registry.list()

    # Publish to each mirror directly rather than through the fan-out: the
    # fan-out deliberately swallows mirror failures so they cannot break a
    # deploy, which means it cannot tell us what actually landed. Reporting
    # "published" when every write failed would be a lie.
    for mirror in registry.mirrors:
        published, failure = 0, None
        for device in devices:
            try:
                mirror.register(device)
                if device.status.checked_at:
                    mirror.record_status(device.id, device.status)
                published += 1
            except Exception as e:
                failure = failure or e
        if published == len(devices):
            console.print(f"📡 Published {published} target(s) to {mirror.name}")
        elif published:
            console.print(f"⚠️  {mirror.name}: published {published}/{len(devices)}; "
                          f"first failure: {failure}")
        else:
            console.print(f"❌ {mirror.name}: nothing published — {failure}")


# ── helpers ──────────────────────────────────────────────────────────────────

def _parse_labels(pairs) -> dict:
    labels = {}
    for pair in pairs or []:
        key, _, value = str(pair).partition("=")
        if key.strip():
            labels[key.strip()] = value.strip()
    return labels


def _report_publication(registry) -> None:
    from workflow.deploy.registry import MultiRegistry
    if isinstance(registry, MultiRegistry) and registry.mirrors:
        console.print(f"   📡 published to {', '.join(m.name for m in registry.mirrors)}")


# ── acting on targets ────────────────────────────────────────────────────────

def _resolve(registry, target: str):
    device = registry.get(target)
    if not device:
        console.print(f"❌ No target '{target}'")
        matches = [d.id for d in registry.list() if d.id.startswith(target)]
        if matches:
            console.print(f"   did you mean: {', '.join(matches[:5])}")
        raise typer.Exit(1)
    return device


def _targets_for(registry, target: Optional[str], all_targets: bool) -> list:
    if all_targets:
        devices = registry.list()
        if not devices:
            console.print("No targets registered.")
            raise typer.Exit(1)
        return devices
    if not target:
        console.print("❌ Name a target, or pass --all")
        raise typer.Exit(1)
    return [_resolve(registry, target)]


def _print_results(results) -> None:
    for result in results:
        if result.skipped:
            console.print(f"   [dim]— {result.step.name} (skipped)[/dim]")
        elif result.ok:
            console.print(f"   [green]✓[/green] {result.step.name} "
                          f"[dim]{result.duration_ms}ms[/dim]")
        else:
            style = "yellow" if result.step.optional else "red"
            mark = "!" if result.step.optional else "✗"
            console.print(f"   [{style}]{mark}[/] {result.step.name}")
            for line in (result.output or "").splitlines()[:6]:
                console.print(f"       [dim]{line}[/dim]")


@deploy_app.command("setup")
def deploy_setup(
    target: str = typer.Argument(..., help="Target id"),
    key: Optional[str] = typer.Option(None, "--key", help="Public key to install"),
):
    """Install this machine's SSH key on a target and verify access."""
    from workflow.deploy.ssh import SshError, install_key, runner_for

    registry = get_registry()
    device = _resolve(registry, target)

    console.print(f"🔑 Installing key on {device.ssh_destination} "
                  "(you may be asked for the password once)")
    try:
        result = install_key(device, key)
    except SshError as e:
        console.print(f"❌ {e}")
        raise typer.Exit(1)

    if not result.ok:
        console.print(f"❌ Key install failed: {result.output or result.exit_code}")
        raise typer.Exit(1)

    runner = runner_for(device)
    if runner.reachable().ok:
        console.print("✅ Key-based SSH works")
    else:
        console.print("⚠️  Key installed, but a key-only connection still fails.")
        raise typer.Exit(1)
    runner.close()


def _agent_prereqs_ok(devices) -> bool:
    """Check agent targets have what jobs need (tmux); print fixes if not."""
    from workflow.deploy.prereqs import check_tmux, tmux_missing_message
    from workflow.deploy.ssh import runner_for

    ok = True
    for device in devices:
        if not device.agent_target:
            continue
        runner = runner_for(device)
        try:
            check = check_tmux(runner)
        finally:
            runner.close()
        if not check.installed:
            ok = False
            for line in tmux_missing_message(check, device.id):
                console.print(line)
    return ok


@deploy_app.command("run")
def deploy_run(
    target: Optional[str] = typer.Argument(None, help="Target id"),
    all_targets: bool = typer.Option(False, "--all", help="Every registered target"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    plan: bool = typer.Option(False, "--plan", help="Show the steps without running them"),
):
    """Run a target's deploy recipe: bring it to its wanted revision."""
    from workflow.deploy import runner as deploy_runner
    from workflow.deploy.recipes import load_recipe
    from workflow.deploy.ssh import runner_for

    registry = get_registry()
    devices = _targets_for(registry, target, all_targets)

    # Before touching any target: an agent device that can't run jobs would
    # only be found out at its first job. Checked for every target up front,
    # so `--all` never stops halfway with some machines already updated.
    if not _agent_prereqs_ok(devices) and not plan:
        console.print("\n🛑 Deployment stopped; nothing was changed. "
                      "Install the above, then run this again.")
        raise typer.Exit(1)

    for device in devices:
        try:
            recipe = load_recipe(device.recipe)
        except ValueError as e:
            console.print(f"❌ {device.id}: {e}")
            raise typer.Exit(1)

        context = recipe.context(device)
        console.print(f"\n[bold]{device.id}[/bold] → {device.ssh_destination} "
                      f"({recipe.name} @ {device.target_ref})")
        for step in recipe.steps:
            console.print(f"   [dim]{step.name}: {step.render(context)}[/dim]")

        if plan:
            continue
        if not yes and not typer.confirm(f"Run {len(recipe.steps)} step(s) on {device.id}?",
                                         default=True):
            console.print("   skipped")
            continue

        ssh = runner_for(device)
        try:
            report = deploy_runner.deploy(device, recipe=recipe, runner=ssh)
            _print_results(report.results)

            status = deploy_runner.check(device, recipe=recipe, runner=ssh)
            registry.record_status(device.id, status)

            if report.ok:
                console.print(f"   ✅ deployed {report.version or ''} "
                              f"[{STATE_STYLE.get(status.state, 'dim')}]{status.state}[/]")
            else:
                console.print(f"   ❌ {len(report.failed)} step(s) failed")
        finally:
            ssh.close()

    if plan:
        console.print("\n[dim]--plan: nothing was run[/dim]")


@deploy_app.command("status")
def deploy_status(
    target: Optional[str] = typer.Argument(None, help="Target id"),
    all_targets: bool = typer.Option(False, "--all", help="Check every target"),
):
    """Run health probes against targets and record the result."""
    from workflow.deploy import runner as deploy_runner
    from workflow.deploy.recipes import load_recipe
    from workflow.deploy.ssh import runner_for

    registry = get_registry()
    devices = _targets_for(registry, target, all_targets or not target)

    for device in devices:
        ssh = runner_for(device)
        try:
            status = deploy_runner.check(device, recipe=load_recipe(device.recipe), runner=ssh)
        except ValueError as e:
            console.print(f"  [red]●[/red] {device.id:<24} {e}")
            continue
        finally:
            ssh.close()

        registry.record_status(device.id, status)
        dot = f"[{STATE_STYLE.get(status.state, 'dim')}]●[/]"
        detail = f" — {status.detail}" if status.detail else ""
        console.print(f"  {dot} [bold]{device.id:<24}[/bold] {status.state:<9} "
                      f"{status.version or '':<12}{detail}")
        for check in status.checks:
            if not check.get("ok"):
                console.print(f"       [yellow]![/yellow] {check['name']}: {check.get('detail', '')}")


@deploy_app.command("update")
def deploy_update(
    target: Optional[str] = typer.Argument(None, help="Target id"),
    all_targets: bool = typer.Option(False, "--all", help="Every target"),
    channel: Optional[str] = typer.Option(None, "--channel", help="Only targets on this channel"),
    pin: Optional[str] = typer.Option(None, "--pin", help="Pin to an exact ref before deploying"),
    unpin: bool = typer.Option(False, "--unpin", help="Clear the pin and follow the branch"),
    yes: bool = typer.Option(False, "--yes", "-y"),
):
    """Push an over-the-air update: set the wanted revision, then deploy.

    Pull-based updating (an agent on the rig polling its channel) reads the
    same per-target `channel` and `pin`, so nothing here has to change when
    that arrives.
    """
    registry = get_registry()
    devices = _targets_for(registry, target, all_targets)
    if channel:
        devices = [d for d in devices if d.channel == channel]
        if not devices:
            console.print(f"No targets on channel '{channel}'")
            raise typer.Exit(1)

    for device in devices:
        if unpin and device.pin:
            device.pin = None
            registry.register(device)
            console.print(f"📌 {device.id}: unpinned, following {device.branch}")
        elif pin and device.pin != pin:
            device.pin = pin
            registry.register(device)
            console.print(f"📌 {device.id}: pinned to {pin}")

    deploy_run(target=None if len(devices) > 1 else devices[0].id,
               all_targets=len(devices) > 1, yes=yes, plan=False)


@deploy_app.command("restart")
def deploy_restart(
    target: str = typer.Argument(..., help="Target id"),
    yes: bool = typer.Option(False, "--yes", "-y"),
):
    """Run a target's restart steps.

    Separate from `run` on purpose: a deploy must never take a live rig down
    as a side effect, so bouncing it is always an explicit act.
    """
    from workflow.deploy import runner as deploy_runner
    from workflow.deploy.recipes import load_recipe
    from workflow.deploy.ssh import runner_for

    registry = get_registry()
    device = _resolve(registry, target)
    recipe = load_recipe(device.recipe)

    if not recipe.restart:
        console.print(f"ℹ️  Recipe '{recipe.name}' defines no restart steps.")
        console.print(f"💡 Add them under deploy.recipes.{recipe.name}.restart")
        return

    context = recipe.context(device)
    console.print(f"[bold]{device.id}[/bold] restart steps:")
    for step in recipe.restart:
        console.print(f"   [dim]{step.name}: {step.render(context)}[/dim]")

    if not yes and not typer.confirm(
        f"This will interrupt anything running on {device.id}. Continue?", default=False
    ):
        console.print("Aborted.")
        raise typer.Exit(1)

    ssh = runner_for(device)
    try:
        report = deploy_runner.restart(device, recipe=recipe, runner=ssh)
        _print_results(report.results)
        console.print("   ✅ restarted" if report.ok else "   ❌ restart failed")
    finally:
        ssh.close()


@deploy_app.command("exec")
def deploy_exec(
    target: str = typer.Argument(..., help="Target id"),
    command: List[str] = typer.Argument(..., help="Command to run"),
):
    """Run one command on a target, in its repo directory when it has one."""
    from workflow.deploy.ssh import runner_for

    device = _resolve(get_registry(), target)
    ssh = runner_for(device)
    try:
        result = ssh.run(" ".join(command), cwd=device.repo or device.path)
    finally:
        ssh.close()

    if result.stdout:
        console.print(result.stdout.rstrip())
    if result.stderr:
        console.print(f"[dim]{result.stderr.rstrip()}[/dim]")
    raise typer.Exit(result.exit_code)


def _ssh_project(project: Optional[str]) -> Optional[str]:
    from workflow.deploy import ssh_config
    from workflow.projects import get_project

    if project and not get_project(project):
        console.print(f"❌ No project named '{project}'")
        raise typer.Exit(1)
    return project or ssh_config.current_project()


def _ssh_section(project: Optional[str], is_global: bool) -> tuple:
    """(config, its `ssh` section, label) to edit: the project's, or the global one."""
    from workflow.config import load_config

    cfg = load_config()
    if is_global or not project:
        return cfg, cfg.setdefault("ssh", {}), "global"
    return cfg, cfg["projects"][project].setdefault("ssh", {}), f"project {project}"


@deploy_app.command("ssh", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def deploy_ssh(
    ctx: typer.Context,
    dry_run: bool = typer.Option(False, "--dry-run", help="Print the ssh command instead of running it"),
    project: Optional[str] = typer.Option(None, "--project", help="Use this project's defaults"),
):
    """Open ssh to a machine by name, with the project's key, user and port.

    Usage: wf deploy ssh NAME, then any ssh options, then a command to run.

    The key, user, port and options come from the project's `ssh` settings
    (see `wf deploy ssh-config`); everything after the name goes to ssh as
    typed. Without a name, the project's default machine.

        wf deploy ssh prod
        wf deploy ssh prod uptime
        wf deploy ssh prod -L 8080:localhost:8080 -N
        wf deploy ssh prod -- tail -f /var/log/syslog
    """
    import os

    from workflow.deploy import ssh_config

    project = _ssh_project(project)
    registry = get_registry()
    args = list(ctx.args)
    target = ssh_config.resolve(args[0], project, registry=registry) if args and not args[0].startswith("-") else None
    if target:
        args = args[1:]
    else:
        # Not a machine's name: the default machine, and all of it is for ssh.
        default = ssh_config.default_name(project)
        target = ssh_config.resolve(default, project, registry=registry) if default else None
        if not target:
            known = ssh_config.host_names(project) + [d.id for d in registry.list()]
            what = f"'{args[0]}' is not a machine" if args and not args[0].startswith("-") else "No machine named"
            console.print(f"❌ {what}, and {'project ' + project if project else 'the config'} "
                          f"has no default one{' that resolves' if default else ''}.")
            if known:
                console.print(f"   known: {', '.join(known)}")
            console.print("💡 Add one with 'wf deploy ssh-host NAME user@host --key PATH --default'")
            raise typer.Exit(1)

    options, command = ssh_config.split_args(args)
    argv = ssh_config.ssh_argv(target, options, command)
    if dry_run:
        import shlex
        typer.echo(" ".join(shlex.quote(a) for a in argv))
        for setting, source in target.sources.items():
            typer.echo(f"  {setting}: {source}")
        return
    try:
        # Replace this process: ssh gets the terminal, the signals and the exit code.
        os.execvp(argv[0], argv)
    except FileNotFoundError:
        console.print("❌ ssh is not on PATH")
        raise typer.Exit(127)


@deploy_app.command("ssh-host")
def deploy_ssh_host(
    name: str = typer.Argument(..., help="What to call the machine"),
    destination: Optional[str] = typer.Argument(None, help="[user@]host[:port]"),
    key: Optional[str] = typer.Option(None, "--key", "-i", help="Private key file for this machine"),
    option: Optional[List[str]] = typer.Option(None, "--option", "-o", help="ssh -o option, repeatable"),
    default: bool = typer.Option(False, "--default", help="Make it the machine used when none is named"),
    remove: bool = typer.Option(False, "--remove", help="Forget this machine"),
    project: Optional[str] = typer.Option(None, "--project", help="Project to save it in (default: this one)"),
    is_global: bool = typer.Option(False, "--global", help="Save it for every project"),
):
    """Name a machine for `wf deploy ssh`, in the project's settings.

    Kept in the wf config only; unlike `wf deploy add` it isn't published
    to the deployment registry.
    """
    from workflow.config import save_config

    cfg, section, label = _ssh_section(_ssh_project(project), is_global)
    hosts = section.setdefault("hosts", {})
    if remove:
        if hosts.pop(name, None) is None:
            console.print(f"❌ No machine '{name}' in {label}")
            raise typer.Exit(1)
        if section.get("default") == name:
            section.pop("default")
        save_config(cfg)
        console.print(f"🗑  Forgot {name} ({label})")
        return

    entry = dict(hosts.get(name) or {}) if isinstance(hosts.get(name), dict) else {}
    if destination:
        try:
            parts = parse_destination(destination)
        except ValueError as e:
            console.print(f"❌ {e}")
            raise typer.Exit(1)
        entry["host"] = parts["host"]
        if parts["user"]:
            entry["user"] = parts["user"]
        if parts["port"] != 22:
            entry["port"] = parts["port"]
    if not entry.get("host"):
        console.print(f"❌ '{name}' is new: give its [user@]host")
        raise typer.Exit(1)
    if key:
        entry["key"] = key
    if option:
        entry["options"] = list(option)
    hosts[name] = entry
    if default:
        section["default"] = name
    save_config(cfg)
    console.print(f"✅ {name} → {entry.get('user', '') + '@' if entry.get('user') else ''}{entry['host']} ({label})"
                  + (", default" if section.get("default") == name else ""))


@deploy_app.command("ssh-config")
def deploy_ssh_config(
    user: Optional[str] = typer.Option(None, "--user", "-l", help="Default user"),
    key: Optional[str] = typer.Option(None, "--key", "-i", help="Default private key file"),
    port: Optional[int] = typer.Option(None, "--port", "-p", help="Default port"),
    option: Optional[List[str]] = typer.Option(None, "--option", "-o", help="Default ssh -o option, repeatable"),
    default: Optional[str] = typer.Option(None, "--default", help="Machine used when none is named"),
    project: Optional[str] = typer.Option(None, "--project", help="Project to read or change (default: this one)"),
    is_global: bool = typer.Option(False, "--global", help="Read or change the defaults for every project"),
):
    """Show how `wf deploy ssh` reaches each machine, or set the defaults.

    With no option, lists the machines and where each setting comes from.
    """
    from workflow.config import save_config
    from workflow.deploy import ssh_config

    project = _ssh_project(project)
    changes = {"user": user, "key": key, "port": port, "options": list(option) if option else None,
               "default": default}
    if any(v is not None for v in changes.values()):
        cfg, section, label = _ssh_section(project, is_global)
        section.update({k: v for k, v in changes.items() if v is not None})
        save_config(cfg)
        console.print(f"✅ Saved ssh defaults ({label}): "
                      + ", ".join(f"{k}={v}" for k, v in changes.items() if v is not None))
        return

    project = None if is_global else project
    registry = get_registry()
    names = ssh_config.host_names(project) + [d.id for d in registry.list()
                                              if d.id not in ssh_config.host_names(project)]
    console.print(f"ssh settings for {'project ' + project if project else 'every project'}"
                  f" (default machine: {ssh_config.default_name(project) or 'none'})")
    if not names:
        console.print("  no machines. Add one with 'wf deploy ssh-host NAME user@host --key PATH'")
    for name in names:
        target = ssh_config.resolve(name, project, registry=registry)
        if not target:
            continue
        console.print(f"  [bold]{name}[/bold]  {target.destination}"
                      + (f":{target.port}" if target.port != 22 else "")
                      + (f"  key {target.key}" if target.key else "")
                      + (f"  -o {' -o '.join(target.options)}" if target.options else ""))
        for setting, source in target.sources.items():
            console.print(f"      [dim]{setting} from {source}[/dim]")


@deploy_app.command("tunnel")
def deploy_tunnel(
    target: str = typer.Argument(..., help="Target id"),
    port: int = typer.Option(8099, "--port", help="Port to open on the target"),
):
    """Hold open a reverse tunnel so the target can reach this machine.

    Gives a field robot one route home: the capture proxy. An agent there
    runs against your account and your conversation store without any
    credential being copied onto the machine.
    """
    from workflow.deploy import ssh_config
    from workflow.deploy.tunnel import export_line, reverse_tunnel
    from workflow.proxy import daemon

    registry = get_registry()
    # A machine named with `wf deploy ssh-host` works here too.
    named = None if registry.get(target) else ssh_config.resolve(target, ssh_config.current_project())
    device = Device(id=named.name, name=named.name, host=named.host, user=named.user, port=named.port) \
        if named else _resolve(registry, target)

    if not daemon.status().get("running"):
        console.print("⚠️  The capture proxy is not running — the tunnel would lead nowhere.")
        console.print("💡 Start it first with 'wf proxy start'")
        raise typer.Exit(1)

    console.print(f"🔌 Tunnelling {device.ssh_destination}:{port} → this machine")
    console.print(f"\n   On the target, run an agent with:\n   {export_line(port)}\n")
    console.print("[dim]Ctrl-C to close the tunnel.[/dim]")

    try:
        with reverse_tunnel(device, remote_port=port) as process:
            process.wait()
    except RuntimeError as e:
        console.print(f"❌ {e}")
        raise typer.Exit(1)
    except KeyboardInterrupt:
        console.print("\n🔌 Tunnel closed")


@deploy_app.command("recipes")
def deploy_recipes():
    """List available deployment recipes and their steps."""
    from workflow.deploy.recipes import available_recipes, load_recipe

    for name in available_recipes():
        recipe = load_recipe(name)
        console.print(f"\n[bold]{name}[/bold]")
        for label, steps in (("deploy", recipe.steps), ("health", recipe.health),
                             ("restart", recipe.restart)):
            if steps:
                console.print(f"  {label}:")
                for step in steps:
                    flag = " [dim](optional)[/dim]" if step.optional else ""
                    console.print(f"    {step.name}: [dim]{step.run}[/dim]{flag}")
            elif label == "restart":
                console.print("  restart: [dim]none — a deploy never bounces this target[/dim]")


# ── Cloud deploys (per project) ──────────────────────────────────────────────

@deploy_app.command("cloud-config")
def deploy_cloud_config(
    command: Optional[str] = typer.Option(None, "--command", "-c",
                                          help="Deploy command, run in the checkout"),
    repo: Optional[str] = typer.Option(None, "--repo",
                                       help="Checkout to deploy from (default: project default repo)"),
    branch: Optional[str] = typer.Option(None, "--branch", "-b", help="Branch that gets deployed"),
    verify_url: Optional[str] = typer.Option(None, "--verify-url",
                                             help="URL that must answer after a deploy"),
):
    """Show or set the current project's cloud deploy."""
    from workflow.deploy.cloud import CONFIG_KEY
    from workflow.projects import get_current_project, get_default_repo, get_project, update_project

    project = get_current_project()
    if not project:
        console.print("❌ No current project. Use 'wf project change <name>'.")
        raise typer.Exit(1)

    section = dict((get_project(project) or {}).get(CONFIG_KEY) or {})
    changes = {"command": command, "repo": repo, "branch": branch, "verify_url": verify_url}
    section.update({k: v for k, v in changes.items() if v is not None})
    if any(v is not None for v in changes.values()):
        section.setdefault("repo", get_default_repo(project))
        section.setdefault("branch", "main")
        update_project(project, {CONFIG_KEY: section})
        console.print(f"✅ Cloud deploy for '{project}' saved")

    if not section:
        console.print(f"No cloud deploy for '{project}'. Set one with "
                      "'wf deploy cloud-config --command <cmd> --verify-url <url>'.")
        return
    for key in ("repo", "branch", "command", "verify_url"):
        console.print(f"   {key}: {section.get(key) or '[dim]-[/dim]'}")


@deploy_app.command("cloud")
def deploy_cloud(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip confirmation"),
    plan: bool = typer.Option(False, "--plan", help="Show what would ship without deploying"),
):
    """Deploy the project's merged code to its cloud service.

    Tasks whose branches have merged move to Merged; a successful deploy
    moves them to Deployed, and a passing verify URL to Done.
    """
    from workflow import ship
    from workflow.backends import get_backend
    from workflow.config import load_effective_config
    from workflow.deploy import cloud
    from workflow.projects import get_current_project_config
    from workflow.session import task_branch_name

    target = cloud.target_from(get_current_project_config())
    if not target:
        console.print("❌ This project has no cloud deploy. Set one with "
                      "'wf deploy cloud-config --command <cmd> --verify-url <url>'.")
        raise typer.Exit(1)

    backend = get_backend(load_effective_config())
    for task in ship.promote_merged(
            backend, _project_tasks(backend),
            lambda t: ship.is_merged(target.repo, task_branch_name(t), target.branch)):
        console.print(f"   🔀 {task.key} merged → {ship.MERGED}")
    shipping = [t for t in _project_tasks(backend) if t.status == ship.MERGED]

    console.print(f"\n[bold]{target.repo}[/bold] @ origin/{target.branch} → [dim]{target.command}[/dim]")
    if shipping:
        console.print("   ships: " + ", ".join(t.key for t in shipping))
    else:
        console.print("   [dim]no Merged tasks waiting on a deploy[/dim]")
    if plan:
        console.print("\n[dim]--plan: nothing was deployed[/dim]")
        return

    problem = cloud.sync_checkout(target)
    if problem:
        console.print(f"❌ {problem}")
        raise typer.Exit(1)
    revision = cloud.head_revision(target)

    if not yes and not typer.confirm(f"Deploy {revision} to the cloud?", default=True):
        console.print("   skipped")
        return

    code = cloud.run_deploy(target)
    if code != 0:
        console.print(f"❌ Deploy exited {code}; no task statuses changed")
        raise typer.Exit(code)
    console.print(f"✅ Deployed {revision}")
    for task in ship.promote(backend, shipping, ship.MERGED, ship.DEPLOYED):
        console.print(f"   🚀 {task.key} → {ship.DEPLOYED}")

    if not target.verify_url:
        console.print("   [dim]no verify URL; tasks stay Deployed until checked[/dim]")
        return
    ok, detail = cloud.verify(target.verify_url)
    if not ok:
        console.print(f"⚠️  {target.verify_url} answered {detail}; tasks stay {ship.DEPLOYED}")
        raise typer.Exit(1)
    console.print(f"   ✅ {target.verify_url} answered {detail}")
    shipped = {t.key for t in shipping}
    deployed = [t for t in _project_tasks(backend) if t.key in shipped]
    for task in ship.promote(backend, deployed, ship.DEPLOYED, ship.DONE):
        console.print(f"   ✔ {task.key} → {ship.DONE}")


def _project_tasks(backend) -> list:
    """The current project's tasks, when the backend can hold shipping stages.

    Only the markdown backend stores arbitrary statuses; Jira and Linear
    tasks keep moving through their own workflows.
    """
    if backend is None or not hasattr(backend, "transition_task"):
        return []
    return [t for t in backend.list_tasks() if hasattr(t, "status")]
