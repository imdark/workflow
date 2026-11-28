"""`wf backup` -- back up the production data of the repository it is run in (workflow/backup.py)."""
import json
from typing import Optional

import typer

backup_app = typer.Typer(help="Back up the production data of the repository you are in",
                         invoke_without_command=True)

_REPO_PATH = "A folder in the repository whose data to back up (default: here)"


def _info(repo_path: str, need: bool = True) -> dict:
    from workflow import backup as bk
    try:
        info = bk.find_backup(repo_path)
    except bk.ReleaseError as e:
        typer.echo(f"❌ {e}", err=True)
        raise typer.Exit(2)
    if need and not bk.configured(info["section"]):
        typer.echo(f"❌ {info['repo']} has no backup set. Set one:", err=True)
        typer.echo("   wf backup config --ssh NAME --dir /path/to/backups --command 'pg_dump …' --extension sql",
                   err=True)
        raise typer.Exit(2)
    return info


def _fail(error: Exception) -> None:
    typer.echo(f"❌ {error}", err=True)
    raise typer.Exit(1)


@backup_app.callback()
def backup_cmd(
    ctx: typer.Context,
    label: Optional[str] = typer.Option(None, "--label", "-l", help="A word for the file's name, e.g. pre-478"),
    repo_path: str = typer.Option(".", "--repo-path", help=_REPO_PATH),
    as_json: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """Take a backup now, verify it, and keep the newest few.

        wf backup                    take one
        wf backup --label pre-478    ...named for why
        wf backup list               what is there
        wf backup fetch              copy the newest off its machine

    A backup that fails any check is removed and the command exits 1, so
    "wf backup && wf release" never releases without one.
    """
    if ctx.invoked_subcommand:
        return
    from workflow import backup as bk

    info = _info(repo_path)
    section = info["section"]
    if not as_json:
        typer.echo(f"==> backing up {info['repo']} on {section.get('ssh') or 'this machine'}")
    try:
        result = bk.take(section, label)
        removed = bk.prune(section)
    except bk.ReleaseError as e:
        _fail(e)
    typer.echo(json.dumps({**result, "removed": removed}, indent=2) if as_json else bk.format_taken(result, removed))


@backup_app.command("list")
def backup_list(
    repo_path: str = typer.Option(".", "--repo-path", help=_REPO_PATH),
    as_json: bool = typer.Option(False, "--json", help="Print JSON"),
):
    """The backups in the repository's backup folder, newest first (✓ = taken by wf)."""
    from workflow import backup as bk

    info = _info(repo_path)
    try:
        rows = bk.list_backups(info["section"])
    except bk.ReleaseError as e:
        _fail(e)
    typer.echo(json.dumps(rows, indent=2) if as_json else bk.format_list(rows))


@backup_app.command("fetch")
def backup_fetch(
    name: Optional[str] = typer.Argument(None, help="The backup to copy (default: wf's newest)"),
    to: str = typer.Option(..., "--to", help="Folder on this machine to copy it into"),
    repo_path: str = typer.Option(".", "--repo-path", help=_REPO_PATH),
):
    """Copy a backup off its machine, so it survives the machine."""
    from workflow import backup as bk

    info = _info(repo_path)
    try:
        result = bk.fetch(info["section"], name, to)
    except (bk.ReleaseError, OSError) as e:
        _fail(e)
    typer.echo(f"✓ {result['file']} ({bk.size(result['bytes'])}), readable by you only")


@backup_app.command("config")
def backup_config(
    command: Optional[str] = typer.Option(None, "--command", "-c", help="Writes the data to stdout, e.g. a pg_dump"),
    directory: Optional[str] = typer.Option(None, "--dir", help="Folder on the machine that holds the backups"),
    ssh: Optional[str] = typer.Option(None, "--ssh", help="The machine's name in 'wf deploy ssh'; omit for this one"),
    extension: Optional[str] = typer.Option(None, "--extension", help="The dump's file extension, e.g. sql"),
    marker: Optional[str] = typer.Option(None, "--complete-marker", help="Text the end of a whole dump contains"),
    keep: Optional[int] = typer.Option(None, "--keep", help="How many of wf's backups to keep (default 10)"),
    min_free_gb: Optional[float] = typer.Option(None, "--min-free-gb",
                                                help="Don't start with less free disk than this (default 5)"),
    repo_path: str = typer.Option(".", "--repo-path", help=_REPO_PATH),
):
    """Show how this repository's data is backed up, or set it."""
    from workflow import backup as bk

    info = _info(repo_path, need=False)
    section = info["section"]
    changes = {"command": command, "dir": directory, "ssh": ssh, "extension": extension,
               "complete_marker": marker, "keep": keep, "min_free_gb": min_free_gb}
    if any(v is not None for v in changes.values()):
        section.update({k: v for k, v in changes.items() if v is not None})
        bk.save_backup(info, section)
        typer.echo(f"✅ Saved the backup of {info['repo']}")
    typer.echo(f"{info['repo']} (project {info['project']})")
    if not section:
        typer.echo("  no backup set")
    for key, value in section.items():
        typer.echo(f"  {key}: {value}")
