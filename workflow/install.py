"""What `wf install` sets up on this machine: dependencies, the launcher on
PATH, shell completion, the `claude` alias and the bundled Claude skills.

`install.sh` only bootstraps the venv and then runs `wf install`, so this is
the one place the steps live. Every step is idempotent: run it again after
pulling and it fixes only what is missing.
"""

import os
import shlex
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "wf"
LAUNCHER_LINK = Path("/usr/local/bin/wf")

# `claude` goes through `wf ai`: task context, the project's Claude account,
# the capture proxy and the task's skills, for the project whose repository
# holds the current folder (--here). Everything after `--` reaches the claude
# CLI untouched (`claude -r`, `claude -p ...`). `command claude` bypasses it,
# and scripts are unaffected: aliases are for interactive shells.
CLAUDE_ALIAS = "alias claude='wf ai --here --'"
# Earlier spellings, replaced in place when found.
OLD_CLAUDE_ALIASES = ["alias claude='wf ai --'"]
CLAUDE_ALIAS_COMMENT = ("# wf: claude runs through wf ai (task context, account, capture proxy). "
                        "`command claude` bypasses it.")

USER_RC_FILES = [Path.home() / ".zshrc", Path.home() / ".bashrc"]
SYSTEM_RC_FILES = [Path("/etc/zshrc"), Path("/etc/bashrc")]


@dataclass
class Step:
    """One thing the installer did (or could not do)."""
    name: str
    ok: bool
    detail: str = ""
    changed: bool = False


@dataclass
class Report:
    steps: List[Step] = field(default_factory=list)

    def add(self, name, ok, detail="", changed=False):
        self.steps.append(Step(name, ok, detail, changed))

    @property
    def failed(self) -> List[Step]:
        return [s for s in self.steps if not s.ok]


def _run(cmd: list, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=False, **kw)


def _sudo_available() -> bool:
    """True when sudo can run now: we are root, or a terminal can take the password."""
    return os.geteuid() == 0 or sys.stdin.isatty()


# --- dependencies -----------------------------------------------------------

def install_deps(python: str = sys.executable, root: Path = REPO_ROOT, quiet: bool = True) -> Step:
    """pip install requirements.txt and this package (editable) into `python`'s env."""
    q = ["-q"] if quiet else []
    for args in (["install", "--upgrade", *q, "pip", "wheel"],
                 ["install", *q, "-r", str(root / "requirements.txt")],
                 ["install", *q, "-e", str(root)]):
        r = _run([python, "-m", "pip", *args], cwd=root, capture_output=True, text=True)
        if r.returncode != 0:
            tail = "\n".join((r.stderr or r.stdout).strip().splitlines()[-5:])
            return Step("deps", False, f"pip {' '.join(args)} failed (exit {r.returncode})\n{tail}")
    return Step("deps", True, "requirements.txt and the workflow package", changed=True)


# --- launcher ---------------------------------------------------------------

def launcher_linked(link: Path = LAUNCHER_LINK, launcher: Path = LAUNCHER) -> bool:
    try:
        return link.resolve() == launcher.resolve()
    except OSError:
        return False


def install_launcher(link: Path = LAUNCHER_LINK, launcher: Path = LAUNCHER) -> Step:
    """Symlink the venv-pinned `wf` launcher onto PATH; sudo when the dir needs it."""
    if not launcher.exists():
        return Step("launcher", False, f"{launcher} missing; run install.sh first")
    if launcher_linked(link, launcher):
        return Step("launcher", True, f"{link} already points here")
    cmd = ["ln", "-sf", str(launcher), str(link)]
    if not os.access(link.parent, os.W_OK):
        if not _sudo_available():
            return Step("launcher", False, f"needs: sudo {shlex.join(cmd)}")
        cmd = ["sudo", *cmd]
    r = _run(cmd)
    if r.returncode != 0:
        return Step("launcher", False, f"failed: {shlex.join(cmd)}")
    return Step("launcher", True, f"{link} -> {launcher}", changed=True)


# --- shell completion -------------------------------------------------------

def current_shell() -> str:
    return Path(os.environ.get("SHELL") or "").name or "zsh"


def completion_script(shell: str, launcher: Path = LAUNCHER) -> str:
    """Typer's completion for `wf`, as its `--install-completion` would emit it.

    Typer registers the classes as source_bash/source_zsh (Click's own
    bash_source names are not), and the variable name comes from the program
    name, which cli.py pins to "wf". The leading blank line Typer emits is
    dropped: zsh only honors `#compdef` on the very first line.
    """
    env = dict(os.environ, _WF_COMPLETE=f"source_{shell}")
    r = _run([str(launcher)], env=env, capture_output=True, text=True)
    text = r.stdout.lstrip("\n") if r.returncode == 0 else ""
    return text if text.strip() else ""


def ensure_lines(path: Path, lines: List[str], marker: Optional[str] = None,
                 sudo: bool = False) -> bool:
    """Append `lines` to `path` unless `marker` (default: the last line) is
    already in it. Returns True when it wrote. With `sudo` the append runs
    as root (system rc files are root-owned and read-only)."""
    marker = marker or lines[-1]
    try:
        if path.exists() and marker in path.read_text():
            return False
    except PermissionError:
        pass
    block = "\n" + "\n".join(lines) + "\n"
    if sudo:
        r = _run(["sudo", "sh", "-c", f"cat >> {shlex.quote(str(path))}"], input=block, text=True)
        if r.returncode != 0:
            raise PermissionError(f"could not append to {path}")
    else:
        with open(path, "a") as f:
            f.write(block)
    return True


def install_completion(shell: Optional[str] = None, home: Path = None, launcher: Path = LAUNCHER) -> Step:
    """Write wf's completion file for `shell` and source it from its rc file."""
    shell = shell or current_shell()
    home = home or Path.home()
    if shell not in ("bash", "zsh"):
        return Step("completion", False, f"unsupported shell {shell!r}; set it up by hand")
    script = completion_script(shell, launcher)
    if not script:
        return Step("completion", False, "could not generate the completion script")
    changed = False
    if shell == "bash":
        target = home / ".bash_completion.d" / "wf-completion.bash"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.read_text() != script:
            target.write_text(script)
            changed = True
        rc = home / ".bashrc"
        changed |= ensure_lines(rc, ["# wf completion", "source ~/.bash_completion.d/wf-completion.bash"],
                                marker="wf-completion.bash")
    else:
        target = home / ".zsh" / "completion" / "_wf"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists() or target.read_text() != script:
            target.write_text(script)
            changed = True
        rc = home / ".zshrc"
        # An empty _wf would shadow every other wf completion on fpath;
        # the script was checked non-empty above.
        changed |= ensure_lines(rc, ["# wf completion", "fpath+=~/.zsh/completion",
                                     "autoload -U compinit && compinit"],
                                marker="fpath+=~/.zsh/completion")
    return Step("completion", True, f"{shell}: {target}", changed=changed)


# --- aliases ----------------------------------------------------------------

def replace_line(path: Path, old: str, new: str, sudo: bool = False) -> bool:
    """Replace the line `old` with `new` in `path`; True when it was there."""
    try:
        if not path.exists() or old not in path.read_text().splitlines():
            return False
    except PermissionError:
        return False
    if sudo:
        script = f"import sys,pathlib;p=pathlib.Path(sys.argv[1]);p.write_text(p.read_text().replace(sys.argv[2]+chr(10),sys.argv[3]+chr(10)))"
        r = _run(["sudo", sys.executable, "-c", script, str(path), old, new])
        if r.returncode != 0:
            raise PermissionError(f"could not edit {path}")
    else:
        path.write_text(path.read_text().replace(old + "\n", new + "\n"))
    return True


def install_aliases(system: bool = False, files: Optional[List[Path]] = None) -> Step:
    """The `claude` alias in the user's rc files, or in /etc's with `system`."""
    if files is None:
        files = SYSTEM_RC_FILES if system else USER_RC_FILES
    sudo = system and os.geteuid() != 0
    if sudo and not _sudo_available():
        return Step("aliases", False, "machine-wide install needs a terminal for sudo")
    written, present = [], []
    try:
        for f in files:
            if system and not f.exists():
                continue  # no such shell on this machine
            upgraded = any(replace_line(f, old, CLAUDE_ALIAS, sudo=sudo) for old in OLD_CLAUDE_ALIASES)
            added = ensure_lines(f, [CLAUDE_ALIAS_COMMENT, CLAUDE_ALIAS], sudo=sudo)
            (written if upgraded or added else present).append(f)
    except (PermissionError, OSError) as e:
        return Step("aliases", False, str(e))
    where = ", ".join(str(f) for f in written + present) or "no rc files"
    return Step("aliases", True, f"{CLAUDE_ALIAS} in {where}", changed=bool(written))


# --- skills -----------------------------------------------------------------

def install_skills() -> Step:
    from workflow import skill_install
    try:
        result = skill_install.install()
    except Exception as e:  # noqa: BLE001 - reported, not fatal
        return Step("skills", False, f"{e}; run: wf skill install")
    parts = []
    if result.linked:
        parts.append(f"linked {', '.join(result.linked)}")
    if result.current:
        parts.append(f"{len(result.current)} already installed")
    if result.skipped:
        parts.append(f"skipped {', '.join(result.skipped)} (not ours; wf skill install --force)")
    return Step("skills", True, "; ".join(parts) or "none bundled", changed=bool(result.linked))


# --- everything -------------------------------------------------------------

def install_all(deps=True, launcher=True, completion=True, aliases=True, skills=True,
                system=False, shell: Optional[str] = None) -> Report:
    report = Report()
    if deps:
        report.steps.append(install_deps())
    if launcher:
        report.steps.append(install_launcher())
    if completion:
        report.steps.append(install_completion(shell))
    if aliases:
        report.steps.append(install_aliases())
        if system:
            report.steps.append(install_aliases(system=True))
    if skills:
        report.steps.append(install_skills())
    return report
