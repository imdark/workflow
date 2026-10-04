"""`wf desktop` -- the apps and window layout `wf cd` sets up per project or repo.

See workflow/desktop.py for the configuration this edits.
"""

from typing import List, Optional, Tuple

import typer
from rich.console import Console

from workflow import desktop

console = Console()

desktop_app = typer.Typer(help="Apps and window layouts `wf cd` sets up per project or repo",
                          no_args_is_help=True)

ProjectOption = typer.Option(None, "--project", "-p", help="Project (default: current)")
RepoOption = typer.Option(None, "--repo", "-r", help="Repository name, as `wf cd` takes it")


def _target(project: Optional[str], repo: Optional[str]) -> Tuple[str, Optional[str]]:
    """The (project, repo key) a command acts on."""
    if repo:
        found = desktop.find_repo(repo)
        if not found:
            console.print(f"❌ No repository found matching '{repo}'")
            raise typer.Exit(1)
        if project and found[0] != project:
            console.print(f"❌ '{repo}' belongs to project '{found[0]}', not '{project}'")
            raise typer.Exit(1)
        return found

    from workflow.projects import get_current_project
    project = project or get_current_project()
    if not project:
        console.print("❌ No project specified and no current project set.")
        raise typer.Exit(1)
    return project, None


def _label(project: str, repo_key: Optional[str]) -> str:
    return f"{project} / {repo_key}" if repo_key else project


def _describe(entry: dict) -> str:
    parts = [entry["app"]]
    if entry.get("open"):
        targets = entry["open"] if isinstance(entry["open"], list) else [entry["open"]]
        parts.append("opens " + ", ".join(targets))
    if entry.get("bounds"):
        parts.append("at " + ",".join(str(v) for v in entry["bounds"]))
    elif entry.get("position"):
        where = entry["position"]
        if entry.get("screen"):
            where += f" of screen {entry['screen']}"
        parts.append(where)
    if entry.get("background"):
        parts.append("in background")
    return " — ".join(parts)


@desktop_app.command("show")
def desktop_show(project: Optional[str] = ProjectOption, repo: Optional[str] = RepoOption):
    """Show the setup `wf cd` applies, a repo's laid over its project's."""
    project, repo_key = _target(project, repo)
    setup = desktop.resolve(project, repo_key)
    console.print(f"🖥  Desktop for {_label(project, repo_key)}:")
    if not setup["apps"] and not setup.get("focus"):
        console.print("  [dim]nothing configured[/dim]")
        console.print("💡 Add an app with: wf desktop add <app> [--repo <name>] --position left")
        return
    for entry in setup["apps"]:
        console.print(f"  • {_describe(entry)}")
    if setup.get("focus"):
        console.print(f"  focus: {setup['focus']}")


@desktop_app.command("add")
def desktop_add(
    app: str = typer.Argument(..., help="Application name, as `open -a` takes it"),
    project: Optional[str] = ProjectOption,
    repo: Optional[str] = RepoOption,
    open_: Optional[List[str]] = typer.Option(
        None, "--open", "-o",
        help="Path or URL for the app to open; {repo_path}, {repo_name} and {project} are filled in",
    ),
    position: Optional[str] = typer.Option(
        None, "--position", help=f"Named slot: {', '.join(desktop.POSITIONS)}",
    ),
    bounds: Optional[str] = typer.Option(None, "--bounds", help="Absolute x,y,width,height"),
    screen: Optional[int] = typer.Option(None, "--screen", help="Screen for --position (1 = main)"),
    background: bool = typer.Option(False, "--background", "-g", help="Launch without bringing it forward"),
):
    """Add an app to a project's or repo's setup, replacing one of that name."""
    project, repo_key = _target(project, repo)
    entry = {"app": app}
    if open_:
        entry["open"] = open_[0] if len(open_) == 1 else list(open_)
    if bounds:
        try:
            entry["bounds"] = [int(v) for v in bounds.split(",")]
        except ValueError:
            entry["bounds"] = []
        if len(entry["bounds"]) != 4:
            console.print("❌ --bounds takes x,y,width,height, e.g. 0,25,1280,800")
            raise typer.Exit(1)
    elif position:
        if position not in desktop.POSITIONS:
            console.print(f"❌ Unknown position '{position}'. One of: {', '.join(desktop.POSITIONS)}")
            raise typer.Exit(1)
        entry["position"] = position
        if screen:
            entry["screen"] = screen
    if background:
        entry["background"] = True

    try:
        desktop.set_entry(project, repo_key, entry)
    except ValueError as e:
        console.print(f"❌ {e}")
        raise typer.Exit(1)
    console.print(f"✅ {_label(project, repo_key)}: {_describe(entry)}")


@desktop_app.command("remove")
def desktop_remove(
    app: str = typer.Argument(..., help="Application name"),
    project: Optional[str] = ProjectOption,
    repo: Optional[str] = RepoOption,
):
    """Remove an app from a project's or repo's setup."""
    project, repo_key = _target(project, repo)
    try:
        removed = desktop.remove_entry(project, repo_key, app)
    except ValueError as e:
        console.print(f"❌ {e}")
        raise typer.Exit(1)
    if not removed:
        console.print(f"❌ {app} isn't in the setup for {_label(project, repo_key)}")
        raise typer.Exit(1)
    console.print(f"✅ Removed {app} from {_label(project, repo_key)}")


@desktop_app.command("focus")
def desktop_focus(
    app: Optional[str] = typer.Argument(None, help="App to activate once windows are placed"),
    project: Optional[str] = ProjectOption,
    repo: Optional[str] = RepoOption,
    clear: bool = typer.Option(False, "--clear", help="Stop activating an app at the end"),
):
    """Set which app ends up in front, e.g. Terminal to keep typing."""
    if not app and not clear:
        console.print("❌ Name an app, or pass --clear")
        raise typer.Exit(1)
    project, repo_key = _target(project, repo)
    try:
        desktop.set_focus(project, repo_key, None if clear else app)
    except ValueError as e:
        console.print(f"❌ {e}")
        raise typer.Exit(1)
    console.print(f"✅ {_label(project, repo_key)}: focus {'cleared' if clear else app}")


@desktop_app.command("capture")
def desktop_capture(
    apps: Optional[List[str]] = typer.Argument(None, help="Apps to record (default: those already configured)"),
    project: Optional[str] = ProjectOption,
    repo: Optional[str] = RepoOption,
):
    """Record where apps' windows are now, so `wf cd` puts them back there.

    Arrange the windows by hand, then capture them -- easier than working out
    coordinates. Each app's front window is recorded as absolute bounds.
    """
    project, repo_key = _target(project, repo)
    setup = desktop.resolve(project, repo_key)
    existing = {e["app"].lower(): e for e in setup["apps"]}
    names = list(apps or [e["app"] for e in setup["apps"]])
    if not names:
        console.print("❌ Nothing configured yet — name the apps to capture.")
        raise typer.Exit(1)

    failed = False
    for name in names:
        try:
            frame = desktop.capture(name)
        except RuntimeError as e:
            console.print(f"❌ {name}: {e}")
            failed = True
            continue
        entry = dict(existing.get(name.lower(), {"app": name}))
        entry.pop("position", None)
        entry.pop("screen", None)
        entry["bounds"] = list(frame)
        desktop.set_entry(project, repo_key, entry)
        console.print(f"📐 {name}: {','.join(str(v) for v in frame)}")

    console.print(f"✅ Saved to {_label(project, repo_key)}")
    if failed:
        console.print("💡 Capturing needs the app running with a window open, and the terminal "
                      "allowed under System Settings › Privacy & Security › Accessibility.")
        raise typer.Exit(1)


@desktop_app.command("apply")
def desktop_apply(project: Optional[str] = ProjectOption, repo: Optional[str] = RepoOption):
    """Launch the apps and lay out the windows now, without changing directory."""
    project, repo_key = _target(project, repo)
    report(desktop.apply(project, repo_key), echo=console.print)


@desktop_app.command("screens")
def desktop_screens():
    """List the connected screens' usable areas, for working out --bounds."""
    try:
        frames = desktop.screens()
    except Exception as e:
        console.print(f"❌ Couldn't read screens: {e}")
        raise typer.Exit(1)
    for i, (x, y, w, h) in enumerate(frames, start=1):
        main = " (main)" if i == 1 else ""
        console.print(f"  {i}{main}: {w}x{h} at {x},{y}")


def report(result: desktop.Result, echo=typer.echo) -> None:
    """Say what a desktop apply did; silent when nothing is configured."""
    if result.skipped:
        echo(f"🖥  Desktop setup skipped: {result.skipped}")
        return
    if result.launched:
        echo(f"🖥  Opened {', '.join(result.launched)}")
    if result.placing:
        echo(f"   Arranging {', '.join(result.placing)} (problems go to {desktop.LOG_PATH})")
    for app, error in result.failed.items():
        echo(f"⚠️  {app}: {error}")
