"""`wf release` -- release the repository it is run in (workflow/release.py)."""
import json
from typing import Optional

import typer

release_app = typer.Typer(help="Release the repository you are in, the way it is released",
                          invoke_without_command=True)

_REPO_PATH = "A folder in the repository to release (default: here)"


def _info(repo_path: str) -> dict:
    from workflow import release as rel
    try:
        return rel.find_release(repo_path)
    except rel.ReleaseError as e:
        typer.echo(f"❌ {e}", err=True)
        raise typer.Exit(2)


def _needs_config(info: dict) -> None:
    typer.echo(f"❌ {info['repo']} has no release yet. Set one:", err=True)
    typer.echo("   wf release config --command scripts/release.sh --watch deploy.yml --verify-url https://…\n"
               "   wf release add NAME --path CHECKOUT [--ssh NAME] [--python PY]   (code that runs from checkouts)",
               err=True)
    raise typer.Exit(2)


def _watch(info: dict, as_json: bool, follow: bool) -> int:
    """Print the newest run of the release's workflow; with `follow`, wait for it first."""
    from workflow import pr as prs
    from workflow import release as rel

    if not info["section"].get("watch"):
        typer.echo("❌ This release names no workflow to watch (wf release config --watch FILE).", err=True)
        return 2
    try:
        run = rel.latest_run(info)
        if not run:
            typer.echo(f"No runs of {info['section']['watch']} yet.")
            return 1
        if follow and (run["status"] or "").lower() != "completed":
            if not as_json:
                typer.echo(f"==> following run {run['id']} ({run['branch']} {(run['sha'] or '')[:8]}) {run['url']}")
            rel.watch_run(info, run["id"])
        detail = prs.ci_run(run["id"], rel.command_cwd(info))
    except prs.PrError as e:
        typer.echo(f"❌ {e}", err=True)
        return 2
    typer.echo(json.dumps(detail, indent=2) if as_json else prs.format_run(detail))
    done = (detail["status"] or "").lower() == "completed"
    return 0 if done and (detail["conclusion"] or "").lower() == "success" else 1 if done else 3


def _release_command(info: dict, plan: bool, yes: bool) -> int:
    from workflow import release as rel
    from workflow.deploy import cloud

    section = info["section"]
    problem = rel.sync(info)
    if problem:
        typer.echo(f"❌ {problem}", err=True)
        return 1
    # The command's own listing of what ships, before anything is asked.
    planned = rel.run_command(info, "plan")
    if plan or planned != 0:
        return planned
    if not yes and not typer.confirm(f"Release {info['repo']}?"):
        typer.echo("nothing released")
        return 1
    code = rel.run_command(info, "release")
    if code != 0:
        typer.echo(f"❌ release exited {code}", err=True)
        if section.get("watch"):
            _watch(info, as_json=False, follow=False)
        return code
    if section.get("verify_url"):
        ok, detail = cloud.verify(section["verify_url"])
        typer.echo(f"{'✅' if ok else '⚠️ '} {section['verify_url']} answered {detail}")
        return 0 if ok else 1
    return 0


def _release_machines(info: dict, plan: bool, yes: bool, wait: int, as_json: bool) -> int:
    from pathlib import Path

    from workflow import release as rel

    section, repo = info["section"], Path(info["repo"]).expanduser()
    try:
        planned = rel.plan(section, repo)
    except rel.ReleaseError as e:
        typer.echo(f"❌ {e}", err=True)
        return 2
    to_update = [row for row in planned["machines"] if row["action"] == "update"]
    blocked = [row for row in planned["machines"] if row["action"] not in ("update", "current")]
    if plan or not to_update:
        typer.echo(json.dumps(planned, indent=2) if as_json else rel.format_plan(planned))
        if not plan and not as_json:
            typer.echo("nothing released" if blocked else "nothing to release")
        return 1 if blocked else 0
    if not as_json:
        typer.echo(rel.format_plan(planned))
    if not yes and not typer.confirm(f"Release to {', '.join(row['name'] for row in to_update)}?"):
        typer.echo("nothing released")
        return 1
    result = rel.release(section, repo, planned=planned, wait=wait,
                         say=(lambda line: None) if as_json else typer.echo)
    typer.echo(json.dumps(result, indent=2) if as_json else rel.format_outcome(result))
    return 0 if result["ok"] else 1


@release_app.callback()
def release_cmd(
    ctx: typer.Context,
    plan: bool = typer.Option(False, "--plan", help="Only list what would ship"),
    yes: bool = typer.Option(False, "--yes", "-y", help="No confirmation (agents, scripts)"),
    watch: bool = typer.Option(False, "--watch", help="Don't release: follow the release run in progress "
                                                      "(or show the last one)"),
    wait: int = typer.Option(180, "--wait", help="Machines: seconds to wait for each runner to restart"),
    repo_path: str = typer.Option(".", "--repo-path", help=_REPO_PATH),
    as_json: bool = typer.Option(False, "--json", help="Print JSON (plans of machines, and --watch)"),
):
    """Release the repository you are in.

        wf release           list what ships, confirm, release, wait for it
        wf release --plan    only list
        wf release --yes     no confirmation
        wf release --watch   follow the release that is running

    What that does is the repository's own (`wf release config` shows it):
    its release command and deploy run, or, for code that runs from
    checkouts on machines, a fast-forward of each one and a wait for its
    agent runner to restart. Exits non-zero when the release didn't land.
    """
    if ctx.invoked_subcommand:
        return
    from workflow import release as rel

    info = _info(repo_path)
    which = rel.kind(info["section"])
    if not which:
        _needs_config(info)
    if which == "machines":
        # --watch: where every machine is, the nearest thing to watching.
        raise typer.Exit(_release_machines(info, plan or watch, yes, wait, as_json))
    if watch:
        raise typer.Exit(_watch(info, as_json, follow=True))
    raise typer.Exit(_release_command(info, plan, yes))


@release_app.command("status")
def release_status(
    repo_path: str = typer.Option(".", "--repo-path", help=_REPO_PATH),
    as_json: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Where the last release stands, without waiting: its run, or each machine's checkout."""
    from workflow import release as rel

    info = _info(repo_path)
    which = rel.kind(info["section"])
    if not which:
        _needs_config(info)
    if which == "machines":
        raise typer.Exit(_release_machines(info, True, False, 0, as_json))
    raise typer.Exit(_watch(info, as_json, follow=False))


@release_app.command("config")
def release_config(
    command: Optional[str] = typer.Option(None, "--command", "-c", help="The release command, run in the repository"),
    plan_args: Optional[str] = typer.Option(None, "--plan-args", help="Added to list what would ship (default --plan)"),
    release_args: Optional[str] = typer.Option(None, "--release-args",
                                               help="Added to release and wait (default '--yes --wait')"),
    checkout: Optional[str] = typer.Option(None, "--checkout", help="Run in this checkout, synced to --branch first"),
    branch: Optional[str] = typer.Option(None, "--branch", "-b", help="The branch that is released"),
    watch: Optional[str] = typer.Option(None, "--watch", help="The Actions workflow a release starts (name or file)"),
    verify_url: Optional[str] = typer.Option(None, "--verify-url", help="URL that must answer after a release"),
    repo_path: str = typer.Option(".", "--repo-path", help=_REPO_PATH),
):
    """Show how this repository is released, or set it."""
    from workflow import release as rel

    info = _info(repo_path)
    section = info["section"]
    changes = {"command": command, "plan_args": plan_args, "release_args": release_args, "checkout": checkout,
               "branch": branch, "watch": watch, "verify_url": verify_url}
    if any(v is not None for v in changes.values()):
        section.update({k: v for k, v in changes.items() if v is not None})
        rel.save_release(info, section)
        typer.echo(f"✅ Saved the release of {info['repo']}")
    typer.echo(f"{info['repo']} (project {info['project']})")
    if not section:
        typer.echo("  no release set")
        return
    for key, value in section.items():
        if key == "machines":
            for name, entry in value.items():
                typer.echo(f"  machine {name}: {(entry.get('ssh') + ':') if entry.get('ssh') else ''}{entry.get('path')}"
                           + (f" (python {entry['python']})" if entry.get("python") else ""))
        else:
            typer.echo(f"  {key}: {value}")


@release_app.command("add")
def release_add(
    name: str = typer.Argument(..., help="What to call the machine"),
    path: str = typer.Option(..., "--path", help="The repository's checkout on it"),
    ssh: Optional[str] = typer.Option(None, "--ssh", help="Its name in 'wf deploy ssh'; omit for this machine"),
    python: Optional[str] = typer.Option(None, "--python", help="The interpreter the code runs with there, to "
                                                                "install changed requirements with"),
    repo_path: str = typer.Option(".", "--repo-path", help=_REPO_PATH),
):
    """Add (or change) a machine this repository is released to."""
    from workflow import release as rel

    info = _info(repo_path)
    section = info["section"]
    section.setdefault("machines", {})[name] = {"path": path, **({"ssh": ssh} if ssh else {}),
                                                **({"python": python} if python else {})}
    rel.save_release(info, section)
    typer.echo(f"✅ {name} → {ssh + ':' if ssh else ''}{path}")


@release_app.command("remove")
def release_remove(
    name: str = typer.Argument(..., help="The machine to stop releasing to"),
    repo_path: str = typer.Option(".", "--repo-path", help=_REPO_PATH),
):
    """Stop releasing to a machine. Its checkout is left as it is."""
    from workflow import release as rel

    info = _info(repo_path)
    section = info["section"]
    if (section.get("machines") or {}).pop(name, None) is None:
        typer.echo(f"❌ No machine '{name}'", err=True)
        raise typer.Exit(1)
    rel.save_release(info, section)
    typer.echo(f"🗑  {name} is no longer released to")
