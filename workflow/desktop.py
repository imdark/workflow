"""Per-project and per-repo desktop setups for `wf cd`.

A project, or one of its repositories, can list the applications that
belong to working on it and where their windows go:

    projects:
      work:
        desktop:
          apps:
            - Slack                              # just make sure it's running
            - app: Visual Studio Code
              open: "{repo_path}"                # passed to `open -a`
              position: left                     # a named slot of the screen
            - app: Google Chrome
              open: http://localhost:3000
              position: right
              screen: 2                          # 1 is the screen with the menu bar
          focus: Terminal                        # activated once everything is placed
        repositories:
          ~/code/api:
            base_branch: main
            desktop:
              apps:
                - app: Postman
                  bounds: [0, 25, 1200, 800]     # absolute x, y, width, height

A repository's setup is laid over its project's: its apps are added, an app
listed at both levels takes the repository's entry, and its `focus` wins.
`inherit: false` on a repository drops the project's apps entirely.

Launching goes through `open -a`, so an app is started or just brought
forward if it's already running. Placing windows goes through System
Events, which needs the terminal to have Accessibility permission. It runs
detached, waiting for each app to show a window, so `wf cd` returns at once;
whatever it couldn't place is written to ~/.wf/logs/desktop.log.
"""

import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

CONFIG_KEY = "desktop"
LOG_PATH = Path.home() / ".wf" / "logs" / "desktop.log"

# How long the layout script waits for an app to show a window, in seconds.
WINDOW_TIMEOUT = 20

# Named slots as fractions of a screen's visible area: x, y, width, height.
POSITIONS: Dict[str, Tuple[float, float, float, float]] = {
    "full": (0, 0, 1, 1),
    "left": (0, 0, 1 / 2, 1),
    "right": (1 / 2, 0, 1 / 2, 1),
    "top": (0, 0, 1, 1 / 2),
    "bottom": (0, 1 / 2, 1, 1 / 2),
    "top-left": (0, 0, 1 / 2, 1 / 2),
    "top-right": (1 / 2, 0, 1 / 2, 1 / 2),
    "bottom-left": (0, 1 / 2, 1 / 2, 1 / 2),
    "bottom-right": (1 / 2, 1 / 2, 1 / 2, 1 / 2),
    "left-third": (0, 0, 1 / 3, 1),
    "center-third": (1 / 3, 0, 1 / 3, 1),
    "right-third": (2 / 3, 0, 1 / 3, 1),
    "left-two-thirds": (0, 0, 2 / 3, 1),
    "right-two-thirds": (1 / 3, 0, 2 / 3, 1),
    "center": (1 / 8, 1 / 8, 3 / 4, 3 / 4),
}

Frame = Tuple[int, int, int, int]


@dataclass
class Result:
    launched: List[str] = field(default_factory=list)
    failed: Dict[str, str] = field(default_factory=dict)
    placing: List[str] = field(default_factory=list)
    skipped: Optional[str] = None


# --- configuration -----------------------------------------------------------

def normalize_entry(entry) -> dict:
    """An app entry as a dict; a bare string is just an app to launch."""
    if isinstance(entry, str):
        return {"app": entry}
    if not isinstance(entry, dict) or not entry.get("app"):
        raise ValueError(f"Desktop app entry needs an 'app' name: {entry!r}")
    return dict(entry)


def _merge(base: dict, overlay: dict) -> dict:
    """Lay one desktop setup over another, entries keyed by app name."""
    apps: Dict[str, dict] = {}
    if overlay.get("inherit", True):
        for entry in base.get("apps") or []:
            entry = normalize_entry(entry)
            apps[entry["app"].lower()] = entry
    for entry in overlay.get("apps") or []:
        entry = normalize_entry(entry)
        apps[entry["app"].lower()] = entry

    merged = {"apps": list(apps.values())}
    focus = overlay.get("focus") or (base.get("focus") if overlay.get("inherit", True) else None)
    if focus:
        merged["focus"] = focus
    return merged


def resolve(project_name: str, repo_key: Optional[str] = None, cfg=None) -> dict:
    """The desktop setup that applies to a project, or to one of its repos."""
    if cfg is None:
        from workflow.config import load_config
        cfg = load_config()

    project = (cfg.get("projects") or {}).get(project_name) or {}
    setup = _merge({}, project.get(CONFIG_KEY) or {})
    if repo_key:
        repo = (project.get("repositories") or {}).get(repo_key) or {}
        setup = _merge(setup, repo.get(CONFIG_KEY) or {})
    return setup


def find_repo(name: str, cfg=None) -> Optional[Tuple[str, str]]:
    """(project, repo key) for a repo named like `wf cd` matches it."""
    if cfg is None:
        from workflow.config import load_config
        cfg = load_config()

    partial = None
    for project_name, project in (cfg.get("projects") or {}).items():
        for repo_key in project.get("repositories") or {}:
            repo_name = Path(repo_key).name
            if name == repo_name or name == repo_key:
                return project_name, repo_key
            if partial is None and name.lower() in repo_name.lower():
                partial = (project_name, repo_key)
    return partial


def _section(cfg: dict, project_name: str, repo_key: Optional[str], create: bool) -> Optional[dict]:
    """The `desktop` mapping of a project or repo inside a loaded config."""
    projects = cfg.get("projects") or {}
    if project_name not in projects:
        raise ValueError(f"Project '{project_name}' not found")
    owner = projects[project_name]
    if repo_key:
        repos = owner.get("repositories") or {}
        if repo_key not in repos:
            raise ValueError(f"Repository '{repo_key}' not found in project '{project_name}'")
        if repos[repo_key] is None:
            repos[repo_key] = {}
        owner = repos[repo_key]
    if create:
        owner.setdefault(CONFIG_KEY, {})
    return owner.get(CONFIG_KEY)


def set_entry(project_name: str, repo_key: Optional[str], entry: dict) -> None:
    """Add an app to a project's or repo's setup, replacing one of that name."""
    from workflow.config import load_config, save_config

    entry = normalize_entry(entry)
    cfg = load_config()
    section = _section(cfg, project_name, repo_key, create=True)
    apps = [normalize_entry(e) for e in section.get("apps") or []]
    for i, existing in enumerate(apps):
        if existing["app"].lower() == entry["app"].lower():
            apps[i] = entry
            break
    else:
        apps.append(entry)
    section["apps"] = apps
    save_config(cfg)


def remove_entry(project_name: str, repo_key: Optional[str], app: str) -> bool:
    """Drop an app from a project's or repo's setup. False if it wasn't there."""
    from workflow.config import load_config, save_config

    cfg = load_config()
    section = _section(cfg, project_name, repo_key, create=False)
    if not section:
        return False
    apps = [normalize_entry(e) for e in section.get("apps") or []]
    kept = [e for e in apps if e["app"].lower() != app.lower()]
    if len(kept) == len(apps):
        return False
    section["apps"] = kept
    save_config(cfg)
    return True


def set_focus(project_name: str, repo_key: Optional[str], app: Optional[str]) -> None:
    """Set the app activated last, or clear it with None."""
    from workflow.config import load_config, save_config

    cfg = load_config()
    section = _section(cfg, project_name, repo_key, create=True)
    if app:
        section["focus"] = app
    else:
        section.pop("focus", None)
    save_config(cfg)


# --- geometry ----------------------------------------------------------------

_SCREENS_JXA = """
ObjC.import('AppKit');
var screens = $.NSScreen.screens;
var height = screens.objectAtIndex(0).frame.size.height;
var out = [];
for (var i = 0; i < screens.count; i++) {
  var f = screens.objectAtIndex(i).visibleFrame;
  out.push([f.origin.x, height - (f.origin.y + f.size.height), f.size.width, f.size.height]);
}
JSON.stringify(out);
"""


def screens() -> List[Frame]:
    """Each screen's usable area (no menu bar or Dock), top-left origin.

    Cocoa measures from the bottom-left of the main screen; System Events
    positions windows from the top-left, so the frames are flipped here.
    The first screen is the one with the menu bar.
    """
    out = subprocess.run(
        ["osascript", "-l", "JavaScript", "-e", _SCREENS_JXA],
        capture_output=True, text=True, check=True,
    ).stdout
    return [tuple(round(v) for v in frame) for frame in json.loads(out)]


def frame_for(entry: dict, screen_frames: Sequence[Frame]) -> Optional[Frame]:
    """Where an entry's window goes, or None when it only needs launching."""
    bounds = entry.get("bounds")
    if bounds:
        if len(bounds) != 4:
            raise ValueError(f"{entry['app']}: bounds must be [x, y, width, height]")
        return tuple(int(v) for v in bounds)

    position = entry.get("position")
    if not position:
        return None
    if position not in POSITIONS:
        raise ValueError(f"{entry['app']}: unknown position '{position}' "
                         f"(one of: {', '.join(POSITIONS)})")

    index = int(entry.get("screen", 1)) - 1
    if not 0 <= index < len(screen_frames):
        raise ValueError(f"{entry['app']}: screen {index + 1} not connected "
                         f"({len(screen_frames)} found)")
    sx, sy, sw, sh = screen_frames[index]
    fx, fy, fw, fh = POSITIONS[position]
    return (round(sx + fx * sw), round(sy + fy * sh), round(fw * sw), round(fh * sh))


def needs_screens(apps: Sequence[dict]) -> bool:
    return any(e.get("position") and not e.get("bounds") for e in apps)


# --- launching and placing ---------------------------------------------------

def _substitute(value: str, context: Dict[str, str]) -> str:
    # Plain replace rather than str.format: URLs and paths may hold braces.
    for key, replacement in context.items():
        value = value.replace("{" + key + "}", replacement)
    return value


def launch_command(entry: dict, context: Dict[str, str]) -> List[str]:
    """The `open` invocation that starts an app with what it should open."""
    targets = entry.get("open") or []
    if isinstance(targets, str):
        targets = [targets]
    cmd = ["open"]
    if entry.get("background"):
        cmd.append("-g")
    cmd += ["-a", entry["app"]]
    cmd += [str(Path(t).expanduser()) if t.startswith("~") else t
            for t in (_substitute(str(t), context) for t in targets)]
    return cmd


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def layout_script(placements: Sequence[Tuple[str, Frame]], focus: Optional[str] = None,
                  timeout: int = WINDOW_TIMEOUT) -> str:
    """AppleScript that waits for each app's front window and moves it.

    Processes are found by bundle id, not by name: an app's process is not
    always called what the app is (Visual Studio Code runs as "Code").
    Each app is placed on its own so one that never opens a window doesn't
    stop the rest; the failures come back as the script's result.
    """
    lines = ["set failures to {}"]
    for app, (x, y, w, h) in placements:
        lines += [
            "try",
            f"  set appId to id of application {_quote(app)}",
            "  tell application \"System Events\"",
            f"    set deadline to (current date) + {timeout}",
            "    repeat until (exists (first application process whose bundle identifier is appId))",
            f"      if (current date) > deadline then error \"not running after {timeout}s\"",
            "      delay 0.25",
            "    end repeat",
            "    set proc to first application process whose bundle identifier is appId",
            "    repeat until (count of windows of proc) > 0",
            f"      if (current date) > deadline then error \"no window after {timeout}s\"",
            "      delay 0.25",
            "    end repeat",
            "    tell window 1 of proc",
            f"      set position to {{{x}, {y}}}",
            f"      set size to {{{w}, {h}}}",
            "    end tell",
            "  end tell",
            "on error errMsg",
            f"  set end of failures to {_quote(app + ': ')} & errMsg",
            "end try",
        ]
    if focus:
        lines += ["try", f"  tell application {_quote(focus)} to activate", "end try"]
    lines += [
        "set AppleScript's text item delimiters to linefeed",
        "return failures as text",
    ]
    return "\n".join(lines) + "\n"


def _run_detached(script: str) -> None:
    """Run an AppleScript in the background, its output appended to the log."""
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = open(LOG_PATH, "a")
    proc = subprocess.Popen(
        ["osascript"], stdin=subprocess.PIPE, stdout=log, stderr=log,
        text=True, start_new_session=True,
    )
    proc.stdin.write(script)
    proc.stdin.close()
    log.close()


def apply(project_name: str, repo_key: Optional[str] = None,
          context: Optional[Dict[str, str]] = None, cfg=None) -> Result:
    """Launch a project's or repo's apps and lay out their windows."""
    result = Result()
    setup = resolve(project_name, repo_key, cfg)
    apps = setup["apps"]
    if not apps and not setup.get("focus"):
        return result
    if sys.platform != "darwin":
        result.skipped = "desktop automation needs macOS"
        return result

    context = dict(context or {})
    context.setdefault("project", project_name)
    if repo_key:
        repo_path = str(Path(repo_key).expanduser())
        context.setdefault("repo_path", repo_path)
        context.setdefault("repo_name", Path(repo_path).name)

    screen_frames: List[Frame] = []
    if needs_screens(apps):
        try:
            screen_frames = screens()
        except (subprocess.CalledProcessError, ValueError) as e:
            result.skipped = f"couldn't read screen sizes: {e}"
            return result

    placements = []
    for entry in apps:
        app = entry["app"]
        try:
            frame = frame_for(entry, screen_frames)
        except ValueError as e:
            result.failed[app] = str(e)
            continue

        launched = subprocess.run(launch_command(entry, context), capture_output=True, text=True)
        if launched.returncode != 0:
            result.failed[app] = (launched.stderr or launched.stdout).strip() or "open failed"
            continue
        result.launched.append(app)
        if frame:
            placements.append((app, frame))
            result.placing.append(app)

    if placements or setup.get("focus"):
        _run_detached(layout_script(placements, setup.get("focus")))
    return result


# --- capture -----------------------------------------------------------------

def capture(app: str) -> Frame:
    """The current position and size of an app's front window."""
    script = "\n".join([
        f"set appId to id of application {_quote(app)}",
        "tell application \"System Events\"",
        "  set proc to first application process whose bundle identifier is appId",
        "  set {x, y} to position of window 1 of proc",
        "  set {w, h} to size of window 1 of proc",
        "end tell",
        "return (x as text) & \",\" & (y as text) & \",\" & (w as text) & \",\" & (h as text)",
    ])
    out = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or f"couldn't read {app}'s window")
    return tuple(int(v) for v in out.stdout.strip().split(","))
