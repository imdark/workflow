"""`wf side` -- fork the running Claude conversation into a new terminal tab.

Claude Code exports its session id (CLAUDE_CODE_SESSION_ID) and its pid
(CLAUDE_PID) to the commands it runs, so a command started from a session
knows which conversation to fork and which tab it lives in. The fork is
`claude --resume <id> --fork-session`: a new session that starts from the
current transcript, leaving the original untouched.

The tab is opened next to the session's own:

- tmux: a new window right after the current one (`new-window -a`).
- iTerm2: a new tab in the window holding the session's tty.
- Terminal.app: a new tab (Cmd-T) in the window holding the session's tty.
  Terminal's AppleScript can't make tabs, so this goes through System
  Events and needs the terminal to have Accessibility permission, like
  workflow/desktop.py's window placement.

Whichever it is, the new tab is selected.
"""

import os
import shlex
import subprocess
from pathlib import Path
from typing import List, Optional

TERMINALS = ("tmux", "iterm", "terminal")


def fork_command(session_id: str, cwd: str, claude: str = "claude") -> str:
    """The shell line the new tab runs."""
    return (f"cd {shlex.quote(cwd)} && "
            f"{shlex.quote(claude)} --resume {shlex.quote(session_id)} --fork-session")


def detect_terminal(env=None) -> Optional[str]:
    env = os.environ if env is None else env
    if env.get("TMUX"):
        return "tmux"
    program = env.get("TERM_PROGRAM", "")
    if program == "iTerm.app":
        return "iterm"
    if program == "Apple_Terminal":
        return "terminal"
    return None


def tty_of(pid: int) -> Optional[str]:
    """The controlling tty of a process, as /dev/ttysNNN, if it has one."""
    out = subprocess.run(["ps", "-o", "tty=", "-p", str(pid)],
                         capture_output=True, text=True).stdout.strip()
    if not out or out.startswith("?"):
        return None
    return out if out.startswith("/dev/") else f"/dev/{out}"


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def terminal_script(command: str, tty: Optional[str]) -> str:
    """AppleScript for Terminal.app: bring the session's window forward,
    open a tab in it and run the command there."""
    lines = ['tell application "Terminal"', "  activate"]
    if tty:
        lines += [
            "  repeat with w in windows",
            "    repeat with t in tabs of w",
            f"      if tty of t is {_quote(tty)} then",
            "        set index of w to 1",
            "        set selected of t to true",
            "      end if",
            "    end repeat",
            "  end repeat",
        ]
    lines += [
        "end tell",
        'tell application "System Events" to tell process "Terminal" '
        'to keystroke "t" using command down',
        "delay 0.3",
        'tell application "Terminal"',
        f"  do script {_quote(command)} in selected tab of front window",
        "end tell",
    ]
    return "\n".join(lines) + "\n"


def iterm_script(command: str, tty: Optional[str]) -> str:
    """AppleScript for iTerm2: a new tab in the window holding the tty."""
    lines = ['tell application "iTerm2"', "  activate", "  set target to current window"]
    if tty:
        lines += [
            "  repeat with w in windows",
            "    repeat with t in tabs of w",
            "      repeat with s in sessions of t",
            f"        if tty of s is {_quote(tty)} then set target to w",
            "      end repeat",
            "    end repeat",
            "  end repeat",
        ]
    lines += [
        "  tell target",
        "    select",
        "    set newTab to (create tab with default profile)",
        f"    tell current session of newTab to write text {_quote(command)}",
        "  end tell",
        "end tell",
    ]
    return "\n".join(lines) + "\n"


def tmux_command(command: str, cwd: str) -> List[str]:
    return ["tmux", "new-window", "-a", "-c", cwd, command]


def open_side(session_id: str, cwd: Optional[str] = None, terminal: Optional[str] = None,
              pid: Optional[int] = None) -> str:
    """Open the fork in a new tab and switch to it. Returns the terminal used."""
    cwd = str(Path(cwd or os.getcwd()).expanduser())
    terminal = terminal or detect_terminal()
    if terminal not in TERMINALS:
        raise RuntimeError("couldn't tell which terminal this is (TERM_PROGRAM="
                           f"{os.environ.get('TERM_PROGRAM', '')!r}); pass --terminal "
                           f"({', '.join(TERMINALS)})")
    command = fork_command(session_id, cwd)

    if terminal == "tmux":
        run = subprocess.run(tmux_command(command, cwd), capture_output=True, text=True)
    else:
        tty = tty_of(pid) if pid else None
        script = (iterm_script if terminal == "iterm" else terminal_script)(command, tty)
        run = subprocess.run(["osascript", "-"], input=script, capture_output=True, text=True)
    if run.returncode != 0:
        raise RuntimeError((run.stderr or run.stdout).strip() or f"{terminal} failed")
    return terminal
