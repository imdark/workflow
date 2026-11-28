"""`wf release`: release the repository it is run in, the way that repository is released.

What a release is differs by repository, so each one says, under its entry
in the project's `repositories`:

    repositories:
      /Users/me/code/notes-graph:
        release:
          command: scripts/release.sh     # run in the repo
          plan_args: --plan               # added to list what would ship
          release_args: --yes --wait      # added to release and wait for it
          checkout: /Users/me/code/notes-graph-release   # optional: run there,
          branch: stage                                  # synced to this branch
          watch: deploy-prod.yml          # the Actions workflow a release starts
          verify_url: https://app.example.com/
      /Users/me/code/workflow:
        release:
          machines:
            laptop: {path: ~/code/workflow}
            prod:   {ssh: prod, path: /home/ubuntu/code/workflow,
                     python: /home/ubuntu/.wf-venv/bin/python3}

Two kinds:

  command   the repository's own release script: `wf release` lists what
            ships, confirms, runs it and checks `verify_url`; `wf release
            --watch` follows the run of `watch` that a release started.

  machines  code that runs from git checkouts on several machines (wf
            itself): fast-forward each checkout to the branch, install
            changed requirements, and wait for each agent runner, which
            restarts itself on new code once its jobs are done. `ssh` is a
            machine's name as `wf deploy ssh` knows it; without it the
            machine is this one. Nothing is interrupted, and a machine with
            uncommitted changes or off the branch is left alone and reported.
"""
from __future__ import annotations

import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, List, Optional

BRANCH = "main"
# `[w]`: the pattern must not match the shell that carries it.
RUNNER_PATTERN = "[w]orkflow.cli agent serve|[w]f agent serve"
DEPENDENCY_FILES = ("requirements.txt", "pyproject.toml")
# A runner checks for new code every 30 s, then exits once its jobs end.
RESTART_WAIT = 180
POLL_SECONDS = 10

_SHA = re.compile(r"^[0-9a-f]{40}$")
_PR = re.compile(r"^Merge pull request #(\d+) from [^/]+/(.*)$")

# (command, timeout seconds) -> (exit code, output)
Shell = Callable[[str, int], tuple]


class ReleaseError(RuntimeError):
    pass


@dataclass
class Machine:
    name: str
    path: str
    ssh: Optional[str] = None
    python: Optional[str] = None

    @property
    def where(self) -> str:
        return f"{self.ssh}:{self.path}" if self.ssh else self.path


def main_checkout(path: Optional[str] = None) -> Path:
    """The repository `path` belongs to: a worktree answers with its main checkout."""
    done = subprocess.run(["git", "-C", str(path or "."), "rev-parse", "--path-format=absolute", "--git-common-dir"],
                          capture_output=True, text=True, timeout=30)
    if done.returncode != 0:
        raise ReleaseError(f"{Path(path or '.').resolve()} is not in a git repository")
    return Path(done.stdout.strip()).parent


def find_release(path: Optional[str] = None, cfg: Optional[dict] = None, key: str = "release") -> dict:
    """The release of the repository `path` is in: {project, repo, section}.
    `section` is empty when the repository is known but has no release yet.
    `key` names another per-repository section kept the same way (`backup`)."""
    from workflow.config import load_config
    cfg = load_config() if cfg is None else cfg
    repo = main_checkout(path).resolve()
    for project, project_cfg in (cfg.get("projects") or {}).items():
        for configured, settings in ((project_cfg or {}).get("repositories") or {}).items():
            if Path(configured).expanduser().resolve() == repo:
                return {"project": project, "repo": str(configured),
                        "section": dict((settings or {}).get(key) or {})}
    raise ReleaseError(f"{repo} is not a repository of any wf project (see 'wf project list')")


def save_release(info: dict, section: dict, key: str = "release") -> None:
    from workflow.config import load_config, save_config
    cfg = load_config()
    repos = cfg["projects"][info["project"]]["repositories"]
    repos[info["repo"]] = {**(repos.get(info["repo"]) or {}), key: section}
    save_config(cfg)


def kind(section: dict) -> Optional[str]:
    return "machines" if section.get("machines") else "command" if section.get("command") else None


def machines(section: dict) -> List[Machine]:
    out = []
    for name, entry in (section.get("machines") or {}).items():
        if isinstance(entry, dict) and entry.get("path"):
            out.append(Machine(name=str(name), path=str(entry["path"]), ssh=entry.get("ssh") or None,
                               python=entry.get("python") or None))
    return out


# --- a repository released by its own command ----------------------------

def command_line(section: dict, mode: str) -> str:
    """The release command with what `mode` ('plan' or 'release') adds."""
    extra = section.get("plan_args", "--plan") if mode == "plan" else section.get("release_args", "--yes --wait")
    return f"{section['command']} {extra}".strip()


def command_cwd(info: dict) -> str:
    """Where the command runs: the release's own checkout when it has one
    (synced first, see sync), else the repository."""
    return str(Path(info["section"].get("checkout") or info["repo"]).expanduser())


def sync(info: dict) -> Optional[str]:
    """Bring the release's own checkout to its branch; a problem as text, or None."""
    section = info["section"]
    if not section.get("checkout"):
        return None
    from workflow.deploy.cloud import CloudTarget, sync_checkout
    return sync_checkout(CloudTarget(repo=command_cwd(info), command=section["command"],
                                     branch=section.get("branch") or "main"))


def run_command(info: dict, mode: str) -> int:
    """Run it with the terminal attached, so its own output and prompts show."""
    return subprocess.call(command_line(info["section"], mode), shell=True, cwd=command_cwd(info))


def latest_run(info: dict, gh=None) -> Optional[dict]:
    """The newest run of the workflow a release starts, or None."""
    from workflow import pr as prs
    rows = prs.ci_runs(command_cwd(info), workflow=info["section"]["watch"], limit=1, **({"run": gh} if gh else {}))
    return rows[0] if rows else None


def watch_run(info: dict, run_id: int) -> int:
    """Follow a run to its end; its exit status is the run's."""
    return subprocess.call(["gh", "run", "watch", str(run_id), "--exit-status", "--interval", "30"],
                           cwd=command_cwd(info), stdout=subprocess.DEVNULL)


# --- a repository that runs from checkouts on machines -------------------


def _ssh_target(name: str, cfg: Optional[dict] = None):
    """The machine `wf deploy ssh NAME` would reach, looked for in the
    current project, then every project, then the global settings."""
    from workflow.config import load_config
    from workflow.deploy import ssh_config
    from workflow.deploy.registry import get_registry

    cfg = load_config() if cfg is None else cfg
    projects = [ssh_config.current_project(), *(cfg.get("projects") or {}), None]
    for project in dict.fromkeys(projects):
        target = ssh_config.resolve(name, project, cfg, registry=get_registry())
        if target:
            return target
    raise ReleaseError(f"no machine '{name}' in wf's ssh settings (see 'wf deploy ssh-config')")


def shell_for(machine: Machine, cfg: Optional[dict] = None) -> Shell:
    """How to run a command on the machine: here, or over ssh."""
    if not machine.ssh:
        def local(command: str, timeout: int = 60) -> tuple:
            done = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=timeout)
            return done.returncode, (done.stdout + done.stderr).strip()
        return local

    from workflow.deploy import ssh_config
    target = _ssh_target(machine.ssh, cfg)

    def remote(command: str, timeout: int = 60) -> tuple:
        argv = ssh_config.ssh_argv(target, ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"], [command])
        try:
            done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, f"timed out after {timeout}s"
        return done.returncode, (done.stdout + done.stderr).strip()
    return remote


def _cd(machine: Machine) -> str:
    # `~` must stay unquoted for the machine's shell to expand it.
    path = machine.path
    return "cd ~/" + shlex.quote(path[2:]) if path.startswith("~/") else "cd " + shlex.quote(path)


def machine_state(machine: Machine, run: Shell) -> dict:
    """Where the machine's checkout is and which runner processes it has."""
    code, out = run(
        f"{_cd(machine)} && git rev-parse HEAD && git rev-parse --abbrev-ref HEAD && echo ::dirty"
        f" && git status --porcelain --untracked-files=no && echo ::runners"
        f" && (pgrep -f {shlex.quote(RUNNER_PATTERN)} || true)", 60)
    lines = out.splitlines()
    if code != 0 or len(lines) < 2 or not _SHA.match(lines[0]) or "::dirty" not in lines or "::runners" not in lines:
        return {"error": out.strip().splitlines()[-1] if out.strip() else f"exit {code}"}
    dirty_at, runners_at = lines.index("::dirty"), lines.index("::runners")
    return {"sha": lines[0], "branch": lines[1],
            "dirty": [line.strip() for line in lines[dirty_at + 1:runners_at] if line.strip()],
            "runners": sorted(int(pid) for pid in lines[runners_at + 1:] if pid.strip().isdigit())}


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120)
    if done.returncode != 0:
        raise ReleaseError(f"git {' '.join(args)}: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout.strip()


def wf_checkout() -> Path:
    from workflow.deploy.updates import PACKAGE_DIR
    return PACKAGE_DIR.parent


def released_sha(repo: Optional[Path] = None, branch: str = BRANCH) -> str:
    """What a release ships: the remote's branch, fetched so its log can be read here."""
    repo = repo or wf_checkout()
    _git(repo, "fetch", "-q", "origin", branch)
    return _git(repo, "rev-parse", f"origin/{branch}")


def shipping(repo: Path, have: str, target: str) -> dict:
    """What moving a checkout from `have` to `target` brings: how many
    commits, the pull requests among them, and whether its dependencies change."""
    try:
        behind = int(_git(repo, "rev-list", "--count", f"{have}..{target}"))
        ahead = int(_git(repo, "rev-list", "--count", f"{target}..{have}"))
        merges = _git(repo, "log", "--first-parent", "--merges", "--format=%s", f"{have}..{target}").splitlines()
        changed = _git(repo, "diff", "--name-only", have, target, "--", *DEPENDENCY_FILES).splitlines()
    except ReleaseError:
        # A commit this checkout has never seen: the machine is on its own line.
        return {"behind": None, "ahead": None, "prs": [], "dependencies": []}
    prs = [f"#{m.group(1)} {m.group(2)}" for m in map(_PR.match, merges) if m]
    return {"behind": behind, "ahead": ahead, "prs": prs, "dependencies": changed}


def plan(section: dict, repo: Optional[Path] = None, target: Optional[str] = None,
         shells: Optional[Callable[[Machine], Shell]] = None) -> dict:
    """Each machine, where it is, and what a release would do to it. `repo`
    is a local checkout whose log says what ships."""
    repo = repo or wf_checkout()
    branch = section.get("branch") or BRANCH
    target = target or released_sha(repo, branch)
    shells = shells or shell_for
    rows = []
    for machine in machines(section):
        row = {"name": machine.name, "where": machine.where}
        try:
            state = machine_state(machine, shells(machine))
        except (ReleaseError, OSError, subprocess.SubprocessError) as e:
            state = {"error": str(e)}
        row.update(state)
        if "error" in state:
            row["action"] = "unreachable"
        else:
            row.update(shipping(repo, state["sha"], target))
            if state["sha"] == target:
                row["action"] = "current"
            elif state["branch"] != branch:
                row["action"] = "skip: not on " + branch
            elif state["dirty"]:
                row["action"] = "skip: uncommitted changes"
            elif row["behind"] is None or row["ahead"]:
                row["action"] = "skip: has commits main doesn't"
            else:
                row["action"] = "update"
        rows.append(row)
    return {"target": target, "branch": branch, "machines": rows}


def roll(machine: Machine, row: dict, target: str, run: Shell, branch: str = BRANCH) -> dict:
    """Move one machine's checkout to `target`. Dependencies first, so the
    runner never restarts onto code whose packages aren't there yet."""
    cd = _cd(machine)
    if row.get("dependencies"):
        if not machine.python:
            return {"ok": False, "detail": f"{', '.join(row['dependencies'])} changed and the machine has no "
                                           f"'python' set to install them with"}
        code, out = run(f"{cd} && git fetch -q origin {branch} && git show {target}:requirements.txt"
                        f" | {shlex.quote(machine.python)} -m pip install -q -r /dev/stdin", 600)
        if code != 0:
            return {"ok": False, "detail": "installing requirements failed: " + out.splitlines()[-1][:200]}
    code, out = run(f"{cd} && git fetch -q origin {branch} && git merge -q --ff-only {target}"
                    f" && git rev-parse HEAD", 120)
    if code != 0 or out.splitlines()[-1:] != [target]:
        return {"ok": False, "detail": (out.splitlines() or [f"exit {code}"])[-1][:200]}
    return {"ok": True, "detail": ""}


def wait_for_restart(machine: Machine, before: List[int], run: Shell, timeout: int = RESTART_WAIT,
                     sleep: Callable[[float], None] = time.sleep) -> str:
    """'restarted', 'no runner', or 'waiting' when the old runner is still
    up at the end (it leaves only when its jobs are done)."""
    if not before:
        return "no runner"
    waited = 0
    while True:
        now = machine_state(machine, run).get("runners")
        if now and not set(now) & set(before):
            return "restarted"
        if waited >= timeout:
            return "waiting"
        sleep(POLL_SECONDS)
        waited += POLL_SECONDS


def release(section: dict, repo: Optional[Path] = None, wait: int = RESTART_WAIT,
            shells: Optional[Callable[[Machine], Shell]] = None, planned: Optional[dict] = None,
            say: Callable[[str], None] = lambda line: None, sleep: Callable[[float], None] = time.sleep) -> dict:
    """Roll the release out: every machine the plan says to update, then
    wait for each one's runner. Returns the plan with each row's outcome."""
    shells = shells or shell_for
    planned = planned or plan(section, repo, shells=shells)
    by_name = {machine.name: machine for machine in machines(section)}
    rolled = []
    for row in planned["machines"]:
        if row["action"] != "update":
            row["outcome"] = row["action"]
            continue
        machine = by_name[row["name"]]
        say(f"==> {machine.name}: updating {row['sha'][:8]}..{planned['target'][:8]}")
        result = roll(machine, row, planned["target"], shells(machine), planned.get("branch") or BRANCH)
        row["outcome"] = "updated" if result["ok"] else "failed: " + result["detail"]
        if result["ok"]:
            rolled.append((machine, row))
    # After every checkout has moved, so the runners restart side by side.
    for machine, row in rolled:
        say(f"==> {machine.name}: waiting for its runner")
        row["runner"] = wait_for_restart(machine, row["runners"], shells(machine), wait, sleep)
    planned["ok"] = all(row["outcome"] in ("updated", "current") for row in planned["machines"])
    return planned


# --- how the CLI prints them ---------------------------------------------

def format_plan(planned: dict) -> str:
    lines = [f"{planned.get('branch') or BRANCH} is at {planned['target'][:8]}"]
    for row in planned["machines"]:
        if "error" in row:
            lines.append(f"✗ {row['name']} ({row['where']}): unreachable: {row['error']}")
            continue
        runner = f"runner pid {', '.join(map(str, row['runners']))}" if row["runners"] else "no runner"
        if row["action"] == "current":
            lines.append(f"✓ {row['name']} ({row['where']}): current · {runner}")
            continue
        mark = "→" if row["action"] == "update" else "✗"
        behind = f"{row['behind']} commit{'' if row['behind'] == 1 else 's'} behind" \
            if row["behind"] is not None else "on commits this checkout doesn't have"
        lines.append(f"{mark} {row['name']} ({row['where']}): {row['sha'][:8]}, {behind} · {runner}"
                     + ("" if row["action"] == "update" else f" · {row['action']}"))
        lines += [f"    {pr}" for pr in row["prs"]]
        if row["dependencies"]:
            lines.append(f"    changes {', '.join(row['dependencies'])}")
        lines += [f"    uncommitted: {line}" for line in row["dirty"][:5]]
    return "\n".join(lines)


def format_outcome(result: dict) -> str:
    lines = []
    for row in result["machines"]:
        outcome = row.get("outcome", row["action"])
        mark = "✓" if outcome in ("updated", "current") else "✗"
        runner = {"restarted": ", runner restarted on the new code",
                  "waiting": ", runner still on the old code: it restarts when its jobs end",
                  "no runner": ", no runner there"}.get(row.get("runner"), "")
        lines.append(f"{mark} {row['name']}: {outcome}{runner}")
    return "\n".join(lines)
